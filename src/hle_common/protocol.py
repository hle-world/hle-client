"""HLE Protocol — Wire protocol definitions shared between client and server."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

# Protocol version — bump on wire-format changes.
# Major bump (1.0 → 2.0): breaking change, server must support both during deprecation.
# Minor bump (1.0 → 1.1): new optional fields/message types, old clients unaffected.
PROTOCOL_VERSION = "1.2"


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


class ProtocolMessage(BaseModel):
    """Base message for the HLE wire protocol."""

    type: MessageType
    tunnel_id: str | None = None
    request_id: str | None = None
    payload: dict[str, Any] | None = None


class TunnelOpenRequest(BaseModel):
    """Request to open a new tunnel."""

    service_url: str
    auth_mode: str = "sso"
    domain: str | None = None
    websocket_enabled: bool = True


class TunnelOpenResponse(BaseModel):
    """Response confirming a tunnel has been opened."""

    tunnel_id: str
    subdomain: str
    public_url: str


class HttpRequest(BaseModel):
    """HTTP request forwarded through the tunnel."""

    request_id: str
    method: str
    path: str
    headers: dict[str, str]
    body: str | None = None
    query_string: str = ""


class HttpResponse(BaseModel):
    """HTTP response sent back through the tunnel."""

    request_id: str
    status_code: int
    headers: dict[str, str]
    body: str | None = None


class WsFrame(BaseModel):
    """WebSocket frame forwarded through the tunnel."""

    stream_id: str
    data: str
    is_binary: bool = False
    path: str = ""


class ErrorPayload(BaseModel):
    """Error payload included in ERROR messages."""

    code: str
    message: str
    request_id: str | None = None
