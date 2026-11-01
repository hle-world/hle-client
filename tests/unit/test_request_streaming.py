"""Protocol 1.7: streamed HTTP request bodies with credit flow control.

Covers the shared models and the client's receive/ack path end to end over real
sockets: a fake relay sends ``HTTP_REQUEST`` with ``body_streamed`` plus body
chunks, a real local HTTP service receives them, and the relay's reader records
``STREAM_WINDOW_UPDATE`` frames.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets

from hle_client.proxy import CONTENT_LENGTH_HEADER
from hle_client.tunnel import WS_MAX_MESSAGE_SIZE, Tunnel, TunnelConfig
from hle_common.models import (
    CAPABILITY_CHUNKED_RESPONSE,
    CAPABILITY_REQUEST_STREAMING,
    REQUEST_BODY_INITIAL_WINDOW,
    REQUEST_BODY_MAX_CHUNK,
    HttpRequestBodyChunk,
    HttpRequestBodyEnd,
    HttpResponseChunk,
    HttpResponseStart,
    ProxiedHttpRequest,
    ProxiedHttpResponse,
    StreamWindowUpdate,
    TunnelRegistrationResponse,
)
from hle_common.protocol import PROTOCOL_VERSION, MessageType, ProtocolMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

API_KEY = "hle_" + "0" * 32


# --------------------------------------------------------------------------- #
# Shared models (no sockets)
# --------------------------------------------------------------------------- #
class TestModels:
    def test_protocol_version(self):
        assert PROTOCOL_VERSION == "1.7"

    def test_capability_value(self):
        assert CAPABILITY_REQUEST_STREAMING == "request_streaming"

    def test_window_constants(self):
        assert REQUEST_BODY_INITIAL_WINDOW == 4 * 1024 * 1024
        assert REQUEST_BODY_MAX_CHUNK == 256 * 1024

    def test_proxied_http_request_default_dump(self):
        req = ProxiedHttpRequest(request_id="r", method="POST", path="/", headers={})
        assert req.body_streamed is False
        assert req.model_dump()["body_streamed"] is False

    def test_message_types(self):
        assert MessageType.HTTP_REQUEST_BODY_CHUNK == "http_request_body_chunk"
        assert MessageType.HTTP_REQUEST_BODY_END == "http_request_body_end"
        assert MessageType.STREAM_WINDOW_UPDATE == "stream_window_update"

    def test_round_trip(self):
        for original in (
            HttpRequestBodyChunk(request_id="r", chunk_index=2, data="Ymxh"),
            HttpRequestBodyEnd(request_id="r", total_bytes=99),
            HttpRequestBodyEnd(request_id="r", total_bytes=99, aborted=True),
            StreamWindowUpdate(request_id="r", bytes=1024),
        ):
            raw = original.model_dump_json()
            restored = type(original).model_validate_json(raw)
            assert restored == original

    def test_window_update_must_be_positive(self):
        with pytest.raises(ValueError):
            StreamWindowUpdate(request_id="r", bytes=0)
        with pytest.raises(ValueError):
            StreamWindowUpdate(request_id="r", bytes=-1)

    def test_proxied_request_is_a_superset_of_1_6(self):
        old = (
            '{"request_id":"r","method":"GET","path":"/","headers":{},'
            '"body":null,"query_string":""}'
        )
        req = ProxiedHttpRequest.model_validate_json(old)
        assert req.body_streamed is False


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@contextlib.asynccontextmanager
async def _serve(handler: Callable[[Any], Awaitable[None]], **kw: Any):
    server = await websockets.serve(handler, "127.0.0.1", 0, **kw)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


class _FakeRelay:
    """Relay side of one tunnel: sends a streamed body and reads window updates."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.credit = REQUEST_BODY_INITIAL_WINDOW
        self.update_bytes = 0
        self.updates = 0
        self.sent_bytes = 0
        self.max_outstanding = 0
        self.responses: list[ProtocolMessage] = []
        self._reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = ProtocolMessage.model_validate_json(raw)
            if msg.type == MessageType.STREAM_WINDOW_UPDATE:
                update = StreamWindowUpdate.model_validate(msg.payload)
                self.updates += 1
                self.update_bytes += update.bytes
                self.credit += update.bytes
            else:
                self.responses.append(msg)

    async def send_message(self, msg: ProtocolMessage) -> None:
        await self.ws.send(msg.model_dump_json())

    async def send_request(self, req: ProxiedHttpRequest) -> None:
        await self.send_message(
            ProtocolMessage(
                type=MessageType.HTTP_REQUEST,
                tunnel_id="t1",
                request_id=req.request_id,
                payload=req.model_dump(),
            )
        )

    async def send_chunk(
        self, request_id: str, index: int, data: bytes, *, respect_credit: bool = True
    ) -> None:
        if respect_credit:
            while len(data) > self.credit:
                # Poll rather than wait on an event: the update can arrive
                # between the check and the wait, and a lost wakeup would stall.
                await asyncio.sleep(0.001)
            self.credit -= len(data)
        self.sent_bytes += len(data)
        self.max_outstanding = max(self.max_outstanding, self.sent_bytes - self.update_bytes)
        chunk = HttpRequestBodyChunk(
            request_id=request_id,
            chunk_index=index,
            data=base64.b64encode(data).decode("ascii"),
        )
        await self.send_message(
            ProtocolMessage(
                type=MessageType.HTTP_REQUEST_BODY_CHUNK,
                tunnel_id="t1",
                request_id=request_id,
                payload=chunk.model_dump(),
            )
        )

    async def send_end(self, request_id: str, total: int, *, aborted: bool = False) -> None:
        end = HttpRequestBodyEnd(request_id=request_id, total_bytes=total, aborted=aborted)
        await self.send_message(
            ProtocolMessage(
                type=MessageType.HTTP_REQUEST_BODY_END,
                tunnel_id="t1",
                request_id=request_id,
                payload=end.model_dump(),
            )
        )

    async def send_body(
        self,
        request_id: str,
        data: bytes,
        *,
        chunk_size: int = REQUEST_BODY_MAX_CHUNK,
        respect_credit: bool = True,
    ) -> None:
        for index, start in enumerate(range(0, len(data), chunk_size)):
            await self.send_chunk(
                request_id,
                index,
                data[start : start + chunk_size],
                respect_credit=respect_credit,
            )
        await self.send_end(request_id, len(data))

    async def send_cancel(self, request_id: str) -> None:
        await self.send_message(
            ProtocolMessage(
                type=MessageType.HTTP_REQUEST_CANCEL,
                tunnel_id="t1",
                payload={"request_id": request_id},
            )
        )

    async def wait_for_response(
        self, request_id: str, *, chunked: bool = False, timeout: float = 90.0
    ) -> tuple[int, dict[str, Any], bytes]:
        deadline = time.monotonic() + timeout
        while True:
            msgs = [m for m in self.responses if m.request_id == request_id]
            if chunked:
                starts = [m for m in msgs if m.type == MessageType.HTTP_RESPONSE_START]
                ends = [m for m in msgs if m.type == MessageType.HTTP_RESPONSE_END]
                if starts and ends:
                    start = HttpResponseStart.model_validate(starts[0].payload)
                    body = b"".join(
                        base64.b64decode(HttpResponseChunk.model_validate(m.payload).data)
                        for m in msgs
                        if m.type == MessageType.HTTP_RESPONSE_CHUNK
                    )
                    return start.status_code, start.headers, body
            else:
                wholes = [m for m in msgs if m.type == MessageType.HTTP_RESPONSE]
                if wholes:
                    resp = ProxiedHttpResponse.model_validate(wholes[0].payload)
                    body = base64.b64decode(resp.body) if resp.body is not None else b""
                    return resp.status_code, resp.headers, body
            if time.monotonic() > deadline:
                raise TimeoutError(f"no response for {request_id}")
            await asyncio.sleep(0.005)


