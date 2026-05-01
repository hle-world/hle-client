"""Implementation of the ``hle config`` command group.

All tunnel-scoped operations live here under a single declarative namespace.
Subdomain resolution: callers supply a label (e.g. ``ha``) and the client
resolves it to ``<label>-<user_code>`` via ``GET /api/auth/me``. Pre-resolved
subdomains (containing ``-``) are passed through unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import click
import httpx
from rich.console import Console
from rich.table import Table

from hle_client.api import ApiClient, ApiClientConfig

console = Console()

F = TypeVar("F", bound=Callable[..., Any])

_VALID_AUTH_PROVIDERS = {"any", "google", "github", "hle"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_auth_spec(spec: str) -> tuple[str, str]:
    """Parse ``[provider:]email`` into ``(provider, email)``."""
    if ":" in spec:
        prefix, _, rest = spec.partition(":")
        if prefix in _VALID_AUTH_PROVIDERS:
            return prefix, rest
    return "any", spec


async def _resolve_subdomain(client: ApiClient, label: str) -> str:
    """Resolve ``label`` → ``<label>-<user_code>``. Pass through if already resolved."""
    if "-" in label:
        return label
    me = await client.get_me()
    user_code = me.get("user_code")
    if not user_code:
        raise click.ClickException("Could not resolve user_code from server")
    return f"{label}-{user_code}"


def _require_key(api_key: str | None) -> str:
    from hle_client.tunnel import _load_api_key

    resolved = api_key or _load_api_key()
    if not resolved:
        raise click.ClickException(
            "No API key found. Run 'hle auth login', set HLE_API_KEY, or pass --api-key."
        )
    return resolved


def _client(api_key: str | None) -> ApiClient:
    return ApiClient(ApiClientConfig(api_key=_require_key(api_key)))


def _die_http(exc: httpx.HTTPStatusError, subdomain: str | None = None) -> None:
    code = exc.response.status_code
    if code == 401:
        raise click.ClickException("Invalid or missing API key.") from None
    if code == 403:
        raise click.ClickException(
            f"You do not own {subdomain!r}." if subdomain else "Forbidden."
        ) from None
    if code == 404:
        raise click.ClickException(
            f"Tunnel {subdomain!r} not found." if subdomain else "Resource not found."
        ) from None
    if code == 409:
        raise click.ClickException("Email already in access list.") from None
    if code == 429:
        raise click.ClickException("Rate limited — try again shortly.") from None
    raise click.ClickException(f"Server returned {code}: {exc.response.text[:200]}") from None


def _handle_exc(exc: Exception, subdomain: str | None = None) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        _die_http(exc, subdomain)
    if isinstance(exc, httpx.ConnectError):
        raise click.ClickException("Could not connect to relay server.") from None
    raise click.ClickException(str(exc)) from None


def _api_key_option(f: F) -> F:
    return click.option(
        "--api-key",
        default=None,
        envvar="HLE_API_KEY",
        help="API key for authentication",
    )(f)


# ---------------------------------------------------------------------------
# Conflict warnings (gate types are mutually exclusive)
# ---------------------------------------------------------------------------


async def _warn_if_basic_auth_active(client: ApiClient, subdomain: str) -> None:
    try:
        data = await client.get_tunnel_basic_auth_status(subdomain)
    except Exception:
        return
    if not data.get("enabled"):
        return
    console.print(
        f"[yellow]Warning:[/yellow] Basic Auth is currently active on "
        f"[cyan]{subdomain}[/cyan].\n"
        "  Email rules and PIN are bypassed while it's active.\n"
        "  Remove Basic Auth first ([dim]hle config basic-auth remove "
        f"{subdomain}[/dim]) to re-enable SSO/PIN access control."
    )
    if not click.confirm("  Continue anyway?", default=False):
        raise SystemExit(0)


async def _warn_if_pin_or_rules_exist(client: ApiClient, subdomain: str) -> None:
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
    if not conflicts:
        return
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
# Status rendering
# ---------------------------------------------------------------------------


def _print_status(status: dict[str, Any]) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Subdomain", status["subdomain"])
    table.add_row("URL", status["public_url"])
    table.add_row("Active", "[green]yes[/green]" if status["is_active"] else "[dim]no[/dim]")
    table.add_row("Auth mode", status["auth_mode"])
    if status.get("webhook_path"):
        table.add_row("Webhook path", status["webhook_path"])
    if status.get("client_version"):
        table.add_row("Client", status["client_version"])

    rules = status.get("access_rules", [])
    if rules:
        rule_lines = [f"{r['allowed_email']} ({r['provider']})" for r in rules]
        table.add_row("Access rules", "\n".join(rule_lines))
    else:
        table.add_row("Access rules", "[dim]none[/dim]")

    pin = status.get("pin", {})
    table.add_row("PIN", "[green]set[/green]" if pin.get("has_pin") else "[dim]none[/dim]")

    ba = status.get("basic_auth", {})
    if ba.get("enabled"):
        table.add_row("Basic auth", f"[green]enabled[/green] ({ba.get('username', '?')})")
    else:
        table.add_row("Basic auth", "[dim]none[/dim]")

    table.add_row(
        "Protected",
        "[green]yes[/green]" if status.get("is_protected") else "[yellow]no[/yellow]",
    )

    console.print(table)


# ---------------------------------------------------------------------------
# Top-level config group
# ---------------------------------------------------------------------------


@click.group()
def config() -> None:
    """Configure tunnels (auth mode, access, pin, basic-auth, share)."""


# ---- show / list ----------------------------------------------------------


@config.command("show")
@click.argument("label")
@_api_key_option
def show_cmd(label: str, api_key: str | None) -> None:
    """Show full configuration and live state for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            status = await api.get_tunnel_status(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        _print_status(status)

    asyncio.run(_run())


@config.command("list")
@_api_key_option
def list_cmd(api_key: str | None) -> None:
    """List active tunnels for your account."""

    async def _run() -> None:
        api = _client(api_key)
        try:
            tunnel_list = await api.list_tunnels()
        except Exception as exc:
            _handle_exc(exc)

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


# ---- auth-mode ------------------------------------------------------------


@config.command("auth-mode")
@click.argument("label")
@click.option(
    "--set",
    "mode",
    type=click.Choice(["sso", "none"]),
    required=True,
    help="Auth mode to apply",
)
@_api_key_option
def auth_mode_cmd(label: str, mode: str, api_key: str | None) -> None:
    """Set the SSO gate mode for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            await api.set_tunnel_auth_mode(subdomain, mode)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise click.ClickException(
                    f"Tunnel {subdomain!r} has never been registered. "
                    "Run 'hle expose' once to create it, then re-run this command."
                ) from None
            if exc.response.status_code == 400 and b"Webhook" in exc.response.content:
                raise click.ClickException(
                    "Webhook tunnels are always public — auth_mode cannot be changed."
                ) from None
            _die_http(exc, subdomain)
        console.print(f"[green]✓[/green] {subdomain} auth_mode = {mode}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# access subgroup
# ---------------------------------------------------------------------------


@config.group("access")
def access_grp() -> None:
    """Manage tunnel access allow-list (SSO email rules)."""


@access_grp.command("list")
@click.argument("label")
@_api_key_option
def access_list(label: str, api_key: str | None) -> None:
    """List access rules for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            rules = await api.list_access_rules(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)

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


@access_grp.command("add")
@click.argument("label")
@click.argument("email")
@click.option(
    "--provider",
    type=click.Choice(["any", "google", "github", "hle"]),
    default="any",
    show_default=True,
    help="Required auth provider",
)
@_api_key_option
def access_add(label: str, email: str, provider: str, api_key: str | None) -> None:
    """Add an email to a tunnel's access allow-list."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        await _warn_if_basic_auth_active(api, subdomain)
        try:
            rule = await api.add_access_rule(subdomain, email, provider)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(
            f"[green]Added[/green] {rule.get('allowed_email', email)} "
            f"(provider={rule.get('provider', provider)}) to {subdomain}"
        )

    asyncio.run(_run())


@access_grp.command("remove")
@click.argument("label")
@click.argument("rule_id", type=int)
@_api_key_option
def access_remove(label: str, rule_id: int, api_key: str | None) -> None:
    """Remove an access rule by ID."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            await api.delete_access_rule(subdomain, rule_id)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]Removed[/green] rule {rule_id} from {subdomain}")

    asyncio.run(_run())


