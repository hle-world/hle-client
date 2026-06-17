"""Shared Pydantic models used by both the HLE client and server.

These models define the data structures carried inside ``ProtocolMessage.payload``
for tunnel registration, HTTP proxying, and WebSocket stream multiplexing.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Tunnel registration (client -> server -> client)
# ---------------------------------------------------------------------------

# Capability tokens exchanged during tunnel handshake
CAPABILITY_CHUNKED_RESPONSE = "chunked_response"

# Validation patterns
_SERVICE_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Guardrails for the generic `options` passthrough bag.
_MAX_OPTIONS = 32
_MAX_OPTION_KEY_LEN = 64
_MAX_OPTION_VALUE_LEN = 1024
_OPTION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class TunnelRegistration(BaseModel):
    """Payload the client sends when requesting a new tunnel."""

    service_url: str
    # User-chosen name, e.g. "ha", "jellyfin". Optional only when `apex` is set
    # (an apex tunnel serves at the bare zone root and has no label).
    service_label: str | None = None
    api_key: str  # required — hle_<32 hex chars>
    client_version: str | None = None
    protocol_version: str | None = None  # sent by clients >= 0.5.0
    websocket_enabled: bool = True
    auth_mode: str = "none"  # SSO not in POC scope
    capabilities: list[str] = []  # e.g. ["chunked_response"]
    zone: str | None = None  # custom zone domain for enterprise routing
    managed_by: str | None = None  # e.g. "hle-operator" for K8s operator tunnels
    webhook_path: str | None = None  # e.g. "/webhook/github" — restricts to this path prefix
    apex: bool = False  # serve at the bare zone root (e.g. t00t.us); requires `zone`
    # Generic, server-interpreted feature parameters. The client transports
    # these verbatim (e.g. from `--option key=value`) without understanding
    # them; the server defines the vocabulary and validates. This lets the
    # server gain features without requiring a client release. Client→server
    # intent only — the client must never act on server-returned config.
    options: dict[str, str] = {}

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
        # Empty/None is allowed here; the apex-vs-label rule is enforced in the
        # model validator below so the wire contract states the rule explicitly.
        if v is None:
            return None
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
            raise ValueError(f"service_label '{v}' does not match required format")
        return v

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: dict[str, str]) -> dict[str, str]:
        # Transport-level guardrails only — the server owns the vocabulary and
        # rejects keys it doesn't recognize. This keeps a malicious or buggy
        # client from flooding the bag, without the contract needing to know
        # which keys exist.
        if len(v) > _MAX_OPTIONS:
            raise ValueError(f"too many options (max {_MAX_OPTIONS})")
        for key, val in v.items():
            if not isinstance(key, str) or not isinstance(val, str):
                raise ValueError("option keys and values must be strings")
            if not _OPTION_KEY_RE.match(key) or len(key) > _MAX_OPTION_KEY_LEN:
                raise ValueError(f"invalid option key: {key!r}")
            if len(val) > _MAX_OPTION_VALUE_LEN:
                raise ValueError(f"option {key!r} value too long (max {_MAX_OPTION_VALUE_LEN})")
        return v

    @model_validator(mode="after")
    def validate_label_or_apex(self) -> TunnelRegistration:
        # A tunnel is addressed either by a label (a subdomain) or by the apex.
        # Exactly the absence of a label requires apex to be set.
        if not self.service_label and not self.apex:
            raise ValueError("service_label is required unless apex is set")
        return self


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
    headers: dict[str, str | list[str]]
    body: str | None = None  # base64 encoded


class HttpResponseStart(BaseModel):
    """First frame of a chunked HTTP response — headers and status, no body."""

    request_id: str
    status_code: int
    headers: dict[str, str | list[str]]


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


class WsStreamAccept(BaseModel):
    """Client → server: upstream WS handshake completed, report selected subprotocol.

    Sent immediately after the client successfully opens the WebSocket to the
    local service. The relay uses ``subprotocol`` when calling ``ws.accept()``
    on the browser-facing socket so the 101 response echoes the requested
    Sec-WebSocket-Protocol value. ``None`` means no subprotocol was negotiated.
    """

    stream_id: str
    subprotocol: str | None = None


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
    # Optional diagnostics added in PROTOCOL_VERSION 1.4. Always-on (does
    # not require LOG_CONFIG) so the relay sees rich close info on every
    # stream, e.g. {"exc_type": "ConnectionClosedError", "frames_in": 42,
    # "frames_out": 17, "ms_open": 312}. Old servers ignore the field.
    diagnostics: dict[str, Any] | None = None


class LogConfig(BaseModel):
    """Server → client: adjust per-tunnel log verbosity and diagnostics.

    Sent at any time by the relay (typically toggled by an admin panel).
    The client honors the requested log level on its hle_client logger
    and starts/stops emitting DIAGNOSTIC events based on ``diagnostics``.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    diagnostics: bool = False


class DiagnosticEvent(BaseModel):
    """Client → server: structured diagnostic event for live debugging.

    The client only emits these while the server has enabled them via
    ``LogConfig(diagnostics=True)``, so older servers that do not yet
    handle DIAGNOSTIC will not see unexpected message types.

    ``event`` is a dotted name (e.g. ``"ws.close"``, ``"ws.connect_error"``).
    ``data`` is an arbitrary JSON-compatible payload — schema is owned by
    the server side, kept open here so new event kinds can be added
    without a protocol bump.
    """

    event: str
    data: dict[str, Any] = {}
    ts: float | None = None


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
