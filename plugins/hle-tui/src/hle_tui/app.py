"""The dashboard itself.

Three tabs — tunnels, agents, daemons — over the same ``ops`` layer the CLI
uses, held in one :class:`~hle_tui.data.StateStore` that polls and tells the
tables when it has news. Selecting a tunnel opens its gates for editing;
selecting an agent shows what the relay knows about it; a daemon's log is one
key away.

Anything destructive asks first. A dashboard makes a keystroke cheap, which is
exactly why deleting a tunnel, restarting a daemon or opening a gate must not
be one. And nothing a relay call can raise ends the app: every worker runs
with ``exit_on_error=False`` and every call goes through ``attempt``, which
turns an ``HleError`` into the status line.
"""

from __future__ import annotations

import contextlib
import webbrowser
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.data_table import RowDoesNotExist

from hle_tui import data
from hle_tui.screens import (
    Confirm,
    LogScreen,
    TunnelPane,
    TunnelScreen,
    attempt,
    selected_key,
)

if TYPE_CHECKING:
    from hle_client.ops.models import Agent

# Below this many columns the tunnel pane gets its own screen instead of half
# of the tunnels tab: its inputs are unusable at 40 columns.
SPLIT_MIN_WIDTH = 120

TUNNEL_ACTIONS = {"open_detail", "open_selected", "delete_selected", "create_tunnel"}
DAEMON_ACTIONS = {"restart_selected", "daemon_logs"}


def agent_text(agent: Agent | None) -> Text:
    if agent is None:
        return Text("No agent selected.", style="dim")
    fields, endpoints, gaps = data.agent_detail(agent)
    text = Text()
    for title, value in fields:
        text.append(f"{title:<16}", style="bold")
        text.append(f"{value}\n")
    if endpoints:
        text.append("\nEndpoints\n", style="bold")
        for line in endpoints:
            text.append(f"  {line}\n")
    if gaps:
        text.append("\nNot reported by the relay: " + ", ".join(gaps) + "\n", style="dim")
    return text


