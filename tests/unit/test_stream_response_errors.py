"""Streaming-response failures after the status has been sent.

Once an upstream response is streaming, the relay already holds an
``HTTP_RESPONSE_START``: a later upstream failure must end the chunked stream
with an error ``HTTP_RESPONSE_END`` rather than emit a second START. An SSE
stream is also exempt from the read timeout between events.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from hle_client.proxy import LocalProxy, ProxyConfig, UpstreamStreamError
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.models import ProxiedHttpRequest
from hle_common.protocol import MessageType, ProtocolMessage


def _proxy(target_url: str = "http://localhost:8123", **kw: object) -> LocalProxy:
    return LocalProxy(ProxyConfig(target_url=target_url, **kw))


def _tunnel(service_url: str = "http://localhost:8123", **kw: object) -> Tunnel:
    return Tunnel(TunnelConfig(service_url=service_url, **kw))


class _FakeResponse:
    """An httpx response stand-in with scripted chunks and an optional failure."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        chunks: list[bytes],
        *,
        delays: list[float] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._chunks = chunks
        self._delays = delays or [0.0] * len(chunks)
        self._error = error

    async def aiter_bytes(self, chunk_size: int):
        for chunk, delay in zip(self._chunks, self._delays, strict=True):
            if delay:
                await asyncio.sleep(delay)
            yield chunk
        if self._error is not None:
            raise self._error


class _FakeStream:
    def __init__(self, response: _FakeResponse, header_delay: float = 0.0) -> None:
        self._response = response
        self._header_delay = header_delay

    async def __aenter__(self) -> _FakeResponse:
        if self._header_delay:
            await asyncio.sleep(self._header_delay)
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    """Stands in for httpx.AsyncClient, one stream per request."""

    def __init__(self, response: _FakeResponse, header_delay: float = 0.0) -> None:
        self._response = response
        self._header_delay = header_delay

    def stream(self, **kwargs: object) -> _FakeStream:
        return _FakeStream(self._response, self._header_delay)


def _request(request_id: str, path: str = "/x") -> ProtocolMessage:
    return ProtocolMessage(
        type=MessageType.HTTP_REQUEST,
        tunnel_id="t",
        request_id=request_id,
        payload=ProxiedHttpRequest(
            request_id=request_id,
            method="GET",
            path=path,
            headers={},
            body=None,
            query_string="",
        ).model_dump(),
    )


def _mock_ws() -> tuple[AsyncMock, list[str]]:
    ws = AsyncMock()
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    ws.send = AsyncMock(side_effect=send)
    return ws, sent


def _types(sent: list[str]) -> list[MessageType]:
    return [ProtocolMessage.model_validate_json(message).type for message in sent]


# ---------------------------------------------------------------------------
# Proxy: mid-body failures become UpstreamStreamError, never a second status
# ---------------------------------------------------------------------------


class TestMidBodyFailure:
    async def test_read_error_after_the_status_raises_upstream_error(self):
        proxy = _proxy()
        proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"first"],
                error=httpx.ReadError("connection reset"),
            ),
        )

        stream = proxy.stream_http(method="GET", path="/x", headers={})
        assert (await anext(stream))[0] == 200
        assert (await anext(stream)) == (None, None, b"first")
        with pytest.raises(UpstreamStreamError) as excinfo:
            await anext(stream)
        assert excinfo.value.reason == "upstream_error"

    async def test_read_timeout_after_the_status_raises_upstream_timeout(self):
        proxy = _proxy()
        proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"first"],
                error=httpx.ReadTimeout("read timed out"),
            ),
        )

        stream = proxy.stream_http(method="GET", path="/x", headers={})
        assert (await anext(stream))[0] == 200
        assert (await anext(stream)) == (None, None, b"first")
        with pytest.raises(UpstreamStreamError) as excinfo:
            await anext(stream)
        assert excinfo.value.reason == "upstream_timeout"


# ---------------------------------------------------------------------------
# Proxy: timeouts — SSE is exempt, other bodies are not
# ---------------------------------------------------------------------------


