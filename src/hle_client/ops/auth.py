"""The gates on a tunnel other than the allow-list: PIN, Basic Auth, share links.

Validation of what a person typed (PIN length, password confirmation) stays
with whoever asked them; the relay validates again. What is checked here is
only what every caller would otherwise have to check the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hle_client.errors import HleError, UsageError
from hle_client.ops.models import BasicAuthStatus, PinStatus, ShareLink
from hle_client.ops.tunnels import fail, resolve_subdomain

if TYPE_CHECKING:
    from hle_client.api import ApiClient

SHARE_DURATIONS = ("1h", "24h", "7d")


# -- PIN --------------------------------------------------------------------


async def pin_status(api: ApiClient, name: str) -> PinStatus:
    subdomain = await resolve_subdomain(api, name)
    try:
        data = await api.get_tunnel_pin_status(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return PinStatus.from_api(data)


async def set_pin(api: ApiClient, name: str, pin: str) -> str:
    """Set or replace the PIN. Returns the resolved subdomain."""
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise HleError("PIN must be 4-8 digits.")
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.set_tunnel_pin(subdomain, pin)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain


async def remove_pin(api: ApiClient, name: str) -> str:
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.remove_tunnel_pin(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain


# -- Basic Auth -------------------------------------------------------------


async def basic_auth_status(api: ApiClient, name: str) -> BasicAuthStatus:
    subdomain = await resolve_subdomain(api, name)
    try:
        data = await api.get_tunnel_basic_auth_status(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return BasicAuthStatus.from_api(data)


async def set_basic_auth(api: ApiClient, name: str, username: str, password: str) -> str:
    """Set or replace the Basic Auth credential. Returns the resolved subdomain."""
    username = username.strip()
    if not username:
        raise HleError("Username cannot be empty.")
    if ":" in username:
        raise HleError("Username must not contain ':'.")
    if len(password) < 8:
        raise HleError("Password must be at least 8 characters.")
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.set_tunnel_basic_auth(subdomain, username, password)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain


async def remove_basic_auth(api: ApiClient, name: str) -> str:
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.remove_tunnel_basic_auth(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain


# -- Share links ------------------------------------------------------------


async def create_share(
    api: ApiClient,
    name: str,
    *,
    duration: str = "24h",
    label: str = "",
    max_uses: int | None = None,
) -> ShareLink:
    """Mint a share link. ``share_url`` is on the result and never shown again."""
    if duration not in SHARE_DURATIONS:
        raise UsageError(f"duration must be one of {', '.join(SHARE_DURATIONS)}.")
    subdomain = await resolve_subdomain(api, name)
    try:
        data = await api.create_share_link(subdomain, duration, label, max_uses)
    except Exception as exc:
        fail(exc, subdomain)
    return ShareLink.from_api(data)


async def list_shares(api: ApiClient, name: str) -> list[ShareLink]:
    subdomain = await resolve_subdomain(api, name)
    try:
        rows = await api.list_share_links(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return [ShareLink.from_api(r) for r in rows]


async def revoke_share(api: ApiClient, name: str, link_id: int) -> str:
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.delete_share_link(subdomain, link_id)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain
