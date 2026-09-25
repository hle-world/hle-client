"""Implementation of the ``hle config`` command group.

All tunnel-scoped operations live here under a single declarative namespace.
Subdomain resolution: callers supply a label (e.g. ``ha``) and
``hle_client.ops.tunnels.resolve_subdomain`` turns it into
``<label>-<user_code>``. Pre-resolved subdomains (already ending in
``-<user_code>``) are passed through unchanged.

Every command here is a renderer: it takes its credential, output and
prompting policy from the invocation context (``hle_client.context``), calls
one function in ``hle_client.ops`` and prints what came back. The decisions
that need a person — confirm a delete, continue despite a conflicting gate —
are made here, not in ``ops``. Nothing here prints an error or exits; a
command raises an ``HleError`` for the root group to render.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import click

from hle_client.aliases import AliasedGroup
from hle_client.context import api, confirm, confirm_or_abort, out, prompt
from hle_client.errors import AbortedError, ConflictError, HleError, UsageError
from hle_client.ops import access as ops_access
from hle_client.ops import auth as ops_auth
from hle_client.ops import tunnels as ops_tunnels
from hle_client.ops.models import Conflict, TunnelDetail
from hle_client.richcompat import Console, Table

if TYPE_CHECKING:
    from hle_client.api import ApiClient

console = Console()

F = TypeVar("F", bound=Callable[..., Any])

# Kept as names because other modules import them; the definitions live in ops.
_VALID_AUTH_PROVIDERS = ops_access.VALID_PROVIDERS


def _parse_auth_spec(spec: str) -> tuple[str, str]:
    """Parse ``[provider:]email`` into ``(provider, email)``."""
    rule = ops_access.parse_spec(spec)
    return rule.provider, rule.email


def _api_key_option(f: F) -> F:
    # Kept on every leaf because the option is part of the published grammar
    # (the tree snapshot pins it, envvar and all). The value feeds the same
    # resolver as the root's, so the two can no longer disagree.
    return click.option(
        "--api-key",
        default=None,
        envvar="HLE_API_KEY",
        help="API key for authentication",
    )(f)


# ---------------------------------------------------------------------------
# Conflict warnings (gate types are mutually exclusive)
# ---------------------------------------------------------------------------


async def _warn_if_basic_auth_active(ctx: click.Context, client: ApiClient, subdomain: str) -> None:
    try:
        status = await ops_auth.basic_auth_status(client, subdomain)
    except HleError:
        return
    if not status.enabled:
        return
    console.print(
        f"[yellow]Warning:[/yellow] Basic Auth is currently active on "
        f"[cyan]{subdomain}[/cyan].\n"
        "  Email rules and PIN are bypassed while it's active.\n"
        "  Remove Basic Auth first ([dim]hle tunnel basic-auth delete "
        f"{subdomain}[/dim]) to re-enable SSO/PIN access control."
    )
    if not confirm(ctx, "  Continue anyway?", default=False):
        raise AbortedError()


async def _warn_if_pin_or_rules_exist(
    ctx: click.Context, client: ApiClient, subdomain: str
) -> None:
    conflicts: list[str] = []
    try:
        if (await ops_auth.pin_status(client, subdomain)).enabled:
            conflicts.append("an active PIN")
    except HleError:
        pass
    try:
        rules = await ops_access.get_access(client, subdomain)
        if rules:
            n = len(rules)
            conflicts.append(f"{n} email rule{'s' if n > 1 else ''}")
    except HleError:
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
    if not confirm(ctx, "  Continue?", default=False):
        raise AbortedError()


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def _print_status(detail: TunnelDetail) -> None:
    t = detail.tunnel
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Subdomain", t.subdomain)
    table.add_row("URL", t.public_url)
    table.add_row("Active", "[green]yes[/green]" if t.online else "[dim]no[/dim]")
    table.add_row("Auth mode", t.auth_mode)
    if t.webhook_path:
        table.add_row("Webhook path", t.webhook_path)
    if t.client_version:
        table.add_row("Client", t.client_version)

    if detail.access_rules:
        rule_lines = [f"{r.email} ({r.provider})" for r in detail.access_rules]
        table.add_row("Access rules", "\n".join(rule_lines))
    else:
        table.add_row("Access rules", "[dim]none[/dim]")

    table.add_row("PIN", "[green]set[/green]" if detail.pin.enabled else "[dim]none[/dim]")

    ba = detail.basic_auth
    if ba.enabled:
        table.add_row("Basic auth", f"[green]enabled[/green] ({ba.username or '?'})")
    else:
        table.add_row("Basic auth", "[dim]none[/dim]")

    table.add_row(
        "Protected",
        "[green]yes[/green]" if detail.protected else "[yellow]no[/yellow]",
    )

    console.print(table)


# ---------------------------------------------------------------------------
# Top-level config group
# ---------------------------------------------------------------------------


@click.group(cls=AliasedGroup, aliases={"show": "get"}, hidden_aliases={"auth-mode": "set"})
def config() -> None:
    """Create, inspect and secure tunnels.

    \b
    Examples:
      hle tunnel create ha http://localhost:8123   Run a tunnel
      hle tunnel list                              What is published
      hle tunnel get ha                            One tunnel, in full
      hle tunnel set ha --auth none                Make it public
      hle tunnel access create ha you@example.com  Let someone in
      hle tunnel delete ha                         Remove the record

    \b
    access, pin, basic-auth and share use the same verbs as tunnel itself:
    list, get, create, set, delete.
    """


# ---- get / list -----------------------------------------------------------


@config.command("get")
@click.argument("label")
@_api_key_option
@click.pass_context
def show_cmd(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Show full configuration and live state for a tunnel.

    \b
    Example:
      hle tunnel get ha
      hle tunnel get ha -o json
    """
    o = out(ctx)

    async def _run() -> None:
        detail = await ops_tunnels.get_tunnel(api(ctx, api_key), label)
        if o.json_mode:
            o.data(detail.raw)
            return
        _print_status(detail)

    asyncio.run(_run())


