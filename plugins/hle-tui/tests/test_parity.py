"""The store, the tunnel pane, daemon logs and the agent pane.

Relay calls are faked at the ``ops`` functions, the same seam the CLI's own
tests use, so what is asserted is which ``ops`` call the dashboard makes and
with what — not what some private HTTP helper looks like.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from hle_client.errors import AuthError, HleError, UnreachableError
from hle_client.ops.models import (
    AccessRule,
    Agent,
    BasicAuthStatus,
    Daemon,
    PinStatus,
    ShareLink,
    Tunnel,
    TunnelDetail,
)
from textual.widgets import Button, DataTable, Input, Log, Static, TabbedContent

from hle_tui import data
from hle_tui.app import HleApp
from hle_tui.screens import LogScreen, TunnelPane, TunnelScreen

KEY = "hle_" + "a" * 32
WIDE = (160, 50)

HA = Tunnel.from_api(
    {
        "subdomain": "ha-x7k",
        "is_active": True,
        "service_url": "http://localhost:8123",
        "auth_mode": "sso",
        "tunnel_id": "t-1",
        "public_url": "https://ha-x7k.hle.world",
    }
)
OLD = Tunnel.from_api(
    {"subdomain": "old-x7k", "is_active": False, "auth_mode": "none", "tunnel_id": "t-2"}
)
AGENT = Agent.from_api(
    {
        "public_id": "ag-1",
        "name": "trikala",
        "online": True,
        "endpoint_count": 3,
        "agent_version": "2609.8",
        "last_seen_at": "2026-09-25T10:00:00",
        "hostname": "rpi",
    }
)
DAEMON = Daemon(name="hle-ha.service", scope="system", kind="tunnel", label="ha")


def snapshot(*tunnels: Tunnel) -> data.Snapshot:
    return data.Snapshot(tunnels=list(tunnels), agents=[AGENT], daemons=[DAEMON])


class Sequence:
    """A fake ``collect`` that answers each poll with the next snapshot."""

    def __init__(self, *snapshots: data.Snapshot) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    async def __call__(self, *_args, **_kwargs) -> data.Snapshot:
        snap = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snap


DETAIL = TunnelDetail(
    tunnel=HA,
    pin=PinStatus(enabled=True, updated_at="2026-09-20"),
    basic_auth=BasicAuthStatus(enabled=True, username="admin"),
)
RULES = [
    AccessRule(email="ann@example.com", provider="any", id=11),
    AccessRule(email="bob@example.com", provider="github", id=12),
]
SHARES = [ShareLink(id=5, label="for-mum", expires_at="2026-09-26", use_count=1, max_uses=3)]


def gate_patches(*, get_tunnel=None):
    """Fake the three reads the tunnel pane makes."""
    return (
        patch(
            "hle_client.ops.tunnels.get_tunnel",
            get_tunnel or AsyncMock(return_value=DETAIL),
        ),
        patch("hle_client.ops.access.get_access", AsyncMock(return_value=RULES)),
        patch("hle_client.ops.auth.list_shares", AsyncMock(return_value=SHARES)),
    )


def text_of(widget: Static) -> str:
    return str(widget.render())


async def settle(pilot, times: int = 3) -> None:
    for _ in range(times):
        await pilot.pause()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class TestStateStore:
    async def test_subscribers_hear_every_refresh_with_keyed_collections(self):
        store = data.StateStore(KEY)
        seen: list[data.StateStore] = []
        unsubscribe = store.subscribe(seen.append)
        with patch.object(data, "collect", Sequence(snapshot(HA, OLD))):
            await store.refresh()
            assert seen == [store]
            assert list(store.tunnels) == ["ha-x7k", "old-x7k"]
            assert list(store.agents) == ["ag-1"]
            assert list(store.daemons) == ["hle-ha.service"]
            assert store.last_updated is not None
            unsubscribe()
            await store.refresh()
        assert len(seen) == 1

    async def test_one_client_for_the_store_life(self):
        store = data.StateStore(KEY)
        assert store.api is store.api

    async def test_no_key_is_an_auth_error_not_a_crash(self):
        with patch.object(data, "load_api_key", return_value=None):
            store = data.StateStore()
        with pytest.raises(AuthError):
            _ = store.api
        assert store.client() is None

    async def test_a_failed_section_keeps_its_last_good_rows(self):
        store = data.StateStore(KEY)
        broken = data.Snapshot(tunnels=None, tunnels_error=UnreachableError("down"))
        with patch.object(data, "collect", Sequence(snapshot(HA), broken)):
            await store.refresh()
            await store.refresh()
        assert list(store.tunnels) == ["ha-x7k"]
        assert store.status_line() == "Could not reach the relay."

    async def test_the_fetches_run_in_parallel(self):
        """Tunnels and agents are gathered, not awaited one after the other."""
        import asyncio

        started: list[str] = []
        gate = asyncio.Event()

        async def tunnels(_api):
            started.append("tunnels")
            await gate.wait()
            return []

        async def agents(_api):
            started.append("agents")
            gate.set()
            return []

        with (
            patch("hle_client.ops.tunnels.list_tunnels", tunnels),
            patch("hle_client.ops.agents.list_agents", agents),
            patch.object(data, "local_daemons", AsyncMock(return_value=[])),
        ):
            snap = await asyncio.wait_for(data.collect(KEY), timeout=2)
        assert sorted(started) == ["agents", "tunnels"]
        assert snap.tunnels == [] and snap.agents == []

    def test_duplicate_daemon_names_get_scoped_keys(self):
        both = [Daemon(name="hle-x.service", scope="system"), Daemon("hle-x.service", "user")]
        assert data.daemon_keys(both) == ["hle-x.service@system", "hle-x.service@user"]


class TestKeyedCursor:
    async def test_a_reordering_refresh_keeps_the_cursor_on_the_same_tunnel(self):
        """By index, "d" after this poll would have pointed at ha-x7k."""
        fake = Sequence(snapshot(HA, OLD), snapshot(OLD, HA))
        with patch.object(data, "collect", fake):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                table = app.query_one("#tunnels-table", DataTable)
                table.move_cursor(row=1)
                assert app._selected("#tunnels-table") == "old-x7k"
                app.refresh_data()
                await settle(pilot)
                assert fake.calls == 2
                assert table.cursor_row == 0
                assert app._selected("#tunnels-table") == "old-x7k"

    async def test_ticks_during_a_slow_poll_are_dropped_not_queued(self):
        import asyncio

        release = asyncio.Event()
        calls = 0

        async def slow(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            return snapshot(HA)

        with patch.object(data, "collect", slow):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                for _ in range(5):
                    app.refresh_data()
                await settle(pilot)
                assert calls == 1
                release.set()
                await settle(pilot)
                assert app.query_one("#tunnels-table", DataTable).row_count == 1
                app.refresh_data()
                await settle(pilot)
                assert calls == 2

    async def test_the_footer_says_when_it_last_heard(self):
        with patch.object(data, "collect", Sequence(snapshot(HA))):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                assert "updated" in text_of(app.query_one("#updated"))


# ---------------------------------------------------------------------------
# Errors do not end the app
# ---------------------------------------------------------------------------


class TestErrorsAreLinesNotExits:
    async def test_a_failing_ops_call_in_the_pane_is_reported(self):
        failing = AsyncMock(side_effect=HleError("Tunnel 'ha-x7k' is broken.", hint="Try X."))
        p1, p2, p3 = gate_patches(get_tunnel=failing)
        with patch.object(data, "collect", Sequence(snapshot(HA))), p1, p2, p3:
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                assert app.is_running
                assert "is broken. Try X." in app.last_status

    async def test_a_failing_restart_is_reported(self):
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            patch.object(data, "restart_daemon", AsyncMock(side_effect=HleError("no systemd"))),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one(TabbedContent).active = "daemons"
                await settle(pilot)
                await pilot.press("s")
                await settle(pilot)
                await pilot.click("#yes")
                await settle(pilot)
                assert app.is_running
                assert "no systemd" in app.last_status

    async def test_an_unexpected_exception_is_a_line_too(self):
        p1, p2, p3 = gate_patches(get_tunnel=AsyncMock(side_effect=RuntimeError("bug")))
        with patch.object(data, "collect", Sequence(snapshot(HA))), p1, p2, p3:
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                assert app.is_running
                assert "RuntimeError: bug" in app.last_status


# ---------------------------------------------------------------------------
# The tunnel pane
# ---------------------------------------------------------------------------


class TestTunnelPane:
    async def test_it_renders_rules_pin_basic_auth_and_shares(self):
        p1, p2, p3 = gate_patches()
        with patch.object(data, "collect", Sequence(snapshot(HA, OLD))), p1, p2, p3:
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                pane = app.query_one(TunnelPane)
                assert pane.subdomain == "ha-x7k"
                assert "https://ha-x7k.hle.world" in text_of(pane.query_one("#td-head"))
                assert pane.query_one("#td-rules", DataTable).row_count == 2
                assert "set" in text_of(pane.query_one("#td-pin-state"))
                assert "admin" in text_of(pane.query_one("#td-ba-state"))
                shares = pane.query_one("#td-shares", DataTable)
                assert shares.row_count == 1
                assert shares.get_row_at(0)[1] == "for-mum"

    async def test_a_narrow_terminal_gets_its_own_screen(self):
        p1, p2, p3 = gate_patches()
        with patch.object(data, "collect", Sequence(snapshot(HA))), p1, p2, p3:
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=(80, 30)) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                assert isinstance(app.screen, TunnelScreen)
                await pilot.press("escape")
                await settle(pilot)
                assert not isinstance(app.screen, TunnelScreen)

    async def test_adding_a_rule_calls_add_rule_and_shows_the_command(self):
        added = AsyncMock(return_value=AccessRule(email="cy@example.com", provider="github"))
        p1, p2, p3 = gate_patches()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            p1,
            p2,
            p3,
            patch("hle_client.ops.access.add_rule", added),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                pane = app.query_one(TunnelPane)
                pane.query_one("#td-rule-input", Input).value = "github:cy@example.com"
                pane.query_one("#td-rule-add", Button).press()
                await settle(pilot)
                added.assert_awaited_once_with(app.store.api, "ha-x7k", "cy@example.com", "github")
                assert app.last_cli == (
                    "hle tunnel access add ha-x7k cy@example.com --provider github"
                )
                assert pane.query_one("#td-rule-input", Input).value == ""

    async def test_removing_a_rule_calls_remove_rule_with_its_id(self):
        removed = AsyncMock(return_value=None)
        p1, p2, p3 = gate_patches()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            p1,
            p2,
            p3,
            patch("hle_client.ops.access.remove_rule", removed),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                pane = app.query_one(TunnelPane)
                pane.query_one("#td-rules", DataTable).move_cursor(row=1)
                pane.query_one("#td-rule-remove", Button).press()
                await settle(pilot)
                removed.assert_awaited_once_with(app.store.api, "ha-x7k", 12)
                assert app.last_cli == "hle tunnel access remove ha-x7k 12"

    async def test_making_a_tunnel_public_asks_first(self):
        set_mode = AsyncMock(return_value="ha-x7k")
        p1, p2, p3 = gate_patches()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            p1,
            p2,
            p3,
            patch("hle_client.ops.tunnels.set_auth_mode", set_mode),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                app.query_one("#td-mode-none", Button).press()
                await settle(pilot)
                await pilot.click("#no")
                await settle(pilot)
                set_mode.assert_not_called()
                app.query_one("#td-mode-none", Button).press()
                await settle(pilot)
                await pilot.click("#yes")
                await settle(pilot)
                set_mode.assert_awaited_once_with(app.store.api, "ha-x7k", "none")
                assert app.last_cli == "hle tunnel auth-mode ha-x7k --set none"

    async def test_pin_share_and_basic_auth_go_through_ops_auth(self):
        set_pin = AsyncMock(return_value="ha-x7k")
        create = AsyncMock(return_value=ShareLink(id=6, share_url="https://s.example/x"))
        revoke = AsyncMock(return_value="ha-x7k")
        set_basic = AsyncMock(return_value="ha-x7k")
        p1, p2, p3 = gate_patches()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            p1,
            p2,
            p3,
            patch("hle_client.ops.auth.set_pin", set_pin),
            patch("hle_client.ops.auth.create_share", create),
            patch("hle_client.ops.auth.revoke_share", revoke),
            patch("hle_client.ops.auth.set_basic_auth", set_basic),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                pane = app.query_one(TunnelPane)
                api = app.store.api

                pane.query_one("#td-pin-input", Input).value = "1234"
                pane.query_one("#td-pin-set", Button).press()
                await settle(pilot)
                set_pin.assert_awaited_once_with(api, "ha-x7k", "1234")
                assert app.last_cli == "hle tunnel pin set ha-x7k"
                # The PIN does not linger in the field after it was sent.
                assert pane.query_one("#td-pin-input", Input).value == ""

                pane.query_one("#td-ba-user", Input).value = "admin"
                pane.query_one("#td-ba-pass", Input).value = "correct horse"
                pane.query_one("#td-ba-set", Button).press()
                await settle(pilot)
                set_basic.assert_awaited_once_with(api, "ha-x7k", "admin", "correct horse")

                pane.query_one("#td-share-label", Input).value = "for-dad"
                pane.query_one("#td-share-create", Button).press()
                await settle(pilot)
                create.assert_awaited_once_with(api, "ha-x7k", duration="24h", label="for-dad")
                assert "https://s.example/x" in text_of(pane.query_one("#td-share-new"))

                pane.query_one("#td-share-revoke", Button).press()
                await settle(pilot)
                await pilot.click("#yes")
                await settle(pilot)
                revoke.assert_awaited_once_with(api, "ha-x7k", 5)
                assert app.last_cli == "hle tunnel share revoke ha-x7k 5"

    async def test_revoking_a_share_asks_and_declining_keeps_it(self):
        """Irreversible: whoever holds the link would need a new one sent."""
        revoke = AsyncMock(return_value="ha-x7k")
        p1, p2, p3 = gate_patches()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            p1,
            p2,
            p3,
            patch("hle_client.ops.auth.revoke_share", revoke),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                app.open_tunnel("ha-x7k")
                await settle(pilot)
                app.query_one("#td-share-revoke", Button).press()
                await settle(pilot)
                question = str(app.screen.query_one("#box Label").render())
                assert "for-mum" in question
                assert "2026-09-26" in question
                await pilot.click("#no")
                await settle(pilot)
                revoke.assert_not_called()
                assert app.last_status == "Left alone."
                assert app.query_one("#td-shares", DataTable).row_count == 1


# ---------------------------------------------------------------------------
# Daemons
# ---------------------------------------------------------------------------


class TestDaemons:
    async def test_restart_needs_confirmation(self):
        restart = AsyncMock(return_value="Restarted hle-ha.service (system).")
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            patch.object(data, "restart_daemon", restart),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one(TabbedContent).active = "daemons"
                await settle(pilot)
                await pilot.press("s")
                await settle(pilot)
                await pilot.click("#no")
                await settle(pilot)
                restart.assert_not_called()
                await pilot.press("s")
                await settle(pilot)
                await pilot.click("#yes")
                await settle(pilot)
                restart.assert_awaited_once_with("hle-ha.service", False)
                assert app.last_cli == "hle daemon restart --label ha --system"

    async def test_restart_is_not_bound_on_the_tunnels_tab(self):
        restart = AsyncMock()
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            patch.object(data, "restart_daemon", restart),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("s")
                await settle(pilot)
                assert app.screen is app.screen_stack[0]
        restart.assert_not_called()

    async def test_logs_open_a_tail(self):
        tail = AsyncMock(return_value="one\ntwo\nthree")
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            patch.object(data, "daemon_log_tail", tail),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one(TabbedContent).active = "daemons"
                await settle(pilot)
                await pilot.press("l")
                await settle(pilot)
                assert isinstance(app.screen, LogScreen)
                assert app.screen.query_one(Log).line_count == 3
                tail.assert_awaited_with(DAEMON)
                # Over the tail, "s" must not reach the hidden daemons table.
                assert app.check_action("restart_selected", ()) is False
                await pilot.press("escape")
                await settle(pilot)
                assert not isinstance(app.screen, LogScreen)

    async def test_a_log_that_cannot_be_read_says_why(self):
        tail = AsyncMock(side_effect=HleError("No log file at /x", hint="Try: hle daemon status"))
        with (
            patch.object(data, "collect", Sequence(snapshot(HA))),
            patch.object(data, "daemon_log_tail", tail),
        ):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                app.push_screen(LogScreen(DAEMON))
                await settle(pilot)
                assert "No log file at /x" in (app.screen.text or "")


class TestDaemonLogSource:
    def test_rcd_reads_var_log(self):
        with patch("hle_client.service_cmd.current_platform", return_value="freebsd"):
            assert data.daemon_log_path(Daemon(name="hle_ha")) == Path("/var/log/hle_ha.log")

    def test_systemd_goes_to_journalctl(self):
        with patch("hle_client.service_cmd.current_platform", return_value="linux"):
            assert data.daemon_log_path(DAEMON) is None

    def test_launchd_reads_the_path_out_of_the_plist(self, tmp_path):
        import plistlib

        plist = tmp_path / "world.hle.ha.plist"
        plist.write_bytes(plistlib.dumps({"StandardOutPath": "/somewhere/ha.log"}))
        with (
            patch("hle_client.service_cmd.current_platform", return_value="darwin"),
            patch("hle_client.service_cmd.service_file", return_value=plist),
        ):
            assert data.daemon_log_path(DAEMON) == Path("/somewhere/ha.log")

    async def test_a_file_is_tailed_to_the_cap(self, tmp_path):
        log = tmp_path / "hle_ha.log"
        log.write_text("\n".join(f"line {i}" for i in range(500)))
        with patch.object(data, "daemon_log_path", return_value=log):
            text = await data.daemon_log_tail(DAEMON)
        lines = text.splitlines()
        assert len(lines) == data.LOG_LINES
        assert lines[-1] == "line 499"

    async def test_a_missing_file_is_an_error_with_a_hint(self, tmp_path):
        with (
            patch.object(data, "daemon_log_path", return_value=tmp_path / "nope.log"),
            pytest.raises(HleError) as err,
        ):
            await data.daemon_log_tail(DAEMON)
        assert "hle daemon status --label ha --system" in (err.value.hint or "")


# ---------------------------------------------------------------------------
# Agents, and the create placeholder
# ---------------------------------------------------------------------------


class TestAgentPane:
    async def test_the_highlighted_agent_is_described(self):
        with patch.object(data, "collect", Sequence(snapshot(HA))):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test(size=WIDE) as pilot:
                await settle(pilot)
                text = text_of(app.query_one("#agent-detail"))
                assert "trikala" in text
                assert "2609.8" in text
                assert "rpi" in text
                assert "endpoint list" in text  # the gap is named, not hidden

    def test_fields_the_relay_adds_later_are_shown(self):
        agent = Agent.from_api(
            {
                "name": "nas",
                "install_method": "pipx",
                "platform": "linux",
                "endpoints": [{"label": "ha", "service_url": "http://localhost:8123"}],
            }
        )
        fields, endpoints, gaps = data.agent_detail(agent)
        assert ("Install method", "pipx") in fields
        assert ("Platform", "linux") in fields
        assert endpoints == ["ha  http://localhost:8123"]
        assert gaps == []


class TestCreatePlaceholder:
    async def test_create_points_at_the_cli(self):
        with patch.object(data, "collect", Sequence(snapshot(HA))):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("n")
                await settle(pilot)
                assert app.last_cli == data.CLI_CREATE


class TestVocabulary:
    async def test_the_third_tab_is_daemons(self):
        with patch.object(data, "collect", Sequence(snapshot(HA))):
            app = HleApp(api_key=KEY, refresh_seconds=0)
            async with app.run_test() as pilot:
                await settle(pilot)
                tabs = app.query_one(TabbedContent)
                assert str(tabs.get_tab("daemons").label) == "Daemons"
