"""REST API client for managing tunnels and access rules via API key."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from hle_common.models import RelayDiscoveryResponse

logger = logging.getLogger(__name__)

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _safe_subdomain(subdomain: str) -> str:
    """Validate and URL-encode a subdomain to prevent path traversal."""
    if not _SUBDOMAIN_RE.match(subdomain):
        raise ValueError(f"Invalid subdomain format: {subdomain!r}")
    return quote(subdomain, safe="")


@dataclass
class ApiClientConfig:
    """Configuration for the HLE API client."""

    api_key: str = ""


class ApiClient:
    """HTTP client for the HLE server REST API using Bearer auth.

    Can be used as an async context manager to reuse the underlying connection
    pool across multiple requests, or instantiated directly (each call creates
    a short-lived client).
    """

    _BASE_URL = "https://hle.world"

    def __init__(self, config: ApiClientConfig) -> None:
        self._base_url = self._BASE_URL
        self._headers = {"Authorization": f"Bearer {config.api_key}"}
        self._shared_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ApiClient:
        self._shared_client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._shared_client:
            await self._shared_client.aclose()
            self._shared_client = None

    def _client_ctx(self) -> httpx.AsyncClient:
        """Return the shared client or create a one-shot client."""
        if self._shared_client is not None:
            return self._shared_client
        return httpx.AsyncClient(timeout=10.0)

    async def discover_relay(self) -> RelayDiscoveryResponse | None:
        """Call the discovery endpoint to find the optimal relay server.

        Returns ``None`` when the endpoint is unavailable (404, timeout,
        network error), allowing the caller to fall back to the default relay.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/connect",
                    headers=self._headers,
                )
                resp.raise_for_status()
                return RelayDiscoveryResponse.model_validate(resp.json())
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException):
            logger.debug("Relay discovery unavailable, will use default relay", exc_info=True)
            return None
        except Exception:
            logger.debug("Relay discovery failed unexpectedly", exc_info=True)
            return None

    async def list_tunnels(self) -> list[dict[str, Any]]:
        """List active tunnels for the authenticated user."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def list_access_rules(self, subdomain: str) -> list[dict[str, Any]]:
        """List access rules for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/access",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def add_access_rule(
        self, subdomain: str, email: str, provider: str = "any"
    ) -> dict[str, Any]:
        """Add an email to a subdomain's access allow-list."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/access",
                headers=self._headers,
                json={"email": email, "provider": provider},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def delete_access_rule(self, subdomain: str, rule_id: int) -> dict[str, Any]:
        """Remove an access rule by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/access/{rule_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_tunnel_pin_status(self, subdomain: str) -> dict[str, Any]:
        """Get PIN status for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def set_tunnel_pin(self, subdomain: str, pin: str) -> dict[str, Any]:
        """Set or update the PIN for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/pin",
                headers=self._headers,
                json={"pin": pin},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def remove_tunnel_pin(self, subdomain: str) -> dict[str, Any]:
        """Remove the PIN for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def create_share_link(
        self,
        subdomain: str,
        duration: str = "24h",
        label: str = "",
        max_uses: int | None = None,
    ) -> dict[str, Any]:
        """Create a temporary share link for a tunnel."""
        body: dict[str, Any] = {"duration": duration, "label": label}
        if max_uses is not None:
            body["max_uses"] = max_uses
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/share-links",
                headers=self._headers,
                json=body,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def list_share_links(self, subdomain: str) -> list[dict[str, Any]]:
        """List share links for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/share-links",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def delete_share_link(self, subdomain: str, link_id: int) -> dict[str, Any]:
        """Revoke a share link."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/share-links/{link_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    # -- Basic Auth ----------------------------------------------------------

    async def get_tunnel_basic_auth_status(self, subdomain: str) -> dict[str, Any]:
        """Get Basic Auth status for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/basic-auth",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def set_tunnel_basic_auth(
        self, subdomain: str, username: str, password: str
    ) -> dict[str, Any]:
        """Set or replace Basic Auth credentials for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/basic-auth",
                headers=self._headers,
                json={"username": username, "password": password},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def remove_tunnel_basic_auth(self, subdomain: str) -> dict[str, Any]:
        """Remove Basic Auth for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/basic-auth",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
