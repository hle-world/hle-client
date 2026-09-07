"""The dashboard runs, shows what it read, and asks before it destroys.

Textual's test harness drives the real app headlessly, so these are not
assertions about a mock — the widgets are mounted and the key bindings are
dispatched the way a keypress would.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hle_tui import data
from hle_tui.app import HleApp

# Only the app tests are async; the row-shaping ones are plain functions.
pytestmark = pytest.mark.asyncio

SNAPSHOT = data.Snapshot(
    tunnels=[
        {
            "subdomain": "ha-x7k",
            "is_active": True,
            "service_url": "http://localhost:8123",
            "auth_mode": "sso",
            "tunnel_id": "t-1",
        },
        {
            "subdomain": "old-x7k",
            "is_active": False,
            "service_url": "http://localhost:9000",
            "auth_mode": "none",
            "tunnel_id": "t-2",
        },
    ],
    agents=[{"name": "trikala", "online": True, "endpoint_count": 3, "agent_version": "2609.4"}],
    daemons=[("hle-agent.service", False)],
)


def _collect(_snapshot=SNAPSHOT):
    async def _inner(*_args, **_kwargs):
        return _snapshot

    return _inner


class TestItShowsWhatItRead:
    async def test_the_tables_are_populated(self):
        with patch.object(data, "collect", _collect()):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                tunnels = app.query_one("#tunnels-table")
                assert tunnels.row_count == 2
                assert app.query_one("#agents-table").row_count == 1
                assert app.query_one("#daemons-table").row_count == 1

    async def test_the_status_line_counts_live_tunnels(self):
        with patch.object(data, "collect", _collect()):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "1 live of 2" in app.last_status

    async def test_an_unreachable_relay_is_not_shown_as_an_empty_list(self):
        """ "No tunnels" and "could not ask" look identical in a table."""
        with patch.object(data, "collect", _collect(data.Snapshot(tunnels=None))):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "Could not reach" in app.last_status

    async def test_a_missing_key_says_what_to_run(self):
        snapshot = data.Snapshot(error="No API key. Run: hle auth login")
        with patch.object(data, "collect", _collect(snapshot)):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "hle auth login" in app.last_status


class TestAgentState:
    """Caught live: every agent showed offline while both were connected.

    The relay's field is `online`; reading `is_online` returned None for all of
    them, which is the most alarming thing this screen can say.
    """

    def test_a_connected_agent_reads_as_online(self):
        snapshot = data.Snapshot(agents=[{"name": "rpi", "online": True}])
        assert data.agent_rows(snapshot)[0][1] == "online"

    def test_a_disconnected_agent_reads_as_offline(self):
        snapshot = data.Snapshot(agents=[{"name": "rpi", "online": False}])
        assert data.agent_rows(snapshot)[0][1] == "offline"

    def test_a_disabled_agent_is_not_called_offline(self):
        """Turned off and not connected are different facts."""
        snapshot = data.Snapshot(agents=[{"name": "rpi", "online": False, "is_active": False}])
        assert data.agent_rows(snapshot)[0][1] == "disabled"


class TestDestructiveActionsAsk:
    async def test_delete_asks_first_and_cancelling_deletes_nothing(self):
        with (
            patch.object(data, "collect", _collect()),
            patch.object(data, "delete_tunnel") as delete,
        ):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                await pilot.click("#no")
                await pilot.pause()
        delete.assert_not_called()

    async def test_confirming_deletes_the_selected_tunnel(self):
        async def _deleted(subdomain, _key=None):
            return f"Deleted {subdomain}."

        with (
            patch.object(data, "collect", _collect()),
            patch.object(data, "delete_tunnel", side_effect=_deleted) as delete,
        ):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                await pilot.click("#yes")
                await pilot.pause()
        assert delete.call_args.args[0] == "ha-x7k"


class TestTheCursorSurvivesARefresh:
    async def test_polling_does_not_send_the_cursor_home(self):
        """A table that jumps to the top every poll cannot be acted on."""
        with patch.object(data, "collect", _collect()):
            app = HleApp(refresh_seconds=0)
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one("#tunnels-table")
                table.move_cursor(row=1)
                app.refresh_data()
                await pilot.pause()
                assert table.cursor_row == 1
