"""Tests for the agent reconciler (no live server)."""

from __future__ import annotations

import asyncio
import json

import websockets.exceptions
from websockets.frames import Close

from hle_client.agent import AgentClient
from hle_common.agent_protocol import AgentHello, EndpointSpec


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


class TestReconnectBackoff:
    """The backoff must reset after a session that actually worked.

    Observed in production: an agent that had been up for 19 minutes waited 8s
    to reconnect after a relay restart, because a previous unrelated blip had
    already doubled the delay and nothing ever reset it. Left alone, a
    long-lived agent drifts to the 60s ceiling and every routine deploy costs a
    full minute of downtime.
    """

    async def _delays_for(self, monkeypatch, sessions) -> list[float]:
        """Run run() through `sessions` outcomes, returning the sleeps taken.

        Each session is True (registers, then drops) or False (fails to connect).
        """
        client = AgentClient("hlea_x")
        slept: list[float] = []
        remaining = list(sessions)

        async def fake_connect_once() -> None:
            if not remaining:
                client._running = False
                return
            registered = remaining.pop(0)
            client._registered = registered
            raise ConnectionError("relay went away")

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await client.run()
        return slept

    async def test_backoff_grows_while_connections_keep_failing(self, monkeypatch):
        delays = await self._delays_for(monkeypatch, [False, False, False])
        assert delays[:3] == [1.0, 2.0, 4.0]

    async def test_backoff_resets_after_a_registered_session(self, monkeypatch):
        # Two failures grow the delay to 1s, 2s. The third session registers
        # before dropping, so the next wait is 1s again rather than 4s — the
        # 4.0 in the failure-only case above is exactly what must not appear.
        delays = await self._delays_for(monkeypatch, [False, False, True, False])
        assert delays[:4] == [1.0, 2.0, 1.0, 2.0]

    async def test_a_session_that_never_registered_does_not_reset(self, monkeypatch):
        # Connecting but dying before the welcome is not evidence of health.
        delays = await self._delays_for(monkeypatch, [False, False, False])
        assert delays[2] == 4.0

    async def test_backoff_is_capped(self, monkeypatch):
        client = AgentClient("hlea_x", reconnect_delay=1.0, max_reconnect_delay=5.0)
        slept: list[float] = []
        count = 0

        async def fake_connect_once() -> None:
            nonlocal count
            count += 1
            if count > 6:
                client._running = False
                return
            raise ConnectionError("nope")

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await client.run()
        assert max(slept) == 5.0


class TestControlUri:
    def test_wss_for_remote(self):
        client = AgentClient("hlea_x", relay_host="hle.world", relay_port=443)
        assert client.control_uri == "wss://hle.world:443/_hle/agent"

    def test_ws_for_localhost(self):
        client = AgentClient("hlea_x", relay_host="localhost", relay_port=8000)
        assert client.control_uri == "ws://localhost:8000/_hle/agent"


class TestPreflightRequests:
    """The server asks, the agent probes, the agent answers.

    A reply is always sent — including when the probe fails — because the server
    awaits this request_id, and silence would leave the dashboard spinning until
    timeout with nothing to show for it.
    """

    class FakeWs:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    async def _handle(self, msg, monkeypatch, report=None, boom=None):
        from hle_common.preflight import PreflightReport

        client, _ = _make_client()
        ws = self.FakeWs()

        async def fake_run_preflight(url, **kwargs):
            if boom is not None:
                raise boom
            return report or PreflightReport(
                request_id=kwargs.get("request_id", ""), service_url=url
            )

        monkeypatch.setattr("hle_client.preflight.run_preflight", fake_run_preflight)
        await client._handle_message(json.dumps(msg), ws)
        # The probe is spawned, not awaited, so the control channel stays live.
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        return ws

    async def test_replies_with_a_report_echoing_the_request_id(self, monkeypatch):
        ws = await self._handle(
            {
                "type": "preflight_request",
                "request_id": "req-7",
                "service_url": "http://192.168.1.1",
            },
            monkeypatch,
        )
        assert ws.sent, "the server is waiting on this reply"
        payload = json.loads(ws.sent[0])
        assert payload["type"] == "preflight_report"
        assert payload["request_id"] == "req-7"

    async def test_a_crashing_probe_still_answers(self, monkeypatch):
        """Otherwise the dashboard waits out a timeout to learn nothing."""
        ws = await self._handle(
            {"type": "preflight_request", "request_id": "req-8", "service_url": "http://x"},
            monkeypatch,
            boom=RuntimeError("resolver exploded"),
        )
        assert ws.sent, "a failed probe must still be reported"
        payload = json.loads(ws.sent[0])
        assert payload["request_id"] == "req-8"
        assert "resolver exploded" in payload["error"]

    async def test_a_malformed_request_is_ignored_not_fatal(self, monkeypatch):
        """No request_id means nothing to answer with; it must not kill the agent."""
        ws = await self._handle(
            {"type": "preflight_request", "service_url": "http://x"}, monkeypatch
        )
        assert ws.sent == []

    async def test_the_control_channel_is_not_blocked_while_probing(self, monkeypatch):
        """A slow probe must not stall state_sync or fp frames."""
        client, _ = _make_client()
        ws = self.FakeWs()
        started = asyncio.Event()

        async def slow(url, **kwargs):
            started.set()
            await asyncio.sleep(5)
            raise AssertionError("should not finish in this test")

        monkeypatch.setattr("hle_client.preflight.run_preflight", slow)
        await client._handle_message(
            json.dumps({"type": "preflight_request", "request_id": "r", "service_url": "http://x"}),
            ws,
        )
        # _handle_message has already returned while the probe is still running.
        await asyncio.wait_for(started.wait(), timeout=1)
        assert ws.sent == []
        for task in list(client._preflight_tasks):
            task.cancel()


