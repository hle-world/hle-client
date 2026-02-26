"""Tests for the HLE client — LocalProxy and Tunnel."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hle_client.proxy import LocalProxy, ProxyConfig
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.models import (
    ProxiedHttpRequest,
    WsStreamClose,
    WsStreamFrame,
)
from hle_common.protocol import MessageType, ProtocolMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncIter:
    """Turn a plain list into an async iterator suitable for ``async for``."""

    def __init__(self, items: list):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _make_async_ws_mock(messages: list[str] | None = None) -> AsyncMock:
    """Build an AsyncMock WS that supports ``async for raw in ws``."""
    mock_ws = AsyncMock()
    sent: list[str] = []
    mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))
    mock_ws._sent = sent  # convenient accessor for assertions

    items = messages or []
    mock_ws.__aiter__ = MagicMock(return_value=_AsyncIter(items))
    return mock_ws


def _proxy(target_url: str = "http://localhost:8123", **kw) -> LocalProxy:
    """Create a LocalProxy with sensible test defaults."""
    return LocalProxy(ProxyConfig(target_url=target_url, **kw))


def _tunnel(service_url: str = "http://localhost:8123", **kw) -> Tunnel:
    """Create a Tunnel with sensible test defaults."""
    return Tunnel(TunnelConfig(service_url=service_url, **kw))


def _mock_httpx_response(
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes = b"OK",
) -> httpx.Response:
    """Build a minimal httpx.Response for stubbing."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = httpx.Headers(headers or {"content-type": "text/plain"})
    resp.content = content
    return resp


# ---------------------------------------------------------------------------
# LocalProxy tests
# ---------------------------------------------------------------------------


class TestProxyConfig:
    def test_defaults(self):
        cfg = ProxyConfig(target_url="http://localhost:3000")
        assert cfg.target_url == "http://localhost:3000"
        assert cfg.websocket_enabled is True
        assert cfg.timeout == 30.0
        assert cfg.max_retries == 3


class TestLocalProxyLifecycle:
    async def test_start_creates_http_client(self):
        proxy = _proxy()
        assert proxy._http_client is None

        await proxy.start()
        assert proxy._http_client is not None
        assert isinstance(proxy._http_client, httpx.AsyncClient)
        await proxy.stop()

    async def test_stop_clears_http_client(self):
        proxy = _proxy()
        await proxy.start()
        assert proxy._http_client is not None

        await proxy.stop()
        assert proxy._http_client is None

    async def test_stop_when_already_stopped_is_safe(self):
        proxy = _proxy()
        # Never started — stop should be a no-op.
        await proxy.stop()
        assert proxy._http_client is None

    async def test_start_stop_start_works(self):
        proxy = _proxy()
        await proxy.start()
        await proxy.stop()
        await proxy.start()
        assert proxy._http_client is not None
        await proxy.stop()


