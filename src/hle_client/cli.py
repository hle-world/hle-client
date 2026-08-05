"""HLE CLI — Main entry point for the HomeLab Everywhere client."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import webbrowser
from datetime import UTC
from typing import Any

import click
from rich.console import Console

from hle_client import __version__
from hle_client.agent import (
    AGENT_TOKEN_PREFIX,
    AgentClient,
    load_agent_token,
    remove_agent_token,
    save_agent_token,
)
from hle_client.config_cmd import config as config_group
from hle_client.fp_cmd import fp as fp_command
from hle_client.service_cmd import service as service_group
from hle_client.tunnel import (
    Tunnel,
    TunnelConfig,
    TunnelFatalError,
    _load_api_key,
    _remove_api_key,
    _save_api_key,
)
from hle_client.update_cmd import update as update_command

console = Console()
logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version=__version__, prog_name="hle")
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging")
def main(debug: bool) -> None:
    """HomeLab Everywhere — Expose homelab services to the internet with built-in SSO."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


_VALID_AUTH_PROVIDERS = {"any", "google", "github", "hle"}


def _parse_auth_spec(spec: str) -> tuple[str, str]:
    """Parse ``[provider:]email`` into ``(provider, email)``."""
    if ":" in spec:
        prefix, _, rest = spec.partition(":")
        if prefix in _VALID_AUTH_PROVIDERS:
            return prefix, rest
    return "any", spec


# ---------------------------------------------------------------------------
# hle expose — run a tunnel
# ---------------------------------------------------------------------------


@main.command()
@click.option("--service", required=True, help="Local service URL (e.g. http://localhost:8080)")
@click.option("--auth", type=click.Choice(["sso", "none"]), default="sso", help="Auth mode")
@click.option(
    "--label",
    "service_label",
    default=None,
    help="Service label (e.g. ha, jellyfin). Required unless --apex is set.",
)
@click.option(
    "--zone",
    default=None,
    help="Custom zone to publish under (e.g. t00t.us). Required for --apex.",
)
@click.option(
    "--apex",
    is_flag=True,
    default=False,
    help="Serve at the bare zone root (e.g. https://t00t.us) instead of a subdomain.",
)
@click.option(
    "--option",
    "options",
    multiple=True,
    metavar="KEY=VALUE",
    help="Generic server-interpreted parameter, passed through verbatim. "
    "Repeatable. The server defines which keys are valid. Example: --option zone=t00t.us",
)
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key (also reads HLE_API_KEY env var, then ~/.config/hle/config.toml)",
)
@click.option("--websocket/--no-websocket", default=True, help="Enable WebSocket proxying")
@click.option(
    "--verify-ssl",
    is_flag=True,
    default=False,
    help="Enable SSL certificate verification (by default self-signed certs are accepted)",
)
@click.option(
    "--upstream-basic-auth",
    "upstream_basic_auth",
    default=None,
    metavar="USER:PASS",
    help="Inject Basic Auth into every request to the local service. Format: USER:PASS",
)
@click.option(
    "--forward-host",
    is_flag=True,
    default=False,
    help="Forward the browser's Host header to the local service "
    "(for services that validate Host).",
)
@click.option(
    "--allow",
    "allow",
    multiple=True,
    metavar="[PROVIDER:]EMAIL",
    help="Allow an email to access this tunnel via SSO. "
    "Format: 'email' or 'provider:email'. "
    "Providers: any (default), google, github, hle. Repeatable.",
)
def expose(
    service: str,
    auth: str,
    service_label: str | None,
    zone: str | None,
    apex: bool,
    options: tuple[str, ...],
    api_key: str | None,
    websocket: bool,
    verify_ssl: bool,
    upstream_basic_auth: str | None,
    forward_host: bool,
    allow: tuple[str, ...],
) -> None:
    """Expose a local service to the internet."""
    # Parse --option KEY=VALUE pairs into a passthrough dict.
    options_dict: dict[str, str] = {}
    for opt in options:
        key, sep, val = opt.partition("=")
        if not sep or not key:
            console.print(f"[red]Error:[/red] --option must be KEY=VALUE (got '{opt}').")
            raise SystemExit(1)
        options_dict[key.strip()] = val

    # Validate apex / label / zone combination up front.
    if apex and not zone:
        console.print("[red]Error:[/red] --apex requires --zone (e.g. --zone t00t.us).")
        raise SystemExit(1)
    if not apex and not service_label:
        console.print("[red]Error:[/red] --label is required (or use --apex with --zone).")
        raise SystemExit(1)

    upstream_auth_tuple: tuple[str, str] | None = None
    if upstream_basic_auth:
        if ":" not in upstream_basic_auth:
            console.print("[red]Error:[/red] --upstream-basic-auth must be in USER:PASS format.")
            raise SystemExit(1)
        u, _, p = upstream_basic_auth.partition(":")
        upstream_auth_tuple = (u, p)

    config = TunnelConfig(
        service_url=service,
        auth_mode=auth,
        service_label=service_label,
        zone=zone,
        apex=apex,
        options=options_dict,
        api_key=api_key,
        websocket_enabled=websocket,
        verify_ssl=verify_ssl,
        upstream_basic_auth=upstream_auth_tuple,
        forward_host=forward_host,
    )

    auth_specs = [_parse_auth_spec(s) for s in allow]
    on_registered_cb = None
    if auth_specs:

        async def _add_auth_callback(subdomain: str) -> None:
            import httpx

            from hle_client.api import ApiClient, ApiClientConfig

            resolved_key = api_key or _load_api_key()
            if not resolved_key:
                console.print("[yellow]Warning:[/yellow] No API key — skipping auth rules")
                return
            client = ApiClient(ApiClientConfig(api_key=resolved_key))
            for prov, email in auth_specs:
                try:
                    await client.add_access_rule(subdomain, email, prov)
                    console.print(f"     Auth   [green]+[/green] {email} [dim]({prov})[/dim]")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        console.print(f"     Auth   [dim]· {email} ({prov}) already exists[/dim]")
                    else:
                        console.print(
                            f"     Auth   [yellow]! {email} failed: "
                            f"{exc.response.status_code}[/yellow]"
                        )

        on_registered_cb = _add_auth_callback

    tunnel = Tunnel(config=config, on_registered=on_registered_cb)

    if api_key and not os.environ.get("HLE_API_KEY"):
        console.print(
            "[yellow]Warning:[/yellow] API key passed via --api-key is visible in process "
            "listings.\n         Use HLE_API_KEY env var or ~/.config/hle/config.toml instead."
        )

    console.print(f"\n[bold]HLE[/bold] v{__version__}  Exposing [cyan]{service}[/cyan]")
    console.print("     Relay   [dim]hle.world[/dim]")
    if service_label:
        console.print(f"     Label   [dim]{service_label}[/dim]")
    console.print(f"     WS      [dim]{'enabled' if websocket else 'disabled'}[/dim]")
    console.print()

    try:
        asyncio.run(tunnel.connect())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down ...[/yellow]")
    except TunnelFatalError as exc:
        console.print(f"\n[red]Error:[/red] {exc}")
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# hle webhook — run a webhook forwarder
# ---------------------------------------------------------------------------