class TestFatalCloses:
    """Closes the agent must not retry.

    Two agents sharing one enrollment token is the failure this guards. Each
    registers the same endpoints, the relay gives them to whichever connected
    last, and if both keep retrying they take every tunnel off each other about
    once a second — for as long as both are running. Observed on a host with a
    system unit from August and a per-user unit from September: a tunnel that
    reconnected every 1.4 seconds for a day, and 67 minutes of CPU burned by
    each agent.
    """

    @staticmethod
    def _closed(code: int, reason: str = "") -> websockets.exceptions.ConnectionClosedError:
        return websockets.exceptions.ConnectionClosedError(Close(code, reason), None)

    async def _run_until_stopped(self, monkeypatch, exc) -> tuple[AgentClient, list[float]]:
        client = AgentClient("hlea_x")
        slept: list[float] = []
        attempts = 0

        async def fake_connect_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts > 5:
                client._running = False
                return
            client._registered = True
            raise exc

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await client.run()
        return client, slept

    async def test_a_duplicate_instance_stops_the_agent(self, monkeypatch):
        client, slept = await self._run_until_stopped(
            monkeypatch, self._closed(4010, "already connected from nas")
        )
        # It stopped on the first close rather than reconnecting at all.
        assert slept == []
        assert client.fatal_error is not None
        assert "already in use by another agent" in client.fatal_error

    async def test_the_message_names_the_other_machine_and_what_to_do(self, monkeypatch):
        client, _ = await self._run_until_stopped(
            monkeypatch, self._closed(4010, "already connected from nas")
        )
        assert "nas" in client.fatal_error
        assert "hle service list" in client.fatal_error

    async def test_being_replaced_also_stops(self, monkeypatch):
        client, slept = await self._run_until_stopped(monkeypatch, self._closed(4009, "replaced"))
        assert slept == []
        assert "took this token over" in client.fatal_error

    async def test_a_revoked_token_stops_and_says_to_re_enroll(self, monkeypatch):
        client, _ = await self._run_until_stopped(monkeypatch, self._closed(4001, "revoked"))
        assert "re-enroll" in client.fatal_error.lower()

    async def test_endpoints_are_torn_down_on_the_way_out(self, monkeypatch):
        """Stopping must not leave tunnels running on a machine that lost the argument."""
        client, created = _make_client()
        await client.reconcile([_spec("tv")])
        await asyncio.sleep(0)  # let the endpoint task start
        assert created[0].connected is True

        async def fake_connect_once() -> None:
            raise self._closed(4010, "already connected from nas")

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await client.run()

        assert client._endpoints == {}
        assert created[0].connected is False

    async def test_an_ordinary_drop_still_reconnects(self, monkeypatch):
        """The guard must only catch codes that mean stop."""
        client, slept = await self._run_until_stopped(monkeypatch, self._closed(1006, "bye"))
        assert slept  # it kept trying
        assert client.fatal_error is None

    async def test_a_flood_close_waits_rather_than_stopping(self, monkeypatch):
        """4029 says slow down. Giving up would leave the agent down over a transient."""
        client, slept = await self._run_until_stopped(monkeypatch, self._closed(4029, "too fast"))
        assert client.fatal_error is None
        assert slept and slept[0] >= 60.0

    async def test_the_hello_says_which_process_it_comes_from(self):
        """Without this the relay cannot tell a reconnect from a second machine."""
        from hle_client.agent import instance_id

        hello = AgentHello(
            token="hlea_x",
            instance_id=instance_id(),
            hostname="mimos",
        )
        assert hello.instance_id == instance_id()
        assert hello.hostname == "mimos"
