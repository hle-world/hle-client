"""HLE CLI — Main entry point for the Home Lab Everywhere client."""

from __future__ import annotations

import asyncio
import logging
import os

import click
from rich.console import Console
from rich.table import Table

from hle_client import __version__
from hle_client.tunnel import Tunnel, TunnelConfig, _load_api_key

console = Console()
logger = logging.getLogger(__name__)


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve an API key from flag, env var, or config file.

    Exits with an error message if no key is found.
    """
    resolved = api_key or _load_api_key()
    if not resolved:
        console.print(
            "[red]No API key found.[/red] Provide --api-key, set HLE_API_KEY, "
            "or save one to ~/.config/hle/config.toml"
        )
        raise SystemExit(1)
    return resolved


@click.group()
@click.version_option(version=__version__, prog_name="hle")
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging")
def main(debug: bool) -> None:
    """Home Lab Everywhere — Expose homelab services to the internet with built-in SSO."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--service", required=True, help="Local service URL (e.g. http://localhost:8080)")
@click.option("--auth", type=click.Choice(["sso", "none"]), default="sso", help="Auth mode")
@click.option("--label", "service_label", default=None, help="Service label (e.g. ha, jellyfin)")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key (also reads HLE_API_KEY env var, then ~/.config/hle/config.toml)",
)
@click.option("--websocket/--no-websocket", default=True, help="Enable WebSocket proxying")
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def expose(
    service: str,
    auth: str,
    service_label: str | None,
    api_key: str | None,
    websocket: bool,
    relay_host: str,
    relay_port: int,
) -> None:
    """Expose a local service to the internet."""
    config = TunnelConfig(
        service_url=service,
        relay_host=relay_host,
        relay_port=relay_port,
        auth_mode=auth,
        service_label=service_label,
        api_key=api_key,
        websocket_enabled=websocket,
    )
    tunnel = Tunnel(config=config)

    # Warn if the API key was passed as a CLI flag (visible in ps/proc).
    if api_key and not os.environ.get("HLE_API_KEY"):
        console.print(
            "[yellow]Warning:[/yellow] API key passed via --api-key is visible in process "
            "listings.\n         Use HLE_API_KEY env var or ~/.config/hle/config.toml instead."
        )

    console.print(f"\n[bold]HLE[/bold] v{__version__}  Exposing [cyan]{service}[/cyan]")
    console.print(f"     Relay   [dim]{relay_host}:{relay_port}[/dim]")
    if service_label:
        console.print(f"     Label   [dim]{service_label}[/dim]")
    console.print(f"     WS      [dim]{'enabled' if websocket else 'disabled'}[/dim]")
    console.print()

    try:
        asyncio.run(tunnel.connect())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down ...[/yellow]")


@main.command()
@click.option("--path", required=True, help="Webhook path (e.g. /webhook/github)")
@click.option("--forward-to", required=True, help="Local URL to forward webhooks to")
def webhook(path: str, forward_to: str) -> None:
    """Forward incoming webhooks to a local service."""
    click.echo(f"Forwarding webhooks {path} -> {forward_to}")
    # TODO: implement webhook forwarding


# ---------------------------------------------------------------------------
# hle tunnels — list active tunnels
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def tunnels(api_key: str | None, relay_host: str, relay_port: int) -> None:
    """List active tunnels for your account."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            tunnel_list = await client.list_tunnels()
        except Exception as exc:
            _handle_api_error(exc)
            return

        if not tunnel_list:
            console.print("[dim]No active tunnels.[/dim]")
            return

        table = Table(title="Active Tunnels")
        table.add_column("Subdomain", style="cyan")
        table.add_column("Service URL")
        table.add_column("WebSocket")
        table.add_column("Connected At", style="dim")
        for t in tunnel_list:
            table.add_row(
                t.get("subdomain", ""),
                t.get("service_url", ""),
                "yes" if t.get("websocket_enabled") else "no",
                t.get("connected_at", ""),
            )
        console.print(table)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# hle access — manage tunnel access rules
# ---------------------------------------------------------------------------


@main.group()
def access() -> None:
    """Manage tunnel access control rules."""


@access.command("list")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def access_list(subdomain: str, api_key: str | None, relay_host: str, relay_port: int) -> None:
    """List access rules for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            rules = await client.list_access_rules(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        if not rules:
            console.print(f"[dim]No access rules for {subdomain}.[/dim]")
            return

        table = Table(title=f"Access Rules — {subdomain}")
        table.add_column("ID", style="dim")
        table.add_column("Email", style="cyan")
        table.add_column("Provider")
        table.add_column("Created At", style="dim")
        for r in rules:
            table.add_row(
                str(r.get("id", "")),
                r.get("allowed_email", ""),
                r.get("provider", ""),
                r.get("created_at", ""),
            )
        console.print(table)

    asyncio.run(_run())


@access.command("add")
@click.argument("subdomain")
@click.argument("email")
@click.option(
    "--provider",
    type=click.Choice(["any", "google", "github", "hle"]),
    default="any",
    show_default=True,
    help="Required auth provider",
)
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def access_add(
    subdomain: str,
    email: str,
    provider: str,
    api_key: str | None,
    relay_host: str,
    relay_port: int,
) -> None:
    """Add an email to a subdomain's access allow-list."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            rule = await client.add_access_rule(subdomain, email, provider)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(
            f"[green]Added[/green] {rule.get('allowed_email', email)} "
            f"(provider={rule.get('provider', provider)}) to {subdomain}"
        )

    asyncio.run(_run())


@access.command("remove")
@click.argument("subdomain")
@click.argument("rule_id", type=int)
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def access_remove(
    subdomain: str, rule_id: int, api_key: str | None, relay_host: str, relay_port: int
) -> None:
    """Remove an access rule by ID."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            await client.delete_access_rule(subdomain, rule_id)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]Removed[/green] rule {rule_id} from {subdomain}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# hle pin — manage tunnel PIN access
# ---------------------------------------------------------------------------


@main.group()
def pin() -> None:
    """Manage tunnel PIN access control."""


@pin.command("set")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def pin_set(subdomain: str, api_key: str | None, relay_host: str, relay_port: int) -> None:
    """Set a PIN for a subdomain (prompts for 4-8 digit PIN)."""
    resolved_key = _resolve_api_key(api_key)

    pin_value = click.prompt("Enter PIN (4-8 digits)", hide_input=True)
    if not pin_value.isdigit() or not (4 <= len(pin_value) <= 8):
        console.print("[red]Error:[/red] PIN must be 4-8 digits.")
        raise SystemExit(1)

    pin_confirm = click.prompt("Confirm PIN", hide_input=True)
    if pin_value != pin_confirm:
        console.print("[red]Error:[/red] PINs do not match.")
        raise SystemExit(1)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            await client.set_tunnel_pin(subdomain, pin_value)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]PIN set[/green] for {subdomain}")

    asyncio.run(_run())


