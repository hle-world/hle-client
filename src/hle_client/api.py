"""REST API client for managing tunnels and access rules via API key."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from hle_common.models import RelayDiscoveryResponse

logger = logging.getLogger(__name__)


@dataclass
class ApiClientConfig:
    """Configuration for the HLE API client."""

    api_key: str = ""


class ApiClient:
    """HTTP client for the HLE server REST API using Bearer auth."""

    _BASE_URL = "https://hle.world"

    def __init__(self, config: ApiClientConfig) -> None:
        self._base_url = self._BASE_URL
        self._headers = {"Authorization": f"Bearer {config.api_key}"}

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
                f"{self._base_url}/api/tunnels/{subdomain}/access",
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
                f"{self._base_url}/api/tunnels/{subdomain}/access",
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
                f"{self._base_url}/api/tunnels/{subdomain}/access/{rule_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_tunnel_pin_status(self, subdomain: str) -> dict[str, Any]:
        """Get PIN status for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def set_tunnel_pin(self, subdomain: str, pin: str) -> dict[str, Any]:
        """Set or update the PIN for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
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
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
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
                f"{self._base_url}/api/tunnels/{subdomain}/share-links",
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
                f"{self._base_url}/api/tunnels/{subdomain}/share-links",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def delete_share_link(self, subdomain: str, link_id: int) -> dict[str, Any]:
        """Revoke a share link."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{subdomain}/share-links/{link_id}",
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
                f"{self._base_url}/api/tunnels/{subdomain}/basic-auth",
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
                f"{self._base_url}/api/tunnels/{subdomain}/basic-auth",
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
                f"{self._base_url}/api/tunnels/{subdomain}/basic-auth",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