class TestLocalProxyForwardHttp:
    async def test_raises_when_not_started(self):
        proxy = _proxy()
        with pytest.raises(RuntimeError, match="Proxy not started"):
            await proxy.forward_http(method="GET", path="/test", headers={})

    async def test_rejects_absolute_url_ssrf(self):
        proxy = _proxy()
        await proxy.start()

        status, headers, body = await proxy.forward_http(
            method="GET",
            path="http://169.254.169.254/latest/meta-data/",
            headers={},
        )
        assert status == 400
        assert b"path must be relative" in body
        await proxy.stop()

    async def test_rejects_scheme_relative_url_ssrf(self):
        proxy = _proxy()
        await proxy.start()

        status, headers, body = await proxy.forward_http(
            method="GET",
            path="//evil.com/steal-data",
            headers={},
        )
        assert status == 400
        assert b"path must be relative" in body
        await proxy.stop()

    async def test_successful_get(self):
        proxy = _proxy()
        await proxy.start()

        mock_resp = _mock_httpx_response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
        )
        proxy._http_client.request = AsyncMock(return_value=mock_resp)

        status, headers, body = await proxy.forward_http(
            method="GET", path="/api/status", headers={"accept": "application/json"}
        )

        assert status == 200
        assert body == b'{"ok":true}'
        proxy._http_client.request.assert_awaited_once_with(
            method="GET",
            url="/api/status",
            headers={"accept": "application/json"},
            content=None,
        )
        await proxy.stop()

    async def test_post_with_body(self):
        proxy = _proxy()
        await proxy.start()

        mock_resp = _mock_httpx_response(status_code=201, content=b"created")
        proxy._http_client.request = AsyncMock(return_value=mock_resp)

        status, headers, body = await proxy.forward_http(
            method="POST",
            path="/items",
            headers={"content-type": "application/json"},
            body=b'{"name":"widget"}',
        )

        assert status == 201
        assert body == b"created"
        call_kwargs = proxy._http_client.request.call_args.kwargs
        assert call_kwargs["content"] == b'{"name":"widget"}'
        assert call_kwargs["method"] == "POST"
        await proxy.stop()

    async def test_query_string_appended(self):
        proxy = _proxy()
        await proxy.start()

        mock_resp = _mock_httpx_response()
        proxy._http_client.request = AsyncMock(return_value=mock_resp)

        await proxy.forward_http(
            method="GET",
            path="/search",
            headers={},
            query_string="q=hello&page=1",
        )

        call_kwargs = proxy._http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "/search?q=hello&page=1"
        await proxy.stop()

    async def test_empty_query_string_not_appended(self):
        proxy = _proxy()
        await proxy.start()

        mock_resp = _mock_httpx_response()
        proxy._http_client.request = AsyncMock(return_value=mock_resp)

        await proxy.forward_http(method="GET", path="/clean", headers={}, query_string="")

        call_kwargs = proxy._http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "/clean"
        await proxy.stop()

    async def test_hop_by_hop_headers_stripped(self):
        proxy = _proxy()
        await proxy.start()

        mock_resp = _mock_httpx_response()
        proxy._http_client.request = AsyncMock(return_value=mock_resp)

        await proxy.forward_http(
            method="GET",
            path="/resource",
            headers={
                "Host": "evil.example.com",
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive",
                "Upgrade": "websocket",
                "X-Custom": "keep-me",
                "Accept": "text/html",
            },
        )

        forwarded = proxy._http_client.request.call_args.kwargs["headers"]
        assert forwarded["Host"] == "evil.example.com"
        assert "Transfer-Encoding" not in forwarded
        assert "Connection" not in forwarded
        assert "Upgrade" not in forwarded
        # Legitimate headers survive
        assert forwarded["X-Custom"] == "keep-me"
        assert forwarded["Accept"] == "text/html"
        await proxy.stop()

    async def test_connect_error_returns_502(self):
        proxy = _proxy()
        await proxy.start()

        proxy._http_client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        status, headers, body = await proxy.forward_http(method="GET", path="/down", headers={})

        assert status == 502
        assert b"Bad Gateway" in body
        assert headers["content-type"] == "text/plain"
        await proxy.stop()

    async def test_timeout_returns_504(self):
        proxy = _proxy()
        await proxy.start()

        proxy._http_client.request = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))

        status, headers, body = await proxy.forward_http(method="GET", path="/slow", headers={})

        assert status == 504
        assert b"Gateway Timeout" in body
        assert headers["content-type"] == "text/plain"
        await proxy.stop()

    async def test_generic_http_error_returns_502(self):
        proxy = _proxy()
        await proxy.start()

        proxy._http_client.request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "server error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )

        status, headers, body = await proxy.forward_http(method="GET", path="/error", headers={})

        assert status == 502
        assert b"Bad Gateway" in body
        await proxy.stop()


# ---------------------------------------------------------------------------
# TunnelConfig tests
# ---------------------------------------------------------------------------