@pin.command("remove")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def pin_remove(subdomain: str, api_key: str | None, relay_host: str, relay_port: int) -> None:
    """Remove the PIN for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            await client.remove_tunnel_pin(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]PIN removed[/green] from {subdomain}")

    asyncio.run(_run())


@pin.command("status")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def pin_status(subdomain: str, api_key: str | None, relay_host: str, relay_port: int) -> None:
    """Show PIN status for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            data = await client.get_tunnel_pin_status(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        if data.get("has_pin"):
            updated = data.get("updated_at", "")
            console.print(f"[cyan]{subdomain}[/cyan]: PIN is [green]active[/green]")
            if updated:
                console.print(f"  Last updated: [dim]{updated}[/dim]")
        else:
            console.print(f"[cyan]{subdomain}[/cyan]: [dim]No PIN set[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# hle share — manage temporary share links
# ---------------------------------------------------------------------------


@main.group()
def share() -> None:
    """Manage temporary share links for tunnels."""


@share.command("create")
@click.argument("subdomain")
@click.option(
    "--duration",
    type=click.Choice(["1h", "24h", "7d"]),
    default="24h",
    show_default=True,
    help="Link validity duration",
)
@click.option("--label", default="", help="Optional label for the link")
@click.option("--max-uses", default=None, type=int, help="Maximum number of uses")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def share_create(
    subdomain: str,
    duration: str,
    label: str,
    max_uses: int | None,
    api_key: str | None,
    relay_host: str,
    relay_port: int,
) -> None:
    """Create a temporary share link for a tunnel."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            result = await client.create_share_link(subdomain, duration, label, max_uses)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print()
        console.print("[green bold]Share link created![/green bold]")
        console.print()
        console.print(f"  [cyan]{result['share_url']}[/cyan]")
        console.print()
        if result.get("link", {}).get("label"):
            console.print(f"  Label:   {result['link']['label']}")
        console.print(f"  Expires: {result['link']['expires_at']}")
        if result["link"].get("max_uses"):
            console.print(f"  Max uses: {result['link']['max_uses']}")
        console.print()
        console.print("[dim]This URL will not be shown again.[/dim]")

    asyncio.run(_run())


@share.command("list")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def share_list(subdomain: str, api_key: str | None, relay_host: str, relay_port: int) -> None:
    """List share links for a tunnel."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            links = await client.list_share_links(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        if not links:
            console.print(f"[dim]No share links for {subdomain}.[/dim]")
            return

        table = Table(title=f"Share Links — {subdomain}")
        table.add_column("ID", style="dim")
        table.add_column("Label")
        table.add_column("Prefix", style="cyan")
        table.add_column("Expires", style="dim")
        table.add_column("Uses")
        table.add_column("Status")
        for link in links:
            uses = str(link.get("use_count", 0))
            if link.get("max_uses"):
                uses += f"/{link['max_uses']}"
            status = "[green]Active[/green]" if link.get("is_active") else "[red]Revoked[/red]"
            table.add_row(
                str(link.get("id", "")),
                link.get("label", "") or "-",
                link.get("token_prefix", ""),
                link.get("expires_at", ""),
                uses,
                status,
            )
        console.print(table)

    asyncio.run(_run())


@share.command("revoke")
@click.argument("subdomain")
@click.argument("link_id", type=int)
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
@click.option("--relay-host", default="hle.world", show_default=True, help="Relay server host")
@click.option("--relay-port", default=443, show_default=True, type=int, help="Relay server port")
def share_revoke(
    subdomain: str, link_id: int, api_key: str | None, relay_host: str, relay_port: int
) -> None:
    """Revoke a share link by ID."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(
            ApiClientConfig(relay_host=relay_host, relay_port=relay_port, api_key=resolved_key)
        )
        try:
            await client.delete_share_link(subdomain, link_id)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]Revoked[/green] share link {link_id} from {subdomain}")

    asyncio.run(_run())


def _handle_api_error(exc: Exception) -> None:
    """Map HTTP errors to user-friendly messages."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        messages = {
            401: "Invalid or missing API key.",
            403: "You do not own this subdomain.",
            404: "Resource not found.",
            409: "Email already in access list.",
        }
        msg = messages.get(status, f"Server error ({status}): {exc.response.text}")
        console.print(f"[red]Error:[/red] {msg}")
    elif isinstance(exc, httpx.ConnectError):
        console.print("[red]Error:[/red] Could not connect to relay server.")
    else:
        console.print(f"[red]Error:[/red] {exc}")


if __name__ == "__main__":
    main()
