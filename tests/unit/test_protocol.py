"""Tests for the HLE wire protocol."""

import pytest
from pydantic import ValidationError

from hle_common.protocol import (
    PROTOCOL_VERSION,
    ErrorPayload,
    MessageType,
    ProtocolMessage,
)


class TestProtocolVersion:
    def test_protocol_version_exists(self):
        assert PROTOCOL_VERSION == "1.2"

    def test_protocol_version_is_string(self):
        assert isinstance(PROTOCOL_VERSION, str)


class TestMessageType:
    def test_message_types_are_strings(self):
        assert MessageType.AUTH_REQUEST == "auth_request"
        assert MessageType.WS_FRAME == "ws_frame"
        assert MessageType.WEBHOOK_INCOMING == "webhook_incoming"

    def test_tunnel_register_type(self):
        assert MessageType.TUNNEL_REGISTER == "tunnel_register"

    def test_all_message_types_exist(self):
        expected = {
            "auth_request",
            "auth_response",
            "tunnel_open",
            "tunnel_close",
            "tunnel_ack",
            "tunnel_register",
            "http_request",
            "http_response",
            "http_response_start",
            "http_response_chunk",
            "http_response_end",
            "http_request_cancel",
            "ws_open",
            "ws_close",
            "ws_frame",
            "webhook_incoming",
            "webhook_response",
            "speed_test_data",
            "speed_test_result",
            "ping",
            "pong",
            "error",
        }
        actual = {member.value for member in MessageType}
        assert actual == expected


class TestProtocolMessage:
    def test_minimal_message(self):
        msg = ProtocolMessage(type=MessageType.PING)
        assert msg.type == MessageType.PING
        assert msg.tunnel_id is None

    def test_full_message(self):
        msg = ProtocolMessage(
            type=MessageType.HTTP_REQUEST,
            tunnel_id="t-123",
            request_id="r-456",
            payload={"key": "value"},
        )
        assert msg.tunnel_id == "t-123"
        assert msg.payload == {"key": "value"}

    def test_payload_accepts_typed_dict(self):
        msg = ProtocolMessage(
            type=MessageType.ERROR,
            payload={"code": "not_found", "message": "Tunnel not found", "count": 42},
        )
        assert msg.payload["code"] == "not_found"
        assert msg.payload["count"] == 42

    def test_payload_defaults_to_none(self):
        msg = ProtocolMessage(type=MessageType.PONG)
        assert msg.payload is None


class TestErrorPayload:
    def test_required_fields(self):
        err = ErrorPayload(code="tunnel_not_found", message="Tunnel does not exist")
        assert err.code == "tunnel_not_found"
        assert err.message == "Tunnel does not exist"
        assert err.request_id is None

    def test_with_request_id(self):
        err = ErrorPayload(
            code="auth_failed",
            message="Invalid token",
            request_id="r-99",
        )
        assert err.request_id == "r-99"

    def test_missing_code_raises(self):
        with pytest.raises((TypeError, ValidationError)):
            ErrorPayload(message="oops")

    def test_missing_message_raises(self):
        with pytest.raises((TypeError, ValidationError)):
            ErrorPayload(code="bad")

    def test_roundtrip_serialization(self):
        err = ErrorPayload(code="rate_limit", message="Too many requests", request_id="r-5")
        data = err.model_dump()
        restored = ErrorPayload(**data)
        assert restored == err