@access_grp.command("replace")
@click.argument("label")
@click.argument("specs", nargs=-1)
@click.option("--clear", "do_clear", is_flag=True, help="Remove all rules (no specs allowed)")
@_api_key_option
def access_replace(
    label: str,
    specs: tuple[str, ...],
    do_clear: bool,
    api_key: str | None,
) -> None:
    """Reconcile a tunnel's access allow-list to exactly SPECS.

    Adds missing rules and removes extras. Use --clear to remove all rules.

    Example:

        hle config access replace ha google:alice@x.com github:bob@y.com
    """
    if do_clear and specs:
        raise click.ClickException("Pass either SPECS or --clear, not both.")
    if not specs and not do_clear:
        raise click.ClickException(
            "At least one spec is required, or pass --clear to remove all rules."
        )

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)

        desired: set[tuple[str, str]] = set()
        for spec in specs:
            if not spec:
                continue
            provider, email = _parse_auth_spec(spec)
            desired.add((email.lower(), provider))

        try:
            existing = await api.list_access_rules(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)

        existing_by_key = {(r["allowed_email"].lower(), r["provider"]): r["id"] for r in existing}
        existing_keys = set(existing_by_key.keys())

        to_add = desired - existing_keys
        to_remove = existing_keys - desired

        for email, provider in sorted(to_add):
            try:
                await api.add_access_rule(subdomain, email, provider)
                console.print(f"  [green]+[/green] {email} ({provider})")
            except httpx.HTTPStatusError as exc:
                console.print(f"  [yellow]! {email} failed: {exc.response.status_code}[/yellow]")

        for key in sorted(to_remove):
            rule_id = existing_by_key[key]
            try:
                await api.delete_access_rule(subdomain, rule_id)
                console.print(f"  [red]-[/red] {key[0]} ({key[1]})")
            except httpx.HTTPStatusError as exc:
                console.print(
                    f"  [yellow]! remove {key[0]} failed: {exc.response.status_code}[/yellow]"
                )

        if not to_add and not to_remove:
            console.print(
                f"[dim]{subdomain} access rules already in sync ({len(desired)} rule(s))[/dim]"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# pin subgroup
# ---------------------------------------------------------------------------


@config.group("pin")
def pin_grp() -> None:
    """Manage tunnel PIN access control."""


@pin_grp.command("set")
@click.argument("label")
@_api_key_option
def pin_set(label: str, api_key: str | None) -> None:
    """Set a PIN for a tunnel (prompts for 4-8 digit PIN)."""
    pin_value = click.prompt("Enter PIN (4-8 digits)", hide_input=True)
    if not pin_value.isdigit() or not (4 <= len(pin_value) <= 8):
        raise click.ClickException("PIN must be 4-8 digits.")
    pin_confirm = click.prompt("Confirm PIN", hide_input=True)
    if pin_value != pin_confirm:
        raise click.ClickException("PINs do not match.")

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        await _warn_if_basic_auth_active(api, subdomain)
        try:
            await api.set_tunnel_pin(subdomain, pin_value)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]PIN set[/green] for {subdomain}")

    asyncio.run(_run())


