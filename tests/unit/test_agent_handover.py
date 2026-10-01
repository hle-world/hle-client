"""Stage B: zero-drop canary handover (protocol 1.2 successor_of + HANDOVER 4011).

The successor is the new version run as a canary; the incumbent keeps serving
until the relay hands the identity over. Nothing here spawns a real process:
the spawner is injected, exactly as the updater is for Stage A.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from pathlib import Path

import pytest
import websockets.exceptions
from websockets.frames import Close

from hle_client import __version__, agent_update
from hle_client.agent import AgentClient
from hle_client.agent_update import UpdateSupport
from hle_common import close_codes
from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    HANDOVER_CAPABILITY,
    AgentHello,
    EndpointSpec,
    UpdateRequest,
)

NEW = f"{__version__}.99"
SUPPORTED_VENV = UpdateSupport(True, "venv")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeTunnel:
    def __init__(self, config) -> None:
        self.config = config
        self.connected = False
        self.active_ws_streams = 0

    async def connect(self) -> None:
        self.connected = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connected = False
            raise

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def public_url(self) -> str | None:
        return f"https://{self.config.service_label}.hle.world"


class FakeUpdater:
    """The pieces of an updater the handover path touches."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def stage(self, version: str) -> Path:
        self.calls.append("stage")
        return Path("/tmp") / version

    def verify(self, version: str) -> None:
        self.calls.append("verify")

    def swap(self, version: str, *, from_version: str) -> None:
        self.calls.append("swap")

    def rollback(self) -> str:
        self.calls.append("rollback")
        return "previous"


class FakeProc:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.signals: list[int] = []
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class FakeWs:
    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.fail_send = fail_send

    async def send(self, raw: str) -> None:
        if self.fail_send:
            raise ConnectionError("control channel is gone")
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def of_type(self, mtype: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == mtype]


def make_agent(
    tmp_path: Path,
    *,
    support: UpdateSupport = SUPPORTED_VENV,
    updater: FakeUpdater | None = None,
    spawner=None,
    successor_of: str | None = None,
    successor_nonce: str | None = None,
    health_timeout: float = 0.2,
) -> tuple[AgentClient, FakeUpdater, list[FakeTunnel]]:
    tunnels: list[FakeTunnel] = []
    up = updater or FakeUpdater()

    def factory(cfg):
        t = FakeTunnel(cfg)
        tunnels.append(t)
        return t

    client = AgentClient(
        "hlea_test",
        tunnel_factory=factory,
        home=tmp_path,
        support_probe=lambda: support,
        updater_factory=lambda s: up,
        successor_spawner=spawner,
        successor_of=successor_of,
        successor_nonce=successor_nonce,
        health_timeout=health_timeout,
    )
    # The tests drive `_run_update` directly; `run()` is what normally sets this.
    client._running = True
    return client, up, tunnels


def request(**kw) -> UpdateRequest:
    base = {"request_id": "r1", "target_version": NEW}
    base.update(kw)
    return UpdateRequest.model_validate(base)


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
class TestHandoverProtocol:
    def test_capability_name(self):
        assert HANDOVER_CAPABILITY == "handover"

    def test_request_nonce_defaults_to_none(self):
        assert request().successor_nonce is None

    def test_request_round_trips_the_nonce(self):
        req = request(successor_nonce="n0nce")
        again = UpdateRequest.model_validate_json(req.model_dump_json())
        assert again.successor_nonce == "n0nce"

    def test_an_old_request_without_the_nonce_still_parses(self):
        req = UpdateRequest.model_validate({"request_id": "r", "target_version": NEW})
        assert req.successor_nonce is None

    def test_handover_is_not_fatal_but_does_not_reconnect(self):
        assert not close_codes.is_fatal(close_codes.HANDOVER)
        assert not close_codes.should_reconnect(close_codes.HANDOVER)

    def test_protocol_version_still_carries_the_handover_fields(self):
        assert tuple(int(p) for p in AGENT_PROTOCOL_VERSION.split(".")) >= (1, 2)


# --------------------------------------------------------------------------- #
# Hello
# --------------------------------------------------------------------------- #
class _HelloWs:
    def __init__(self, welcome: str) -> None:
        self.sent: list[str] = []
        self._welcome = welcome

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def recv(self) -> str:
        return self._welcome

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def __aenter__(self) -> _HelloWs:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


