"""REST API client for managing tunnels and access rules via API key."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from hle_common.models import RelayDiscoveryResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

    The first request opens one ``httpx.AsyncClient`` and every later request
    reuses it, so a command that talks to the relay several times and the
    dashboard's 10s poll pay a single handshake rather than one per call.
    ``context.api(ctx)`` and the TUI store each hold one ``ApiClient`` per
    invocation, which is what makes that sharing safe.

    The session is bound to the event loop that opened it. A client reused
    from a different loop (``asyncio.run`` called twice) opens a fresh session
    rather than touching connections owned by a dead loop.

    ``async with ApiClient(...)`` closes the session on exit; ``aclose()`` does
    the same for a client used directly. A closed client reopens on next use.
    """

    _BASE_URL = "https://hle.world"

    def __init__(self, config: ApiClientConfig) -> None:
        self._base_url = self._BASE_URL
        self._headers = {"Authorization": f"Bearer {config.api_key}"}
        self._shared_client: httpx.AsyncClient | None = None
        self._shared_loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the shared session, if one is open on this loop."""
        client, self._shared_client = self._shared_client, None
        loop, self._shared_loop = self._shared_loop, None
        if client is not None and loop is asyncio.get_running_loop():
            await client.aclose()

    @contextlib.asynccontextmanager
    async def _client_ctx(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the shared session, opening it on first use on this loop."""
        loop = asyncio.get_running_loop()
        if self._shared_client is None or self._shared_loop is not loop:
            self._shared_client = httpx.AsyncClient(timeout=10.0)
            self._shared_loop = loop
        yield self._shared_client

    async def discover_relay(self) -> RelayDiscoveryResponse | None:
        """Call the discovery endpoint to find the optimal relay server.

        Returns ``None`` when the endpoint is unavailable (404, timeout,
        network error), allowing the caller to fall back to the default relay.

        Failures are logged at ``warning`` rather than ``debug``, because the
        fallback is silent and correct-looking: the tunnel connects, everything
        appears fine, and discovery has simply stopped working. Agent-managed
        tunnels got a 401 here on every single reconnect for exactly this reason,
        and it went unnoticed until a server-side error report surfaced it.

        A 404 stays at debug — that means the relay predates the endpoint, which
        is expected rather than wrong.
        """
        try:
            async with self._client_ctx() as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/connect",
                    headers=self._headers,
                    timeout=5.0,
                )
                resp.raise_for_status()
                return RelayDiscoveryResponse.model_validate(resp.json())
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                logger.debug("Relay discovery not implemented by this relay (404)")
            elif status in (401, 403):
                logger.warning(
                    "Relay discovery rejected our credential (HTTP %d) — using the default "
                    "relay. The tunnel still works, but this relay cannot route us.",
                    status,
                )
            else:
                logger.warning("Relay discovery failed (HTTP %d), using the default relay", status)
            return None
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "Relay discovery unreachable (%s), using the default relay",
                type(exc).__name__,
            )
            return None
        except Exception:
            logger.warning(
                "Relay discovery failed unexpectedly, using the default relay", exc_info=True
            )
            return None

    async def list_tunnels(self) -> list[dict[str, Any]]:
        """List active tunnels for the authenticated user."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def delete_tunnel_record(self, tunnel_id: str) -> dict[str, Any]:
        """Delete a tunnel's stored record.

        The relay refuses while the tunnel is connected — the record is what
        holds its access rules and settings, so removing it under a live
        connection would silently unprotect a published host.
        """
        async with self._client_ctx() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{tunnel_id}/record",
                headers=self._headers,
            )
            resp.raise_for_status()
            return {"message": "ok"}

    async def list_agents(self) -> list[dict[str, Any]]:
        """List the authenticated user's agents."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/agents",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def list_access_rules(self, subdomain: str) -> list[dict[str, Any]]:
        """List access rules for a subdomain."""
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/access/{rule_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_tunnel_pin_status(self, subdomain: str) -> dict[str, Any]:
        """Get PIN status for a subdomain."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/pin",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def set_tunnel_pin(self, subdomain: str, pin: str) -> dict[str, Any]:
        """Set or update the PIN for a subdomain."""
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/share-links",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: list[dict[str, Any]] = resp.json()
            return result

    async def delete_share_link(self, subdomain: str, link_id: int) -> dict[str, Any]:
        """Revoke a share link."""
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
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
        async with self._client_ctx() as client:
            resp = await client.delete(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/basic-auth",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    # -- Auth mode -----------------------------------------------------------

    async def get_tunnel_auth_mode(self, subdomain: str) -> dict[str, Any]:
        """Get the current auth_mode ('sso' or 'none') for a tunnel."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/auth-mode",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def set_tunnel_auth_mode(self, subdomain: str, auth_mode: str) -> dict[str, Any]:
        """Set a tunnel's auth_mode. Accepts 'sso' or 'none'.

        'sso' enforces the allow-list/PIN/Basic-Auth gate; 'none' makes the tunnel public
        regardless of configured rules.
        """
        if auth_mode not in ("sso", "none"):
            raise ValueError("auth_mode must be 'sso' or 'none'")
        async with self._client_ctx() as client:
            resp = await client.patch(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/auth-mode",
                headers=self._headers,
                json={"auth_mode": auth_mode},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_tunnel_status(self, subdomain: str) -> dict[str, Any]:
        """Return the aggregated config + live state for a tunnel."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/tunnels/{_safe_subdomain(subdomain)}/status",
                headers=self._headers,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_me(self) -> dict[str, Any]:
        """Return the authenticated user (for resolving ``user_code``)."""
        async with self._client_ctx() as client:
            resp = await client.get(
                f"{self._base_url}/api/auth/me",
                headers=self._headers,
            )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            return payload.get("user", payload)
