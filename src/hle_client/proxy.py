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

# Headers to strip when forwarding HTTP requests to the local service.
_HOP_BY_HOP_HEADERS = frozenset({"transfer-encoding", "connection", "upgrade", "accept-encoding"})

# Headers to strip from responses before sending back through the tunnel.
_STRIP_RESPONSE_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding", "connection"}
)


def _collect_response_headers(
    raw_headers: list[tuple[bytes, bytes]],
) -> dict[str, str | list[str]]:
    """Build a header dict that preserves multi-value headers (e.g. Set-Cookie).

    Single-value headers are stored as plain strings.  Headers that appear
    more than once are stored as a list of strings.
    """
    result: dict[str, str | list[str]] = {}
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1").lower()
        if name in _STRIP_RESPONSE_HEADERS:
            continue
        value = raw_value.decode("latin-1")
        existing = result.get(name)
        if existing is None:
            result[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[name] = [existing, value]
    return result


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
    forward_host: bool = False
    """Forward the browser's Host header instead of using the target hostname."""


class LocalProxy:
    """Proxies incoming HTTP requests from the tunnel to local services.

    WebSocket connections are handled directly by the Tunnel class,
    which opens its own ``websockets`` connection to the local service
    for each proxied WS stream.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._http_client: httpx.AsyncClient | None = None
        # Sticky Host header detection: None = not yet determined,
        # True = forward browser Host, False = strip Host (use target).
        self._detected_forward_host: bool | None = None

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

    @property
    def _should_forward_host(self) -> bool:
        """Whether to forward the browser's Host header."""
        if self.config.forward_host:
            return True
        if self._detected_forward_host is not None:
            return self._detected_forward_host
        return False

    def _build_forwarded_headers(
        self,
        headers: dict[str, str],
        *,
        include_host: bool | None = None,
    ) -> dict[str, str]:
        """Build headers to forward, stripping hop-by-hop and optionally Host."""
        forward_host = include_host if include_host is not None else self._should_forward_host
        skip = _HOP_BY_HOP_HEADERS | (frozenset() if forward_host else frozenset({"host"}))
        result = {k: v for k, v in headers.items() if k.lower() not in skip}

        if self.config.upstream_basic_auth is not None:
            uname, upass = self.config.upstream_basic_auth
            token = base64.b64encode(f"{uname}:{upass}".encode()).decode()
            result["authorization"] = f"Basic {token}"

        return result

    async def forward_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
        query_string: str = "",
    ) -> tuple[int, dict[str, str | list[str]], bytes]:
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

        forwarded_headers = self._build_forwarded_headers(headers)

        try:
            response = await self._http_client.request(
                method=method,
                url=url,
                headers=forwarded_headers,
                content=body,
            )

            # Sticky Host header auto-detection: on the first request,
            # if the target returns 502, retry with the browser's Host
            # header forwarded.  Whichever approach succeeds is locked in
            # for all subsequent requests (no per-request retry overhead).
            if (
                response.status_code == 502
                and self._detected_forward_host is None
                and not self.config.forward_host
                and "host" in {k.lower() for k in headers}
            ):
                browser_host = next(v for k, v in headers.items() if k.lower() == "host")
                logger.info(
                    "Got 502 from target — retrying %s %s with original Host: %s",
                    method,
                    url,
                    browser_host,
                )
                retry_headers = self._build_forwarded_headers(headers, include_host=True)
                retry_resp = await self._http_client.request(
                    method=method,
                    url=url,
                    headers=retry_headers,
                    content=body,
                )
                if retry_resp.status_code != 502:
                    self._detected_forward_host = True
                    logger.info(
                        "Forwarding browser Host header resolved 502 "
                        "(status %d) — locked in for this session.",
                        retry_resp.status_code,
                    )
                    response = retry_resp
                else:
                    self._detected_forward_host = False
                    logger.info(
                        "Retry with forwarded Host also returned 502 "
                        "— stripping Host locked in for this session."
                    )
            elif self._detected_forward_host is None and not self.config.forward_host:
                # First successful request — lock in "strip Host" mode.
                self._detected_forward_host = False
                logger.debug("Host header stripping confirmed working")

            resp_headers = _collect_response_headers(response.headers.raw)
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
    ) -> AsyncIterator[tuple[int | None, dict[str, str | list[str]] | None, bytes | None]]:
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

        forwarded_headers = self._build_forwarded_headers(headers)

        chunk_size = int(os.environ.get("HLE_HTTP_CHUNK_SIZE", "524288"))

        try:
            async with self._http_client.stream(
                method=method,
                url=url,
                headers=forwarded_headers,
                content=body,
            ) as response:
                resp_headers = _collect_response_headers(response.headers.raw)
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