_WELCOME = json.dumps({"type": "welcome", "agent_public_id": "pub-1", "base_domain": "hle.world"})


async def _hello_sent(monkeypatch, client: AgentClient) -> dict:
    import hle_client.agent as agent_mod

    ws = _HelloWs(_WELCOME)
    monkeypatch.setattr(agent_mod.websockets, "connect", lambda *a, **kw: ws)
    monkeypatch.setattr(agent_mod, "active_providers", list)

    async def no_discovery(_ws) -> None:
        return None

    monkeypatch.setattr(client, "_report_discovery", no_discovery)
    await client._connect_once()
    return json.loads(ws.sent[0])


class TestHelloHandover:
    async def test_a_self_updatable_agent_advertises_handover(self, tmp_path, monkeypatch):
        client, _, _ = make_agent(tmp_path)
        hello = await _hello_sent(monkeypatch, client)
        assert HANDOVER_CAPABILITY in hello["capabilities"]
        assert hello["successor_of"] is None
        assert hello["successor_nonce"] is None

    async def test_an_install_that_cannot_update_does_not(self, tmp_path, monkeypatch):
        client, _, _ = make_agent(
            tmp_path, support=UpdateSupport(False, "brew", "unsupported:brew")
        )
        hello = await _hello_sent(monkeypatch, client)
        assert HANDOVER_CAPABILITY not in hello["capabilities"]

    async def test_a_canary_announces_what_it_replaces(self, tmp_path, monkeypatch):
        client, _, _ = make_agent(tmp_path, successor_of="inst-old", successor_nonce="n0nce")
        hello = await _hello_sent(monkeypatch, client)
        assert hello["successor_of"] == "inst-old"
        assert hello["successor_nonce"] == "n0nce"
        # A one-off canary model serialises the same fields.
        again = AgentHello.model_validate(hello)
        assert (again.successor_of, again.successor_nonce) == ("inst-old", "n0nce")


