"""Everything the dashboard shows, read through ``hle_client.ops``.

No HTTP, no paths and no systemd knowledge live here — the CLI's service
layer already owns all of it, and a dashboard that reimplemented any of it
would drift from the commands it is a front end for (it did: this module
once carried its own copy of the relay fetch and of the delete rule). This
module only turns ``ops`` results into rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hle_client.config import load_api_key
from hle_client.errors import HleError
from hle_client.ops import agents as ops_agents
from hle_client.ops import daemon as ops_daemon
from hle_client.ops import tunnels as ops_tunnels
from hle_client.ops.models import Agent, Conflict, Daemon, Tunnel, public_url_for

if TYPE_CHECKING:
    from hle_client.api import ApiClient


@dataclass
class Snapshot:
    """One poll of everything, with per-section failure kept separate.

    A section that could not be read is ``None``, which is not the same as an
    empty one: "the relay did not answer" and "you have no tunnels" look
    identical in a table and mean opposite things.
    """

    tunnels: list[Tunnel] | None = None
    agents: list[Agent] | None = None
    daemons: list[Daemon] = field(default_factory=list)
    error: str | None = None


def resolve_api_key(explicit: str | None = None) -> str | None:
    return explicit or load_api_key()


def _client(api_key: str) -> ApiClient:
    # Looked up on the module at call time, so a test that patches
    # ``hle_client.api.ApiClient`` sees the dashboard go through its fake.
    from hle_client import api as api_module

    return api_module.ApiClient(api_module.ApiClientConfig(api_key=api_key))


async def local_daemons() -> list[Daemon]:
    """Installed services, or none when there is no manager to ask."""
    try:
        return await ops_daemon.list_daemons()
    except Exception:  # noqa: BLE001 — no systemd, or no permission, is an answer
        return []


async def collect(api_key: str | None = None) -> Snapshot:
    """One full poll."""
    key = resolve_api_key(api_key)
    daemons = await local_daemons()
    if not key:
        return Snapshot(daemons=daemons, error="No API key. Run: hle auth login")
    api = _client(key)
    tunnels: list[Tunnel] | None
    agents: list[Agent] | None
    try:
        tunnels = await ops_tunnels.list_tunnels(api)
    except HleError:
        tunnels = None
    try:
        agents = await ops_agents.list_agents(api)
    except HleError:
        agents = None
    return Snapshot(tunnels=tunnels, agents=agents, daemons=daemons)


async def delete_tunnel(subdomain: str, api_key: str | None = None) -> str:
    """Delete a tunnel's record. Returns a line to show the user."""
    key = resolve_api_key(api_key)
    if not key:
        return "No API key."
    try:
        result = await ops_tunnels.delete_tunnel(_client(key), subdomain)
    except HleError as exc:
        if exc.exit_code == 4:
            return f"{subdomain} is not in the list any more."
        return str(exc)
    if isinstance(result, Conflict):
        # The same rule the CLI enforces: the record holds the access rules,
        # so removing it under a live connection unprotects a published host.
        return f"{subdomain} is connected — stop it first."
    return f"Deleted {subdomain}."


async def restart_daemon(name: str, user_scope: bool) -> str:
    ok = await ops_daemon.restart(name, user_scope)
    scope = "user" if user_scope else "system"
    return f"Restarted {name} ({scope})." if ok else f"Could not restart {name} ({scope})."


def tunnel_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    if snapshot.tunnels is None:
        return []
    return [
        (
            t.subdomain or "?",
            "live" if t.online else "idle",
            t.service_url,
            t.auth_mode,
        )
        for t in snapshot.tunnels
    ]


def agent_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    """Rows for the agents table.

    ``Agent.state`` decides the word: ``online``, ``offline`` or ``disabled``.
    Reading ``is_online`` off the raw dict once showed every agent as offline,
    which is the most alarming thing this screen can say and was wrong for
    all of them; the mapper in ``ops.models`` is now the only reader.
    """
    if snapshot.agents is None:
        return []
    return [(a.name, a.state, str(a.endpoints), a.version or "-") for a in snapshot.agents]


def daemon_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    return [(d.name, d.scope) for d in snapshot.daemons]


def public_url(snapshot: Snapshot, subdomain: str) -> str:
    """Where the tunnel answers, from the relay's record rather than a guess."""
    for t in snapshot.tunnels or []:
        if t.subdomain == subdomain:
            return t.public_url
    return public_url_for(subdomain, None)
