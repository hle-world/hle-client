"""REST API client for managing tunnels and access rules via API key."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

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

    async def list_tunnels(self) -> list[dict]:
        """List active tunnels for the authenticated user."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def list_access_rules(self, subdomain: str) -> list[dict]:
        """List access rules for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{subdomain}/access",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def add_access_rule(self, subdomain: str, email: str, provider: str = "any") -> dict:
        """Add an email to a subdomain's access allow-list."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/tunnels/{subdomain}/access",
                headers=self._headers,
                json={"email": email, "provider": provider},
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_access_rule(self, subdomain: str, rule_id: int) -> dict:
        """Remove an access rule by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{subdomain}/access/{rule_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_tunnel_pin_status(self, subdomain: str) -> dict:
        """Get PIN status for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def set_tunnel_pin(self, subdomain: str, pin: str) -> dict:
        """Set or update the PIN for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
                headers=self._headers,
                json={"pin": pin},
            )
            resp.raise_for_status()
            return resp.json()

    async def remove_tunnel_pin(self, subdomain: str) -> dict:
        """Remove the PIN for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{subdomain}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def create_share_link(
        self,
        subdomain: str,
        duration: str = "24h",
        label: str = "",
        max_uses: int | None = None,
    ) -> dict:
        """Create a temporary share link for a tunnel."""
        body: dict = {"duration": duration, "label": label}
        if max_uses is not None:
            body["max_uses"] = max_uses
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/tunnels/{subdomain}/share-links",
                headers=self._headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def list_share_links(self, subdomain: str) -> list[dict]:
        """List share links for a subdomain."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{subdomain}/share-links",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_share_link(self, subdomain: str, link_id: int) -> dict:
        """Revoke a share link."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{subdomain}/share-links/{link_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()