# --------------------------------------------------------------------------- #
# Canary
# --------------------------------------------------------------------------- #
class TestCanaryHealth:
    async def test_a_canary_that_never_connects_exits_without_a_fuss(self, tmp_path):
        client, up, tunnels = make_agent(
            tmp_path, successor_of="inst-old", successor_nonce="n0nce", health_timeout=0.05
        )
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        tunnels[0].connected = False  # never comes up
        ws = FakeWs()

        await client._after_welcome(ws)
        assert client._canary_task is not None
        await client._canary_task

        assert client.exit_code == 1
        assert client._running is False
        assert ws.closed
        # The incumbent is untouched: the canary never rolls the install back.
        assert up.calls == []
        await client._stop_all()

    async def test_a_healthy_canary_keeps_serving(self, tmp_path):
        client, _, _ = make_agent(
            tmp_path, successor_of="inst-old", successor_nonce="n0nce", health_timeout=5.0
        )
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        client._running = True
        ws = FakeWs()
        await client._after_welcome(ws)
        await client._canary_task
        assert client.exit_code == 0
        assert client._running is True
        assert not ws.closed
        # Once it is serving, it is an ordinary agent, not a successor.
        assert client._canary_probation is False
        assert (client._successor_of, client._successor_nonce) == (None, None)
        await client._stop_all()

    async def test_a_healthy_canary_reconnect_omits_the_successor_fields(
        self, tmp_path, monkeypatch
    ):
        client, _, tunnels = make_agent(
            tmp_path, successor_of="inst-old", successor_nonce="n0nce", health_timeout=5.0
        )
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        client._running = True
        tunnels[0].connected = True
        await client._after_welcome(FakeWs())
        await client._canary_task

        hello = await _hello_sent(monkeypatch, client)
        assert hello["successor_of"] is None
        assert hello["successor_nonce"] is None
        # No longer a canary, so it may start its own update again.
        assert client._refuse_update_reason(request(request_id="r2")) is None
        await client._stop_all()

    async def test_a_canary_that_never_gets_welcome_exits_non_zero(self, tmp_path):
        # The incumbent always writes update.json, so the real canary starts in
        # the `watch` state. It must still skip rollback and just exit.
        state = agent_update.UpdateState(
            request_id="r1",
            from_version="2609.7",
            to_version=__version__,
            started_at=time.time(),
        )
        agent_update.write_update_state(tmp_path, state)
        up = FakeUpdater()
        client = AgentClient(
            "hlea_test",
            home=tmp_path,
            successor_of="inst-old",
            successor_nonce="n0nce",
            updater_factory=lambda s: up,
            health_timeout=0.05,
        )
        client._running = True
        client._arm_watchdog()
        assert client._boot.kind == "watch"
        if client._watchdog_task is not None:
            client._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client._watchdog_task
            client._watchdog_task = None

        await client._watchdog_timer()

        assert client.exit_code == 1
        assert client._running is False
        assert up.calls == []  # the incumbent owns rollback, not the canary

    async def test_a_canary_without_update_state_still_expires(self, tmp_path):
        # A canary may boot with no update.json at all (the incumbent's state
        # was cleared, or it was started by hand). It must still die on its
        # deadline instead of reconnecting forever while the incumbent serves.
        up = FakeUpdater()
        client = AgentClient(
            "hlea_test",
            home=tmp_path,
            successor_of="inst-old",
            successor_nonce="n0nce",
            updater_factory=lambda s: up,
            health_timeout=0.01,
        )
        client._running = True
        client._arm_watchdog()
        assert client._boot.kind != "watch"
        assert client._watchdog_task is not None
        await client._watchdog_task

        assert client.exit_code == 1
        assert client._running is False
        # The incumbent owns the rollback; the canary never touches the install.
        assert up.calls == []

    async def test_a_canary_with_update_state_still_confirms_health(self, tmp_path):
        # The incumbent always writes update.json, so the real canary boots in
        # the `watch` state; that must not skip proving the endpoints.
        state = agent_update.UpdateState(
            request_id="r1",
            from_version="2609.7",
            to_version=__version__,
            started_at=time.time(),
        )
        agent_update.write_update_state(tmp_path, state)
        client, _, tunnels = make_agent(
            tmp_path, successor_of="inst-old", successor_nonce="n0nce", health_timeout=5.0
        )
        client._boot = agent_update.BootCheck("watch", state)
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        client._running = True
        tunnels[0].connected = True
        ws = FakeWs()

        await client._after_welcome(ws)
        assert client._canary_task is not None
        await client._canary_task
        assert client._canary_probation is False

        if client._health_task is not None:
            client._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client._health_task
        await client._stop_all()

    async def test_a_canary_refuses_to_start_its_own_update(self, tmp_path):
        client, _, _ = make_agent(tmp_path, successor_of="inst-old", successor_nonce="n0nce")
        assert client._refuse_update_reason(request(successor_nonce="other")) == "busy:updating"


