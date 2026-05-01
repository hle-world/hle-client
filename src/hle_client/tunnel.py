"""Tunnel client — establishes and maintains tunnel connections to the HLE relay server."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

import websockets
import websockets.asyncio.client
import websockets.exceptions

from hle_client import __version__
from hle_client.notices import render_notice
from hle_client.proxy import LocalProxy, ProxyConfig
from hle_common.models import (
    CAPABILITY_CHUNKED_RESPONSE,
    HttpResponseChunk,
    HttpResponseEnd,
    HttpResponseStart,
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
from hle_common.protocol import (
    PROTOCOL_VERSION,
    MessageType,
    NoticePayload,
    ProtocolMessage,
)


class TunnelFatalError(Exception):
    """Raised for non-retryable server rejections (tunnel limit, auth failure)."""


logger = logging.getLogger(__name__)

_ClientConn = websockets.asyncio.client.ClientConnection

# Default config directory for persisting settings.
_CONFIG_DIR = Path.home() / ".config" / "hle"
_CONFIG_FILE = _CONFIG_DIR / "config.toml"


def _load_api_key() -> str | None:
    """Load api_key from the config file, if it exists."""
    if not _CONFIG_FILE.exists():
        return None
    try:
        import tomllib

        with open(_CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        return data.get("api_key")
    except Exception:
        # nosemgrep: python-logger-credential-disclosure
        logger.debug("Failed to load API key from %s", _CONFIG_FILE)
        return None


def _save_api_key(api_key: str) -> None:
    """Persist api_key to the config file with restrictive permissions."""
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

        existing_lines: list[str] = []
        found = False
        if _CONFIG_FILE.exists():
            with open(_CONFIG_FILE) as f:
                for line in f:
                    if line.startswith("api_key ") or line.startswith("api_key="):
                        existing_lines.append(f'api_key = "{api_key}"\n')
                        found = True
                    else:
                        existing_lines.append(line)

        if not found:
            existing_lines.append(f'api_key = "{api_key}"\n')

        # Write with 0o600 (owner-only read/write) to protect the API key.
        fd = os.open(_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(existing_lines)

        # nosemgrep: python-logger-credential-disclosure
        logger.info("API key saved to %s", _CONFIG_FILE)
    except Exception:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning("Failed to save API key to %s", _CONFIG_FILE, exc_info=True)


def _remove_api_key() -> bool:
    """Remove api_key from the config file. Returns True if a key was removed."""
    if not _CONFIG_FILE.exists():
        return False
    try:
        with open(_CONFIG_FILE) as f:
            lines = f.readlines()

        new_lines = [
            line
            for line in lines
            if not (line.startswith("api_key ") or line.startswith("api_key="))
        ]
        if len(new_lines) == len(lines):
            return False  # No api_key line found

        fd = os.open(_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(new_lines)

        logger.info("API key removed from %s", _CONFIG_FILE)
        return True
    except Exception:
        logger.warning("Failed to remove API key from %s", _CONFIG_FILE, exc_info=True)
        return False


@dataclass
class TunnelConfig:
    """Configuration for a tunnel connection."""

    service_url: str
    relay_host: str = "hle.world"
    relay_port: int = 443
    auth_mode: str = "sso"
    service_label: str = ""  # required — set by CLI
    api_key: str | None = None
    websocket_enabled: bool = True
    verify_ssl: bool = False
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 60.0
    upstream_basic_auth: tuple[str, str] | None = None
    """Optional (username, password) injected as Authorization: Basic toward the local service."""
    forward_host: bool = False
    """Forward the browser's Host header instead of using the target hostname."""
    managed_by: str | None = None
    """Identifier for the system managing this tunnel (e.g. 'hle-operator')."""
    webhook_path: str | None = None
    """When set, only forward requests matching this path prefix (webhook mode)."""


# Hard limits to protect against a malicious or compromised relay server.
WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024  # 4 MB — control-plane WebSocket message limit
MAX_WS_STREAMS = 100
MAX_SPEED_TEST_CHUNKS = 100  # ~6.4 MB at 64 KB/chunk
MAX_SPEED_TEST_CHUNK_SIZE = 1_048_576  # 1 MB — cap server-requested chunk size

