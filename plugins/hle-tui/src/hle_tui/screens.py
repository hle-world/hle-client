"""The panes and screens pushed over the three tables.

Every relay call made from here goes through :func:`attempt`, which turns a
typed ``HleError`` into a status line instead of a traceback. A dashboard
that exits because one PIN was refused is worse than no dashboard: the user
loses the screen they were acting on and learns nothing about why.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, TypeVar

from hle_client.errors import HleError
from hle_client.ops import access as ops_access
from hle_client.ops import auth as ops_auth
from hle_client.ops import tunnels as ops_tunnels
from rich.text import Text
from textual import work
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Log, Select, Static

from hle_tui import data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hle_client.ops.models import AccessRule, Daemon, ShareLink, Tunnel
    from textual.app import App, ComposeResult

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


class Confirm(ModalScreen[bool]):
    """A yes/no the user has to mean."""

    CSS = """
    Confirm { align: center middle; }
    #box { width: 64; height: auto; border: thick $warning; padding: 1 2; background: $surface; }
    #buttons { height: auto; padding-top: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str, *, yes: str = "Delete") -> None:
        super().__init__()
        self.question = question
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Label(self.question, markup=False)
            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="primary", id="no")
                yield Button(self.yes, variant="error", id="yes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


def say(app: App[Any], message: str, cli: str | None = None) -> None:
    # The app owns the status line; this only forwards to it.
    app.say(message, cli=cli)  # type: ignore[attr-defined]


async def attempt(
    app: App[Any],
    call: Callable[[], Awaitable[T]],
    *,
    done: str | Callable[[T], str] | None = None,
    cli: str | None = None,
) -> tuple[bool, T | None]:
    """Run one relay call. On failure, say why and return ``(False, None)``.

    ``call`` is a zero-argument factory rather than a coroutine so that
    building the call (reading ``store.api``, which raises when there is no
    key) fails inside the guard too.
    """
    try:
        result = await call()
    except HleError as exc:
        say(app, data.describe_error(exc))
        return False, None
    except Exception as exc:  # noqa: BLE001 — a bug should be a line, not an exit
        say(app, f"Unexpected error: {exc.__class__.__name__}: {exc}")
        return False, None
    if done is not None:
        say(app, done(result) if callable(done) else done, cli=cli)
    return True, result


def selected_key(table: DataTable[Any]) -> str | None:
    """The row key under the cursor — the thing to act on, whatever its index."""
    if table.row_count == 0:
        return None
    value = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Tunnel detail
# ---------------------------------------------------------------------------


MAX_PANE_ROWS = 8


def fit_rows(table: DataTable[Any]) -> None:
    # ``height: auto`` on a DataTable inside a scroll view showed one row of
    # two; size it to its rows (plus the header), capped so a long allow-list
    # scrolls inside its table instead of pushing the PIN section off screen.
    table.styles.height = 1 + max(1, min(table.row_count, MAX_PANE_ROWS))


def _rule_key(rule: AccessRule) -> str:
    return str(rule.id) if rule.id is not None else f"{rule.email}/{rule.provider}"


def tunnel_head(tunnel: Tunnel) -> Text:
    text = Text()
    text.append(tunnel.subdomain, style="bold")
    text.append("  ")
    text.append("live" if tunnel.online else "idle", style="green" if tunnel.online else "dim")
    text.append(f"\n{tunnel.public_url}\n")
    text.append(f"Service  {tunnel.service_url or '-'}\n")
    text.append(f"Gate     {tunnel.auth_mode or '-'}")
    return text


class TunnelPane(VerticalScroll):
    """One tunnel's gates, editable: allow-list, gate mode, PIN, Basic Auth, share links.

    The allow-list is the core of it. The web dashboard redesign was rolled
    back for shipping without rule editing, and the same holds here: a
    dashboard that shows a tunnel but cannot say who may open it is a list.
    """

    DEFAULT_CSS = """
    TunnelPane { padding: 0 1; }
    TunnelPane .section { margin-top: 1; text-style: bold; color: $accent; }
    TunnelPane Horizontal { height: auto; }
    TunnelPane Input { width: 1fr; }
    TunnelPane DataTable { height: 2; }
    TunnelPane Select { width: 14; }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, store: data.StateStore, subdomain: str) -> None:
        super().__init__(id="tunnel-pane")
        self.store = store
        self.subdomain = subdomain
        self.gates: data.TunnelGates | None = None
        self._rules: dict[str, AccessRule] = {}
        self._shares: dict[str, ShareLink] = {}

    def compose(self) -> ComposeResult:
        yield Static(Text(self.subdomain, style="bold"), id="td-head")
        yield Label("Gate mode", classes="section")
        with Horizontal():
            yield Button("SSO", id="td-mode-sso")
            yield Button("Public (none)", id="td-mode-none", variant="warning")
        yield Label("Access rules (SSO allow-list)", classes="section")
        yield DataTable(id="td-rules", cursor_type="row")
        with Horizontal():
            yield Input(placeholder="[provider:]email", id="td-rule-input")
            yield Button("Add", id="td-rule-add", variant="primary")
            yield Button("Remove", id="td-rule-remove")
        yield Label("PIN", classes="section")
        yield Static("-", id="td-pin-state")
        with Horizontal():
            yield Input(placeholder="4-8 digits", password=True, id="td-pin-input")
            yield Button("Set", id="td-pin-set", variant="primary")
            yield Button("Remove", id="td-pin-remove")
        yield Label("Basic auth", classes="section")
        yield Static("-", id="td-ba-state")
        with Horizontal():
            yield Input(placeholder="username", id="td-ba-user")
            yield Input(placeholder="password (8+)", password=True, id="td-ba-pass")
        with Horizontal():
            yield Button("Set", id="td-ba-set", variant="primary")
            yield Button("Remove", id="td-ba-remove")
        yield Label("Share links", classes="section")
        yield DataTable(id="td-shares", cursor_type="row")
        with Horizontal():
            yield Select(
                [(d, d) for d in ops_auth.SHARE_DURATIONS],
                value="24h",
                allow_blank=False,
                id="td-share-duration",
            )
            yield Input(placeholder="label (optional)", id="td-share-label")
        with Horizontal():
            yield Button("Create", id="td-share-create", variant="primary")
            yield Button("Revoke", id="td-share-revoke")
        yield Static("", id="td-share-new")

    def on_mount(self) -> None:
        self.query_one("#td-rules", DataTable).add_columns("Id", "Email", "Provider")
        self.query_one("#td-shares", DataTable).add_columns(
            "Id", "Label", "Expires", "Uses", "Active"
        )
        tunnel = self.store.tunnels.get(self.subdomain)
        if tunnel is not None:
            self.update_head(tunnel)
        self.reload()

    # -- reading ----------------------------------------------------------

    def update_head(self, tunnel: Tunnel | None) -> None:
        if tunnel is not None:
            self.query_one("#td-head", Static).update(tunnel_head(tunnel))

    @work(exclusive=True, group="tunnel-pane-load", exit_on_error=False)
    async def reload(self) -> None:
        ok, gates = await attempt(self.app, lambda: data.load_gates(self.store.api, self.subdomain))
        if ok and gates is not None:
            self.show(gates)

    def show(self, gates: data.TunnelGates) -> None:
        self.gates = gates
        detail = gates.detail
        self.update_head(detail.tunnel)

        rules = self.query_one("#td-rules", DataTable)
        rules.clear()
        self._rules = {}
        for rule in gates.rules:
            key = _rule_key(rule)
            if key in self._rules:
                continue
            self._rules[key] = rule
            rules.add_row(
                "-" if rule.id is None else str(rule.id), rule.email, rule.provider, key=key
            )

        pin = detail.pin
        self.query_one("#td-pin-state", Static).update(
            f"set (updated {pin.updated_at or '?'})" if pin.enabled else "not set"
        )
        basic = detail.basic_auth
        self.query_one("#td-ba-state", Static).update(
            f"set for user {basic.username or '?'}" if basic.enabled else "not set"
        )

        shares = self.query_one("#td-shares", DataTable)
        shares.clear()
        self._shares = {}
        if gates.shares is None:
            reason = gates.shares_error.message if gates.shares_error else "unavailable"
            self.query_one("#td-share-new", Static).update(
                Text(f"Share links could not be read: {reason}", style="dim")
            )
        for link in gates.shares or []:
            key = str(link.id) if link.id is not None else link.token_prefix
            if not key or key in self._shares:
                continue
            self._shares[key] = link
            uses = f"{link.use_count}/{link.max_uses}" if link.max_uses else str(link.use_count)
            shares.add_row(
                key,
                link.label or "-",
                link.expires_at or "-",
                uses,
                "yes" if link.active else "no",
                key=key,
            )
        fit_rows(rules)
        fit_rows(shares)

    # -- editing ----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "td-mode-sso": lambda: self.set_mode("sso"),
            "td-mode-none": lambda: self.set_mode("none"),
            "td-rule-add": self.add_rule,
            "td-rule-remove": self.remove_rule,
            "td-pin-set": self.set_pin,
            "td-pin-remove": self.remove_pin,
            "td-ba-set": self.set_basic_auth,
            "td-ba-remove": self.remove_basic_auth,
            "td-share-create": self.create_share,
            "td-share-revoke": self.revoke_share,
        }.get(event.button.id or "")
        if handler is not None:
            event.stop()
            handler()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "td-rule-input":
            event.stop()
            self.add_rule()
        elif event.input.id == "td-pin-input":
            event.stop()
            self.set_pin()

    async def _confirm(self, question: str, yes: str) -> bool:
        return bool(await self.app.push_screen_wait(Confirm(question, yes=yes)))

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def set_mode(self, mode: str) -> None:
        sub = self.subdomain
        if mode == "none" and not await self._confirm(
            f"Make {sub} public? Without the SSO gate anyone with the URL gets through, "
            "unless a PIN or basic auth is set.",
            "Make public",
        ):
            say(self.app, "Left alone.")
            return
        ok, _ = await attempt(
            self.app,
            lambda: ops_tunnels.set_auth_mode(self.store.api, sub, mode),
            done=f"Gate mode of {sub} is now {mode}.",
            cli=data.cli_auth_mode(sub, mode),
        )
        if ok:
            self.reload()
            self.app.refresh_data()  # type: ignore[attr-defined]

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def add_rule(self) -> None:
        sub = self.subdomain
        field = self.query_one("#td-rule-input", Input)
        spec = field.value.strip()
        if not spec:
            say(self.app, "Type an email address first ([provider:]email).")
            return
        ok, _ = await attempt(
            self.app,
            lambda: data.add_rule(self.store.api, sub, spec),
            done=lambda rule: f"Allowed {rule.email} ({rule.provider}) on {sub}.",
            cli=data.cli_access_add(sub, spec),
        )
        if ok:
            field.value = ""
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def remove_rule(self) -> None:
        sub = self.subdomain
        key = selected_key(self.query_one("#td-rules", DataTable))
        rule = self._rules.get(key or "")
        if rule is None:
            say(self.app, "No rule selected.")
            return
        rule_id = rule.id
        if rule_id is None:
            say(self.app, f"The relay returned no id for {rule.email}; it cannot be removed here.")
            return
        ok, _ = await attempt(
            self.app,
            lambda: ops_access.remove_rule(self.store.api, sub, rule_id),
            done=f"Removed {rule.email} from {sub}.",
            cli=data.cli_access_remove(sub, rule_id),
        )
        if ok:
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def set_pin(self) -> None:
        sub = self.subdomain
        field = self.query_one("#td-pin-input", Input)
        pin = field.value.strip()
        ok, _ = await attempt(
            self.app,
            lambda: ops_auth.set_pin(self.store.api, sub, pin),
            done=f"PIN set on {sub}.",
            cli=data.cli_pin(sub, "set"),
        )
        field.value = ""
        if ok:
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def remove_pin(self) -> None:
        sub = self.subdomain
        if not await self._confirm(f"Remove the PIN from {sub}?", "Remove PIN"):
            say(self.app, "Left alone.")
            return
        ok, _ = await attempt(
            self.app,
            lambda: ops_auth.remove_pin(self.store.api, sub),
            done=f"PIN removed from {sub}.",
            cli=data.cli_pin(sub, "remove"),
        )
        if ok:
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def set_basic_auth(self) -> None:
        sub = self.subdomain
        user_field = self.query_one("#td-ba-user", Input)
        pass_field = self.query_one("#td-ba-pass", Input)
        username, password = user_field.value, pass_field.value
        ok, _ = await attempt(
            self.app,
            lambda: ops_auth.set_basic_auth(self.store.api, sub, username, password),
            done=f"Basic auth set on {sub} for {username.strip()}.",
            cli=data.cli_basic_auth(sub, "set"),
        )
        pass_field.value = ""
        if ok:
            user_field.value = ""
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def remove_basic_auth(self) -> None:
        sub = self.subdomain
        if not await self._confirm(f"Remove basic auth from {sub}?", "Remove"):
            say(self.app, "Left alone.")
            return
        ok, _ = await attempt(
            self.app,
            lambda: ops_auth.remove_basic_auth(self.store.api, sub),
            done=f"Basic auth removed from {sub}.",
            cli=data.cli_basic_auth(sub, "remove"),
        )
        if ok:
            self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def create_share(self) -> None:
        sub = self.subdomain
        duration = str(self.query_one("#td-share-duration", Select).value)
        label_field = self.query_one("#td-share-label", Input)
        label = label_field.value.strip()
        ok, link = await attempt(
            self.app,
            lambda: ops_auth.create_share(self.store.api, sub, duration=duration, label=label),
            done=f"Share link for {sub} created, valid {duration}.",
            cli=data.cli_share_create(sub, duration, label),
        )
        if not ok or link is None:
            return
        label_field.value = ""
        if link.share_url:
            # Shown once: the relay keeps only a prefix, so this is the only
            # time the full link exists anywhere.
            self.query_one("#td-share-new", Static).update(
                Text.assemble(("New link (shown once): ", "bold"), link.share_url)
            )
            # A terminal without OSC 52 just does not get the copy.
            with contextlib.suppress(Exception):
                self.app.copy_to_clipboard(link.share_url)
        self.reload()

    @work(group="tunnel-pane-edit", exit_on_error=False)
    async def revoke_share(self) -> None:
        sub = self.subdomain
        key = selected_key(self.query_one("#td-shares", DataTable))
        link = self._shares.get(key or "")
        if link is None or link.id is None:
            say(self.app, "No share link selected.")
            return
        link_id = link.id
        ok, _ = await attempt(
            self.app,
            lambda: ops_auth.revoke_share(self.store.api, sub, link_id),
            done=f"Share link {link_id} on {sub} revoked.",
            cli=data.cli_share_revoke(sub, link_id),
        )
        if ok:
            self.reload()

    def action_close(self) -> None:
        self.app.action_close_detail()  # type: ignore[attr-defined]


class TunnelScreen(Screen[None]):
    """The tunnel pane on its own screen, for terminals too narrow to split."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, pane: TunnelPane) -> None:
        super().__init__()
        self.pane = pane

    def compose(self) -> ComposeResult:
        yield self.pane
        yield Label("", classes="status", markup=False)
        yield Footer()


# ---------------------------------------------------------------------------
# Daemon logs
# ---------------------------------------------------------------------------


class LogScreen(Screen[None]):
    """The last lines of one daemon's log, re-read every two seconds while open."""

    DEFAULT_CSS = """
    LogScreen #log-title { padding: 0 1; text-style: bold; }
    LogScreen Log { height: 1fr; }
    LogScreen .status { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    REFRESH_SECONDS = 2.0

    def __init__(self, daemon: Daemon) -> None:
        super().__init__()
        self.daemon = daemon
        self.text: str | None = None

    def compose(self) -> ComposeResult:
        yield Label(
            f"{self.daemon.name} ({self.daemon.scope}) — last {data.LOG_LINES} lines · "
            f"{data.cli_daemon('logs', self.daemon)} -n {data.LOG_LINES}",
            id="log-title",
            markup=False,
        )
        yield Log(id="log-view", max_lines=data.LOG_LINES)
        yield Label("", classes="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(".status", Label).update(
            f"Re-read every {self.REFRESH_SECONDS:g}s. Esc to go back. "
            f"Follow it live: {data.cli_daemon('logs', self.daemon)} -f"
        )
        self.reload()
        self.set_interval(self.REFRESH_SECONDS, self.reload)

    @work(exclusive=True, group="daemon-log", exit_on_error=False)
    async def reload(self) -> None:
        try:
            text = await data.daemon_log_tail(self.daemon)
        except HleError as exc:
            text = data.describe_error(exc)
        except Exception as exc:  # noqa: BLE001 — shown, not raised
            text = f"Could not read the log: {exc}"
        if text == self.text:
            return
        self.text = text
        view = self.query_one("#log-view", Log)
        view.clear()
        view.write_lines(text.splitlines() or ["(empty)"])