class TestTunnelConfig:
    def test_defaults(self):
        cfg = TunnelConfig(service_url="http://localhost:3000")
        assert cfg.service_url == "http://localhost:3000"
        assert cfg.relay_host == "hle.world"
        assert cfg.relay_port == 443
        assert cfg.auth_mode == "sso"
        assert cfg.service_label is None
        assert cfg.api_key is None
        assert cfg.websocket_enabled is True
        assert cfg.reconnect_delay == 1.0
        assert cfg.max_reconnect_delay == 60.0

    def test_custom_values(self):
        cfg = TunnelConfig(
            service_url="http://10.0.0.5:9090",
            relay_host="my-relay.example.com",
            relay_port=8443,
            auth_mode="token",
            service_label="ha",
            api_key="hle_testkey123",
            websocket_enabled=False,
            reconnect_delay=5.0,
            max_reconnect_delay=120.0,
        )
        assert cfg.relay_host == "my-relay.example.com"
        assert cfg.relay_port == 8443
        assert cfg.auth_mode == "token"
        assert cfg.service_label == "ha"
        assert cfg.api_key == "hle_testkey123"
        assert cfg.websocket_enabled is False
        assert cfg.reconnect_delay == 5.0
        assert cfg.max_reconnect_delay == 120.0


# ---------------------------------------------------------------------------
# Tunnel tests (all WebSocket interactions mocked)
# ---------------------------------------------------------------------------


class TestTunnelInit:
    def test_post_init_creates_proxy(self):
        tunnel = _tunnel()
        assert isinstance(tunnel._proxy, LocalProxy)
        assert tunnel._proxy.config.target_url == "http://localhost:8123"

    def test_initial_state(self):
        tunnel = _tunnel()
        assert tunnel._running is False
        assert tunnel._tunnel_id is None
        assert tunnel._public_url is None
        assert tunnel._ws is None
        assert tunnel._ws_streams == {}
        assert tunnel._tasks == set()


class TestTunnelRegistrationHandshake:
    """Verify the registration message the client builds."""

    async def test_registration_message_structure(self):
        tunnel = _tunnel(
            service_url="http://localhost:3000",
            service_label="myapp",
            api_key="hle_testkey_for_handshake",
            websocket_enabled=True,
            auth_mode="sso",
        )

        # We capture the message sent during _connect_once by mocking
        # websockets.connect to return a mock WS.
        mock_ws = AsyncMock()
        sent_messages: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent_messages.append(m))

        # The ack that the server would return:
        ack = ProtocolMessage(
            type=MessageType.TUNNEL_ACK,
            payload={
                "tunnel_id": "t-test-123",
                "subdomain": "myapp-abc",
                "public_url": "https://myapp-abc.hle.world",
                "websocket_enabled": True,
                "user_code": "abc",
                "service_label": "myapp",
            },
        )
        mock_ws.recv = AsyncMock(return_value=ack.model_dump_json())
        # Make the receive loop exit immediately (empty async iterator).
        mock_ws.__aiter__ = MagicMock(return_value=_AsyncIter([]))

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("hle_client.tunnel.websockets.connect", return_value=mock_ctx):
            await tunnel._proxy.start()
            await tunnel._connect_once()
            await tunnel._proxy.stop()

        # The first message sent should be the registration.
        assert len(sent_messages) >= 1
        reg_msg = ProtocolMessage.model_validate_json(sent_messages[0])
        assert reg_msg.type == MessageType.TUNNEL_REGISTER
        assert reg_msg.payload is not None
        assert reg_msg.payload["service_url"] == "http://localhost:3000"
        assert reg_msg.payload["service_label"] == "myapp"
        assert reg_msg.payload["api_key"] == "hle_testkey_for_handshake"
        assert reg_msg.payload["websocket_enabled"] is True
        assert reg_msg.payload["auth_mode"] == "sso"

        # Tunnel should have stored the ack data.
        assert tunnel._tunnel_id == "t-test-123"
        assert tunnel._public_url == "https://myapp-abc.hle.world"

    async def test_wrong_ack_type_raises(self):
        tunnel = _tunnel(api_key="hle_testkey_for_bad_ack")

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        # Return an ERROR instead of TUNNEL_ACK.
        bad_ack = ProtocolMessage(
            type=MessageType.ERROR,
            payload={"code": "bad", "message": "nope"},
        )
        mock_ws.recv = AsyncMock(return_value=bad_ack.model_dump_json())

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("hle_client.tunnel.websockets.connect", return_value=mock_ctx):
            await tunnel._proxy.start()
            with pytest.raises(ConnectionError, match="Expected TUNNEL_ACK"):
                await tunnel._connect_once()
            await tunnel._proxy.stop()


