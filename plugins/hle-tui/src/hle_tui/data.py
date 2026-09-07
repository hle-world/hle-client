"""Everything the dashboard shows, read through hle_client.

No HTTP, no paths and no systemd knowledge live here — the CLI already owns
all of it, and a dashboard that reimplemented any of it would drift from the
commands it is a front end for. This module only turns those calls into rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Snapshot:
    """One poll of everything, with per-section failure kept separate.

    A section that could not be read is ``None``, which is not the same as an
    empty one: "the relay did not answer" and "you have no tunnels" look
    identical in a table and mean opposite things.
    """

    tunnels: list[dict[str, Any]] | None = None
    agents: list[dict[str, Any]] | None = None
    daemons: list[tuple[str, bool]] = field(default_factory=list)
    error: str | None = None


def resolve_api_key(explicit: str | None = None) -> str | None:
    from hle_client.tunnel import _load_api_key

    return explicit or _load_api_key()


def local_daemons() -> list[tuple[str, bool]]:
    """Installed services, as ``(name, user_scope)``."""
    try:
        from hle_client.service_cmd import installed_services

        return list(installed_services())
    except Exception:  # noqa: BLE001 — no systemd, or no permission, is an answer
        return []


async def _fetch_remote(api_key: str) -> tuple[list | None, list | None]:
    from hle_client.api import ApiClient, ApiClientConfig

    api = ApiClient(ApiClientConfig(api_key=api_key))
    try:
        tunnels = await api.list_tunnels()
    except Exception:  # noqa: BLE001
        tunnels = None
    try:
        agents = await api.list_agents()
    except Exception:  # noqa: BLE001
        agents = None
    return tunnels, agents


async def collect(api_key: str | None = None) -> Snapshot:
    """One full poll."""
    key = resolve_api_key(api_key)
    daemons = await asyncio.to_thread(local_daemons)
    if not key:
        return Snapshot(daemons=daemons, error="No API key. Run: hle auth login")
    tunnels, agents = await _fetch_remote(key)
    return Snapshot(tunnels=tunnels, agents=agents, daemons=daemons)


async def delete_tunnel(subdomain: str, api_key: str | None = None) -> str:
    """Delete a tunnel's record. Returns a line to show the user."""
    from hle_client.api import ApiClient, ApiClientConfig

    key = resolve_api_key(api_key)
    if not key:
        return "No API key."
    api = ApiClient(ApiClientConfig(api_key=key))
    tunnels = await api.list_tunnels()
    match = next((t for t in tunnels if t.get("subdomain") == subdomain), None)
    if match is None:
        return f"{subdomain} is not in the list any more."
    if match.get("is_active"):
        # The same rule the CLI enforces: the record holds the access rules,
        # so removing it under a live connection unprotects a published host.
        return f"{subdomain} is connected — stop it first."
    await api.delete_tunnel_record(str(match.get("tunnel_id")))
    return f"Deleted {subdomain}."


async def restart_daemon(name: str, user_scope: bool) -> str:
    from hle_client.service_cmd import restart_service

    ok = await asyncio.to_thread(restart_service, name, user_scope)
    scope = "user" if user_scope else "system"
    return f"Restarted {name} ({scope})." if ok else f"Could not restart {name} ({scope})."


def tunnel_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    if snapshot.tunnels is None:
        return []
    return [
        (
            t.get("subdomain", "?"),
            "live" if t.get("is_active") else "idle",
            t.get("service_url", ""),
            t.get("auth_mode", ""),
        )
        for t in snapshot.tunnels
    ]


def agent_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    """Rows for the agents table.

    The field is ``online``, not ``is_online`` — reading the wrong one showed
    every agent as offline, which is the most alarming thing this screen can
    say and was wrong for all of them. ``is_active`` is separate: a disabled
    agent is not the same as one that is merely not connected.
    """
    if snapshot.agents is None:
        return []
    rows = []
    for a in snapshot.agents:
        if not a.get("is_active", True):
            state = "disabled"
        else:
            state = "online" if a.get("online") else "offline"
        rows.append(
            (
                str(a.get("name", "?")),
                state,
                str(a.get("endpoint_count", 0)),
                str(a.get("agent_version") or "-"),
            )
        )
    return rows


def daemon_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    return [(name, "user" if user else "system") for name, user in snapshot.daemons]
