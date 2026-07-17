"""Tests for upstream WS connect diagnostics reported to the relay."""

from __future__ import annotations

from hle_client.tunnel import _sanitize_ws_url, _upstream_connect_diagnostics


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeInvalidStatusError(Exception):
    """Mimics websockets.exceptions.InvalidStatus (carries .response)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.response = _FakeResponse(status_code)


class TestSanitizeWsUrl:
    def test_strips_query_with_secrets(self):
        url = "wss://192.168.2.200:8006//api2/json/.../vncwebsocket?port=5900&vncticket=SECRET"
        out = _sanitize_ws_url(url)
        assert out == "wss://192.168.2.200:8006//api2/json/.../vncwebsocket"
        assert "vncticket" not in out
        assert "SECRET" not in out

    def test_no_query_unchanged(self):
        assert _sanitize_ws_url("ws://localhost:7681/ws") == "ws://localhost:7681/ws"


class TestUpstreamConnectDiagnostics:
    def test_includes_status_and_sanitized_url(self):
        # The exact Proxmox failure: doubled slash + HTTP 500.
        url = "wss://192.168.2.200:8006//api2/json/.../vncwebsocket?vncticket=SECRET"
        diag = _upstream_connect_diagnostics(url, _FakeInvalidStatusError(500))
        assert diag["phase"] == "upstream_connect"
        assert diag["upstream_status"] == 500
        assert diag["exc_type"] == "_FakeInvalidStatusError"
        assert diag["upstream_url"].endswith("//api2/json/.../vncwebsocket")
        assert "vncticket" not in diag["upstream_url"]

    def test_no_status_when_not_http_error(self):
        diag = _upstream_connect_diagnostics("wss://h/x", OSError("connection refused"))
        assert "upstream_status" not in diag
        assert diag["exc_type"] == "OSError"