class HleApp(App[None]):
    """`hle tui`."""

    TITLE = "HLE"
    CSS = """
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    DataTable { height: 1fr; }
    .split { height: 1fr; }
    #tunnel-side { width: 55%; display: none; border-left: tall $panel; }
    #tunnel-side.open { display: block; }
    #agent-detail { width: 45%; padding: 0 1; border-left: tall $panel; }
    #bar { height: auto; }
    #status { width: 1fr; padding: 0 1; color: $text-muted; }
    #updated { width: auto; padding: 0 1; color: $text-muted; }
    Screen .status { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "open_detail", "Detail"),
        Binding("o", "open_selected", "Open"),
        Binding("d", "delete_selected", "Delete tunnel"),
        Binding("n", "create_tunnel", "Create"),
        Binding("s", "restart_selected", "Restart daemon"),
        Binding("l", "daemon_logs", "Logs"),
        Binding("escape", "close_detail", "Close", show=False),
    ]

    def __init__(self, *, api_key: str | None = None, refresh_seconds: int = 10) -> None:
        super().__init__()
        self.store = data.StateStore(api_key)
        self.refresh_seconds = refresh_seconds
        self.last_status = ""
        self.last_cli: str | None = None
        self._poll_line: str | None = None
        self._tunnel_pane: TunnelPane | None = None
        self.store.subscribe(self._on_store)

    # Kept for callers that read the last poll off the app.
    @property
    def api_key(self) -> str | None:
        return self.store.api_key

    @property
    def snapshot(self) -> data.Snapshot:
        return self.store.snapshot

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tunnels"):
            with TabPane("Tunnels", id="tunnels"), Horizontal(classes="split"):
                yield DataTable(id="tunnels-table", cursor_type="row")
                yield Container(id="tunnel-side")
            with TabPane("Agents", id="agents"), Horizontal(classes="split"):
                yield DataTable(id="agents-table", cursor_type="row")
                yield Static(agent_text(None), id="agent-detail")
            with TabPane("Daemons", id="daemons"):
                yield DataTable(id="daemons-table", cursor_type="row")
        with Horizontal(id="bar"):
            yield Label("", id="status", markup=False)
            yield Label("", id="updated", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tunnels-table", DataTable).add_columns(
            "Subdomain", "State", "Service", "Auth"
        )
        self.query_one("#agents-table", DataTable).add_columns(
            "Agent", "State", "Endpoints", "Version", "Last seen"
        )
        self.query_one("#daemons-table", DataTable).add_columns("Daemon", "Scope", "Kind", "State")
        self._status = self.query_one("#status", Label)
        # Said before the first poll returns. Three empty tables and a blank
        # status line read as "you have nothing", which is a different claim
        # from "I have not asked yet".
        self.say("Loading...")
        self.refresh_data()
        if self.refresh_seconds > 0:
            self.set_interval(self.refresh_seconds, self.refresh_data)

    # -- status -------------------------------------------------------------

    def say(self, message: str, cli: str | None = None) -> None:
        """Put one line in the status bar, with the equivalent command if there is one.

        Kept on the app as well as rendered: the widget's text is a private
        detail that moves between textual versions, and this is the line that
        says whether anything worked.
        """
        self.last_status = message
        self.last_cli = cli
        line = f"{message}  ·  {cli}" if cli else message
        self._status.update(line)
        # A pushed screen has its own status line; the main one is under it.
        for label in self.screen.query(".status"):
            if isinstance(label, Label):
                label.update(line)

    # -- data ---------------------------------------------------------------

    @work(group="poll", exit_on_error=False)
    async def refresh_data(self) -> None:
        # The store serialises refreshes itself: a tick that lands while one
        # is in flight waits for it instead of cancelling it.
        await self.store.refresh()

    def _on_store(self, store: data.StateStore) -> None:
        self._fill("#tunnels-table", store.tunnel_table())
        self._fill("#agents-table", store.agent_table())
        self._fill("#daemons-table", store.daemon_table())
        self._show_agent()
        if self._tunnel_pane is not None and self._tunnel_pane.is_attached:
            self._tunnel_pane.update_head(store.tunnels.get(self._tunnel_pane.subdomain))
        if store.last_updated is not None:
            self.query_one("#updated", Label).update(f"updated {store.last_updated:%H:%M:%S}")
        # Only when the poll's news changes: an edit's confirmation, and the
        # command it stands for, should not be wiped by the next tick.
        line = store.status_line()
        if line != self._poll_line:
            self._poll_line = line
            self.say(line)

    def _fill(self, selector: str, rows: list[tuple[str, tuple[str, ...]]]) -> None:
        """Replace a table's rows, keeping the cursor on the same *key*.

        By key, not by index: after a poll that reorders the list, the row
        at the old index is a different tunnel, and "d" would then ask to
        delete something the user never pointed at.
        """
        table = self.query_one(selector, DataTable)
        current = selected_key(table)
        index = table.cursor_row
        table.clear()
        for key, row in rows:
            table.add_row(*row, key=key)
        if not rows:
            return
        if current is not None:
            with contextlib.suppress(RowDoesNotExist):
                index = table.get_row_index(current)
        table.move_cursor(row=min(max(index, 0), len(rows) - 1))

    # -- selection ----------------------------------------------------------

    def _active_tab(self) -> str | None:
        try:
            return self.query_one(TabbedContent).active
        except Exception:  # noqa: BLE001 — before compose there is no tab
            return None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        tab = self._active_tab()
        if tab is None:
            return True
        if action in TUNNEL_ACTIONS | DAEMON_ACTIONS and self.screen is not self.screen_stack[0]:
            # Over a log tail or a tunnel screen, "s" must not restart whatever
            # the hidden daemons table happens to point at.
            return False
        if action in TUNNEL_ACTIONS:
            return tab == "tunnels"
        if action in DAEMON_ACTIONS:
            return tab == "daemons"
        return True

    def on_tabbed_content_tab_activated(self, _event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()

    def _selected(self, selector: str) -> str | None:
        return selected_key(self.query_one(selector, DataTable))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "tunnels-table" and event.row_key.value is not None:
            self.open_tunnel(str(event.row_key.value))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "agents-table":
            self._show_agent()

    def _show_agent(self) -> None:
        key = self._selected("#agents-table")
        agent = self.store.agents.get(key) if key else None
        self.query_one("#agent-detail", Static).update(agent_text(agent))

    # -- tunnel detail ------------------------------------------------------

    def open_tunnel(self, subdomain: str) -> None:
        self.action_close_detail()
        pane = TunnelPane(self.store, subdomain)
        self._tunnel_pane = pane
        if self.size.width < SPLIT_MIN_WIDTH:
            self.push_screen(TunnelScreen(pane))
            return
        side = self.query_one("#tunnel-side", Container)
        side.mount(pane)
        side.add_class("open")

    def action_open_detail(self) -> None:
        subdomain = self._selected("#tunnels-table")
        if subdomain is None:
            self.say("Nothing selected.")
            return
        self.open_tunnel(subdomain)

    def action_close_detail(self) -> None:
        if isinstance(self.screen, TunnelScreen | LogScreen):
            self.pop_screen()
        pane = self._tunnel_pane
        self._tunnel_pane = None
        if pane is not None and pane.is_attached and not isinstance(pane.screen, TunnelScreen):
            pane.remove()
        self.query_one("#tunnel-side", Container).remove_class("open")

    # -- actions ------------------------------------------------------------

    def action_refresh(self) -> None:
        self._poll_line = None
        self.say("Refreshing...")
        self.refresh_data()
        if self._tunnel_pane is not None and self._tunnel_pane.is_attached:
            self._tunnel_pane.reload()

    def action_open_selected(self) -> None:
        subdomain = self._selected("#tunnels-table")
        if subdomain is None:
            self.say("Nothing selected.")
            return
        url = data.public_url(self.store.snapshot, subdomain)
        tunnel = self.store.tunnels.get(subdomain)
        if tunnel is not None and tunnel.public_url:
            url = tunnel.public_url
        webbrowser.open(url)
        self.say(f"Opened {url}")

    def action_create_tunnel(self) -> None:
        # Out of scope here for now: creating one means running it, which is
        # the CLI's (or a daemon's) job, not a dashboard's.
        self.say("Creating a tunnel is CLI-only for now.", cli=data.CLI_CREATE)

    @work(group="action", exit_on_error=False)
    async def action_delete_selected(self) -> None:
        subdomain = self._selected("#tunnels-table")
        if subdomain is None:
            self.say("Nothing selected.")
            return
        # A keystroke is cheap here, which is the reason to ask.
        if not await self.push_screen_wait(Confirm(f"Delete {subdomain} and its access rules?")):
            self.say("Left alone.")
            return
        ok, line = await attempt(
            self,
            lambda: data.delete_tunnel(subdomain, self.store.api_key, api=self.store.client()),
        )
        if ok and line is not None:
            self.say(line, cli=f"hle tunnel delete {subdomain}")
        if self._tunnel_pane is not None and self._tunnel_pane.subdomain == subdomain:
            self.action_close_detail()
        self.refresh_data()

    @work(group="action", exit_on_error=False)
    async def action_restart_selected(self) -> None:
        key = self._selected("#daemons-table")
        daemon = self.store.daemons.get(key) if key else None
        if daemon is None:
            self.say("Nothing selected.")
            return
        cli = data.cli_daemon("restart", daemon)
        # Restarting a tunnel daemon drops every connection through it, and
        # restarting the agent drops all of its endpoints at once.
        if not await self.push_screen_wait(
            Confirm(f"Restart {daemon.name} ({daemon.scope})?", yes="Restart")
        ):
            self.say("Left alone.")
            return
        await attempt(
            self,
            lambda: data.restart_daemon(daemon.name, daemon.user_mode),
            done=lambda line: line,
            cli=cli,
        )
        self.refresh_data()

    def action_daemon_logs(self) -> None:
        key = self._selected("#daemons-table")
        daemon = self.store.daemons.get(key) if key else None
        if daemon is None:
            self.say("Nothing selected.")
            return
        self.push_screen(LogScreen(daemon))
        self.say(f"Tailing {daemon.name}.", cli=data.cli_daemon("logs", daemon) + " -f")
