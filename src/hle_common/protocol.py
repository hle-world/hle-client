"""HLE Protocol — Wire protocol definitions shared between client and server."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

# Protocol version — bump on wire-format changes.
# Major bump (1.0 → 2.0): breaking change, server must support both during deprecation.
# Minor bump (1.0 → 1.1): new optional fields/message types, old clients unaffected.
PROTOCOL_VERSION = "1.3"


class MessageType(StrEnum):
    """Types of messages exchanged between client and relay server."""

    # Handshake
    AUTH_REQUEST = "auth_request"
    AUTH_RESPONSE = "auth_response"

    # Tunnel management
    TUNNEL_OPEN = "tunnel_open"
    TUNNEL_CLOSE = "tunnel_close"
    TUNNEL_ACK = "tunnel_ack"
    TUNNEL_REGISTER = "tunnel_register"

    # Traffic proxying
    HTTP_REQUEST = "http_request"
    HTTP_RESPONSE = "http_response"

    # Chunked HTTP response (for large bodies like video streaming)
    HTTP_RESPONSE_START = "http_response_start"
    HTTP_RESPONSE_CHUNK = "http_response_chunk"
    HTTP_RESPONSE_END = "http_response_end"

    # Server → client: abort an in-flight chunked request
    HTTP_REQUEST_CANCEL = "http_request_cancel"

    # WebSocket proxying
    WS_OPEN = "ws_open"
    WS_CLOSE = "ws_close"
    WS_FRAME = "ws_frame"

    # Webhook
    WEBHOOK_INCOMING = "webhook_incoming"
    WEBHOOK_RESPONSE = "webhook_response"

    # Speed test
    SPEED_TEST_DATA = "speed_test_data"
    SPEED_TEST_RESULT = "speed_test_result"

    # Control
    PING = "ping"
    PONG = "pong"
    ERROR = "error"

    # Server → client informational message. Rendered by the client as a
    # human-readable line. Server controls wording so new messages do not
    # require a client release. Unknown codes still render via `message`.
    NOTICE = "notice"


class ProtocolMessage(BaseModel):
    """Base message for the HLE wire protocol."""

    type: MessageType
    tunnel_id: str | None = None
    request_id: str | None = None
    payload: dict[str, Any] | None = None


class ErrorPayload(BaseModel):
    """Error payload included in ERROR messages."""

    code: str
    message: str
    request_id: str | None = None


class NoticePayload(BaseModel):
    """Server → client informational message payload."""

    level: Literal["info", "success", "warning", "error"] = "info"
    code: str
    message: str
    details: dict[str, Any] | None = None
    url: str | None = None