class TestTunnelHandleHttpRequest:
    """Test _handle_http_request: forwards to proxy, sends response back."""

    async def test_forwards_request_and_sends_response(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-42"
        await tunnel._proxy.start()

        # Stub the proxy's forward_http.
        tunnel._proxy.forward_http = AsyncMock(
            return_value=(200, {"content-type": "text/plain"}, b"hello world")
        )

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        request_payload = ProxiedHttpRequest(
            request_id="req-1",
            method="GET",
            path="/api/hello",
            headers={"accept": "text/plain"},
            body=None,
            query_string="",
        )
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-42",
            request_id="req-1",
            payload=request_payload.model_dump(),
        )

        await tunnel._handle_http_request(mock_ws, msg)

        # Verify forward_http was called correctly.
        tunnel._proxy.forward_http.assert_awaited_once_with(
            method="GET",
            path="/api/hello",
            headers={"accept": "text/plain"},
            body=None,
            query_string="",
        )

        # Verify the response was sent back over the WS.
        assert len(sent) == 1
        resp_msg = ProtocolMessage.model_validate_json(sent[0])
        assert resp_msg.type == MessageType.HTTP_RESPONSE
        assert resp_msg.tunnel_id == "t-42"
        assert resp_msg.request_id == "req-1"
        assert resp_msg.payload["status_code"] == 200
        assert resp_msg.payload["headers"]["content-type"] == "text/plain"
        # Body should be base64-encoded.
        assert base64.b64decode(resp_msg.payload["body"]) == b"hello world"
        await tunnel._proxy.stop()

    async def test_forwards_post_with_body(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-42"
        await tunnel._proxy.start()

        tunnel._proxy.forward_http = AsyncMock(return_value=(201, {}, b""))

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        raw_body = b'{"key":"value"}'
        encoded_body = base64.b64encode(raw_body).decode("ascii")
        request_payload = ProxiedHttpRequest(
            request_id="req-post",
            method="POST",
            path="/items",
            headers={"content-type": "application/json"},
            body=encoded_body,
            query_string="",
        )
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-42",
            request_id="req-post",
            payload=request_payload.model_dump(),
        )

        await tunnel._handle_http_request(mock_ws, msg)

        call_kwargs = tunnel._proxy.forward_http.call_args.kwargs
        assert call_kwargs["body"] == raw_body
        assert call_kwargs["method"] == "POST"
        await tunnel._proxy.stop()

    async def test_empty_body_response_encoded_as_empty_base64(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-42"
        await tunnel._proxy.start()

        # Empty body (b"") should be encoded as empty base64, not None.
        tunnel._proxy.forward_http = AsyncMock(return_value=(204, {}, b""))

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        request_payload = ProxiedHttpRequest(
            request_id="req-nobody",
            method="DELETE",
            path="/items/1",
            headers={},
        )
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-42",
            request_id="req-nobody",
            payload=request_payload.model_dump(),
        )

        await tunnel._handle_http_request(mock_ws, msg)

        resp_msg = ProtocolMessage.model_validate_json(sent[0])
        assert resp_msg.payload["body"] == ""
        await tunnel._proxy.stop()

    async def test_none_body_response_encoded_as_none(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-42"
        await tunnel._proxy.start()

        # None body should remain None.
        tunnel._proxy.forward_http = AsyncMock(return_value=(204, {}, None))

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        request_payload = ProxiedHttpRequest(
            request_id="req-nobody",
            method="DELETE",
            path="/items/1",
            headers={},
        )
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-42",
            request_id="req-nobody",
            payload=request_payload.model_dump(),
        )

        await tunnel._handle_http_request(mock_ws, msg)

        resp_msg = ProtocolMessage.model_validate_json(sent[0])
        assert resp_msg.payload["body"] is None
        await tunnel._proxy.stop()


