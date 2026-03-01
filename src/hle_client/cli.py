"""HLE CLI — Main entry point for the Home Lab Everywhere client."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import webbrowser

import click
from rich.console import Console
from rich.table import Table

from hle_client import __version__
from hle_client.tunnel import Tunnel, TunnelConfig, _load_api_key, _remove_api_key, _save_api_key

console = Console()
logger = logging.getLogger(__name__)


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve an API key from flag, env var, or config file.

    Exits with an error message if no key is found.
    """
    resolved = api_key or _load_api_key()
    if not resolved:
        console.print(
            "[red]No API key found.[/red] Run 'hle auth login', set HLE_API_KEY, or pass --api-key."
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
def expose(
    service: str,
    auth: str,
    service_label: str | None,
    api_key: str | None,
    websocket: bool,
    verify_ssl: bool,
    upstream_basic_auth: str | None,
    forward_host: bool,
) -> None:
    """Expose a local service to the internet."""
    # Parse --upstream-basic-auth USER:PASS
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
        api_key=api_key,
        websocket_enabled=websocket,
        verify_ssl=verify_ssl,
        upstream_basic_auth=upstream_auth_tuple,
        forward_host=forward_host,
    )
    tunnel = Tunnel(config=config)

    # Warn if the API key was passed as a CLI flag (visible in ps/proc).
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


# ---------------------------------------------------------------------------
# hle auth — manage API key authentication
# ---------------------------------------------------------------------------

_API_KEY_PATTERN = re.compile(r"^hle_[0-9a-f]{32}$")


@main.group()
def auth() -> None:
    """Manage API key authentication."""


@auth.command()
@click.option(
    "--api-key",
    default=None,
    help="API key to save (skips browser prompt)",
)
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
def tunnels(api_key: str | None) -> None:
    """List active tunnels for your account."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
# Auth conflict helpers — warn when methods would override each other
# ---------------------------------------------------------------------------


async def _warn_if_basic_auth_active(client: object, subdomain: str) -> None:
    """Warn the user if Basic Auth is active (it will override PIN/email rules)."""
    try:
        data = await client.get_tunnel_basic_auth_status(subdomain)
        if data.get("enabled"):
            console.print(
                f"[yellow]Warning:[/yellow] Basic Auth is currently active on "
                f"[cyan]{subdomain}[/cyan].\n"
                "  Email rules and PIN are bypassed while it's active.\n"
                "  Remove Basic Auth first ([dim]hle basic-auth remove "
                f"{subdomain}[/dim]) to re-enable SSO/PIN access control."
            )
            if not click.confirm("  Continue anyway?", default=False):
                raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        pass  # If the status check fails (network error etc.), proceed without warning


async def _warn_if_pin_or_rules_exist(client: object, subdomain: str) -> None:
    """Warn the user if PIN or email rules exist (Basic Auth will override them)."""
    conflicts: list[str] = []
    try:
        pin = await client.get_tunnel_pin_status(subdomain)
        if pin.get("has_pin"):
            conflicts.append("an active PIN")
    except Exception:
        pass
    try:
        rules = await client.list_access_rules(subdomain)
        if rules:
            n = len(rules)
            conflicts.append(f"{n} email rule{'s' if n > 1 else ''}")
    except Exception:
        pass
    if conflicts:
        conflict_str = " and ".join(conflicts)
        console.print(
            f"[yellow]Warning:[/yellow] [cyan]{subdomain}[/cyan] already has "
            f"{conflict_str}.\n"
            "  Enabling Basic Auth will [bold]override[/bold] "
            f"{'them' if len(conflicts) > 1 else 'it'} — visitors will only be "
            "able to authenticate with the Basic Auth username/password."
        )
        if not click.confirm("  Continue?", default=False):
            raise SystemExit(0)


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
def access_list(subdomain: str, api_key: str | None) -> None:
    """List access rules for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def access_add(
    subdomain: str,
    email: str,
    provider: str,
    api_key: str | None,
) -> None:
    """Add an email to a subdomain's access allow-list."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        await _warn_if_basic_auth_active(client, subdomain)
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
def access_remove(subdomain: str, rule_id: int, api_key: str | None) -> None:
    """Remove an access rule by ID."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def pin_set(subdomain: str, api_key: str | None) -> None:
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

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        await _warn_if_basic_auth_active(client, subdomain)
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
def pin_remove(subdomain: str, api_key: str | None) -> None:
    """Remove the PIN for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def pin_status(subdomain: str, api_key: str | None) -> None:
    """Show PIN status for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def share_create(
    subdomain: str,
    duration: str,
    label: str,
    max_uses: int | None,
    api_key: str | None,
) -> None:
    """Create a temporary share link for a tunnel."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def share_list(subdomain: str, api_key: str | None) -> None:
    """List share links for a tunnel."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
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
def share_revoke(subdomain: str, link_id: int, api_key: str | None) -> None:
    """Revoke a share link by ID."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        try:
            await client.delete_share_link(subdomain, link_id)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]Revoked[/green] share link {link_id} from {subdomain}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# hle basic-auth — manage tunnel HTTP Basic Auth
# ---------------------------------------------------------------------------


@main.group("basic-auth")
def basic_auth() -> None:
    """Manage tunnel HTTP Basic Auth access control."""


@basic_auth.command("set")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
def basic_auth_set(subdomain: str, api_key: str | None) -> None:
    """Set HTTP Basic Auth credentials for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    username = click.prompt("Username")
    if not username.strip():
        console.print("[red]Error:[/red] Username cannot be empty.")
        raise SystemExit(1)
    if ":" in username:
        console.print("[red]Error:[/red] Username must not contain ':'.")
        raise SystemExit(1)

    password = click.prompt("Password (min 8 chars)", hide_input=True)
    if len(password) < 8:
        console.print("[red]Error:[/red] Password must be at least 8 characters.")
        raise SystemExit(1)

    password_confirm = click.prompt("Confirm password", hide_input=True)
    if password != password_confirm:
        console.print("[red]Error:[/red] Passwords do not match.")
        raise SystemExit(1)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        await _warn_if_pin_or_rules_exist(client, subdomain)
        try:
            await client.set_tunnel_basic_auth(subdomain, username.strip(), password)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]Basic Auth set[/green] for {subdomain} (user: {username.strip()})")

    asyncio.run(_run())


@basic_auth.command("remove")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
def basic_auth_remove(subdomain: str, api_key: str | None) -> None:
    """Remove HTTP Basic Auth from a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        try:
            await client.remove_tunnel_basic_auth(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        console.print(f"[green]Basic Auth removed[/green] from {subdomain}")

    asyncio.run(_run())


@basic_auth.command("status")
@click.argument("subdomain")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key for authentication",
)
def basic_auth_status(subdomain: str, api_key: str | None) -> None:
    """Show HTTP Basic Auth status for a subdomain."""
    resolved_key = _resolve_api_key(api_key)

    async def _run() -> None:
        from hle_client.api import ApiClient, ApiClientConfig

        client = ApiClient(ApiClientConfig(api_key=resolved_key))
        try:
            data = await client.get_tunnel_basic_auth_status(subdomain)
        except Exception as exc:
            _handle_api_error(exc)
            return

        if data.get("enabled"):
            updated = data.get("updated_at", "")
            console.print(
                f"[cyan]{subdomain}[/cyan]: Basic Auth is [green]active[/green] "
                f"(user: [bold]{data.get('username', '')}[/bold])"
            )
            if updated:
                console.print(f"  Last updated: [dim]{updated}[/dim]")
        else:
            console.print(f"[cyan]{subdomain}[/cyan]: [dim]No Basic Auth set[/dim]")

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