# --------------------------------------------------------------------------- #
# Incumbent
# --------------------------------------------------------------------------- #
class TestIncumbentHandover:
    async def test_stage_b_spawns_a_successor_instead_of_exiting(self, tmp_path):
        spawned: list[tuple[list[str], dict[str, str]]] = []

        def spawner(argv, *, env=None):
            spawned.append((argv, env or {}))
            return FakeProc()

        client, up, _ = make_agent(tmp_path, spawner=spawner)
        await client._run_update(request(successor_nonce="n0nce"), FakeWs())

        assert up.calls == ["stage", "verify", "swap"]
        assert len(spawned) == 1
        argv, env = spawned[0]
        assert argv[1:3] == ["agent", "run"]
        assert "--successor-of" in argv
        assert "--successor-nonce" in argv
        assert argv[argv.index("--successor-nonce") + 1] == "n0nce"
        # The token travels in the environment, never on the command line.
        assert env["HLE_AGENT_TOKEN"] == "hlea_test"
        assert not [a for a in argv if a.startswith("hlea_")]
        # It stays up: no restart, no exit, the incumbent keeps its tunnels.
        assert client.exit_code == 0
        assert client._running is True
        assert client._update_in_flight is True
        assert client._successor_task is not None
        client._successor_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client._successor_task

    async def test_without_a_nonce_it_is_still_stage_a(self, tmp_path):
        spawned: list[list[str]] = []

        def spawner(argv, *, env=None):
            spawned.append(argv)
            return FakeProc()

        client, up, _ = make_agent(tmp_path, spawner=spawner)
        ws = FakeWs()
        await client._run_update(request(), ws)
        assert spawned == []  # no canary
        assert up.calls == ["stage", "verify", "swap"]
        assert client.exit_code == 1
        assert ws.closed

    async def test_a_successor_that_dies_is_reported_and_the_incumbent_stays(self, tmp_path):
        client, up, _ = make_agent(
            tmp_path, spawner=lambda argv, *, env=None: FakeProc(returncode=1)
        )
        ws = FakeWs()
        await client._run_update(request(successor_nonce="n0nce"), ws)
        await client._successor_task

        result = ws.of_type("update_result")[0]
        assert result["ok"] is False
        assert result["request_id"] == "r1"
        assert result["from_version"] == __version__
        assert result["to_version"] == NEW
        assert "rollback" in up.calls
        assert client._update_in_flight is False
        assert client._running is True
        assert client.exit_code == 0

    async def test_a_successor_that_cannot_start_is_reported_and_keeps_running(self, tmp_path):
        def boom(argv, *, env=None):
            raise OSError("cannot exec")

        client, up, _ = make_agent(tmp_path, spawner=boom)
        ws = FakeWs()
        await client._run_update(request(successor_nonce="n0nce"), ws)

        assert ws.of_type("update_result")[0]["ok"] is False
        assert client._update_in_flight is False
        assert client._running is True
        assert client.exit_code == 0
        assert "rollback" in up.calls

    async def test_a_second_update_while_one_is_in_flight_is_busy(self, tmp_path):
        client, _, _ = make_agent(tmp_path, spawner=lambda argv, *, env=None: FakeProc())
        await client._run_update(request(successor_nonce="n0nce"), FakeWs())
        assert client._refuse_update_reason(request(request_id="r2")) == "busy:updating"
        client._successor_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client._successor_task

    async def test_retake_restarts_only_stood_down_endpoints(self, tmp_path):
        client, _, tunnels = make_agent(tmp_path)
        await client.reconcile(
            [
                EndpointSpec(id=1, label="up", service_url="http://x"),
                EndpointSpec(id=2, label="down", service_url="http://y"),
            ]
        )
        await asyncio.sleep(0)
        tunnels[0].connected = True  # healthy incumbent tunnel
        tunnels[1].connected = False  # stood down for the successor
        healthy = tunnels[0]
        before = len(tunnels)

        await client._retake_endpoints()

        assert len(tunnels) == before + 1  # only "down" was started again
        assert client._endpoints["up"].tunnel is healthy
        assert client._endpoints["down"].tunnel is tunnels[-1]
        await client._stop_all()