@main.command()
@click.option("--path", required=True, help="Webhook path (e.g. /webhook/github)")
@click.option("--forward-to", required=True, help="Local URL to forward webhooks to")
@click.option("--label", "service_label", required=True, help="Webhook label (e.g. github-hook)")
@click.option(
    "--api-key",
    envvar="HLE_API_KEY",
    default=None,
    help="API key. Falls back to ~/.config/hle/config.toml if not set.",
)
def webhook(
    path: str,
    forward_to: str,
    service_label: str,
    api_key: str | None,
) -> None:
    """Forward incoming webhooks to a local service.

    Example:

        hle webhook --path /hook/github --forward-to http://localhost:3000/webhook --label gh
    """
    import posixpath

    if not path.startswith("/"):
        path = f"/{path}"
    path = posixpath.normpath(path)
    if not path or path == "/":
        console.print("[red]Error:[/red] --path must be a non-root path (e.g. /webhook/github)")
        raise SystemExit(1)
    if ".." in path.split("/"):
        console.print("[red]Error:[/red] --path must not contain '..' segments")
        raise SystemExit(1)

    config = TunnelConfig(
        service_url=forward_to,
        auth_mode="none",
        service_label=service_label,
        api_key=api_key,
        websocket_enabled=False,
        verify_ssl=False,
        webhook_path=path,
    )

    tunnel = Tunnel(config=config)

    if api_key and not os.environ.get("HLE_API_KEY"):
        console.print(
            "[yellow]Warning:[/yellow] API key passed via --api-key is visible in process "
            "listings.\n         Use HLE_API_KEY env var or ~/.config/hle/config.toml instead."
        )

    console.print(f"\n[bold]HLE[/bold] v{__version__}  Webhook forwarder")
    console.print(f"     Path    [cyan]{path}[/cyan]")
    console.print(f"     Forward [cyan]{forward_to}[/cyan]")
    console.print("     Relay   [dim]hle.world[/dim]")
    console.print()

    try:
        asyncio.run(tunnel.connect())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down ...[/yellow]")
    except TunnelFatalError as exc:
        console.print(f"\n[red]Error:[/red] {exc}")
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# hle auth — API key authentication
# ---------------------------------------------------------------------------

_API_KEY_PATTERN = re.compile(r"^hle_[0-9a-f]{32}$")


