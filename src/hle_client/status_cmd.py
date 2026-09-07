"""``hle status`` — everything this machine is set up to do, on one screen.

The information already existed and was scattered across four commands that a
new user has no reason to know about: `auth status` for the API key, `agent
status` for the agent token, `service list` for units, and the relay for what
is actually published. Nobody assembled it, so "is this thing working?" had no
answer short of a tour.

Every section degrades on its own. A machine with no network still gets its
local answers; a machine with no credential still gets its unit list. A status
command that fails as a unit is useless exactly when it is needed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hle_client import __version__
from hle_client.output import api_key_from_ctx, from_ctx

if TYPE_CHECKING:
    import click


def _credentials() -> dict[str, Any]:
    import os

    from hle_client.agent import load_agent_token
    from hle_client.tunnel import _load_api_key

    env_key = os.environ.get("HLE_API_KEY")
    saved = _load_api_key()
    source = None
    if env_key:
        source = "HLE_API_KEY"
    elif saved:
        source = "~/.config/hle/config.toml"

    key = env_key or saved
    return {
        "api_key": bool(key),
        "api_key_source": source,
        "api_key_prefix": (key[:8] + "…") if key else None,
        "agent_token": load_agent_token() is not None,
    }


def _daemons() -> list[dict[str, Any]]:
    try:
        from hle_client.service_cmd import installed_services

        return [
            {"name": name, "scope": "user" if user else "system"}
            for name, user in installed_services()
        ]
    except Exception:  # noqa: BLE001 — no systemd, or no permission, is an answer
        return []


async def _remote(api_key: str | None) -> dict[str, Any]:
    """What the relay knows. Absent rather than fatal when it cannot be asked."""
    from hle_client.api import ApiClient, ApiClientConfig

    if not api_key:
        return {}
    api = ApiClient(ApiClientConfig(api_key=api_key))
    result: dict[str, Any] = {}
    try:
        result["tunnels"] = await api.list_tunnels()
    except Exception:  # noqa: BLE001
        result["tunnels"] = None
    try:
        result["agents"] = await api.list_agents()
    except Exception:  # noqa: BLE001
        result["agents"] = None
    return result


def collect(api_key: str | None) -> dict[str, Any]:
    """Assemble the whole picture. Pure data, so ``-o json`` is the same call."""
    creds = _credentials()
    from hle_client.tunnel import _load_api_key

    resolved = api_key or _load_api_key()
    remote = asyncio.run(_remote(resolved)) if resolved else {}
    return {
        "version": __version__,
        "credentials": creds,
        "daemons": _daemons(),
        "tunnels": remote.get("tunnels"),
        "agents": remote.get("agents"),
    }


def render_status(ctx: click.Context) -> None:
    out = from_ctx(ctx)
    api_key = api_key_from_ctx(ctx, None)
    state = collect(api_key)

    if out.json_mode:
        out.data(state)
        return

    creds = state["credentials"]
    out.print(f"[bold]HLE[/bold] v{state['version']}")
    out.print()

    out.print("[bold]Credentials[/bold]")
    if creds["api_key"]:
        out.print(f"  API key      [green]set[/green] [dim]({creds['api_key_source']})[/dim]")
    else:
        out.print("  API key      [yellow]none[/yellow] [dim]— run: hle auth login[/dim]")
    if creds["agent_token"]:
        out.print("  Agent token  [green]set[/green] [dim](~/.config/hle/agent.toml)[/dim]")
    out.print()

    daemons = state["daemons"]
    out.print("[bold]Services on this machine[/bold]")
    if daemons:
        for d in daemons:
            out.print(f"  {d['name']} [dim]({d['scope']})[/dim]")
    else:
        out.print("  [dim]none installed[/dim]")
    out.print()

    tunnels = state["tunnels"]
    out.print("[bold]Published tunnels[/bold]")
    if tunnels is None:
        out.print("  [dim]could not ask the relay[/dim]")
    elif not tunnels:
        out.print("  [dim]none[/dim]")
    else:
        for t in tunnels:
            live = "[green]live[/green]" if t.get("is_active") else "[dim]idle[/dim]"
            out.print(f"  {t.get('subdomain', '?')}  {live}  [dim]{t.get('service_url', '')}[/dim]")
    out.print()

    agents = state["agents"]
    if agents:
        out.print("[bold]Agents[/bold]")
        for a in agents:
            online = "[green]online[/green]" if a.get("is_online") else "[dim]offline[/dim]"
            out.print(f"  {a.get('name', '?')}  {online}")
