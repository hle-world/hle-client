"""Shared Pydantic models used by both the HLE client and server.

These models define the data structures carried inside ``ProtocolMessage.payload``
for tunnel registration, HTTP proxying, and WebSocket stream multiplexing.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Tunnel registration (client -> server -> client)
# ---------------------------------------------------------------------------

# Capability tokens exchanged during tunnel handshake
CAPABILITY_CHUNKED_RESPONSE = "chunked_response"

# Validation patterns
_SERVICE_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class TunnelRegistration(BaseModel):
    """Payload the client sends when requesting a new tunnel."""

    service_url: str
    service_label: str | None = None  # user-chosen name, e.g. "ha", "jellyfin"
    api_key: str  # required — hle_<32 hex chars>
    client_version: str | None = None
    protocol_version: str | None = None  # sent by clients >= 0.5.0
    websocket_enabled: bool = True
    auth_mode: str = "none"  # SSO not in POC scope
    capabilities: list[str] = []  # e.g. ["chunked_response"]
    zone: str | None = None  # custom zone domain for enterprise routing
    managed_by: str | None = None  # e.g. "hle-operator" for K8s operator tunnels
    webhook_path: str | None = None  # e.g. "/webhook/github" — restricts to this path prefix
    response_timeout: int | None = None  # seconds; server caps at its own max

    @field_validator("response_timeout")
    @classmethod
    def validate_response_timeout(cls, v: int | None) -> int | None:
        if v is not None:
            if v < 1:
                raise ValueError("response_timeout must be at least 1 second")
            if v > 1200:
                raise ValueError("response_timeout must not exceed 1200 seconds (20 minutes)")
        return v

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, v: str | None) -> str | None:
        if v is not None:
            if not v or v == "/":
                raise ValueError("webhook_path must be a non-root absolute path")
            if not v.startswith("/"):
                raise ValueError("webhook_path must start with /")
            # Normalize and reject traversal
            import posixpath

            normalized = posixpath.normpath(v)
            if normalized != v.rstrip("/"):
                raise ValueError("webhook_path must not contain '..' or redundant separators")
            if len(v) > 255:
                raise ValueError("webhook_path too long (max 255)")
        return v

    @field_validator("service_label")
    @classmethod
    def validate_service_label(cls, v: str | None) -> str | None:
        if v is not None:
            # Auto-sanitize: lowercase, replace common separators with
            # hyphens, strip invalid characters, collapse runs.
            v = v.lower()
            v = re.sub(r"[_ .]+", "-", v)
            v = re.sub(r"[^a-z0-9-]", "", v)
            v = re.sub(r"-{2,}", "-", v)
            v = v.strip("-")
            if not v:
                return None
            if len(v) > 63:
                v = v[:63].rstrip("-")
            if not _SERVICE_LABEL_RE.match(v):
                return None
        return v


class TunnelRegistrationResponse(BaseModel):
    """Server response after a tunnel has been successfully registered."""

    tunnel_id: str
    subdomain: str
    public_url: str
    websocket_enabled: bool
    user_code: str
    service_label: str
    server_capabilities: list[str] = []  # e.g. ["chunked_response"]
    zone: str | None = None  # custom zone domain if tunnel uses one


class RelayDiscoveryResponse(BaseModel):
    """Server response from the relay discovery endpoint (GET /api/v1/connect).

    Tells the client which relay server to connect to.  Only ``relay_url`` is
    required; every other field has a sensible default so the server can start
    simple and add routing metadata over time.
    """

    relay_url: str  # e.g. "wss://us-east.hle.world:443/_hle/tunnel"
    relay_region: str = ""  # informational, e.g. "us-east-1"
    ttl: int = 300  # seconds the assignment is considered valid
    fallback_urls: list[str] = []  # backup relay URLs for future failover
    metadata: dict[str, str] = {}  # reserved for future use


# ---------------------------------------------------------------------------
# HTTP proxying (server <-> client, carried inside ProtocolMessage.payload)
# ---------------------------------------------------------------------------


class ProxiedHttpRequest(BaseModel):
    """HTTP request being forwarded through the tunnel (used internally)."""

    request_id: str
    method: str
    path: str
    headers: dict[str, str]
    body: str | None = None  # base64 encoded
    query_string: str = ""


class ProxiedHttpResponse(BaseModel):
    """HTTP response coming back through the tunnel."""

    request_id: str
    status_code: int
    headers: dict[str, str]
    body: str | None = None  # base64 encoded


class HttpResponseStart(BaseModel):
    """First frame of a chunked HTTP response — headers and status, no body."""

    request_id: str
    status_code: int
    headers: dict[str, str]


class HttpResponseChunk(BaseModel):
    """One body segment of a chunked HTTP response."""

    request_id: str
    chunk_index: int  # 0-based, for ordering / debug
    data: str  # base64-encoded bytes


class HttpResponseEnd(BaseModel):
    """Terminal frame signalling the chunked response is complete."""

    request_id: str
    error: str | None = None


# ---------------------------------------------------------------------------
# WebSocket stream proxying
# ---------------------------------------------------------------------------


class WsStreamOpen(BaseModel):
    """Open a new WebSocket stream through the tunnel."""

    stream_id: str
    path: str
    headers: dict[str, str] = {}


class WsStreamFrame(BaseModel):
    """A single WebSocket frame in a proxied stream."""

    stream_id: str
    data: str  # base64 for binary frames, plain text for text frames
    is_binary: bool = False


class WsStreamClose(BaseModel):
    """Close a proxied WebSocket stream."""

    stream_id: str
    code: int = 1000
    reason: str = ""


# ---------------------------------------------------------------------------
# Speed test
# ---------------------------------------------------------------------------


class SpeedTestData(BaseModel):
    """Payload for speed test data chunks."""

    test_id: str
    direction: str  # "download" or "upload"
    chunk_index: int
    total_chunks: int
    data: str  # base64-encoded random payload
    chunk_size_bytes: int | None = None  # hint for upload start signal


class SpeedTestResult(BaseModel):
    """Result of a speed test measurement."""

    test_id: str
    direction: str
    total_bytes: int
    duration_seconds: float
    throughput_mbps: float
