"""Tests for the agent reconciler (no live server)."""

from __future__ import annotations

import asyncio

from hle_client.agent import AgentClient
from hle_common.agent_protocol import EndpointSpec


class FakeTunnel:
    """Records lifecycle calls; connect() blocks until cancelled like the real one."""

    def __init__(self, config) -> None:
        self.config = config
        self.connected = False
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connected = True
        try:
            await asyncio.Event().wait()  # block forever until task cancelled
        except asyncio.CancelledError:
            self.connected = False
            raise

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def public_url(self) -> str | None:
        zone = self.config.zone or "hle.world"
        return f"https://{self.config.service_label}.{zone}"


def _make_client() -> tuple[AgentClient, list[FakeTunnel]]:
    created: list[FakeTunnel] = []

    def factory(cfg):
        t = FakeTunnel(cfg)
        created.append(t)
        return t

    client = AgentClient("hlea_test", tunnel_factory=factory)
    return client, created


def _spec(label: str, url: str = "http://localhost:8000", **kw) -> EndpointSpec:
    return EndpointSpec(id=hash(label) % 1000, label=label, service_url=url, **kw)


class TestReconcile:
    async def test_starts_new_endpoints(self):
        client, created = _make_client()
        await client.reconcile([_spec("ha"), _spec("git")])
        await asyncio.sleep(0)  # let connect tasks start
        assert {t.config.service_label for t in created} == {"ha", "git"}
        assert all(t.connected for t in created)
        await client._stop_all()

    async def test_removes_dropped_endpoints(self):
        client, created = _make_client()
        await client.reconcile([_spec("ha"), _spec("git")])
        await asyncio.sleep(0)
        await client.reconcile([_spec("ha")])  # git removed
        git = next(t for t in created if t.config.service_label == "git")
        assert git.disconnect_calls == 1
        assert "git" not in client._endpoints
        assert "ha" in client._endpoints
        await client._stop_all()

    async def test_unchanged_spec_does_not_restart(self):
        client, created = _make_client()
        await client.reconcile([_spec("ha")])
        await asyncio.sleep(0)
        await client.reconcile([_spec("ha")])  # identical
        assert len(created) == 1  # no new tunnel created
        await client._stop_all()

    async def test_changed_spec_restarts(self):
        client, created = _make_client()
        await client.reconcile([_spec("ha", url="http://localhost:8000")])
        await asyncio.sleep(0)
        await client.reconcile([_spec("ha", url="http://localhost:9999")])
        await asyncio.sleep(0)
        assert len(created) == 2  # old stopped, new started
        assert created[0].disconnect_calls == 1
        assert created[1].config.service_url == "http://localhost:9999"
        await client._stop_all()

    async def test_zone_passed_through(self):
        client, created = _make_client()
        await client.reconcile([_spec("jelly", zone="t00t.us")])
        await asyncio.sleep(0)
        assert created[0].config.zone == "t00t.us"
        await client._stop_all()

    async def test_build_status_reflects_tunnels(self):
        client, _ = _make_client()
        await client.reconcile([_spec("ha", zone="t00t.us")])
        await asyncio.sleep(0)
        statuses = client._build_status()
        assert len(statuses) == 1
        assert statuses[0].label == "ha"
        assert statuses[0].connected is True
        assert statuses[0].public_url == "https://ha.t00t.us"
        await client._stop_all()

    async def test_stop_all_clears(self):
        client, created = _make_client()
        await client.reconcile([_spec("a"), _spec("b")])
        await asyncio.sleep(0)
        await client._stop_all()
        assert client._endpoints == {}
        assert all(t.disconnect_calls == 1 for t in created)


class TestControlUri:
    def test_wss_for_remote(self):
        client = AgentClient("hlea_x", relay_host="hle.world", relay_port=443)
        assert client.control_uri == "wss://hle.world:443/_hle/agent"

    def test_ws_for_localhost(self):
        client = AgentClient("hlea_x", relay_host="localhost", relay_port=8000)
        assert client.control_uri == "ws://localhost:8000/_hle/agent"
