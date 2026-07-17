"""Tests for the server-toggled diagnostics channel — new emit sites + log ring."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx

from hle_client.proxy import UPSTREAM_ERROR_HEADER, LocalProxy, ProxyConfig
from hle_client.tunnel import (
    Tunnel,
    TunnelConfig,
    _DiagnosticLogHandler,
    _redact_secrets,
    _service_check_data,
)
from hle_common.models import LogConfig, ProxiedHttpRequest
from hle_common.protocol import MessageType, ProtocolMessage


def _proxy(target_url: str = "http://localhost:8123", **kw) -> LocalProxy:
    return LocalProxy(ProxyConfig(target_url=target_url, **kw))


def _tunnel(service_url: str = "http://localhost:8123", **kw) -> Tunnel:
    return Tunnel(TunnelConfig(service_url=service_url, **kw))


def _mock_httpx_response(
    status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = httpx.Headers(headers or {})
    return resp


# ---------------------------------------------------------------------------
# Proxy: upstream-error marker header
# ---------------------------------------------------------------------------


class TestProxyUpstreamErrorHeader:
    async def test_connect_error_sets_marker_header(self):
        proxy = _proxy()
        await proxy.start()
        proxy._http_client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        status, headers, _ = await proxy.forward_http(method="GET", path="/down", headers={})
        assert status == 502
        assert headers[UPSTREAM_ERROR_HEADER] == "ConnectError"
        await proxy.stop()

    async def test_timeout_sets_marker_header(self):
        proxy = _proxy()
        await proxy.start()
        proxy._http_client.request = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))
        status, headers, _ = await proxy.forward_http(method="GET", path="/slow", headers={})
        assert status == 504
        assert headers[UPSTREAM_ERROR_HEADER] == "ReadTimeout"
        await proxy.stop()

    async def test_success_has_no_marker_header(self):
        proxy = _proxy()
        await proxy.start()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = httpx.Headers({"content-type": "text/plain"})
        resp.content = b"ok"
        proxy._http_client.request = AsyncMock(return_value=resp)
        _, headers, _ = await proxy.forward_http(method="GET", path="/ok", headers={})
        assert UPSTREAM_ERROR_HEADER not in headers
        await proxy.stop()

    async def test_stream_connect_error_sets_marker_header(self):
        proxy = _proxy()
        await proxy.start()

        def _boom(*a, **k):
            raise httpx.ConnectError("connection refused")

        proxy._http_client.stream = MagicMock(side_effect=_boom)
        yields = [item async for item in proxy.stream_http(method="GET", path="/down", headers={})]
        status, headers, _ = yields[0]
        assert status == 502
        assert headers[UPSTREAM_ERROR_HEADER] == "ConnectError"
        await proxy.stop()


# ---------------------------------------------------------------------------
# Tunnel: strip marker + emit http.upstream_error
# ---------------------------------------------------------------------------


class TestTunnelUpstreamErrorDiagnostic:
    def test_pop_upstream_error_string_value(self):
        tunnel = _tunnel()
        headers = {"content-type": "text/plain", UPSTREAM_ERROR_HEADER: "ConnectError"}
        assert tunnel._pop_upstream_error(headers) == "ConnectError"
        assert UPSTREAM_ERROR_HEADER not in headers

    def test_pop_upstream_error_case_insensitive_and_list(self):
        tunnel = _tunnel()
        headers = {"X-Hle-Upstream-Error": ["TimeoutException", "extra"]}
        assert tunnel._pop_upstream_error(headers) == "TimeoutException"
        assert headers == {}

    def test_pop_upstream_error_absent(self):
        tunnel = _tunnel()
        headers = {"content-type": "text/plain"}
        assert tunnel._pop_upstream_error(headers) is None
        assert headers == {"content-type": "text/plain"}

    async def test_buffered_emits_and_strips(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-err"
        tunnel._diagnostics_enabled = True
        await tunnel._proxy.start()
        tunnel._proxy.forward_http = AsyncMock(
            return_value=(
                502,
                {"content-type": "text/plain", UPSTREAM_ERROR_HEADER: "ConnectError"},
                b"Bad Gateway: local service connection refused",
            )
        )
        emitted: list[tuple[str, dict]] = []
        tunnel._emit_diagnostic = lambda event, **data: emitted.append((event, data))

        mock_ws = AsyncMock()
        sent: list[str] = []
        mock_ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        req = ProxiedHttpRequest(
            request_id="req-e", method="GET", path="/down", headers={}, body=None, query_string=""
        )
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-err",
            request_id="req-e",
            payload=req.model_dump(),
        )
        await tunnel._handle_http_request_buffered(mock_ws, msg)

        resp_msg = ProtocolMessage.model_validate_json(sent[0])
        assert UPSTREAM_ERROR_HEADER not in resp_msg.payload["headers"]
        assert emitted == [
            (
                "http.upstream_error",
                {
                    "request_id": "req-e",
                    "method": "GET",
                    "path": "/down",
                    "status": 502,
                    "reason": "ConnectError",
                },
            )
        ]
        await tunnel._proxy.stop()


# ---------------------------------------------------------------------------
# Tunnel: service.check payload helper + transition trigger
# ---------------------------------------------------------------------------


class TestServiceCheckData:
    def test_reachable_response(self):
        resp = _mock_httpx_response(status_code=200)
        data = _service_check_data("https://localhost:8006", True, 12.34, response=resp)
        assert data == {
            "reachable": True,
            "status": 200,
            "scheme": "https",
            "is_tls": True,
            "verify_ssl": True,
            "redirect_location": None,
            "elapsed_ms": 12.3,
            "error": None,
        }

    def test_redirect_strips_query(self):
        resp = _mock_httpx_response(
            status_code=302, headers={"location": "http://x/login?token=abc&next=/"}
        )
        data = _service_check_data("http://localhost:80", False, 1.0, response=resp)
        assert data["redirect_location"] == "http://x/login"
        assert data["is_tls"] is False

    def test_error_records_class_and_message(self):
        exc = httpx.ConnectError("connection refused")
        data = _service_check_data("http://localhost:9", False, 5.0, error=exc)
        assert data["reachable"] is False
        assert data["status"] is None
        assert data["error"].startswith("ConnectError:")

    def test_transition_false_to_true_spawns_probe(self):
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-p"
        spawned: list = []
        tunnel._spawn = lambda coro: (spawned.append(coro), coro.close())
        msg = ProtocolMessage(
            type=MessageType.LOG_CONFIG,
            payload=LogConfig(level="DEBUG", diagnostics=True).model_dump(),
        )
        tunnel._handle_log_config(msg)
        assert len(spawned) == 1

    def test_no_probe_when_already_enabled(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = True
        spawned: list = []
        tunnel._spawn = lambda coro: (spawned.append(coro), coro.close())
        msg = ProtocolMessage(
            type=MessageType.LOG_CONFIG,
            payload=LogConfig(level="DEBUG", diagnostics=True).model_dump(),
        )
        tunnel._handle_log_config(msg)
        assert spawned == []


# ---------------------------------------------------------------------------
# Log ring: redaction + bound + rate limit + recursion guard
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_api_key(self):
        s = _redact_secrets("using key hle_" + "0" * 32 + " now")
        assert "hle_" not in s
        assert "[REDACTED]" in s

    def test_vncticket_and_token(self):
        assert _redact_secrets("url?vncticket=SECRET&x=1") == "url?vncticket=[REDACTED]&x=1"
        assert _redact_secrets("token=abc123 tail") == "token=[REDACTED] tail"

    def test_authorization(self):
        assert _redact_secrets("Authorization: Bearer xyz") == "Authorization: [REDACTED]"

    def test_no_secret_unchanged(self):
        assert _redact_secrets("plain message") == "plain message"


class TestDiagnosticLogHandler:
    def _record(self, level=logging.WARNING, msg="m", name="hle_client.x"):
        return logging.LogRecord(name, level, __file__, 1, msg, None, None)

    def test_ring_is_bounded(self):
        handler = _DiagnosticLogHandler(_tunnel(), capacity=5)
        for i in range(20):
            handler.emit(self._record(level=logging.INFO, msg=f"line{i}"))
        assert len(handler.ring) == 5
        assert list(handler.ring)[-1] == "line19"

    def test_emits_warning_when_enabled_and_redacts(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = True
        emitted: list = []
        tunnel._emit_diagnostic = lambda event, **data: emitted.append((event, data))
        handler = _DiagnosticLogHandler(tunnel)
        handler.emit(self._record(msg="hle_" + "a" * 32))
        assert len(emitted) == 1
        event, data = emitted[0]
        assert event == "log.line"
        assert data["level"] == "WARNING"
        assert "[REDACTED]" in data["message"]

    def test_info_not_emitted(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = True
        emitted: list = []
        tunnel._emit_diagnostic = lambda event, **data: emitted.append(event)
        handler = _DiagnosticLogHandler(tunnel)
        handler.emit(self._record(level=logging.INFO))
        assert emitted == []

    def test_disabled_does_not_emit_but_rings(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = False
        emitted: list = []
        tunnel._emit_diagnostic = lambda event, **data: emitted.append(event)
        handler = _DiagnosticLogHandler(tunnel)
        handler.emit(self._record())
        assert emitted == []
        assert len(handler.ring) == 1

    def test_rate_limit_drops_and_reports(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = True
        emitted: list = []
        tunnel._emit_diagnostic = lambda event, **data: emitted.append(data)
        handler = _DiagnosticLogHandler(tunnel, rate=3)
        for _ in range(10):
            handler.emit(self._record())
        assert len(emitted) == 3
        handler._window_start -= 2.0
        handler.emit(self._record())
        assert len(emitted) == 4
        assert emitted[-1]["dropped"] == 7

    def test_recursion_guard(self):
        tunnel = _tunnel()
        tunnel._diagnostics_enabled = True
        handler = _DiagnosticLogHandler(tunnel)
        record = self._record(msg="boom")
        calls: list = []

        def _reenter(event, **data):
            calls.append(event)
            handler.emit(record)  # simulate emit path logging back in

        tunnel._emit_diagnostic = _reenter
        handler.emit(record)
        assert calls == ["log.line"]