# Headers to strip from relayed WebSocket handshakes (hop-by-hop / WS-specific).
_WS_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-accept",
        "sec-websocket-protocol",
        "transfer-encoding",
        "content-length",
        "keep-alive",
    }
)


@dataclass
class Tunnel:
    """Manages a single tunnel connection to the relay server.

    Connects to the relay via WebSocket, registers the tunnel, then
    enters a receive loop that processes incoming ``ProtocolMessage``
    messages — forwarding HTTP requests to the local service via
    :class:`LocalProxy` and proxying WebSocket streams directly.
    """

    config: TunnelConfig
    on_registered: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _post_register_done: bool = field(default=False, init=False, repr=False)
    _tunnel_id: str | None = field(default=None, init=False, repr=False)
    _public_url: str | None = field(default=None, init=False, repr=False)
    _proxy: LocalProxy = field(init=False, repr=False)
    _ws: _ClientConn | None = field(default=None, init=False, repr=False)
    _ws_streams: dict[str, _ClientConn] = field(default_factory=dict, init=False, repr=False)
    _ws_streams_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _active_chunked: dict[str, asyncio.Task[None]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._proxy = LocalProxy(
            ProxyConfig(
                target_url=self.config.service_url,
                websocket_enabled=self.config.websocket_enabled,
                verify_ssl=self.config.verify_ssl,
                upstream_basic_auth=self.config.upstream_basic_auth,
                forward_host=self.config.forward_host,
            )
        )
        self._server_caps: list[str] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish tunnel connection to the relay server with reconnection."""
        self._running = True
        delay = self.config.reconnect_delay

        while self._running:
            try:
                await self._proxy.start()
                await self._connect_once()
            except (
                OSError,
                websockets.exceptions.WebSocketException,
                ConnectionError,
            ) as exc:
                if isinstance(exc, websockets.exceptions.ConnectionClosed) and exc.rcvd is not None:
                    code = exc.rcvd.code
                    if code == 4003:
                        raise TunnelFatalError(
                            "Tunnel limit reached. Your plan does not allow more "
                            "active tunnels.\n"
                            "Stop another tunnel or upgrade at https://hle.world/dashboard"
                        ) from exc
                    if code == 4001:
                        raise TunnelFatalError(
                            "Authentication failed. Your API key is invalid or revoked.\n"
                            "Run 'hle auth login' to save a new key."
                        ) from exc
                logger.warning("Connection lost: %s", exc)
            except asyncio.CancelledError:
                logger.info("Tunnel cancelled")
                break
            finally:
                await self._cleanup()

            if not self._running:
                break

            logger.info("Reconnecting in %.1fs ...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.config.max_reconnect_delay)

    async def disconnect(self) -> None:
        """Gracefully disconnect the tunnel."""
        self._running = False
        if self._ws:
            await self._ws.close()
        await self._cleanup()
        logger.info("Tunnel disconnected")

    @property
    def is_connected(self) -> bool:
        return self._running and self._ws is not None

    @property
    def public_url(self) -> str | None:
        return self._public_url

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    # Allowed relay hostnames — discovery URLs must match one of these.
    _RELAY_HOST_ALLOWLIST = re.compile(r"^([a-z0-9-]+\.)*hle\.world$")

    async def _discover_relay_uri(self, api_key: str) -> str:
        """Resolve the relay WebSocket URI via the discovery endpoint.

        Falls back to building the URI from ``self.config`` defaults when
        the discovery endpoint is unavailable (e.g. the server hasn't
        implemented it yet).
        """
        from hle_client.api import ApiClient, ApiClientConfig

        try:
            client = ApiClient(ApiClientConfig(api_key=api_key))
            discovery = await client.discover_relay()
        except Exception:
            discovery = None

        if discovery is not None:
            # Validate the discovered URL: must be wss:// to a known host.
            parsed = urlparse(discovery.relay_url)
            if parsed.scheme != "wss":
                logger.warning(
                    "Relay discovery returned non-wss scheme (%s), ignoring",
                    parsed.scheme,
                )
            elif not parsed.hostname or not self._RELAY_HOST_ALLOWLIST.match(parsed.hostname):
                logger.warning(
                    "Relay discovery returned untrusted host (%s), ignoring",
                    parsed.hostname,
                )
            else:
                logger.info(
                    "Relay discovery: url=%s region=%s ttl=%ds",
                    discovery.relay_url,
                    discovery.relay_region,
                    discovery.ttl,
                )
                return discovery.relay_url

        # Fallback: build URL from config defaults (always wss)
        return f"wss://{self.config.relay_host}:{self.config.relay_port}/_hle/tunnel"

    async def _connect_once(self) -> None:
        """Single connection attempt: discover relay, register, then enter the receive loop."""
        # Resolve API key early — needed for both discovery and registration
        api_key = self.config.api_key or _load_api_key()
        if not api_key:
            raise ConnectionError(
                "No API key found. Run 'hle auth login', set HLE_API_KEY, or pass --api-key."
            )

        relay_uri = await self._discover_relay_uri(api_key)
        logger.info("Connecting to relay at %s", relay_uri)

        async with websockets.connect(relay_uri, max_size=WS_MAX_MESSAGE_SIZE) as ws:
            self._ws = ws

            # --- Registration handshake ---
            registration = TunnelRegistration(
                service_url=self.config.service_url,
                service_label=self.config.service_label,
                api_key=api_key,
                client_version=__version__,
                protocol_version=PROTOCOL_VERSION,
                websocket_enabled=self.config.websocket_enabled,
                auth_mode=self.config.auth_mode,
                capabilities=[CAPABILITY_CHUNKED_RESPONSE],
                managed_by=self.config.managed_by,
                webhook_path=self.config.webhook_path,
            )
            register_msg = ProtocolMessage(
                type=MessageType.TUNNEL_REGISTER,
                payload=registration.model_dump(),
            )
            await ws.send(register_msg.model_dump_json())

            # Wait for acknowledgement (30s timeout to avoid hanging on buggy relay)
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            ack_msg = ProtocolMessage.model_validate_json(ack_raw)
            if ack_msg.type != MessageType.TUNNEL_ACK:
                raise ConnectionError(f"Expected TUNNEL_ACK, received {ack_msg.type}")

            ack_data = TunnelRegistrationResponse.model_validate(ack_msg.payload)
            self._tunnel_id = ack_data.tunnel_id
            self._public_url = ack_data.public_url
            self._server_caps = getattr(ack_data, "server_capabilities", []) or []
            logger.info(
                "Tunnel registered: id=%s  url=%s",
                self._tunnel_id,
                self._public_url,
            )

            # Fire post-registration callback (once) for --add-auth etc.
            if self.on_registered and not self._post_register_done:
                self._post_register_done = True
                if ack_data.subdomain:
                    await self.on_registered(ack_data.subdomain)

            # --- Receive loop ---
            await self._receive_loop(ws)

    async def _receive_loop(self, ws: _ClientConn) -> None:
        """Process incoming messages from the relay server."""
        async for raw in ws:
            try:
                msg = ProtocolMessage.model_validate_json(raw)
            except Exception:
                logger.exception("Failed to parse incoming message")
                continue

            match msg.type:
                case MessageType.HTTP_REQUEST:
                    self._spawn(self._handle_http_request(ws, msg))
                case MessageType.WS_OPEN:
                    self._spawn(self._handle_ws_open(ws, msg))
                case MessageType.WS_FRAME:
                    self._spawn(self._handle_ws_frame(msg))
                case MessageType.WS_CLOSE:
                    self._spawn(self._handle_ws_close(msg))
                case MessageType.PING:
                    pong = ProtocolMessage(
                        type=MessageType.PONG,
                        tunnel_id=self._tunnel_id,
                    )
                    await ws.send(pong.model_dump_json())
                case MessageType.SPEED_TEST_DATA:
                    self._spawn(self._handle_speed_test_data(ws, msg))
                case MessageType.HTTP_REQUEST_CANCEL:
                    request_id = (msg.payload or {}).get("request_id")
                    if request_id:
                        task = self._active_chunked.pop(request_id, None)
                        if task and not task.done():
                            task.cancel()
                case MessageType.NOTICE:
                    self._handle_notice(msg)
                case _:
                    logger.debug("Unhandled message type: %s", msg.type)

    def _handle_notice(self, msg: ProtocolMessage) -> None:
        """Render a server-pushed informational message.

        Server controls wording so new notice codes do not require a client
        release. Unknown levels fall back to ``info``.
        """
        try:
            notice = NoticePayload.model_validate(msg.payload or {})
        except Exception:
            logger.exception("Malformed NOTICE payload: %s", msg.payload)
            return
        render_notice(notice)

    # ------------------------------------------------------------------
    # HTTP request handling
    # ------------------------------------------------------------------

    async def _handle_http_request(
        self,
        ws: _ClientConn,
        msg: ProtocolMessage,
    ) -> None:
        """Forward an HTTP request to the local service and return the response."""
        # Webhook mode: reject requests that don't match the registered path prefix
        if self.config.webhook_path:
            import posixpath

            req_path = posixpath.normpath((msg.payload or {}).get("path", ""))
            prefix = self.config.webhook_path.rstrip("/")
            # Match exact path or path with trailing segments (segment boundary)
            if not (req_path == prefix or req_path.startswith(prefix + "/")):
                req = ProxiedHttpRequest.model_validate(msg.payload)
                response = ProxiedHttpResponse(
                    request_id=req.request_id,
                    status_code=404,
                    headers={"content-type": "application/json"},
                    body=base64.b64encode(
                        b'{"error":"Not found","detail":"This webhook endpoint only accepts '
                        b'requests on the registered path."}'
                    ).decode("ascii"),
                )
                resp_msg = ProtocolMessage(
                    type=MessageType.HTTP_RESPONSE,
                    tunnel_id=self._tunnel_id,
                    request_id=req.request_id,
                    payload=response.model_dump(),
                )
                with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                    await ws.send(resp_msg.model_dump_json())
                return

        if CAPABILITY_CHUNKED_RESPONSE in self._server_caps:
            await self._handle_http_request_chunked(ws, msg)
        else:
            await self._handle_http_request_buffered(ws, msg)

    async def _handle_http_request_buffered(
        self,
        ws: _ClientConn,
        msg: ProtocolMessage,
    ) -> None:
        """Forward HTTP request using the original single-message response path."""
        req = ProxiedHttpRequest.model_validate(msg.payload)

        body: bytes | None = None
        if req.body is not None:
            body = base64.b64decode(req.body)

        status_code, resp_headers, resp_body = await self._proxy.forward_http(
            method=req.method,
            path=req.path,
            headers=req.headers,
            body=body,
            query_string=req.query_string,
        )

        encoded_body: str | None = None
        if resp_body is not None:
            encoded_body = base64.b64encode(resp_body).decode("ascii")

        response = ProxiedHttpResponse(
            request_id=req.request_id,
            status_code=status_code,
            headers=resp_headers,
            body=encoded_body,
        )
        response_msg = ProtocolMessage(
            type=MessageType.HTTP_RESPONSE,
            tunnel_id=self._tunnel_id,
            request_id=req.request_id,
            payload=response.model_dump(),
        )
        try:
            await ws.send(response_msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Connection closed while sending HTTP response for %s", req.request_id)

    async def _handle_http_request_chunked(
        self,
        ws: _ClientConn,
        msg: ProtocolMessage,
    ) -> None:
        """Forward HTTP request using chunked streaming response."""
        req = ProxiedHttpRequest.model_validate(msg.payload)

        # Register this task so the server can cancel it via HTTP_REQUEST_CANCEL.
        current = asyncio.current_task()
        if current is not None:
            self._active_chunked[req.request_id] = current

        body: bytes | None = None
        if req.body is not None:
            body = base64.b64decode(req.body)

        chunk_index = 0

        try:
            async for status_code, resp_headers, chunk in self._proxy.stream_http(
                method=req.method,
                path=req.path,
                headers=req.headers,
                body=body,
                query_string=req.query_string,
            ):
                if status_code is not None:
                    # First yield: send START
                    start = HttpResponseStart(
                        request_id=req.request_id,
                        status_code=status_code,
                        headers=resp_headers or {},
                    )
                    start_msg = ProtocolMessage(
                        type=MessageType.HTTP_RESPONSE_START,
                        tunnel_id=self._tunnel_id,
                        request_id=req.request_id,
                        payload=start.model_dump(),
                    )
                    await ws.send(start_msg.model_dump_json())
                elif chunk is not None:
                    # Body chunk
                    encoded = base64.b64encode(chunk).decode("ascii")
                    ch = HttpResponseChunk(
                        request_id=req.request_id,
                        chunk_index=chunk_index,
                        data=encoded,
                    )
                    chunk_msg = ProtocolMessage(
                        type=MessageType.HTTP_RESPONSE_CHUNK,
                        tunnel_id=self._tunnel_id,
                        request_id=req.request_id,
                        payload=ch.model_dump(),
                    )
                    await ws.send(chunk_msg.model_dump_json())
                    chunk_index += 1

            # Send END
            end = HttpResponseEnd(request_id=req.request_id)
            end_msg = ProtocolMessage(
                type=MessageType.HTTP_RESPONSE_END,
                tunnel_id=self._tunnel_id,
                request_id=req.request_id,
                payload=end.model_dump(),
            )
            await ws.send(end_msg.model_dump_json())
        except asyncio.CancelledError:
            logger.debug("Chunked stream cancelled by server for %s", req.request_id)
            with contextlib.suppress(Exception):
                end = HttpResponseEnd(
                    request_id=req.request_id,
                    error="cancelled",
                )
                end_msg = ProtocolMessage(
                    type=MessageType.HTTP_RESPONSE_END,
                    tunnel_id=self._tunnel_id,
                    request_id=req.request_id,
                    payload=end.model_dump(),
                )
                await ws.send(end_msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            logger.debug(
                "Connection closed while sending chunked response for %s",
                req.request_id,
            )
        except Exception:
            logger.exception("Error in chunked response for %s", req.request_id)
            # Try to send error END so server doesn't hang
            with contextlib.suppress(Exception):
                end = HttpResponseEnd(
                    request_id=req.request_id,
                    error="Client error during chunked response",
                )
                end_msg = ProtocolMessage(
                    type=MessageType.HTTP_RESPONSE_END,
                    tunnel_id=self._tunnel_id,
                    request_id=req.request_id,
                    payload=end.model_dump(),
                )
                await ws.send(end_msg.model_dump_json())
        finally:
            self._active_chunked.pop(req.request_id, None)

    # ------------------------------------------------------------------
    # WebSocket stream handling
    # ------------------------------------------------------------------

    async def _handle_ws_open(
        self,
        ws: _ClientConn,
        msg: ProtocolMessage,
    ) -> None:
        """Open a WebSocket connection to the local service and bridge frames."""
        open_req = WsStreamOpen.model_validate(msg.payload)
        stream_id = open_req.stream_id

        # Guard against SSRF: reject non-relative paths from the relay.
        if not open_req.path.startswith("/") or open_req.path.startswith("//"):
            logger.error("Rejecting non-relative WS path: %s", open_req.path[:100])
            close_msg = ProtocolMessage(
                type=MessageType.WS_CLOSE,
                tunnel_id=self._tunnel_id,
                payload=WsStreamClose(
                    stream_id=stream_id, code=1008, reason="invalid path"
                ).model_dump(),
            )
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await ws.send(close_msg.model_dump_json())
            return

        # Limit concurrent streams to prevent resource exhaustion.
        async with self._ws_streams_lock:
            if len(self._ws_streams) >= MAX_WS_STREAMS:
                logger.warning(
                    "WS stream limit reached (%d), rejecting stream_id=%s",
                    MAX_WS_STREAMS,
                    stream_id,
                )
                close_msg = ProtocolMessage(
                    type=MessageType.WS_CLOSE,
                    tunnel_id=self._tunnel_id,
                    payload=WsStreamClose(
                        stream_id=stream_id, code=1013, reason="stream limit reached"
                    ).model_dump(),
                )
                with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                    await ws.send(close_msg.model_dump_json())
                return

        # Build the local WS URL.
        local_base = self.config.service_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        local_ws_url = f"{local_base}{open_req.path}"

        # Strip WebSocket handshake and hop-by-hop headers — these belong to
        # the browser↔relay connection, not the client↔local service connection.
        clean_headers = {
            k: v
            for k, v in (open_req.headers or {}).items()
            if k.lower() not in _WS_HOP_BY_HOP_HEADERS
        }

        # Rewrite Origin to match the local service so the upstream server
        # doesn't reject the WebSocket handshake with a CORS 403.
        parsed = urlparse(self.config.service_url)
        local_origin = f"{parsed.scheme}://{parsed.netloc}"
        for k in list(clean_headers):
            if k.lower() == "origin":
                clean_headers[k] = local_origin

        # Inject upstream Basic Auth if configured (same as HTTP path).
        if self.config.upstream_basic_auth is not None:
            uname, upass = self.config.upstream_basic_auth
            token = base64.b64encode(f"{uname}:{upass}".encode()).decode()
            clean_headers["authorization"] = f"Basic {token}"

        # Match HTTP proxy TLS behaviour: skip verification when verify_ssl=False
        import ssl

        ws_ssl: ssl.SSLContext | None = None
        if local_ws_url.startswith("wss://") and not self.config.verify_ssl:
            ws_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ws_ssl.check_hostname = False
            ws_ssl.verify_mode = ssl.CERT_NONE

        try:
            local_ws = await websockets.connect(
                local_ws_url,
                additional_headers=clean_headers,
                ssl=ws_ssl,
            )
        except Exception:
            logger.exception("Failed to open local WS connection to %s", local_ws_url)
            close_msg = ProtocolMessage(
                type=MessageType.WS_CLOSE,
                tunnel_id=self._tunnel_id,
                payload=WsStreamClose(
                    stream_id=stream_id, code=1011, reason="local connect failed"
                ).model_dump(),
            )
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await ws.send(close_msg.model_dump_json())
            return

        async with self._ws_streams_lock:
            self._ws_streams[stream_id] = local_ws
        logger.info("WS stream opened: stream_id=%s -> %s", stream_id, local_ws_url)

        # Start a background task that reads from the local WS and sends
        # frames back through the relay tunnel.
        self._spawn(self._ws_local_reader(ws, local_ws, stream_id))

    async def _ws_local_reader(
        self,
        relay_ws: _ClientConn,
        local_ws: _ClientConn,
        stream_id: str,
    ) -> None:
        """Read frames from a local WS and forward them to the relay."""
        try:
            async for frame_data in local_ws:
                if isinstance(frame_data, bytes):
                    data_str = base64.b64encode(frame_data).decode("ascii")
                    is_binary = True
                else:
                    data_str = frame_data
                    is_binary = False

                frame_payload = WsStreamFrame(
                    stream_id=stream_id,
                    data=data_str,
                    is_binary=is_binary,
                )
                frame_msg = ProtocolMessage(
                    type=MessageType.WS_FRAME,
                    tunnel_id=self._tunnel_id,
                    payload=frame_payload.model_dump(),
                )
                await relay_ws.send(frame_msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            logger.info("Local WS connection closed: stream_id=%s", stream_id)
        except Exception:
            logger.exception("Error reading from local WS: stream_id=%s", stream_id)
        finally:
            async with self._ws_streams_lock:
                self._ws_streams.pop(stream_id, None)
            # Notify the relay that this stream is closed.
            with contextlib.suppress(Exception):
                close_msg = ProtocolMessage(
                    type=MessageType.WS_CLOSE,
                    tunnel_id=self._tunnel_id,
                    payload=WsStreamClose(stream_id=stream_id).model_dump(),
                )
                await relay_ws.send(close_msg.model_dump_json())

    async def _handle_ws_frame(self, msg: ProtocolMessage) -> None:
        """Forward a WS frame received from the relay to the local WS connection."""
        frame = WsStreamFrame.model_validate(msg.payload)
        async with self._ws_streams_lock:
            local_ws = self._ws_streams.get(frame.stream_id)
        if local_ws is None:
            logger.warning("WS_FRAME for unknown stream_id=%s", frame.stream_id)
            return

        try:
            if frame.is_binary:
                await local_ws.send(base64.b64decode(frame.data))
            else:
                await local_ws.send(frame.data)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Local WS already closed for stream_id=%s", frame.stream_id)
            async with self._ws_streams_lock:
                self._ws_streams.pop(frame.stream_id, None)

    async def _handle_ws_close(self, msg: ProtocolMessage) -> None:
        """Close a local WS connection when the relay signals stream closure."""
        close_req = WsStreamClose.model_validate(msg.payload)
        async with self._ws_streams_lock:
            local_ws = self._ws_streams.pop(close_req.stream_id, None)
        if local_ws is None:
            return

        with contextlib.suppress(Exception):
            await local_ws.close(code=close_req.code, reason=close_req.reason)
        logger.info("WS stream closed: stream_id=%s", close_req.stream_id)

    # ------------------------------------------------------------------
    # Speed test handling
    # ------------------------------------------------------------------

    async def _handle_speed_test_data(
        self,
        ws: _ClientConn,
        msg: ProtocolMessage,
    ) -> None:
        """Handle a speed test data message from the server."""
        data = SpeedTestData.model_validate(msg.payload)

        if data.total_chunks > MAX_SPEED_TEST_CHUNKS:
            logger.warning(
                "Speed test rejected: total_chunks=%d exceeds limit of %d",
                data.total_chunks,
                MAX_SPEED_TEST_CHUNKS,
            )
            return

        if data.direction == "download":
            # Receiving download test chunks — track timing
            if not hasattr(self, "_speed_test_state"):
                self._speed_test_state: dict[str, dict[str, Any]] = {}

            if data.test_id not in self._speed_test_state:
                self._speed_test_state[data.test_id] = {
                    "start": time.monotonic(),
                    "bytes": 0,
                    "chunks": 0,
                }

            state = self._speed_test_state[data.test_id]
            # Count decoded bytes, not base64 string length (base64 inflates ~33%)
            state["bytes"] += len(data.data) * 3 // 4
            state["chunks"] += 1

            if state["chunks"] >= data.total_chunks:
                elapsed = time.monotonic() - state["start"]
                total_bytes = state["bytes"]
                throughput_mbps = (total_bytes * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0
                result = SpeedTestResult(
                    test_id=data.test_id,
                    direction="download",
                    total_bytes=total_bytes,
                    duration_seconds=round(elapsed, 3),
                    throughput_mbps=round(throughput_mbps, 2),
                )
                result_msg = ProtocolMessage(
                    type=MessageType.SPEED_TEST_RESULT,
                    tunnel_id=self._tunnel_id,
                    payload=result.model_dump(),
                )
                try:
                    await ws.send(result_msg.model_dump_json())
                except websockets.exceptions.ConnectionClosed:
                    logger.debug("Connection closed while sending speed test result")
                del self._speed_test_state[data.test_id]

        elif data.direction == "upload" and data.chunk_index == -1:
            # Upload start signal — generate and send chunks
            upload_chunk_size = min(data.chunk_size_bytes or 65536, MAX_SPEED_TEST_CHUNK_SIZE)
            chunk_data = base64.b64encode(os.urandom(upload_chunk_size)).decode("ascii")
            for i in range(data.total_chunks):
                chunk = SpeedTestData(
                    test_id=data.test_id,
                    direction="upload",
                    chunk_index=i,
                    total_chunks=data.total_chunks,
                    data=chunk_data,
                )
                chunk_msg = ProtocolMessage(
                    type=MessageType.SPEED_TEST_DATA,
                    tunnel_id=self._tunnel_id,
                    payload=chunk.model_dump(),
                )
                try:
                    await ws.send(chunk_msg.model_dump_json())
                except websockets.exceptions.ConnectionClosed:
                    logger.debug("Connection closed during speed test upload")
                    return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule a coroutine as a fire-and-forget task, tracked for cleanup."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cleanup(self) -> None:
        """Close all local WS streams, cancel background tasks, and stop the proxy."""
        async with self._ws_streams_lock:
            for _stream_id, local_ws in list(self._ws_streams.items()):
                with contextlib.suppress(Exception):
                    await local_ws.close()
            self._ws_streams.clear()

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        self._ws = None
        await self._proxy.stop()
