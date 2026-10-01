"""The data layer is the CLI's, not a copy of it.

The one live bug this dashboard has shipped was a field name: it read
``is_online`` while the relay sent ``online``, and every agent showed offline.
``hle status`` had the same bug independently. Both now read the same ``ops``
models, and this file holds the proof: one relay payload, two front ends,
one answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from hle_client import status_cmd

from hle_tui import data

_KEY = "hle_" + "a" * 32

PAYLOAD_AGENTS = [
    {"name": "rpi", "online": True, "is_active": True, "endpoint_count": 2},
    {"name": "nas", "online": False, "is_active": True},
    {"name": "old", "online": False, "is_active": False},
]
PAYLOAD_TUNNELS = [
    {"subdomain": "ha-x7k", "tunnel_id": "t1", "is_active": True, "service_url": "http://a"},
    {"subdomain": "old-x7k", "tunnel_id": "t2", "is_active": False},
]


def _fake_client(*, agents=PAYLOAD_AGENTS, tunnels=PAYLOAD_TUNNELS) -> AsyncMock:
    client = AsyncMock()
    client.list_agents = AsyncMock(return_value=agents)
    client.list_tunnels = AsyncMock(return_value=tunnels)
    client.get_me = AsyncMock(return_value={"user_code": "x7k"})
    return client


class TestStatusAndDashboardAgree:
    @pytest.mark.asyncio
    async def test_the_same_payload_gives_the_same_online_flags(self):
        client = _fake_client()
        with (
            patch("hle_client.api.ApiClient", return_value=client),
            patch("hle_client.ops.daemon.list_daemons", AsyncMock(return_value=[])),
        ):
            snapshot = await data.collect(_KEY)
            tunnels, agents = await status_cmd._remote(_KEY)

        assert [a.online for a in snapshot.agents] == [a.online for a in agents]
        assert [a.state for a in snapshot.agents] == [a.state for a in agents]
        assert [t.online for t in snapshot.tunnels] == [t.online for t in tunnels]
        # And both agree with the relay.
        assert [a.online for a in agents] == [True, False, False]
        assert [a.state for a in agents] == ["online", "offline", "disabled"]
        assert [t.online for t in tunnels] == [True, False]

    @pytest.mark.asyncio
    async def test_an_unreachable_relay_is_none_not_empty(self):
        import httpx

        client = _fake_client()
        client.list_tunnels = AsyncMock(side_effect=httpx.ConnectError("down"))
        client.list_agents = AsyncMock(side_effect=httpx.ConnectError("down"))
        with (
            patch("hle_client.api.ApiClient", return_value=client),
            patch("hle_client.ops.daemon.list_daemons", AsyncMock(return_value=[])),
        ):
            snapshot = await data.collect(_KEY)
        assert snapshot.tunnels is None
        assert snapshot.agents is None


class TestRows:
    def test_agent_rows_say_online_offline_disabled(self):
        from hle_client.ops.models import Agent

        snapshot = data.Snapshot(agents=[Agent.from_api(a) for a in PAYLOAD_AGENTS])
        assert [row[1] for row in data.agent_rows(snapshot)] == ["online", "offline", "disabled"]

    def test_public_url_comes_from_the_record(self):
        from hle_client.ops.models import Tunnel

        snapshot = data.Snapshot(
            tunnels=[Tunnel.from_api({"subdomain": "ha-x7k", "public_url": "https://ha.t00t.us"})]
        )
        assert data.public_url(snapshot, "ha-x7k") == "https://ha.t00t.us"
        assert data.public_url(snapshot, "unknown-x7k") == "https://unknown-x7k.hle.world"


class TestDelete:
    @pytest.mark.asyncio
    async def test_a_live_tunnel_is_refused_with_the_cli_wording(self):
        client = _fake_client()
        with patch("hle_client.api.ApiClient", return_value=client):
            line = await data.delete_tunnel("ha-x7k", _KEY)
        assert "connected" in line
        client.delete_tunnel_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_idle_tunnel_is_deleted(self):
        client = _fake_client()
        with patch("hle_client.api.ApiClient", return_value=client):
            line = await data.delete_tunnel("old-x7k", _KEY)
        assert line == "Deleted old-x7k."
        client.delete_tunnel_record.assert_awaited_once_with("t2")

    @pytest.mark.asyncio
    async def test_a_vanished_tunnel_is_reported_not_raised(self):
        client = _fake_client()
        with patch("hle_client.api.ApiClient", return_value=client):
            line = await data.delete_tunnel("gone-x7k", _KEY)
        assert "not in the list" in line


class TestEquivalentCommandsTeachTheCanonicalVerbs:
    """The status-bar hint is documentation: it must show the spelling `--help` does.

    Each of these emitted an old verb (`access add`, `auth-mode --set`,
    `share revoke`, `pin remove`) that still works through a hidden alias but
    is not what the CLI teaches. A dashboard whose whole point is "everything
    here can be scripted afterwards" cannot hand out the retired spelling.
    """

    def test_access_add_is_create(self):
        assert (
            data.cli_access_add("ha-x7k", "cy@example.com")
            == "hle tunnel access create ha-x7k cy@example.com"
        )

    def test_access_add_carries_the_provider(self):
        assert (
            data.cli_access_add("ha-x7k", "github:cy@example.com")
            == "hle tunnel access create ha-x7k cy@example.com --provider github"
        )

    def test_access_remove_is_delete(self):
        assert data.cli_access_remove("ha-x7k", 12) == "hle tunnel access delete ha-x7k 12"

    def test_auth_mode_is_set_auth(self):
        assert data.cli_auth_mode("ha-x7k", "none") == "hle tunnel set ha-x7k --auth none"

    def test_pin_verbs_are_positional(self):
        assert data.cli_pin("ha-x7k", "set") == "hle tunnel pin set ha-x7k"
        assert data.cli_pin("ha-x7k", "delete") == "hle tunnel pin delete ha-x7k"

    def test_basic_auth_verbs_are_canonical(self):
        assert data.cli_basic_auth("ha-x7k", "set") == "hle tunnel basic-auth set ha-x7k"
        assert data.cli_basic_auth("ha-x7k", "delete") == "hle tunnel basic-auth delete ha-x7k"

    def test_share_create_names_the_link_with_name(self):
        assert (
            data.cli_share_create("ha-x7k", "24h", "for-mum")
            == "hle tunnel share create ha-x7k --duration 24h --name 'for-mum'"
        )

    def test_share_create_omits_an_empty_name(self):
        assert (
            data.cli_share_create("ha-x7k", "1h", "")
            == "hle tunnel share create ha-x7k --duration 1h"
        )

    def test_share_revoke_is_delete(self):
        assert data.cli_share_revoke("ha-x7k", 5) == "hle tunnel share delete ha-x7k 5"

    def test_daemon_name_is_positional(self):
        from hle_client.ops.models import Daemon

        daemon = Daemon(name="hle-ha.service", scope="system", kind="tunnel", label="ha")
        assert data.cli_daemon("restart", daemon) == "hle daemon restart ha"
        # The scope flag is kept only where the verb accepts it: `restart`
        # works the scope out itself and has no --user/--system.
        assert data.cli_daemon("logs", daemon) == "hle daemon logs ha --system"
        assert data.cli_daemon("status", daemon) == "hle daemon status ha --system"

    def test_an_agent_daemon_uses_its_label_not_a_flag(self):
        from hle_client.ops.models import Daemon

        daemon = Daemon(name="hle-agent.service", scope="user", kind="agent", label="agent")
        assert data.cli_daemon("logs", daemon) == "hle daemon logs agent --user"

    def test_a_daemon_without_a_label_falls_back_to_its_name(self):
        from hle_client.ops.models import Daemon

        daemon = Daemon(name="hle_old", scope="system", kind="unknown", label=None)
        assert data.cli_daemon("status", daemon) == "hle daemon status hle_old --system"