@main.group()
def auth() -> None:
    """Manage API key authentication."""


@auth.command()
@click.option("--api-key", default=None, help="API key to save (skips browser prompt)")
def login(api_key: str | None) -> None:
    """Save an API key to ~/.config/hle/config.toml."""
    if api_key is None:
        console.print("Opening [cyan]https://hle.world/dashboard[/cyan] ...")
        webbrowser.open("https://hle.world/dashboard")
        console.print("Copy your API key from the dashboard and paste it here.\n")
        api_key = click.prompt("API key", hide_input=True)

    if not _API_KEY_PATTERN.match(api_key):
        console.print(
            "[red]Error:[/red] Invalid API key format. "
            "Expected 'hle_' followed by 32 hex characters."
        )
        raise SystemExit(1)

    _save_api_key(api_key)
    console.print("[green]Saved[/green] to ~/.config/hle/config.toml")


@auth.command("status")
def auth_status() -> None:
    """Show the current API key source and masked value."""
    env_key = os.environ.get("HLE_API_KEY")
    if env_key:
        masked = f"{env_key[:4]}...{env_key[-4:]}" if len(env_key) > 8 else env_key
        console.print("API key source: [cyan]HLE_API_KEY environment variable[/cyan]")
        console.print(f"Key: [dim]{masked}[/dim]")
        return

    config_key = _load_api_key()
    if config_key:
        masked = f"{config_key[:4]}...{config_key[-4:]}" if len(config_key) > 8 else config_key
        console.print("API key source: [cyan]config file (~/.config/hle/config.toml)[/cyan]")
        console.print(f"Key: [dim]{masked}[/dim]")
        return

    console.print("[dim]No API key configured.[/dim]")


@auth.command()
def logout() -> None:
    """Remove the saved API key from ~/.config/hle/config.toml."""
    if _remove_api_key():
        console.print("[green]API key removed[/green] from ~/.config/hle/config.toml")
    else:
        console.print("[dim]No API key saved in config file.[/dim]")


main.add_command(config_group, name="config")
main.add_command(update_command, name="update")
main.add_command(service_group, name="service")
main.add_command(fp_command, name="fp")


@main.group()
def agent() -> None:
    """Run a multi-tunnel agent controlled from the dashboard."""


@agent.command()
@click.argument("token", required=False)
def enroll(token: str | None) -> None:
    """Save an agent enrollment token (created in the dashboard)."""
    if token is None:
        console.print(
            "Create an agent at [cyan]https://hle.world/dashboard[/cyan] and copy its token.\n"
        )
        token = click.prompt("Agent token", hide_input=True)

    if not token.startswith(AGENT_TOKEN_PREFIX):
        console.print(
            f"[red]Error:[/red] Invalid agent token. Expected one starting with "
            f"'{AGENT_TOKEN_PREFIX}'."
        )
        raise SystemExit(1)

    save_agent_token(token)
    console.print("[green]Enrolled[/green] — token saved to ~/.config/hle/agent.toml")
    console.print("Start the agent with: [cyan]hle agent run[/cyan]")


@agent.command()
@click.option(
    "--token", default=None, envvar="HLE_AGENT_TOKEN", help="Agent token (overrides saved)"
)
@click.option("--relay-host", default="hle.world", help="Relay host")
@click.option("--relay-port", default=443, type=int, help="Relay port")
def run(token: str | None, relay_host: str, relay_port: int) -> None:
    """Run the agent: connect, fetch endpoints from the dashboard, and reconcile."""
    token = token or load_agent_token()
    if not token:
        console.print("[red]Error:[/red] No agent token. Run [cyan]hle agent enroll[/cyan] first.")
        raise SystemExit(1)

    client = AgentClient(token, relay_host=relay_host, relay_port=relay_port)
    console.print(f"[green]Agent running[/green] — control: {client.control_uri}")
    console.print("[dim]Manage endpoints from https://hle.world/dashboard. Ctrl+C to stop.[/dim]")
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped.[/yellow]")


@agent.command("status")
def agent_status() -> None:
    """Show whether an agent token is configured."""
    env_token = os.environ.get("HLE_AGENT_TOKEN")
    if env_token:
        console.print("Agent token source: [cyan]HLE_AGENT_TOKEN environment variable[/cyan]")
        return
    token = load_agent_token()
    if token:
        masked = f"{token[:9]}...{token[-4:]}" if len(token) > 13 else token
        console.print("Agent token source: [cyan]~/.config/hle/agent.toml[/cyan]")
        console.print(f"Token: [dim]{masked}[/dim]")
    else:
        console.print("[dim]No agent token configured. Run 'hle agent enroll'.[/dim]")
        # Non-zero so a script can act on it. Without this the installer could
        # confirm enrollment "succeeded", install a service, and leave it
        # restarting forever on a missing token.
        raise SystemExit(1)