# --------------------------------------------------------------------------- #
# REPLACED while an update is in flight
# --------------------------------------------------------------------------- #
class TestReplacedDuringUpdate:
    @staticmethod
    def _closed(code: int, reason: str = "") -> websockets.exceptions.ConnectionClosedError:
        return websockets.exceptions.ConnectionClosedError(Close(code, reason), None)

    async def _run_once(self, monkeypatch, client: AgentClient, exc) -> list[float]:
        slept: list[float] = []

        async def fake_connect_once() -> None:
            client._registered = True
            raise exc

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await client.run()
        return slept

    async def test_replaced_during_an_update_keeps_tunnels_and_waits(self, monkeypatch):
        client = AgentClient("hlea_x")
        client._running = True
        client._update_in_flight = True
        stopped: list[bool] = []

        async def fake_stop_all() -> None:
            stopped.append(True)

        monkeypatch.setattr(client, "_stop_all", fake_stop_all)
        slept = await self._run_once(monkeypatch, client, self._closed(4009, "overlap"))
        assert slept == []
        assert client.fatal_error is None
        # The incumbent kept every tunnel and did not reconnect: the successor
        # owns the control channel now.
        assert stopped == []
        assert client._running is True

    async def test_wait_for_successor_control_returns_when_the_successor_fails(self):
        client = AgentClient("hlea_x")
        client._running = True
        client._update_in_flight = True
        proc = FakeProc()
        client._successor_proc = proc
        client._successor_task = asyncio.create_task(
            client._watch_successor(request(successor_nonce="n"), FakeWs(fail_send=True), poll=0.01)
        )
        proc.returncode = 1

        await client._wait_for_successor_control()

        assert client._update_in_flight is False  # reported; ready to resume
        assert client._handover_done is False
        # The control channel was gone (that is what REPLACED means), so the
        # failure waits for the next welcome to be reported.
        assert client._pending_result is not None

    async def test_replaced_without_an_update_is_still_fatal(self, monkeypatch):
        client = AgentClient("hlea_x")
        client._update_in_flight = False
        slept = await self._run_once(monkeypatch, client, self._closed(4009, "taken"))
        assert slept == []
        assert client.fatal_error is not None

    async def test_replaced_then_all_tunnels_stood_down_becomes_supervisor(
        self, tmp_path, monkeypatch
    ):
        # After a REPLACED the control socket is gone, so HANDOVER can never
        # arrive there. The data plane is the signal: once the relay has taken
        # every tunnel the handover is done and the incumbent supervises.
        client, _, _ = make_agent(tmp_path, spawner=lambda argv, *, env=None: FakeProc())
        proc = client._successor_proc = FakeProc()
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        client._endpoints["ha"].task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await client._endpoints["ha"].task
        assert client._endpoints["ha"].task.done()
        client._update_in_flight = True
        client._successor_task = asyncio.create_task(
            client._watch_successor(request(successor_nonce="n"), FakeWs(), poll=0.01)
        )

        async def fake_connect_once() -> None:
            client._registered = True
            raise self._closed(close_codes.REPLACED, "overlap")

        async def fake_sleep(seconds: float) -> None:
            proc.returncode = 5  # the supervised successor exits here

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await client.run()

        assert client._handover_done is True
        assert client._update_in_flight is False
        assert client.exit_code == 5  # the child's status became ours


# --------------------------------------------------------------------------- #
# Handover: the incumbent supervises the successor instead of exiting
# --------------------------------------------------------------------------- #
class TestSupervisor:
    @staticmethod
    def _closed(code: int, reason: str = "") -> websockets.exceptions.ConnectionClosedError:
        return websockets.exceptions.ConnectionClosedError(Close(code, reason), None)

    async def test_handover_stops_tunnels_and_exits_with_the_child_code(self, monkeypatch):
        client = AgentClient("hlea_x")
        client._running = True
        client._update_in_flight = True
        proc = FakeProc()
        client._successor_proc = proc
        stopped: list[bool] = []

        async def fake_connect_once() -> None:
            client._registered = True
            raise self._closed(close_codes.HANDOVER, "handover")

        async def fake_stop_all() -> None:
            stopped.append(True)

        async def fake_sleep(seconds: float) -> None:
            proc.returncode = 7  # the successor exits while we wait on it

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(client, "_stop_all", fake_stop_all)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await client.run()

        assert stopped == [True]  # its own tunnels stopped
        assert client.fatal_error is None  # not an error
        assert client.exit_code == 7  # the child's exit code became ours

    async def test_supervisor_waits_and_forwards_signals(self, monkeypatch):
        client = AgentClient("hlea_x")
        proc = FakeProc()
        client._successor_proc = proc
        handlers: dict[int, tuple] = {}

        loop = asyncio.get_running_loop()

        def fake_add(sig, cb, *args):
            handlers[sig] = (cb, args)

        monkeypatch.setattr(loop, "add_signal_handler", fake_add)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: None)

        async def fake_sleep(seconds: float) -> None:
            if proc.returncode is None:
                cb, args = handlers[signal.SIGTERM]
                cb(*args)
                proc.returncode = 0

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await client._supervise_successor()

        assert signal.SIGTERM in proc.signals
        assert client.exit_code == 0

    async def test_supervisor_maps_a_signal_death_to_128_plus_signal(self, monkeypatch):
        client = AgentClient("hlea_x")
        proc = FakeProc()
        client._successor_proc = proc

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **kw: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: None)

        async def fake_sleep(seconds: float) -> None:
            proc.returncode = -15  # killed by SIGTERM

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await client._supervise_successor()

        assert client.exit_code == 143  # 128 + SIGTERM, not 241