class TestTunnelSsrfProtection:
    """Test SSRF guards on proxied paths."""

    async def test_ws_open_rejects_absolute_url(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-ssrf"

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        from hle_common.models import WsStreamOpen

        open_payload = WsStreamOpen(
            stream_id="s-evil",
            path="http://169.254.169.254/latest/meta-data/",
        )
        msg = ProtocolMessage(
            type=MessageType.WS_OPEN,
            payload=open_payload.model_dump(),
        )

        await tunnel._handle_ws_open(mock_ws, msg)

        # Should send a WS_CLOSE back, not open a connection.
        assert len(sent) == 1
        close_msg = ProtocolMessage.model_validate_json(sent[0])
        assert close_msg.type == MessageType.WS_CLOSE
        assert close_msg.payload["code"] == 1008

    async def test_ws_open_rejects_scheme_relative_path(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-ssrf2"

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        from hle_common.models import WsStreamOpen

        open_payload = WsStreamOpen(stream_id="s-evil2", path="//evil.com/ws")
        msg = ProtocolMessage(
            type=MessageType.WS_OPEN,
            payload=open_payload.model_dump(),
        )

        await tunnel._handle_ws_open(mock_ws, msg)

        assert len(sent) == 1
        close_msg = ProtocolMessage.model_validate_json(sent[0])
        assert close_msg.type == MessageType.WS_CLOSE


class TestTunnelHandleWsFrame:
    """Test _handle_ws_frame: relays frame to the local WS connection."""

    async def test_forwards_text_frame(self):
        tunnel = _tunnel()
        mock_local_ws = AsyncMock()
        tunnel._ws_streams["stream-1"] = mock_local_ws

        frame = WsStreamFrame(stream_id="stream-1", data="hello", is_binary=False)
        msg = ProtocolMessage(
            type=MessageType.WS_FRAME,
            payload=frame.model_dump(),
        )

        await tunnel._handle_ws_frame(msg)

        mock_local_ws.send.assert_awaited_once_with("hello")

    async def test_forwards_binary_frame_decoded(self):
        tunnel = _tunnel()
        mock_local_ws = AsyncMock()
        tunnel._ws_streams["stream-2"] = mock_local_ws

        raw_bytes = b"\x00\x01\x02\x03"
        b64_data = base64.b64encode(raw_bytes).decode("ascii")
        frame = WsStreamFrame(stream_id="stream-2", data=b64_data, is_binary=True)
        msg = ProtocolMessage(
            type=MessageType.WS_FRAME,
            payload=frame.model_dump(),
        )

        await tunnel._handle_ws_frame(msg)

        mock_local_ws.send.assert_awaited_once_with(raw_bytes)

    async def test_unknown_stream_id_is_ignored(self):
        tunnel = _tunnel()
        # No streams registered — should not raise.
        frame = WsStreamFrame(stream_id="ghost", data="nope", is_binary=False)
        msg = ProtocolMessage(
            type=MessageType.WS_FRAME,
            payload=frame.model_dump(),
        )
        await tunnel._handle_ws_frame(msg)  # no error

    async def test_closed_connection_removes_stream(self):
        import websockets.exceptions

        tunnel = _tunnel()
        mock_local_ws = AsyncMock()
        mock_local_ws.send = AsyncMock(
            side_effect=websockets.exceptions.ConnectionClosed(None, None)
        )
        tunnel._ws_streams["stream-3"] = mock_local_ws

        frame = WsStreamFrame(stream_id="stream-3", data="msg", is_binary=False)
        msg = ProtocolMessage(
            type=MessageType.WS_FRAME,
            payload=frame.model_dump(),
        )

        await tunnel._handle_ws_frame(msg)

        # Stream should have been removed.
        assert "stream-3" not in tunnel._ws_streams


class TestTunnelHandleWsClose:
    """Test _handle_ws_close: closes the local WS connection."""

    async def test_closes_local_ws(self):
        tunnel = _tunnel()
        mock_local_ws = AsyncMock()
        tunnel._ws_streams["stream-c1"] = mock_local_ws

        close_req = WsStreamClose(stream_id="stream-c1", code=1001, reason="going away")
        msg = ProtocolMessage(
            type=MessageType.WS_CLOSE,
            payload=close_req.model_dump(),
        )

        await tunnel._handle_ws_close(msg)

        mock_local_ws.close.assert_awaited_once_with(code=1001, reason="going away")
        assert "stream-c1" not in tunnel._ws_streams

    async def test_close_unknown_stream_is_noop(self):
        tunnel = _tunnel()
        close_req = WsStreamClose(stream_id="nonexistent")
        msg = ProtocolMessage(
            type=MessageType.WS_CLOSE,
            payload=close_req.model_dump(),
        )
        # Should not raise.
        await tunnel._handle_ws_close(msg)

    async def test_close_tolerates_exception(self):
        tunnel = _tunnel()
        mock_local_ws = AsyncMock()
        mock_local_ws.close = AsyncMock(side_effect=Exception("already gone"))
        tunnel._ws_streams["stream-c2"] = mock_local_ws

        close_req = WsStreamClose(stream_id="stream-c2")
        msg = ProtocolMessage(
            type=MessageType.WS_CLOSE,
            payload=close_req.model_dump(),
        )

        # Should not raise despite the exception.
        await tunnel._handle_ws_close(msg)
        assert "stream-c2" not in tunnel._ws_streams


class TestTunnelPingPong:
    """Test that PING messages are answered with PONG."""

    async def test_ping_responds_with_pong(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-ping"

        ping_msg = ProtocolMessage(type=MessageType.PING)
        mock_ws = _make_async_ws_mock([ping_msg.model_dump_json()])

        await tunnel._receive_loop(mock_ws)

        sent = mock_ws._sent
        assert len(sent) == 1
        pong = ProtocolMessage.model_validate_json(sent[0])
        assert pong.type == MessageType.PONG
        assert pong.tunnel_id == "t-ping"


class TestTunnelSpawn:
    """Test _spawn: schedules coroutines and tracks tasks."""

    async def test_spawn_tracks_task(self):
        tunnel = _tunnel()
        ran = False

        async def _dummy():
            nonlocal ran
            ran = True

        tunnel._spawn(_dummy())
        assert len(tunnel._tasks) == 1

        # Let the event loop run the task.
        await asyncio.sleep(0)
        assert ran is True

    async def test_spawn_auto_discards_completed_task(self):
        tunnel = _tunnel()

        async def _instant():
            pass

        tunnel._spawn(_instant())
        assert len(tunnel._tasks) == 1

        # Allow the task to complete and the done callback to fire.
        await asyncio.sleep(0)
        # Give the done callback a chance to run.
        await asyncio.sleep(0)
        assert len(tunnel._tasks) == 0


class TestTunnelCleanup:
    """Test _cleanup: closes streams, cancels tasks, stops proxy."""

    async def test_cleanup_closes_ws_streams(self):
        tunnel = _tunnel()
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        tunnel._ws_streams["s1"] = mock_ws1
        tunnel._ws_streams["s2"] = mock_ws2

        await tunnel._cleanup()

        mock_ws1.close.assert_awaited_once()
        mock_ws2.close.assert_awaited_once()
        assert tunnel._ws_streams == {}

    async def test_cleanup_cancels_tasks(self):
        tunnel = _tunnel()

        async def _block():
            await asyncio.sleep(999)

        tunnel._spawn(_block())
        tunnel._spawn(_block())
        assert len(tunnel._tasks) == 2

        await tunnel._cleanup()

        assert len(tunnel._tasks) == 0

    async def test_cleanup_resets_ws(self):
        tunnel = _tunnel()
        tunnel._ws = MagicMock()

        await tunnel._cleanup()

        assert tunnel._ws is None

    async def test_cleanup_stops_proxy(self):
        tunnel = _tunnel()
        await tunnel._proxy.start()
        assert tunnel._proxy._http_client is not None

        await tunnel._cleanup()

        assert tunnel._proxy._http_client is None

    async def test_cleanup_tolerates_ws_close_error(self):
        tunnel = _tunnel()
        bad_ws = AsyncMock()
        bad_ws.close = AsyncMock(side_effect=Exception("boom"))
        tunnel._ws_streams["bad"] = bad_ws

        # Should not raise.
        await tunnel._cleanup()
        assert tunnel._ws_streams == {}


class TestTunnelProperties:
    def test_is_connected_false_initially(self):
        tunnel = _tunnel()
        assert tunnel.is_connected is False

    def test_is_connected_true_when_running_and_ws_set(self):
        tunnel = _tunnel()
        tunnel._running = True
        tunnel._ws = MagicMock()
        assert tunnel.is_connected is True

    def test_is_connected_false_when_not_running(self):
        tunnel = _tunnel()
        tunnel._running = False
        tunnel._ws = MagicMock()
        assert tunnel.is_connected is False

    def test_public_url_none_initially(self):
        tunnel = _tunnel()
        assert tunnel.public_url is None

    def test_public_url_reflects_stored_value(self):
        tunnel = _tunnel()
        tunnel._public_url = "https://myapp.relay.hle.world"
        assert tunnel.public_url == "https://myapp.relay.hle.world"


class TestTunnelReceiveLoopDispatch:
    """Verify that _receive_loop dispatches to the correct handler."""

    async def test_dispatches_http_request(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-dispatch"
        await tunnel._proxy.start()

        tunnel._proxy.forward_http = AsyncMock(return_value=(200, {}, b"ok"))

        http_req = ProxiedHttpRequest(
            request_id="req-d1",
            method="GET",
            path="/dispatch-test",
            headers={},
        )
        incoming = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-dispatch",
            request_id="req-d1",
            payload=http_req.model_dump(),
        )
        mock_ws = _make_async_ws_mock([incoming.model_dump_json()])

        await tunnel._receive_loop(mock_ws)
        # Allow spawned tasks to complete.
        await asyncio.sleep(0.05)

        sent = mock_ws._sent
        assert len(sent) >= 1
        resp_msg = ProtocolMessage.model_validate_json(sent[0])
        assert resp_msg.type == MessageType.HTTP_RESPONSE
        assert resp_msg.payload["request_id"] == "req-d1"
        await tunnel._proxy.stop()

    async def test_dispatches_ws_close(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-wsc"

        mock_local_ws = AsyncMock()
        tunnel._ws_streams["sid-close"] = mock_local_ws

        close_payload = WsStreamClose(stream_id="sid-close", code=1000, reason="")
        incoming = ProtocolMessage(
            type=MessageType.WS_CLOSE,
            payload=close_payload.model_dump(),
        )

        mock_ws = _make_async_ws_mock([incoming.model_dump_json()])

        await tunnel._receive_loop(mock_ws)
        await asyncio.sleep(0.05)

        mock_local_ws.close.assert_awaited_once()

    async def test_unhandled_type_does_not_raise(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-unk"

        incoming = ProtocolMessage(
            type=MessageType.ERROR,
            payload={"code": "test", "message": "test"},
        )

        mock_ws = _make_async_ws_mock([incoming.model_dump_json()])

        # Should not raise.
        await tunnel._receive_loop(mock_ws)

    async def test_malformed_message_skipped(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-bad"

        mock_ws = _make_async_ws_mock(["this is not valid json {{{"])

        # Should not raise; the bad message is just logged and skipped.
        await tunnel._receive_loop(mock_ws)