class TestReadTimeout:
    async def test_sse_survives_a_silence_longer_than_the_timeout(self):
        proxy = _proxy(timeout=0.05)
        proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/event-stream"},
                [b"data: hello\n\n"],
                delays=[0.2],
            ),
        )

        yields = [
            item async for item in proxy.stream_http(method="GET", path="/events", headers={})
        ]

        assert yields[0][0] == 200
        assert yields[1] == (None, None, b"data: hello\n\n")

    async def test_non_sse_idle_body_read_times_out(self):
        proxy = _proxy(timeout=0.05)
        proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"first", b"second"],
                delays=[0.0, 0.2],
            ),
        )

        stream = proxy.stream_http(method="GET", path="/slow", headers={})
        assert (await anext(stream))[0] == 200
        assert (await anext(stream)) == (None, None, b"first")
        with pytest.raises(UpstreamStreamError) as excinfo:
            await anext(stream)
        assert excinfo.value.reason == "upstream_timeout"

    async def test_headers_that_never_arrive_keep_the_504_path(self):
        proxy = _proxy(timeout=0.05)
        proxy._http_client = _FakeClient(
            _FakeResponse(200, {"content-type": "text/plain"}, [b"late"]),
            header_delay=0.3,
        )

        yields = [item async for item in proxy.stream_http(method="GET", path="/slow", headers={})]

        assert yields[0][0] == 504
        assert yields[1] == (None, None, b"Gateway Timeout: local service did not respond")

    async def test_slow_streamed_upload_is_bounded_per_chunk_not_in_total(self):
        # Five 0.03 s gaps = 0.15 s in total, three times the 0.05 s timeout,
        # but every gap is under it: the upload must not be cut.
        response = _FakeResponse(200, {"content-type": "text/plain"}, [b"ok"])

        class _UploadingClient:
            def stream(self, **kwargs: object) -> _UploadingStream:
                return _UploadingStream(kwargs["content"])

        class _UploadingStream:
            def __init__(self, content: object) -> None:
                self._content = content

            async def __aenter__(self) -> _FakeResponse:
                async for _ in self._content:  # type: ignore[attr-defined]
                    pass
                return response

            async def __aexit__(self, *exc: object) -> bool:
                return False

        async def upload():
            for _ in range(5):
                await asyncio.sleep(0.03)
                yield b"part"

        proxy = _proxy(timeout=0.05)
        proxy._http_client = _UploadingClient()

        yields = [
            item
            async for item in proxy.stream_http(
                method="POST", path="/upload", headers={}, body=upload()
            )
        ]

        assert yields[0][0] == 200
        assert yields[1] == (None, None, b"ok")


# ---------------------------------------------------------------------------
# Tunnel: an error END is sent, and only one START
# ---------------------------------------------------------------------------


class TestTunnelErrorEnd:
    async def test_mid_body_read_error_ends_with_one_start(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t"
        tunnel._proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"first"],
                error=httpx.ReadError("connection reset"),
            ),
        )
        ws, sent = _mock_ws()

        await tunnel._handle_http_request_chunked(ws, _request("req-read-error"))

        types = _types(sent)
        assert types == [
            MessageType.HTTP_RESPONSE_START,
            MessageType.HTTP_RESPONSE_CHUNK,
            MessageType.HTTP_RESPONSE_END,
        ]
        assert types.count(MessageType.HTTP_RESPONSE_START) == 1
        end = ProtocolMessage.model_validate_json(sent[-1])
        assert end.payload["error"] == "upstream_error"

    async def test_mid_body_read_timeout_ends_with_upstream_timeout(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t"
        tunnel._proxy._http_client = _FakeClient(
            _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"first"],
                error=httpx.ReadTimeout("read timed out"),
            ),
        )
        ws, sent = _mock_ws()

        await tunnel._handle_http_request_chunked(ws, _request("req-read-timeout"))

        assert _types(sent).count(MessageType.HTTP_RESPONSE_START) == 1
        end = ProtocolMessage.model_validate_json(sent[-1])
        assert end.payload["error"] == "upstream_timeout"

    async def test_upstream_stream_error_ends_with_its_reason(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t"

        async def fake_stream(**_kw: object):
            yield (200, {"content-type": "text/event-stream"}, None)
            raise UpstreamStreamError("upstream_timeout")

        tunnel._proxy.stream_http = fake_stream
        ws, sent = _mock_ws()

        await tunnel._handle_http_request_chunked(ws, _request("req-upstream"))

        assert _types(sent) == [
            MessageType.HTTP_RESPONSE_START,
            MessageType.HTTP_RESPONSE_END,
        ]
        end = ProtocolMessage.model_validate_json(sent[-1])
        assert end.payload["error"] == "upstream_timeout"

    async def test_generic_exception_ends_with_client_error(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t"

        async def fake_stream(**_kw: object):
            yield (200, {"content-type": "text/plain"}, None)
            raise RuntimeError("boom")

        tunnel._proxy.stream_http = fake_stream
        ws, sent = _mock_ws()

        await tunnel._handle_http_request_chunked(ws, _request("req-generic"))

        assert _types(sent) == [
            MessageType.HTTP_RESPONSE_START,
            MessageType.HTTP_RESPONSE_END,
        ]
        end = ProtocolMessage.model_validate_json(sent[-1])
        assert end.payload["error"] == "client_error"