@config.command("list")
@_api_key_option
@click.pass_context
def list_cmd(ctx: click.Context, api_key: str | None) -> None:
    """List active tunnels for your account.

    \b
    Example:
      hle tunnel list
      hle tunnel list -o json | jq '.[].subdomain'
    """
    o = out(ctx)

    async def _run() -> None:
        tunnels = await ops_tunnels.list_tunnels(api(ctx, api_key))

        if o.json_mode:
            o.data([t.raw for t in tunnels])
            return

        if not tunnels:
            console.print("[dim]No active tunnels.[/dim]")
            return

        table = Table(title="Active Tunnels")
        table.add_column("Subdomain", style="cyan")
        table.add_column("Service URL")
        table.add_column("WebSocket")
        table.add_column("Connected At", style="dim")
        for t in tunnels:
            table.add_row(
                t.subdomain,
                t.service_url,
                "yes" if t.websocket_enabled else "no",
                t.connected_at or "",
            )
        console.print(table)

    asyncio.run(_run())


@config.command("delete")
@click.argument("label")
@click.option("--yes", "-y", is_flag=True, default=False, help="Do not ask for confirmation.")
@_api_key_option
@click.pass_context
def delete_cmd(ctx: click.Context, label: str, yes: bool, api_key: str | None) -> None:
    """Delete a tunnel's record.

    Creating without deleting was the gap people hit first: a tunnel could be
    published but never taken back, and its access rules outlived any memory
    of it.

    A live tunnel is refused rather than disconnected first. Asking the relay
    to drop the connection does not stop anything — the close it sends is not
    fatal, so the client reconnects — and deleting the record out from under a
    connection that is about to come back is a race, not a feature. Stop the
    client instead.

    \b
    Example:
      hle tunnel delete ha
    """
    o = out(ctx)

    async def _run() -> None:
        client = api(ctx, api_key)
        tunnel = await ops_tunnels.find_tunnel(client, label)

        if not yes:
            # Deleting takes the access rules with it, which is the part that
            # is not obvious and not recoverable.
            confirm_or_abort(ctx, f"Delete {tunnel.subdomain} and its access rules?", default=False)

        result = await ops_tunnels.delete_tunnel(client, tunnel)
        if isinstance(result, Conflict):
            # Nobody to ask: the relay refuses a live tunnel whatever we say,
            # so a prompt here would only lead to a 409.
            raise ConflictError(result.reason, hint=result.hint)

        if o.json_mode:
            o.data({"subdomain": result.subject, "deleted": True})
            return
        o.print(f"[green]Deleted[/green] {result.subject}")

    asyncio.run(_run())


