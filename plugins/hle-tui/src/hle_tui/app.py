"""The dashboard itself.

Three tables — tunnels, agents, services — over the same data the CLI reads,
with the few actions worth having a cursor for: open a tunnel, delete one,
restart a service.

Anything destructive asks first. A dashboard makes a keystroke cheap, which is
exactly why deleting a tunnel must not be one.
"""

from __future__ import annotations

import webbrowser
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    TabbedContent,
    TabPane,
)

from hle_tui import data


class Confirm(ModalScreen[bool]):
    """A yes/no the user has to mean."""

    CSS = """
    Confirm { align: center middle; }
    #box { width: 60; height: auto; border: thick $warning; padding: 1 2; background: $surface; }
    #buttons { height: auto; padding-top: 1; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Label(self.question)
            with Container(id="buttons"):
                yield Button("Cancel", variant="primary", id="no")
                yield Button("Delete", variant="error", id="yes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class HleApp(App[None]):
    """`hle tui`."""

    TITLE = "HLE"
    CSS = """
    DataTable { height: 1fr; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("o", "open_selected", "Open"),
        ("d", "delete_selected", "Delete tunnel"),
        ("s", "restart_selected", "Restart service"),
    ]

    def __init__(self, *, api_key: str | None = None, refresh_seconds: int = 10) -> None:
        super().__init__()
        self.api_key = api_key
        self.refresh_seconds = refresh_seconds
        self.snapshot = data.Snapshot()
        self.last_status = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tunnels"):
            with TabPane("Tunnels", id="tunnels"):
                yield DataTable(id="tunnels-table", cursor_type="row")
            with TabPane("Agents", id="agents"):
                yield DataTable(id="agents-table", cursor_type="row")
            with TabPane("Services", id="daemons"):
                yield DataTable(id="daemons-table", cursor_type="row")
        yield Label("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tunnels-table", DataTable).add_columns(
            "Subdomain", "State", "Service", "Auth"
        )
        self.query_one("#agents-table", DataTable).add_columns(
            "Agent", "State", "Endpoints", "Version"
        )
        self.query_one("#daemons-table", DataTable).add_columns("Service", "Scope")
        # Said before the first poll returns. Three empty tables and a blank
        # status line read as "you have nothing", which is a different claim
        # from "I have not asked yet".
        self.say("Loading...")
        self.refresh_data()
        if self.refresh_seconds > 0:
            self.set_interval(self.refresh_seconds, self.refresh_data)

    # -- data ---------------------------------------------------------------

    def say(self, message: str) -> None:
        # Kept on the app as well as rendered: the widget's text is a private
        # detail that moves between textual versions, and this is the line
        # that says whether anything worked.
        self.last_status = message
        self.query_one("#status", Label).update(message)

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        self.snapshot = await data.collect(self.api_key)
        self._fill("#tunnels-table", data.tunnel_rows(self.snapshot))
        self._fill("#agents-table", data.agent_rows(self.snapshot))
        self._fill("#daemons-table", data.daemon_rows(self.snapshot))

        if self.snapshot.error:
            self.say(self.snapshot.error)
        elif self.snapshot.tunnels is None:
            # Not the same as "no tunnels", and must not look like it.
            self.say("Could not reach the relay.")
        else:
            live = sum(1 for t in self.snapshot.tunnels if t.get("is_active"))
            self.say(f"{live} live of {len(self.snapshot.tunnels)} tunnels.")

    def _fill(self, selector: str, rows: list[tuple[str, ...]]) -> None:
        table = self.query_one(selector, DataTable)
        # Keep the cursor where it was: a table that jumps to the top every
        # poll cannot be used to act on anything.
        cursor = table.cursor_row
        table.clear()
        for row in rows:
            table.add_row(*row)
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1))

    # -- actions ------------------------------------------------------------

    def _selected(self, selector: str) -> tuple[Any, ...] | None:
        table = self.query_one(selector, DataTable)
        if table.row_count == 0:
            return None
        return tuple(table.get_row_at(table.cursor_row))

    def action_refresh(self) -> None:
        self.say("Refreshing...")
        self.refresh_data()

    def action_open_selected(self) -> None:
        row = self._selected("#tunnels-table")
        if row is None:
            self.say("Nothing selected.")
            return
        url = f"https://{row[0]}.hle.world"
        webbrowser.open(url)
        self.say(f"Opened {url}")

    @work
    async def action_delete_selected(self) -> None:
        row = self._selected("#tunnels-table")
        if row is None:
            self.say("Nothing selected.")
            return
        subdomain = str(row[0])
        # A keystroke is cheap here, which is the reason to ask.
        if not await self.push_screen_wait(Confirm(f"Delete {subdomain} and its access rules?")):
            self.say("Left alone.")
            return
        self.say(await data.delete_tunnel(subdomain, self.api_key))
        self.refresh_data()

    @work
    async def action_restart_selected(self) -> None:
        row = self._selected("#daemons-table")
        if row is None:
            self.say("Nothing selected.")
            return
        name, scope = str(row[0]), str(row[1])
        self.say(await data.restart_daemon(name, scope == "user"))
        self.refresh_data()