@agent.command("list")
@click.option("--api-key", "api_key", default=None, help="API key (else env/config)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output")
def agent_list(api_key: str | None, as_json: bool) -> None:
    """List the agents on your account, and whether they're online.

    Unlike 'hle agent status', which only inspects this machine, this asks the
    relay. The Name column is what 'hle fp --agent' expects.
    """
    import json as _json

    import httpx
    from rich.table import Table

    from hle_client.api import ApiClient, ApiClientConfig

    key = api_key or _load_api_key()
    if not key:
        console.print("[red]Error:[/red] No API key. Run [cyan]hle auth login[/cyan] first.")
        raise SystemExit(1)

    async def _fetch() -> list[dict[str, Any]]:
        return await ApiClient(ApiClientConfig(api_key=key)).list_agents()

    try:
        agents = asyncio.run(_fetch())
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            console.print("[red]Error:[/red] API key rejected. Run [cyan]hle auth login[/cyan].")
        elif status == 404:
            # The endpoint 404s (rather than 403s) when the feature flag is off.
            console.print("[yellow]Agents are not enabled on this server.[/yellow]")
        else:
            console.print(f"[red]Error:[/red] server returned {status}")
        raise SystemExit(1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]Error:[/red] could not reach the relay — {exc}")
        raise SystemExit(1) from None

    if as_json:
        console.print(_json.dumps(agents, indent=2))
        return

    if not agents:
        console.print("[dim]No agents yet.[/dim]")
        console.print(
            "[dim]Create one at https://hle.world/dashboard → Agents, "
            "then run 'hle agent enroll <token>'.[/dim]"
        )
        return

    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Status")
    table.add_column("Endpoints", justify="right")
    table.add_column("Version", style="dim")
    table.add_column("Last seen", style="dim")
    for a in sorted(agents, key=lambda a: str(a.get("name", ""))):
        online = a.get("online", False)
        state = "[green]online[/green]" if online else "[dim]offline[/dim]"
        if not a.get("is_active", True):
            state = "[yellow]disabled[/yellow]"
        table.add_row(
            str(a.get("name", "?")),
            state,
            str(a.get("endpoint_count", 0)),
            a.get("agent_version") or "-",
            _humanize_last_seen(a.get("last_seen_at")),
        )
    console.print(table)


def _humanize_last_seen(value: str | None) -> str:
    """Render an ISO timestamp as a rough age. Falls back to the raw string.

    Exact timestamps aren't the useful thing here — "3m ago" answers "did this
    agent just drop?" at a glance, which is what you want when a forward fails.
    """
    if not value:
        return "never"
    from datetime import datetime

    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - seen).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


@agent.command("services")
@click.option("--provider", default=None, help="Only show one provider (k8s, docker)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output")
def agent_services(provider: str | None, as_json: bool) -> None:
    """List services this machine can see and could expose.

    Runs the same discovery the agent reports to the dashboard, locally — useful
    for checking what an agent would find before enrolling it, or for debugging
    why something isn't showing up.
    """
    import json as _json

    from rich.table import Table

    from hle_client.discovery import active_providers, scan_all

    providers = active_providers()
    if provider:
        providers = [p for p in providers if p.name == provider]

    if not providers:
        console.print("[yellow]No discovery providers are active here.[/yellow]")
        console.print(
            "[dim]Kubernetes needs an in-cluster ServiceAccount; Docker needs "
            "/var/run/docker.sock mounted.[/dim]"
        )
        return

    services, names, error = asyncio.run(scan_all(providers))

    if as_json:
        console.print(_json.dumps([s.model_dump() for s in services], indent=2))
        return

    if error:
        console.print(f"[yellow]Some providers had trouble:[/yellow] {error}")
    if not services:
        console.print(f"[dim]No services found (scanned: {', '.join(names)}).[/dim]")
        return

    table = Table(title=f"Discovered services ({', '.join(names)})")
    table.add_column("Name", style="cyan")
    table.add_column("Where", style="dim")
    table.add_column("Address")
    table.add_column("Suggested label", style="green")
    for s in sorted(services, key=lambda s: (s.provider, s.namespace or "", s.name)):
        table.add_row(s.name, s.namespace or s.provider, s.address, s.suggested_label())
    console.print(table)
    console.print(
        "\n[dim]Expose any of these from https://hle.world/dashboard → Agents → Endpoints.[/dim]"
    )


@agent.command("logout")
def agent_logout() -> None:
    """Remove the saved agent token."""
    if remove_agent_token():
        console.print("[green]Agent token removed[/green] from ~/.config/hle/agent.toml")
    else:
        console.print("[dim]No agent token saved.[/dim]")


if __name__ == "__main__":
    main()