# --------------------------------------------------------------------------- #
# Stopping the incumbent while a handover is in flight
# --------------------------------------------------------------------------- #
class TestHandoverStop:
    @staticmethod
    def _closed(code: int, reason: str = "") -> websockets.exceptions.ConnectionClosedError:
        return websockets.exceptions.ConnectionClosedError(Close(code, reason), None)

    async def test_cancel_during_in_flight_update_terminates_the_successor(
        self, tmp_path, monkeypatch
    ):
        client, _, _ = make_agent(tmp_path, spawner=lambda argv, *, env=None: FakeProc())
        await client._run_update(request(successor_nonce="n0nce"), FakeWs())
        proc = client._successor_proc
        assert proc is not None

        started = asyncio.Event()

        async def fake_connect_once() -> None:
            client._registered = True
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        task = asyncio.create_task(client.run())
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # The successor runs in its own session, so the stop never reached it;
        # the incumbent must terminate it rather than orphan it.
        assert signal.SIGTERM in proc.signals
        assert proc.poll() is not None

    async def test_cancel_during_replaced_hold_terminates_the_successor(
        self, tmp_path, monkeypatch
    ):
        client, _, _ = make_agent(tmp_path, spawner=lambda argv, *, env=None: FakeProc())
        await client._run_update(request(successor_nonce="n0nce"), FakeWs())
        proc = client._successor_proc
        assert proc is not None
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)  # endpoint task running, never stands down

        async def fake_connect_once() -> None:
            client._registered = True
            raise self._closed(close_codes.REPLACED, "overlap")

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)  # let it reach the hold
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert signal.SIGTERM in proc.signals
        assert proc.poll() is not None

    async def test_cancel_between_handover_and_supervision_terminates_the_successor(
        self, monkeypatch
    ):
        """A stop landing while the incumbent stands its tunnels down after 4011."""
        client = AgentClient("hlea_x")
        client._running = True
        client._update_in_flight = True
        proc = FakeProc()
        client._successor_proc = proc
        stopping = asyncio.Event()

        async def fake_connect_once() -> None:
            client._registered = True
            raise self._closed(close_codes.HANDOVER, "handover")

        async def slow_stop_all() -> None:
            stopping.set()
            await asyncio.Event().wait()  # cancelled here, before supervision

        monkeypatch.setattr(client, "_connect_once", fake_connect_once)
        monkeypatch.setattr(client, "_stop_all", slow_stop_all)
        task = asyncio.create_task(client.run())
        await stopping.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert client._handover_done is True
        assert signal.SIGTERM in proc.signals  # not orphaned
        assert proc.poll() is not None


class TestStoodDownRace:
    async def test_a_dead_successor_is_not_supervised_even_if_tunnels_stood_down(self):
        """It took the tunnels and died within a poll: report, don't supervise."""
        client = AgentClient("hlea_x")
        client._running = True
        client._update_in_flight = True
        proc = FakeProc(returncode=1)  # already dead
        client._successor_proc = proc
        client._all_tunnels_stood_down = lambda: True  # type: ignore[method-assign]
        client._successor_task = asyncio.create_task(
            client._watch_successor(request(successor_nonce="n"), FakeWs(fail_send=True), poll=0.01)
        )

        await client._wait_for_successor_control(poll=0.01)

        assert client._handover_done is False
        assert client._update_in_flight is False  # the watcher reported the failure
        assert client._pending_result is not None


# --------------------------------------------------------------------------- #
# Successor argv
# --------------------------------------------------------------------------- #
class TestSuccessorArgv:
    def test_includes_the_handover_fields(self):
        argv = agent_update.successor_argv(
            "/opt/hle/current/bin/hle",
            successor_of="inst-old",
            successor_nonce="n0nce",
        )
        assert argv == [
            "/opt/hle/current/bin/hle",
            "agent",
            "run",
            "--successor-of",
            "inst-old",
            "--successor-nonce",
            "n0nce",
        ]

    def test_carries_the_relay_when_known(self):
        argv = agent_update.successor_argv(
            "hle",
            successor_of="i",
            successor_nonce="n",
            relay_host="relay.example",
            relay_port=8443,
        )
        assert argv[argv.index("--relay-host") + 1] == "relay.example"
        assert argv[argv.index("--relay-port") + 1] == "8443"

    def test_no_secret_goes_on_the_command_line(self):
        argv = agent_update.successor_argv("hle", successor_of="i", successor_nonce="n")
        assert not [a for a in argv if a.startswith("hlea_")]