@contextlib.asynccontextmanager
async def _upstream(record: dict[str, Any]):
    """A local HTTP service whose behaviour is chosen by the request path."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)

            body = b""
            if path.startswith("/ping"):
                body = b""
            elif path.startswith("/slow"):
                remaining = length
                while remaining > 0:
                    piece = await reader.read(min(64 * 1024, remaining))
                    if not piece:
                        raise asyncio.IncompleteReadError(body, length)
                    body += piece
                    remaining -= len(piece)
                    await asyncio.sleep(0.01)
            elif path.startswith("/stall"):
                # Do not read at all for a moment, so the client's queue fills.
                await asyncio.sleep(0.3)
                body = await reader.readexactly(length) if length else b""
            else:
                body = await reader.readexactly(length) if length else b""

            status = 413 if path.startswith("/maybe413") else 200
            record.setdefault("requests", []).append(
                {"method": method, "path": path, "headers": headers, "body": body}
            )
            payload = b"ok"
            header = (
                f"HTTP/1.1 {status} X\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(header.encode() + payload)
            await writer.drain()
        except Exception as exc:
            # For assertions only; the handler must never crash the server.
            record.setdefault("errors", []).append((type(exc).__name__, str(exc)))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.002)


def _upstream_saw_abort(record: dict[str, Any]) -> bool:
    """Whether an upstream read died because the client dropped the connection."""
    names = {name for name, _ in record.get("errors", [])}
    return bool(names & {"IncompleteReadError", "ConnectionResetError"})


async def _run_tunnel(
    service_url: str,
    server_caps: list[str],
    scenario: Callable[[_FakeRelay, Tunnel], Awaitable[None]],
) -> tuple[dict[str, Any], Tunnel]:
    record: dict[str, Any] = {}
    tunnel = Tunnel(TunnelConfig(service_url=service_url, service_label="app", api_key=API_KEY))

    async def handler(ws: Any) -> None:
        try:
            reg = ProtocolMessage.model_validate_json(await ws.recv())
            record["capabilities"] = (reg.payload or {}).get("capabilities")
            ack = TunnelRegistrationResponse(
                tunnel_id="t1",
                subdomain="app-x",
                public_url="https://app-x.hle.world",
                websocket_enabled=True,
                user_code="x",
                service_label="app",
                server_capabilities=server_caps,
            )
            await ws.send(
                ProtocolMessage(
                    type=MessageType.TUNNEL_ACK, payload=ack.model_dump()
                ).model_dump_json()
            )
            relay = _FakeRelay(ws)
            record["relay"] = relay
            relay.start()
            try:
                await scenario(relay, tunnel)
            finally:
                await relay.stop()
        except BaseException as exc:  # surfaced to the test below
            record["error"] = exc
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    async with _serve(handler, max_size=WS_MAX_MESSAGE_SIZE) as port:
        with patch.object(
            Tunnel,
            "_discover_relay_uri",
            AsyncMock(return_value=f"ws://127.0.0.1:{port}"),
        ):
            await tunnel._proxy.start()
            try:
                await asyncio.wait_for(tunnel._connect_once(), timeout=120)
            finally:
                await tunnel._cleanup()
                await tunnel._proxy.stop()
    if "error" in record:
        raise record["error"]
    return record, tunnel


def _stream_request(
    request_id: str,
    path: str,
    total: int,
    headers: dict[str, str] | None = None,
) -> ProxiedHttpRequest:
    merged = {"content-type": "application/octet-stream", "content-length": str(total)}
    merged.update(headers or {})
    return ProxiedHttpRequest(
        request_id=request_id,
        method="POST",
        path=path,
        headers=merged,
        body=None,
        body_streamed=True,
    )


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
class TestRequestStreaming:
    async def test_registration_advertises_request_streaming(self):
        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            return None

        async with _upstream({}) as url:
            record, _ = await _run_tunnel(url, [], scenario)
        assert CAPABILITY_REQUEST_STREAMING in record["capabilities"]
        assert CAPABILITY_CHUNKED_RESPONSE in record["capabilities"]

    async def test_20mib_body_reaches_upstream_byte_identical(self):
        body = os.urandom(20 * 1024 * 1024)
        upstream: dict[str, Any] = {}

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("big", "/upload", len(body))
            await relay.send_request(req)
            await relay.send_body("big", body)
            status, _, resp_body = await relay.wait_for_response("big", chunked=True)
            assert status == 200
            assert resp_body == b"ok"

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)

        got = [r for r in upstream["requests"] if r["path"].startswith("/upload")]
        assert len(got) == 1
        assert got[0]["body"] == body
        assert got[0]["headers"]["content-length"] == str(len(body))

    async def test_content_length_preserved_on_chunked_path(self):
        body = os.urandom(3 * 1024 * 1024)
        upstream: dict[str, Any] = {}

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("clen", "/upload", len(body))
            await relay.send_request(req)
            await relay.send_body("clen", body)
            status, _, _ = await relay.wait_for_response("clen", chunked=True)
            assert status == 200

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)

        got = upstream["requests"][0]
        assert got["body"] == body
        assert got["headers"]["content-length"] == str(len(body))
        assert CONTENT_LENGTH_HEADER not in got["headers"]

    async def test_window_updates_bound_the_queue_and_sum_to_consumed(self):
        body = os.urandom(8 * 1024 * 1024)
        boxes: dict[str, Any] = {"max": 0}

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("slow", "/slow", len(body))
            await relay.send_request(req)
            await _wait_until(lambda: "slow" in tunnel._request_bodies)

            stop = asyncio.Event()

            async def poll() -> None:
                while not stop.is_set():
                    stream = tunnel._request_bodies.get("slow")
                    if stream is not None:
                        boxes["max"] = max(boxes["max"], stream.received - stream.consumed)
                    await asyncio.sleep(0.001)

            poller = asyncio.create_task(poll())
            await relay.send_body("slow", body)
            status, _, _ = await relay.wait_for_response("slow", chunked=True)
            stop.set()
            await poller
            assert status == 200

        async with _upstream({}) as url:
            record, _ = await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)

        relay: _FakeRelay = record["relay"]
        assert boxes["max"] <= REQUEST_BODY_INITIAL_WINDOW + REQUEST_BODY_MAX_CHUNK
        assert relay.updates >= 1
        assert relay.update_bytes == len(body)
        assert relay.max_outstanding <= REQUEST_BODY_INITIAL_WINDOW

    async def test_overrun_aborts_request_and_read_loop_survives(self):
        upstream: dict[str, Any] = {}
        body = os.urandom(8 * 1024 * 1024)

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("over", "/stall", len(body))
            await relay.send_request(req)
            # Ignore credit entirely: the client must abort once it has queued
            # more than its window allows.
            await relay.send_body("over", body, respect_credit=False)
            await _wait_until(lambda: "over" not in tunnel._request_bodies)

            # A concurrent request still completes: the read loop is unaffected.
            ping = ProxiedHttpRequest(
                request_id="after", method="GET", path="/ping", headers={"content-length": "0"}
            )
            await relay.send_request(ping)
            status, _, _ = await relay.wait_for_response("after", chunked=True)
            assert status == 200

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)
            await _wait_until(lambda: _upstream_saw_abort(upstream), timeout=5.0)

        assert _upstream_saw_abort(upstream)

    async def test_unknown_request_chunk_is_dropped(self):
        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            await relay.send_chunk("ghost", 0, b"x" * 1024)
            await relay.send_end("ghost", 1024)
            # The read loop is alive: a following request answers normally.
            ping = ProxiedHttpRequest(
                request_id="live", method="GET", path="/ping", headers={"content-length": "0"}
            )
            await relay.send_request(ping)
            status, _, _ = await relay.wait_for_response("live", chunked=True)
            assert status == 200

        async with _upstream({}) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)

    async def test_cancel_mid_upload_aborts_upstream_and_frees_state(self):
        upstream: dict[str, Any] = {}
        body = os.urandom(4 * 1024 * 1024)

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("cancel", "/slow", len(body))
            await relay.send_request(req)
            await relay.send_chunk("cancel", 0, body[:REQUEST_BODY_MAX_CHUNK])
            await relay.send_cancel("cancel")
            status, _, _ = await relay.wait_for_response("cancel")
            assert status in (400, 502)
            await _wait_until(lambda: "cancel" not in tunnel._request_bodies)

            ping = ProxiedHttpRequest(
                request_id="live", method="GET", path="/ping", headers={"content-length": "0"}
            )
            await relay.send_request(ping)
            status, _, _ = await relay.wait_for_response("live", chunked=True)
            assert status == 200

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)
            await _wait_until(lambda: _upstream_saw_abort(upstream), timeout=5.0)

        assert _upstream_saw_abort(upstream)

    async def test_upstream_answers_early_response_sent_state_freed(self):
        upstream: dict[str, Any] = {}
        body = os.urandom(2 * 1024 * 1024)

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("early", "/maybe413", len(body))
            await relay.send_request(req)
            await relay.send_body("early", body)
            status, _, _ = await relay.wait_for_response("early", chunked=True)
            assert status == 413
            await _wait_until(lambda: "early" not in tunnel._request_bodies)

            # A chunk for the finished request is dropped silently.
            await relay.send_chunk("early", 99, b"late")
            ping = ProxiedHttpRequest(
                request_id="live", method="GET", path="/ping", headers={"content-length": "0"}
            )
            await relay.send_request(ping)
            status, _, _ = await relay.wait_for_response("live", chunked=True)
            assert status == 200

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)

    async def test_aborted_end_aborts_upstream_request(self):
        upstream: dict[str, Any] = {}
        body = os.urandom(4 * 1024 * 1024)

        async def scenario(relay: _FakeRelay, tunnel: Tunnel) -> None:
            req = _stream_request("aborted", "/slow", len(body))
            await relay.send_request(req)
            await relay.send_chunk("aborted", 0, body[:REQUEST_BODY_MAX_CHUNK])
            await relay.send_end("aborted", len(body), aborted=True)
            status, _, _ = await relay.wait_for_response("aborted")
            assert status in (400, 502)
            await _wait_until(lambda: "aborted" not in tunnel._request_bodies)

            ping = ProxiedHttpRequest(
                request_id="live", method="GET", path="/ping", headers={"content-length": "0"}
            )
            await relay.send_request(ping)
            status, _, _ = await relay.wait_for_response("live", chunked=True)
            assert status == 200

        async with _upstream(upstream) as url:
            await _run_tunnel(url, [CAPABILITY_CHUNKED_RESPONSE], scenario)
            await _wait_until(lambda: _upstream_saw_abort(upstream), timeout=5.0)

        assert _upstream_saw_abort(upstream)


class TestStreamedRetryGuard:
    async def test_502_retry_is_skipped_for_a_streamed_body(self):
        from hle_client.proxy import LocalProxy, ProxyConfig

        proxy = LocalProxy(ProxyConfig(target_url="http://localhost:8123"))
        await proxy.start()
        calls: list[Any] = []

        response = MagicMock()
        response.status_code = 502
        response.headers.raw = []
        response.content = b""

        async def fake_request(**kwargs: Any) -> Any:
            calls.append(kwargs["content"])
            return response

        proxy._http_client.request = fake_request  # type: ignore[assignment, union-attr]

        async def body_iter():
            yield b"chunk"

        status, _, _ = await proxy.forward_http(
            method="POST",
            path="/x",
            headers={"host": "app.hle.world"},
            body=body_iter(),
        )
        # One call, not two: a streamed body cannot be replayed.
        assert len(calls) == 1
        assert status == 502
        await proxy.stop()
