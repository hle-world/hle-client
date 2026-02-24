"""Tests for the HLE shared Pydantic models (hle_common.models)."""

from __future__ import annotations

import pytest

from hle_common.models import (
    ProxiedHttpRequest,
    ProxiedHttpResponse,
    SpeedTestData,
    SpeedTestResult,
    TunnelRegistration,
    TunnelRegistrationResponse,
    WsStreamClose,
    WsStreamFrame,
    WsStreamOpen,
)


class TestTunnelRegistration:
    def test_tunnel_registration_defaults(self):
        reg = TunnelRegistration(
            service_url="http://localhost:5000",
            api_key="hle_abc123",
        )
        assert reg.service_url == "http://localhost:5000"
        assert reg.service_label is None
        assert reg.api_key == "hle_abc123"
        assert reg.client_version is None
        assert reg.protocol_version is None
        assert reg.websocket_enabled is True
        assert reg.auth_mode == "none"

    def test_tunnel_registration_with_protocol_version(self):
        reg = TunnelRegistration(
            service_url="http://localhost:5000",
            api_key="hle_abc123",
            protocol_version="1.0",
        )
        assert reg.protocol_version == "1.0"

    def test_tunnel_registration_with_overrides(self):
        reg = TunnelRegistration(
            service_url="http://localhost:5000",
            service_label="ha",
            api_key="hle_mykey123",
            websocket_enabled=False,
            auth_mode="token",
        )
        assert reg.service_label == "ha"
        assert reg.api_key == "hle_mykey123"
        assert reg.websocket_enabled is False
        assert reg.auth_mode == "token"

    def test_tunnel_registration_requires_api_key(self):
        with pytest.raises(ValueError):
            TunnelRegistration(service_url="http://localhost:5000")

    def test_tunnel_registration_service_label_validation(self):
        with pytest.raises(ValueError, match="service_label"):
            TunnelRegistration(
                service_url="http://localhost:5000",
                api_key="hle_key",
                service_label="-bad",
            )
        with pytest.raises(ValueError, match="service_label"):
            TunnelRegistration(
                service_url="http://localhost:5000",
                api_key="hle_key",
                service_label="bad-",
            )
        with pytest.raises(ValueError, match="service_label"):
            TunnelRegistration(
                service_url="http://localhost:5000",
                api_key="hle_key",
                service_label="BAD",
            )


class TestTunnelRegistrationResponse:
    def test_tunnel_registration_response(self):
        resp = TunnelRegistrationResponse(
            tunnel_id="t-resp-1",
            subdomain="resptest.abc",
            public_url="http://resptest.abc.localhost:8000",
            websocket_enabled=True,
            user_code="abc",
            service_label="resptest",
        )
        assert resp.tunnel_id == "t-resp-1"
        assert resp.subdomain == "resptest.abc"
        assert resp.websocket_enabled is True


class TestProxiedHttpRequest:
    def test_proxied_http_request_defaults(self):
        req = ProxiedHttpRequest(
            request_id="r-proxy-1",
            method="GET",
            path="/api/health",
            headers={"Host": "example.com"},
        )
        assert req.query_string == ""
        assert req.body is None

    def test_proxied_http_request_with_body(self):
        req = ProxiedHttpRequest(
            request_id="r-proxy-2",
            method="POST",
            path="/api/submit",
            headers={"Content-Type": "application/json"},
            body="eyJrZXkiOiAidmFsdWUifQ==",
            query_string="debug=true",
        )
        assert req.body == "eyJrZXkiOiAidmFsdWUifQ=="
        assert isinstance(req.body, str)


class TestProxiedHttpResponse:
    def test_proxied_http_response(self):
        resp = ProxiedHttpResponse(
            request_id="r-resp-1",
            status_code=200,
            headers={"Content-Type": "text/html"},
            body="PGgxPkhlbGxvPC9oMT4=",
        )
        assert resp.request_id == "r-resp-1"
        assert resp.status_code == 200


class TestWsStreamOpen:
    def test_ws_stream_open(self):
        stream = WsStreamOpen(stream_id="s-open-1", path="/ws/notifications")
        assert stream.stream_id == "s-open-1"
        assert stream.headers == {}

    def test_ws_stream_open_with_headers(self):
        stream = WsStreamOpen(
            stream_id="s-open-2",
            path="/ws/chat",
            headers={"Authorization": "Bearer tok123"},
        )
        assert stream.headers == {"Authorization": "Bearer tok123"}


class TestWsStreamFrame:
    def test_ws_stream_frame_text(self):
        frame = WsStreamFrame(stream_id="s-frame-1", data="hello world")
        assert frame.is_binary is False

    def test_ws_stream_frame_binary(self):
        frame = WsStreamFrame(stream_id="s-frame-2", data="AQIDBA==", is_binary=True)
        assert frame.is_binary is True


class TestWsStreamClose:
    def test_ws_stream_close_defaults(self):
        close = WsStreamClose(stream_id="s-close-1")
        assert close.code == 1000
        assert close.reason == ""

    def test_ws_stream_close_custom(self):
        close = WsStreamClose(stream_id="s-close-2", code=1001, reason="going away")
        assert close.code == 1001


class TestSpeedTest:
    def test_speed_test_data(self):
        data = SpeedTestData(
            test_id="test-1",
            direction="download",
            chunk_index=0,
            total_chunks=10,
            data="AAAA",
        )
        assert data.test_id == "test-1"
        assert data.direction == "download"

    def test_speed_test_result(self):
        result = SpeedTestResult(
            test_id="test-1",
            direction="download",
            total_bytes=1024,
            duration_seconds=1.5,
            throughput_mbps=5.46,
        )
        assert result.throughput_mbps == 5.46


class TestRoundtripSerialization:
    def test_roundtrip_tunnel_registration(self):
        original = TunnelRegistration(
            service_url="http://localhost:5000",
            service_label="regrt",
            api_key="hle_roundtrip123",
            protocol_version="1.0",
            websocket_enabled=False,
            auth_mode="token",
        )
        data = original.model_dump()
        restored = TunnelRegistration(**data)
        assert restored == original

    def test_roundtrip_tunnel_registration_response(self):
        original = TunnelRegistrationResponse(
            tunnel_id="t-rt-3",
            subdomain="resprt.abc",
            public_url="http://resprt.abc.localhost:8000",
            websocket_enabled=True,
            user_code="abc",
            service_label="resprt",
        )
        data = original.model_dump()
        restored = TunnelRegistrationResponse(**data)
        assert restored == original

    def test_roundtrip_proxied_http_request(self):
        original = ProxiedHttpRequest(
            request_id="r-rt-1",
            method="POST",
            path="/api/data",
            headers={"Content-Type": "application/json"},
            body="eyJ0ZXN0IjogdHJ1ZX0=",
            query_string="v=2",
        )
        data = original.model_dump()
        restored = ProxiedHttpRequest(**data)
        assert restored == original

    def test_roundtrip_ws_stream_close(self):
        original = WsStreamClose(stream_id="s-rt-3", code=1001, reason="going away")
        data = original.model_dump()
        restored = WsStreamClose(**data)
        assert restored == original