@pin_grp.command("remove")
@click.argument("label")
@_api_key_option
def pin_remove(label: str, api_key: str | None) -> None:
    """Remove the PIN for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            await api.remove_tunnel_pin(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]PIN removed[/green] from {subdomain}")

    asyncio.run(_run())


@pin_grp.command("status")
@click.argument("label")
@_api_key_option
def pin_status(label: str, api_key: str | None) -> None:
    """Show PIN status for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            data = await api.get_tunnel_pin_status(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        if data.get("has_pin"):
            updated = data.get("updated_at", "")
            console.print(f"[cyan]{subdomain}[/cyan]: PIN is [green]active[/green]")
            if updated:
                console.print(f"  Last updated: [dim]{updated}[/dim]")
        else:
            console.print(f"[cyan]{subdomain}[/cyan]: [dim]No PIN set[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# basic-auth subgroup
# ---------------------------------------------------------------------------


@config.group("basic-auth")
def basic_auth_grp() -> None:
    """Manage tunnel HTTP Basic Auth access control."""


@basic_auth_grp.command("set")
@click.argument("label")
@_api_key_option
def basic_auth_set(label: str, api_key: str | None) -> None:
    """Set HTTP Basic Auth credentials for a tunnel."""
    username = click.prompt("Username")
    if not username.strip():
        raise click.ClickException("Username cannot be empty.")
    if ":" in username:
        raise click.ClickException("Username must not contain ':'.")

    password = click.prompt("Password (min 8 chars)", hide_input=True)
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters.")
    password_confirm = click.prompt("Confirm password", hide_input=True)
    if password != password_confirm:
        raise click.ClickException("Passwords do not match.")

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        await _warn_if_pin_or_rules_exist(api, subdomain)
        try:
            await api.set_tunnel_basic_auth(subdomain, username.strip(), password)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]Basic Auth set[/green] for {subdomain} (user: {username.strip()})")

    asyncio.run(_run())


@basic_auth_grp.command("remove")
@click.argument("label")
@_api_key_option
def basic_auth_remove(label: str, api_key: str | None) -> None:
    """Remove HTTP Basic Auth from a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            await api.remove_tunnel_basic_auth(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]Basic Auth removed[/green] from {subdomain}")

    asyncio.run(_run())


@basic_auth_grp.command("status")
@click.argument("label")
@_api_key_option
def basic_auth_status(label: str, api_key: str | None) -> None:
    """Show HTTP Basic Auth status for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            data = await api.get_tunnel_basic_auth_status(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)
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


# ---------------------------------------------------------------------------
# share subgroup
# ---------------------------------------------------------------------------


@config.group("share")
def share_grp() -> None:
    """Manage temporary share links for a tunnel."""


@share_grp.command("create")
@click.argument("label")
@click.option(
    "--duration",
    type=click.Choice(["1h", "24h", "7d"]),
    default="24h",
    show_default=True,
    help="Link validity duration",
)
@click.option("--label", "link_label", default="", help="Optional label for the link")
@click.option("--max-uses", default=None, type=int, help="Maximum number of uses")
@_api_key_option
def share_create(
    label: str,
    duration: str,
    link_label: str,
    max_uses: int | None,
    api_key: str | None,
) -> None:
    """Create a temporary share link for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            result = await api.create_share_link(subdomain, duration, link_label, max_uses)
        except Exception as exc:
            _handle_exc(exc, subdomain)

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


@share_grp.command("list")
@click.argument("label")
@_api_key_option
def share_list(label: str, api_key: str | None) -> None:
    """List share links for a tunnel."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            links = await api.list_share_links(subdomain)
        except Exception as exc:
            _handle_exc(exc, subdomain)

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


@share_grp.command("revoke")
@click.argument("label")
@click.argument("link_id", type=int)
@_api_key_option
def share_revoke(label: str, link_id: int, api_key: str | None) -> None:
    """Revoke a share link by ID."""

    async def _run() -> None:
        api = _client(api_key)
        subdomain = await _resolve_subdomain(api, label)
        try:
            await api.delete_share_link(subdomain, link_id)
        except Exception as exc:
            _handle_exc(exc, subdomain)
        console.print(f"[green]Revoked[/green] share link {link_id} from {subdomain}")

    asyncio.run(_run())
