"""Local proxy — handles HTTP proxying to local services."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    """Configuration for local service proxying."""

    target_url: str
    websocket_enabled: bool = True
    timeout: float = 30.0
    max_retries: int = 3
    verify_ssl: bool = False
    upstream_basic_auth: tuple[str, str] | None = field(default=None)
    """Optional (username, password) to inject as Authorization: Basic toward the local service."""


class LocalProxy:
    """Proxies incoming HTTP requests from the tunnel to local services.

    WebSocket connections are handled directly by the Tunnel class,
    which opens its own ``websockets`` connection to the local service
    for each proxied WS stream.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._http_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Initialize the proxy and HTTP client."""
        if not self.config.verify_ssl:
            logger.debug("SSL verification disabled for %s", self.config.target_url)
        self._http_client = httpx.AsyncClient(
            base_url=self.config.target_url,
            timeout=self.config.timeout,
            follow_redirects=False,
            verify=self.config.verify_ssl,
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=30,
            ),
        )
        logger.info("Local proxy started for %s", self.config.target_url)

    async def stop(self) -> None:
        """Shutdown the proxy and release resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Local proxy stopped")

    async def forward_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
        query_string: str = "",
    ) -> tuple[int, dict[str, str], bytes]:
        """Forward an HTTP request to the local service.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, etc.)
        path:
            Request path (e.g. ``/api/states``).
        headers:
            Request headers to forward.
        body:
            Raw request body bytes, or *None*.
        query_string:
            URL query string (without the leading ``?``).

        Returns
        -------
        tuple
            ``(status_code, response_headers, response_body_bytes)``
        """
        if not self._http_client:
            raise RuntimeError("Proxy not started — call start() first")

        # Guard against SSRF: the relay controls `path`, so reject anything
        # that isn't a simple relative path.  An absolute URL (e.g.
        # "http://169.254.169.254/...") would cause httpx to ignore base_url.
        # Protocol-relative URLs ("//evil.com/...") are also dangerous.
        if not path.startswith("/") or path.startswith("//"):
            logger.error("Rejecting non-relative path: %s", path[:100])
            return (
                400,
                {"content-type": "text/plain"},
                b"Bad Request: path must be relative",
            )

        url = path
        if query_string:
            url = f"{path}?{query_string}"

        # Strip hop-by-hop headers and accept-encoding.  We let httpx handle
        # content negotiation and auto-decompression itself; forwarding the
        # browser's accept-encoding would bypass httpx's decompression logic,
        # leaving gzip-compressed bodies in response.content.
        #
        # IMPORTANT: we preserve the original "host" header from the browser
        # (e.g. "ha-ian.hle.world").  Services like Home Assistant validate the
        # Host header (HA 2023.6+) and reject requests whose Host doesn't match
        # their configured external_url.  The TCP connection still goes to the
        # configured target_url, but the HTTP Host header reflects the public
        # hostname — exactly what a standard reverse proxy does.
        forwarded_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in {"transfer-encoding", "connection", "upgrade", "accept-encoding"}
        }

        # Inject Basic Auth toward the upstream local service if configured.
        # This overrides any Authorization header that came from the browser,
        # which is intentional: the tunnel may protect the public URL with its
        # own auth while the local service requires separate credentials.
        if self.config.upstream_basic_auth is not None:
            uname, upass = self.config.upstream_basic_auth
            token = base64.b64encode(f"{uname}:{upass}".encode()).decode()
            forwarded_headers["authorization"] = f"Basic {token}"

        try:
            response = await self._http_client.request(
                method=method,
                url=url,
                headers=forwarded_headers,
                content=body,
            )
            # httpx auto-decompresses gzip/deflate/br, so the body in
            # response.content is already decompressed.  Strip content-encoding
            # (and other hop-by-hop) so downstream doesn't try to decompress again.
            resp_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                not in {"content-encoding", "content-length", "transfer-encoding", "connection"}
            }
            return response.status_code, resp_headers, response.content
        except httpx.ConnectError as exc:
            exc_str = str(exc).lower()
            if "ssl" in exc_str or "certificate" in exc_str or "tls" in exc_str:
                logger.error(
                    "SSL certificate error connecting to %s %s — "
                    "if the service uses a self-signed cert, use --no-verify-ssl",
                    method,
                    url,
                )
                return (
                    502,
                    {"content-type": "text/plain"},
                    b"Bad Gateway: SSL certificate verification failed "
                    b"(use --no-verify-ssl for self-signed certificates)",
                )
            logger.error("Connection refused forwarding %s %s to local service", method, url)
            return (
                502,
                {"content-type": "text/plain"},
                b"Bad Gateway: local service connection refused",
            )
        except httpx.TimeoutException:
            logger.error("Timeout forwarding %s %s to local service", method, url)
            return (
                504,
                {"content-type": "text/plain"},
                b"Gateway Timeout: local service did not respond",
            )
        except httpx.HTTPError as exc:
            logger.error("HTTP error forwarding %s %s: %s", method, url, exc)
            return 502, {"content-type": "text/plain"}, b"Bad Gateway: unexpected error"

    async def stream_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
        query_string: str = "",
    ) -> AsyncIterator[tuple[int | None, dict[str, str] | None, bytes | None]]:
        """Stream an HTTP response from the local service in chunks.

        Yields
        ------
        First yield:
            ``(status_code, response_headers, None)`` — metadata only.
        Subsequent yields:
            ``(None, None, chunk_bytes)`` — body segments.
        """
        if not self._http_client:
            raise RuntimeError("Proxy not started — call start() first")

        if not path.startswith("/") or path.startswith("//"):
            logger.error("Rejecting non-relative path: %s", path[:100])
            yield (400, {"content-type": "text/plain"}, None)
            yield (None, None, b"Bad Request: path must be relative")
            return

        url = path
        if query_string:
            url = f"{path}?{query_string}"

        forwarded_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in {"transfer-encoding", "connection", "upgrade", "accept-encoding"}
        }

        # Inject Basic Auth for streaming path too.
        if self.config.upstream_basic_auth is not None:
            uname, upass = self.config.upstream_basic_auth
            token = base64.b64encode(f"{uname}:{upass}".encode()).decode()
            forwarded_headers["authorization"] = f"Basic {token}"

        chunk_size = int(os.environ.get("HLE_HTTP_CHUNK_SIZE", "524288"))

        try:
            async with self._http_client.stream(
                method=method,
                url=url,
                headers=forwarded_headers,
                content=body,
            ) as response:
                resp_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower()
                    not in {
                        "content-encoding",
                        "content-length",
                        "transfer-encoding",
                        "connection",
                    }
                }
                yield (response.status_code, resp_headers, None)

                async for chunk in response.aiter_bytes(chunk_size):
                    yield (None, None, chunk)
        except httpx.ConnectError as exc:
            exc_str = str(exc).lower()
            if "ssl" in exc_str or "certificate" in exc_str or "tls" in exc_str:
                logger.error(
                    "SSL certificate error connecting to %s %s — "
                    "if the service uses a self-signed cert, use --no-verify-ssl",
                    method,
                    url,
                )
                yield (502, {"content-type": "text/plain"}, None)
                yield (
                    None,
                    None,
                    b"Bad Gateway: SSL certificate verification failed "
                    b"(use --no-verify-ssl for self-signed certificates)",
                )
                return
            logger.error("Connection refused forwarding %s %s to local service", method, url)
            yield (502, {"content-type": "text/plain"}, None)
            yield (None, None, b"Bad Gateway: local service connection refused")
        except httpx.TimeoutException:
            logger.error("Timeout forwarding %s %s to local service", method, url)
            yield (504, {"content-type": "text/plain"}, None)
            yield (None, None, b"Gateway Timeout: local service did not respond")
        except httpx.HTTPError as exc:
            logger.error("HTTP error forwarding %s %s: %s", method, url, exc)
            yield (502, {"content-type": "text/plain"}, None)
            yield (None, None, b"Bad Gateway: unexpected error")
