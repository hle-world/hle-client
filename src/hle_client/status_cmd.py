"""``hle status`` — everything this machine is set up to do, on one screen.

The information already existed and was scattered across four commands that a
new user has no reason to know about: `auth status` for the API key, `agent
status` for the agent token, `service list` for units, and the relay for what
is actually published. Nobody assembled it, so "is this thing working?" had no
answer short of a tour.

Every section degrades on its own. A machine with no network still gets its
local answers; a machine with no credential still gets its unit list. A status
command that fails as a unit is useless exactly when it is needed.

The relay's answers come through ``hle_client.ops`` as typed models, the same
ones the dashboard renders. This screen once read ``is_online`` off the raw
dict while the relay sent ``online``, and every agent showed offline; now
there is no raw dict to misread.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hle_client import __version__, config
from hle_client.context import out, resolve_api_key
from hle_client.errors import HleError
from hle_client.ops import agents as ops_agents
from hle_client.ops import daemon as ops_daemon
from hle_client.ops import tunnels as ops_tunnels

if TYPE_CHECKING:
    import click

    from hle_client.ops.models import Agent, Daemon, Tunnel


def _credentials() -> dict[str, Any]:
    creds = config.load_credentials()
    key = creds.api_key
    return {
        "api_key": bool(key),
        "api_key_source": creds.api_key_source,
        "api_key_prefix": (key[:8] + "…") if key else None,
        "agent_token": creds.agent_token is not None,
    }


async def _daemons() -> list[Daemon]:
    try:
        return await ops_daemon.list_daemons()
    except Exception:  # noqa: BLE001 — no systemd, or no permission, is an answer
        return []


async def _remote(api_key: str | None) -> tuple[list[Tunnel] | None, list[Agent] | None]:
    """What the relay knows. Absent rather than fatal when it cannot be asked."""
    from hle_client.api import ApiClient, ApiClientConfig

    if not api_key:
        return None, None
    api = ApiClient(ApiClientConfig(api_key=api_key))
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
    return tunnels, agents


async def _collect(api_key: str | None) -> dict[str, Any]:
    daemons = await _daemons()
    tunnels, agents = await _remote(api_key)
    return {
        "version": __version__,
        "credentials": _credentials(),
        "daemons": daemons,
        "tunnels": tunnels,
        "agents": agents,
    }


def collect(api_key: str | None) -> dict[str, Any]:
    """Assemble the whole picture.

    ``daemons``, ``tunnels`` and ``agents`` are lists of ``ops`` models (the
    latter two ``None`` when the relay could not be asked); :func:`as_json`
    turns the same picture into what ``-o json`` prints.
    """
    resolved = api_key or config.load_api_key()
    return asyncio.run(_collect(resolved))


def as_json(state: dict[str, Any]) -> dict[str, Any]:
    """The ``-o json`` shape: the relay's own payloads, untouched."""
    tunnels = state["tunnels"]
    agents = state["agents"]
    return {
        "version": state["version"],
        "credentials": state["credentials"],
        "daemons": [{"name": d.name, "scope": d.scope} for d in state["daemons"]],
        "tunnels": None if tunnels is None else [t.raw for t in tunnels],
        "agents": None if agents is None else [a.raw for a in agents],
    }


def render_status(ctx: click.Context) -> None:
    o = out(ctx)
    state = collect(resolve_api_key(ctx))

    if o.json_mode:
        o.data(as_json(state))
        return

    creds = state["credentials"]
    o.print(f"[bold]HLE[/bold] v{state['version']}")
    o.print()

    o.print("[bold]Credentials[/bold]")
    if creds["api_key"]:
        o.print(f"  API key      [green]set[/green] [dim]({creds['api_key_source']})[/dim]")
    else:
        o.print("  API key      [yellow]none[/yellow] [dim]— run: hle auth login[/dim]")
    if creds["agent_token"]:
        o.print("  Agent token  [green]set[/green] [dim](~/.config/hle/agent.toml)[/dim]")
    o.print()

    daemons: list[Daemon] = state["daemons"]
    o.print("[bold]Services on this machine[/bold]")
    if daemons:
        for d in daemons:
            o.print(f"  {d.name} [dim]({d.scope})[/dim]")
    else:
        o.print("  [dim]none installed[/dim]")
    o.print()

    tunnels: list[Tunnel] | None = state["tunnels"]
    o.print("[bold]Published tunnels[/bold]")
    if tunnels is None:
        o.print("  [dim]could not ask the relay[/dim]")
    elif not tunnels:
        o.print("  [dim]none[/dim]")
    else:
        for t in tunnels:
            live = "[green]live[/green]" if t.online else "[dim]idle[/dim]"
            o.print(f"  {t.subdomain or '?'}  {live}  [dim]{t.service_url}[/dim]")
    o.print()

    agents: list[Agent] | None = state["agents"]
    if agents:
        o.print("[bold]Agents[/bold]")
        for a in agents:
            o.print(f"  {a.name}  {_agent_state(a)}")


_STATE_STYLE = {"online": "green", "offline": "dim", "disabled": "yellow"}


def _agent_state(agent: Agent) -> str:
    """The one word for an agent's state, coloured. ``Agent.state`` decides the word."""
    style = _STATE_STYLE.get(agent.state, "dim")
    return f"[{style}]{agent.state}[/{style}]"