# ---- set ------------------------------------------------------------------


@config.command("set")
@click.argument("label")
@click.option(
    "--auth",
    "mode",
    type=click.Choice(["sso", "none"]),
    default=None,
    help="Gate mode: sso (visitors sign in) or none (public).",
)
# `auth-mode LABEL --set X` resolves here (a hidden alias), so the flag it
# was spelled with has to parse here too.
@click.option(
    "--set",
    "legacy_mode",
    type=click.Choice(["sso", "none"]),
    default=None,
    hidden=True,
)
@_api_key_option
@click.pass_context
def set_cmd(
    ctx: click.Context,
    label: str,
    mode: str | None,
    legacy_mode: str | None,
    api_key: str | None,
) -> None:
    """Change a published tunnel's settings.

    The one verb for changing a tunnel in place. `auth-mode LABEL --set X`
    was a noun of its own with its value behind an option named after the
    verb; it still works, spelled the old way.

    \b
    Example:
      hle tunnel set ha --auth sso     SSO gate on
      hle tunnel set ha --auth none    Anyone with the URL gets in
    """
    if mode is not None and legacy_mode is not None and mode != legacy_mode:
        raise UsageError("--auth and --set disagree; pass one.")
    chosen = mode or legacy_mode
    if chosen is None:
        raise UsageError("Nothing to set.", hint="hle tunnel set LABEL --auth sso|none")

    async def _run() -> None:
        subdomain = await ops_tunnels.set_auth_mode(api(ctx, api_key), label, chosen)
        console.print(f"[green]✓[/green] {subdomain} auth_mode = {chosen}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# access subgroup
# ---------------------------------------------------------------------------


@config.group("access", cls=AliasedGroup, hidden_aliases={"add": "create", "remove": "delete"})
def access_grp() -> None:
    """Manage tunnel access allow-list (SSO email rules)."""


@access_grp.command("list")
@click.argument("label")
@_api_key_option
@click.pass_context
def access_list(ctx: click.Context, label: str, api_key: str | None) -> None:
    """List access rules for a tunnel."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        rules = await ops_access.get_access(client, subdomain)

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
                "" if r.id is None else str(r.id),
                r.email,
                r.provider,
                r.created_at or "",
            )
        console.print(table)

    asyncio.run(_run())


@access_grp.command("create")
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
@click.pass_context
def access_add(
    ctx: click.Context, label: str, email: str, provider: str, api_key: str | None
) -> None:
    """Add an email to a tunnel's access allow-list."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        await _warn_if_basic_auth_active(ctx, client, subdomain)
        rule = await ops_access.add_rule(client, subdomain, email, provider)
        console.print(
            f"[green]Added[/green] {rule.email} (provider={rule.provider}) to {subdomain}"
        )

    asyncio.run(_run())


@access_grp.command("delete")
@click.argument("label")
@click.argument("rule_id", type=int)
@_api_key_option
@click.pass_context
def access_remove(ctx: click.Context, label: str, rule_id: int, api_key: str | None) -> None:
    """Remove an access rule by ID."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        await ops_access.remove_rule(client, subdomain, rule_id)
        console.print(f"[green]Removed[/green] rule {rule_id} from {subdomain}")

    asyncio.run(_run())


@access_grp.command("replace")
@click.argument("label")
@click.argument("specs", nargs=-1)
@click.option("--clear", "do_clear", is_flag=True, help="Remove all rules (no specs allowed)")
@_api_key_option
@click.pass_context
def access_replace(
    ctx: click.Context,
    label: str,
    specs: tuple[str, ...],
    do_clear: bool,
    api_key: str | None,
) -> None:
    """Reconcile a tunnel's access allow-list to exactly SPECS.

    Adds missing rules and removes extras. Use --clear to remove all rules.

    Example:

        hle tunnel access replace ha google:alice@x.com github:bob@y.com
    """
    if do_clear and specs:
        raise UsageError("Pass either SPECS or --clear, not both.")
    if not specs and not do_clear:
        raise UsageError("At least one spec is required, or pass --clear to remove all rules.")

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        desired = [ops_access.parse_spec(spec) for spec in specs if spec]

        diff = await ops_access.set_access(client, subdomain, desired)

        for rule in diff.added:
            console.print(f"  [green]+[/green] {rule.email} ({rule.provider})")
        for rule in diff.removed:
            console.print(f"  [red]-[/red] {rule.email} ({rule.provider})")
        for rule, _reason, code in diff.failed:
            verb = "remove " if rule.id is not None else ""
            console.print(f"  [yellow]! {verb}{rule.email} failed: {code}[/yellow]")

        if diff.in_sync:
            n = len({r.key for r in desired})
            console.print(f"[dim]{subdomain} access rules already in sync ({n} rule(s))[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# pin subgroup
# ---------------------------------------------------------------------------


@config.group("pin", cls=AliasedGroup, hidden_aliases={"status": "get", "remove": "delete"})
def pin_grp() -> None:
    """Manage tunnel PIN access control."""


@pin_grp.command("set")
@click.argument("label")
@_api_key_option
@click.pass_context
def pin_set(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Set a PIN for a tunnel (prompts for 4-8 digit PIN)."""
    pin_value = str(prompt(ctx, "Enter PIN (4-8 digits)", hide_input=True))
    if not pin_value.isdigit() or not (4 <= len(pin_value) <= 8):
        raise HleError("PIN must be 4-8 digits.")
    pin_confirm = str(prompt(ctx, "Confirm PIN", hide_input=True))
    if pin_value != pin_confirm:
        raise HleError("PINs do not match.")

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        await _warn_if_basic_auth_active(ctx, client, subdomain)
        await ops_auth.set_pin(client, subdomain, pin_value)
        console.print(f"[green]PIN set[/green] for {subdomain}")

    asyncio.run(_run())


@pin_grp.command("delete")
@click.argument("label")
@_api_key_option
@click.pass_context
def pin_remove(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Remove the PIN for a tunnel."""

    async def _run() -> None:
        subdomain = await ops_auth.remove_pin(api(ctx, api_key), label)
        console.print(f"[green]PIN removed[/green] from {subdomain}")

    asyncio.run(_run())


@pin_grp.command("get")
@click.argument("label")
@_api_key_option
@click.pass_context
def pin_status(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Show PIN status for a tunnel."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        status = await ops_auth.pin_status(client, subdomain)
        if status.enabled:
            console.print(f"[cyan]{subdomain}[/cyan]: PIN is [green]active[/green]")
            if status.updated_at:
                console.print(f"  Last updated: [dim]{status.updated_at}[/dim]")
        else:
            console.print(f"[cyan]{subdomain}[/cyan]: [dim]No PIN set[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# basic-auth subgroup
# ---------------------------------------------------------------------------


@config.group("basic-auth", cls=AliasedGroup, hidden_aliases={"status": "get", "remove": "delete"})
def basic_auth_grp() -> None:
    """Manage tunnel HTTP Basic Auth access control."""


@basic_auth_grp.command("set")
@click.argument("label")
@_api_key_option
@click.pass_context
def basic_auth_set(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Set HTTP Basic Auth credentials for a tunnel."""
    username = str(prompt(ctx, "Username"))
    if not username.strip():
        raise HleError("Username cannot be empty.")
    if ":" in username:
        raise HleError("Username must not contain ':'.")

    password = str(prompt(ctx, "Password (min 8 chars)", hide_input=True))
    if len(password) < 8:
        raise HleError("Password must be at least 8 characters.")
    password_confirm = str(prompt(ctx, "Confirm password", hide_input=True))
    if password != password_confirm:
        raise HleError("Passwords do not match.")

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        await _warn_if_pin_or_rules_exist(ctx, client, subdomain)
        await ops_auth.set_basic_auth(client, subdomain, username.strip(), password)
        console.print(f"[green]Basic Auth set[/green] for {subdomain} (user: {username.strip()})")

    asyncio.run(_run())


@basic_auth_grp.command("delete")
@click.argument("label")
@_api_key_option
@click.pass_context
def basic_auth_remove(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Remove HTTP Basic Auth from a tunnel."""

    async def _run() -> None:
        subdomain = await ops_auth.remove_basic_auth(api(ctx, api_key), label)
        console.print(f"[green]Basic Auth removed[/green] from {subdomain}")

    asyncio.run(_run())


@basic_auth_grp.command("get")
@click.argument("label")
@_api_key_option
@click.pass_context
def basic_auth_status(ctx: click.Context, label: str, api_key: str | None) -> None:
    """Show HTTP Basic Auth status for a tunnel."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        status = await ops_auth.basic_auth_status(client, subdomain)
        if status.enabled:
            console.print(
                f"[cyan]{subdomain}[/cyan]: Basic Auth is [green]active[/green] "
                f"(user: [bold]{status.username or ''}[/bold])"
            )
            if status.updated_at:
                console.print(f"  Last updated: [dim]{status.updated_at}[/dim]")
        else:
            console.print(f"[cyan]{subdomain}[/cyan]: [dim]No Basic Auth set[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# share subgroup
# ---------------------------------------------------------------------------


@config.group("share", cls=AliasedGroup, hidden_aliases={"revoke": "delete"})
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
@click.option("--name", "link_name", default=None, help="Optional name for the link")
# The old spelling. `--label` names the tunnel everywhere else, and here it
# named the link instead, right next to a LABEL argument that is the tunnel.
@click.option("--label", "legacy_link_label", default=None, hidden=True)
@click.option("--max-uses", default=None, type=int, help="Maximum number of uses")
@_api_key_option
@click.pass_context
def share_create(
    ctx: click.Context,
    label: str,
    duration: str,
    link_name: str | None,
    legacy_link_label: str | None,
    max_uses: int | None,
    api_key: str | None,
) -> None:
    """Create a temporary share link for a tunnel."""
    if link_name is not None and legacy_link_label is not None and link_name != legacy_link_label:
        raise UsageError("--name and --label both name the link; pass one.")
    link_label = link_name if link_name is not None else (legacy_link_label or "")

    async def _run() -> None:
        link = await ops_auth.create_share(
            api(ctx, api_key), label, duration=duration, label=link_label, max_uses=max_uses
        )

        console.print()
        console.print("[green bold]Share link created![/green bold]")
        console.print()
        console.print(f"  [cyan]{link.share_url}[/cyan]")
        console.print()
        if link.label:
            console.print(f"  Label:   {link.label}")
        console.print(f"  Expires: {link.expires_at}")
        if link.max_uses:
            console.print(f"  Max uses: {link.max_uses}")
        console.print()
        console.print("[dim]This URL will not be shown again.[/dim]")

    asyncio.run(_run())


@share_grp.command("list")
@click.argument("label")
@_api_key_option
@click.pass_context
def share_list(ctx: click.Context, label: str, api_key: str | None) -> None:
    """List share links for a tunnel."""

    async def _run() -> None:
        client = api(ctx, api_key)
        subdomain = await ops_tunnels.resolve_subdomain(client, label)
        links = await ops_auth.list_shares(client, subdomain)

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
            uses = str(link.use_count)
            if link.max_uses:
                uses += f"/{link.max_uses}"
            status = "[green]Active[/green]" if link.active else "[red]Revoked[/red]"
            table.add_row(
                "" if link.id is None else str(link.id),
                link.label or "-",
                link.token_prefix,
                link.expires_at or "",
                uses,
                status,
            )
        console.print(table)

    asyncio.run(_run())


@share_grp.command("delete")
@click.argument("label")
@click.argument("link_id", type=int)
@_api_key_option
@click.pass_context
def share_revoke(ctx: click.Context, label: str, link_id: int, api_key: str | None) -> None:
    """Revoke a share link by ID."""

    async def _run() -> None:
        subdomain = await ops_auth.revoke_share(api(ctx, api_key), label, link_id)
        console.print(f"[green]Revoked[/green] share link {link_id} from {subdomain}")

    asyncio.run(_run())
