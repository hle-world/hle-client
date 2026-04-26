"""Implementation of the ``hle config`` command group.

Declarative tunnel configuration: read aggregate state, set ``auth_mode``,
or reconcile access rules to a desired set. Designed for IaC / CI/CD use
where the dashboard would otherwise be the only source of truth.

Subdomain resolution: callers supply a label (e.g. ``ha``) and the client
resolves it to ``<label>-<user_code>`` via ``GET /api/auth/me``. Custom
zone tunnels must be addressed by full subdomain.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import click
import httpx
from rich.console import Console
from rich.table import Table

from hle_client.api import ApiClient, ApiClientConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

console = Console()


def _parse_auth_spec(spec: str) -> tuple[str, str]:
    """Parse ``[provider:]email`` into ``(provider, email)``.

    Mirrors the helper in cli.py so config-cmd does not import from cli.
    """
    if ":" in spec:
        prefix, _, rest = spec.partition(":")
        if prefix in {"any", "google", "github", "hle"}:
            return prefix, rest
    return "any", spec


async def _resolve_subdomain(client: ApiClient, label: str) -> str:
    """Resolve ``label`` to ``<label>-<user_code>`` via the /me endpoint.

    Pre-resolved subdomains (those containing a ``-``) are returned as-is so
    custom zone tunnels work transparently.
    """
    if "-" in label:
        return label
    me = await client.get_me()
    user_code = me.get("user_code")
    if not user_code:
        raise click.ClickException("Could not resolve user_code from server")
    return f"{label}-{user_code}"


def _print_status(status: dict) -> None:
    """Render the aggregated tunnel-status payload."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Subdomain", status["subdomain"])
    table.add_row("URL", status["public_url"])
    table.add_row("Active", "[green]yes[/green]" if status["is_active"] else "[dim]no[/dim]")
    table.add_row("Auth mode", status["auth_mode"])
    if status.get("webhook_path"):
        table.add_row("Webhook path", status["webhook_path"])
    if status.get("zone"):
        table.add_row("Zone", status["zone"])
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
# Click command group
# ---------------------------------------------------------------------------


@click.group()
def config() -> None:
    """Declarative tunnel configuration (auth mode, access rules, etc.)."""


@config.command("show")
@click.argument("label")
@click.option("--api-key", default=None, help="Override the configured API key")
def show(label: str, api_key: str | None) -> None:
    """Show full configuration and live state for a tunnel."""
    asyncio.run(_show_async(label, api_key))


async def _show_async(label: str, api_key: str | None) -> None:
    api = ApiClient(ApiClientConfig(api_key=_require_key(api_key)))
    subdomain = await _resolve_subdomain(api, label)
    try:
        status = await api.get_tunnel_status(subdomain)
    except httpx.HTTPStatusError as exc:
        _die_http(exc, subdomain)
    _print_status(status)


@config.command("auth-mode")
@click.argument("label")
@click.option(
    "--set",
    "mode",
    type=click.Choice(["sso", "none"]),
    required=True,
    help="Auth mode to apply",
)
@click.option("--api-key", default=None, help="Override the configured API key")
def auth_mode(label: str, mode: str, api_key: str | None) -> None:
    """Set the SSO gate mode for a tunnel.

    Webhook tunnels are always public — the server rejects ``--set sso`` for
    them. The tunnel must have been registered at least once (``hle expose``)
    before its auth_mode can be changed.
    """
    asyncio.run(_auth_mode_async(label, mode, api_key))


async def _auth_mode_async(label: str, mode: str, api_key: str | None) -> None:
    api = ApiClient(ApiClientConfig(api_key=_require_key(api_key)))
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


@config.command("access")
@click.argument("label")
@click.option(
    "--replace",
    "replace_specs",
    multiple=True,
    metavar="[PROVIDER:]EMAIL",
    help="Reconcile rules to exactly this set (repeatable). Adds missing rules and removes extras.",
)
@click.option("--api-key", default=None, help="Override the configured API key")
def access(label: str, replace_specs: tuple[str, ...], api_key: str | None) -> None:
    """Reconcile a tunnel's access allow-list to a desired set.

    Unlike ``hle expose --allow``, which only adds rules, ``--replace`` is
    declarative: rules in the server but not in the flags are removed.
    """
    if not replace_specs:
        raise click.ClickException(
            "At least one --replace flag is required. "
            "Pass --replace '' explicitly to clear all rules."
        )
    asyncio.run(_access_async(label, replace_specs, api_key))


async def _access_async(label: str, replace_specs: tuple[str, ...], api_key: str | None) -> None:
    api = ApiClient(ApiClientConfig(api_key=_require_key(api_key)))
    subdomain = await _resolve_subdomain(api, label)

    desired: set[tuple[str, str]] = set()
    for spec in replace_specs:
        if not spec:
            continue
        provider, email = _parse_auth_spec(spec)
        desired.add((email.lower(), provider))

    try:
        existing = await api.list_access_rules(subdomain)
    except httpx.HTTPStatusError as exc:
        _die_http(exc, subdomain)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_key(api_key: str | None) -> str:
    """Return the resolved API key or raise a click error."""
    from hle_client.tunnel import _load_api_key

    resolved = api_key or _load_api_key()
    if not resolved:
        raise click.ClickException(
            "No API key found. Run 'hle auth login', set HLE_API_KEY, or pass --api-key."
        )
    return resolved


def _die_http(exc: httpx.HTTPStatusError, subdomain: str) -> None:
    code = exc.response.status_code
    if code == 403:
        raise click.ClickException(f"You do not own {subdomain!r}.") from None
    if code == 404:
        raise click.ClickException(f"Tunnel {subdomain!r} not found.") from None
    if code == 429:
        raise click.ClickException("Rate limited — try again shortly.") from None
    raise click.ClickException(f"Server returned {code}: {exc.response.text}") from None


def _all_commands() -> Iterable[click.Command]:
    """Iterate the commands so cli.py can attach the group cleanly."""
    return (config,)
