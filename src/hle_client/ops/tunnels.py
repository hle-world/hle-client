"""Tunnels: list, look one up, delete, change the gate mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import httpx

from hle_client.errors import ApiError, ConflictError, HleError, NotFoundError
from hle_client.ops.models import Conflict, Deleted, Tunnel, TunnelDetail

if TYPE_CHECKING:
    from hle_client.api import ApiClient


def fail(exc: Exception, subdomain: str | None = None) -> NoReturn:
    """Re-raise anything the relay call threw as the typed error it means."""
    raise ApiError.from_exception(exc, subdomain) from None


async def user_code(api: ApiClient) -> str:
    """The account's ``user_code``, the suffix every subdomain carries."""
    try:
        me = await api.get_me()
    except Exception as exc:
        fail(exc)
    code = me.get("user_code")
    if not code:
        raise HleError("Could not resolve user_code from server")
    return str(code)


async def resolve_subdomain(api: ApiClient, name: str) -> str:
    """Resolve a label to ``<label>-<user_code>``; pass a subdomain through.

    Labels may themselves contain hyphens (``home-assistant``), so "contains a
    hyphen" is not the same as "already resolved". A value is passed through
    only when it already ends with ``-<user_code>``.
    """
    suffix = f"-{await user_code(api)}"
    if name.endswith(suffix) and len(name) > len(suffix):
        return name
    return f"{name}{suffix}"


async def list_tunnels(api: ApiClient) -> list[Tunnel]:
    """Every tunnel on the account, connected or not."""
    try:
        rows = await api.list_tunnels()
    except Exception as exc:
        fail(exc)
    return [Tunnel.from_api(row) for row in rows]


async def find_tunnel(api: ApiClient, name: str) -> Tunnel:
    """The tunnel called ``name`` (label or subdomain), from the list.

    Cheaper than :func:`get_tunnel` when only the record is wanted, and the
    only way to learn a tunnel's id, which the delete endpoint needs.
    """
    subdomain = await resolve_subdomain(api, name)
    for tunnel in await list_tunnels(api):
        if tunnel.subdomain == subdomain:
            return tunnel
    raise NotFoundError(
        f"No tunnel named {subdomain!r}.",
        hint="Run 'hle tunnel list' to see what exists.",
    )


async def get_tunnel(api: ApiClient, name: str) -> TunnelDetail:
    """The record plus every gate on it, in one round-trip."""
    subdomain = await resolve_subdomain(api, name)
    try:
        status = await api.get_tunnel_status(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return TunnelDetail.from_api(status)


async def delete_tunnel(
    api: ApiClient, tunnel: Tunnel | str, *, force: bool = False
) -> Deleted | Conflict:
    """Delete a tunnel's record, and the access rules that live on it.

    A live tunnel is refused rather than disconnected first: the close the
    relay sends is not fatal, so the client reconnects, and deleting the
    record out from under a connection that is about to come back is a race.
    That refusal comes back as a :class:`Conflict` so the caller can explain
    it, or pass ``force=True`` to send the request anyway and let the relay
    have the last word.
    """
    if isinstance(tunnel, str):
        tunnel = await find_tunnel(api, tunnel)
    subdomain = tunnel.subdomain
    if not tunnel.tunnel_id:
        raise HleError(f"The relay returned no id for {subdomain!r}.")
    if tunnel.online and not force:
        return Conflict(
            subject=subdomain,
            reason=f"{subdomain} is connected — stop it before deleting its record.",
            hint=(
                "Foreground:  Ctrl-C the 'hle tunnel create' that is running it.\n"
                "As a service: hle daemon list, then hle daemon uninstall --label <label>."
            ),
        )
    try:
        await api.delete_tunnel_record(tunnel.tunnel_id)
    except Exception as exc:
        fail(exc, subdomain)
    return Deleted(subject=subdomain)


async def set_auth_mode(api: ApiClient, name: str, mode: str) -> str:
    """Set the gate mode (``sso`` or ``none``). Returns the resolved subdomain."""
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.set_tunnel_auth_mode(subdomain, mode)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise NotFoundError(
                f"Tunnel {subdomain!r} has never been registered.",
                hint="Run 'hle tunnel create' once to create it, then re-run this command.",
                status=404,
            ) from None
        if exc.response.status_code == 400 and b"Webhook" in exc.response.content:
            raise ConflictError(
                "Webhook tunnels are always public — auth_mode cannot be changed.",
                status=400,
            ) from None
        fail(exc, subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return subdomain
