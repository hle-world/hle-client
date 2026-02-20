"""Local proxy — handles HTTP proxying to local services."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    """Configuration for local service proxying."""

    target_url: str
    websocket_enabled: bool = True
    timeout: float = 30.0
    max_retries: int = 3


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
        self._http_client = httpx.AsyncClient(
            base_url=self.config.target_url,
            timeout=self.config.timeout,
            follow_redirects=False,
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
        forwarded_headers = {
            k: v
            for k, v in headers.items()
            if k.lower()
            not in {"host", "transfer-encoding", "connection", "upgrade", "accept-encoding"}
        }

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
        except httpx.ConnectError:
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
