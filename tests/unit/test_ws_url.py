"""Tests for local WebSocket URL construction (trailing-slash handling)."""

from __future__ import annotations

from hle_client.tunnel import _build_local_ws_url


class TestBuildLocalWsUrl:
    def test_trailing_slash_does_not_double(self):
        # Regression: a service URL with a trailing slash produced
        # "wss://host:8006//api2/..." which Proxmox's vncwebsocket rejects
        # with HTTP 500, breaking every VM/CT console over the tunnel.
        url = _build_local_ws_url(
            "https://192.168.2.200:8006/",
            "/api2/json/nodes/milkyway/qemu/107/vncwebsocket?port=5900",
        )
        assert url == (
            "wss://192.168.2.200:8006/api2/json/nodes/milkyway/qemu/107/vncwebsocket?port=5900"
        )
        assert "//api2" not in url

    def test_no_trailing_slash_unchanged(self):
        url = _build_local_ws_url("http://localhost:7681", "/ws")
        assert url == "ws://localhost:7681/ws"

    def test_https_maps_to_wss(self):
        assert _build_local_ws_url("https://host:8006", "/x").startswith("wss://")

    def test_http_maps_to_ws(self):
        assert _build_local_ws_url("http://host:7900", "/websockify").startswith("ws://")

    def test_query_string_preserved(self):
        url = _build_local_ws_url("https://h:8006/", "/vncwebsocket?port=5901&vncticket=AB%2FCD")
        assert url == "wss://h:8006/vncwebsocket?port=5901&vncticket=AB%2FCD"
