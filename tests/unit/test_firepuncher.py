"""Tests for firepuncher: allowlist enforcement and the byte path."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import websockets

from hle_client.firepuncher import FpAgentSide, FpLocalClient
from hle_common.fp_protocol import (
    ForwardRule,
    FpData,
    FpErrorCode,
    FpMsgType,
    FpOpen,
    default_rules,
    is_allowed,
)


class Collector:
    """Captures frames an fp side would have sent."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, raw: str) -> None:
        self.frames.append(json.loads(raw))

    def of_type(self, mtype: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == mtype]

    def first(self, mtype: str) -> dict | None:
        found = self.of_type(mtype)
        return found[0] if found else None


class TestCloseHandling:
    """Losing the relay must not produce a traceback.

    A relay restart used to crash `hle fp` with a raw
    ConnectionClosedError. Deployments are routine, so the forward has to ride
    through them the way the agent already does.
    """

    def test_relay_restart_is_described_in_plain_words(self):
        from websockets.frames import Close

        from hle_client.fp_cmd import _close_reason

        exc = websockets.exceptions.ConnectionClosedError(Close(1012, "service restart"), None)
        assert _close_reason(exc) == "relay restarting"

    def test_reason_falls_back_to_the_code(self):
        from websockets.frames import Close

        from hle_client.fp_cmd import _close_reason

        exc = websockets.exceptions.ConnectionClosedError(Close(1006, ""), None)
        assert "1006" in _close_reason(exc)

    def test_no_close_frame_is_handled(self):
        from hle_client.fp_cmd import _close_reason

        exc = websockets.exceptions.ConnectionClosedError(None, None)
        assert _close_reason(exc) == "no close frame"

    @pytest.mark.parametrize("code", [4001, 4003])
    def test_credential_failures_are_fatal(self, code):
        """Retrying a rejected key just repeats the rejection."""
        from hle_client.fp_cmd import _FATAL_CLOSE_CODES

        assert code in _FATAL_CLOSE_CODES

    @pytest.mark.parametrize("code", [1012, 1006, 1001, 4404])
    def test_transient_failures_are_retried(self, code):
        """Restarts, network drops, and an offline agent are all recoverable."""
        from hle_client.fp_cmd import _FATAL_CLOSE_CODES

        assert code not in _FATAL_CLOSE_CODES


class _ScriptedWS:
    """One fake relay session: replies to the hello, then ends.

    ``first`` is the JSON the relay sends back after ``fp_hello``. ``then``
    is an exception to raise from the message loop, standing in for the
    connection dropping mid-session.
    """

    def __init__(self, first: dict, then: Exception | None = None) -> None:
        self._first = first
        self._then = then
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def recv(self) -> str:
        return json.dumps(self._first)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._then is not None:
            raise self._then
        raise StopAsyncIteration


class TestReconnectPacing:
    """How `hle fp` behaves while it can't serve the forward.

    Reported from a real relay deploy: the forward printed the same
    "agent is not connected" line repeatedly and took far too long to come
    back, because waiting for an agent shared the backoff counter used for
    unreachable-relay retries.
    """

    async def _run_until_exhausted(self, monkeypatch, sessions, capsys):
        """Drive _run through `sessions`, returning (sleeps, output)."""
        from hle_client import fp_cmd

        remaining = list(sessions)
        sleeps: list[float] = []

        def fake_connect(uri, **kw):
            if not remaining:
                raise KeyboardInterrupt  # ends the loop deterministically
            nxt = remaining.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(fp_cmd.websockets, "connect", fake_connect)
        monkeypatch.setattr(fp_cmd.asyncio, "sleep", fake_sleep)

        with contextlib.suppress(KeyboardInterrupt):
            await fp_cmd._run(
                api_key="hle_x",
                agent="rpi",
                target_host="localhost",
                target_port=22,
                bind_host="127.0.0.1",
                bind_port=0,
                relay_host="hle.world",
                relay_port=443,
            )
        return sleeps, capsys.readouterr().out

    def _offline(self):
        return _ScriptedWS(
            {
                "type": "fp_error",
                "code": "agent_offline",
                "message": "Agent 'rpi' is not connected right now.",
            }
        )

    def _welcome(self, then=None):
        return _ScriptedWS(
            {"type": "fp_welcome", "agent_public_id": "abc-123", "allowed": ["localhost:*"]},
            then=then,
        )

    async def test_waiting_for_an_agent_uses_the_short_schedule(self, monkeypatch, capsys):
        """Not the connection backoff: the relay answered, so polling is cheap."""
        sleeps, _ = await self._run_until_exhausted(
            monkeypatch, [self._offline(), self._offline(), self._offline()], capsys
        )
        from hle_client.fp_cmd import AGENT_WAIT_DELAY, MAX_AGENT_WAIT_DELAY

        assert sleeps[0] == AGENT_WAIT_DELAY
        assert max(sleeps) <= MAX_AGENT_WAIT_DELAY

    async def test_agent_wait_never_reaches_the_connection_ceiling(self, monkeypatch, capsys):
        """The bug: an agent that returned after a blip still cost up to 30s."""
        from hle_client.fp_cmd import MAX_RECONNECT_DELAY

        sleeps, _ = await self._run_until_exhausted(
            monkeypatch, [self._offline() for _ in range(8)], capsys
        )
        assert len(sleeps) == 8, sleeps
        assert max(sleeps) < MAX_RECONNECT_DELAY, sleeps

    async def test_the_offline_message_is_not_repeated(self, monkeypatch, capsys):
        """Four identical lines read as a fault; the retry is working."""
        _, out = await self._run_until_exhausted(
            monkeypatch, [self._offline() for _ in range(4)], capsys
        )
        assert out.count("not connected right now") == 1

    async def test_reaching_the_relay_clears_a_stale_connection_backoff(self, monkeypatch, capsys):
        """A network outage then an agent wait must not inherit the old delay.

        Two unreachable-relay failures grow the connection delay to 4s. Once the
        relay answers — even with 'agent offline' — the network is demonstrably
        fine, so the next real reconnect should start from 1s again.
        """
        sessions = [
            OSError("no route to host"),
            OSError("no route to host"),
            self._offline(),
            self._welcome(then=ConnectionResetError("dropped")),
        ]
        sleeps, _ = await self._run_until_exhausted(monkeypatch, sessions, capsys)
        # 1s, 2s (connection backoff), 2s (agent wait), then back to 1s.
        assert sleeps[:2] == [1.0, 2.0]
        assert sleeps[-1] == 1.0

    async def test_first_connection_prints_the_banner(self, monkeypatch, capsys):
        _, out = await self._run_until_exhausted(
            monkeypatch,
            [self._offline(), self._welcome(then=ConnectionResetError("dropped"))],
            capsys,
        )
        assert "Forwarding" in out
        assert "Reconnected" not in out

    async def test_recovery_after_a_drop_is_announced(self, monkeypatch, capsys):
        """Coming back must be as visible as going away, or the user can't tell
        whether the forward is usable again."""
        _, out = await self._run_until_exhausted(
            monkeypatch,
            [
                self._welcome(then=ConnectionResetError("dropped")),
                self._offline(),
                self._welcome(then=ConnectionResetError("dropped again")),
            ],
            capsys,
        )
        assert "Reconnected" in out

    async def test_a_rejected_key_still_exits_rather_than_looping(self, monkeypatch, capsys):
        from hle_client import fp_cmd

        session = _ScriptedWS({"type": "fp_error", "code": "forbidden", "message": "nope"})
        monkeypatch.setattr(fp_cmd.websockets, "connect", lambda uri, **kw: session)
        monkeypatch.setattr(fp_cmd.asyncio, "sleep", lambda s: asyncio.sleep(0))

        with pytest.raises(SystemExit):
            await fp_cmd._run(
                api_key="hle_x",
                agent="rpi",
                target_host="localhost",
                target_port=22,
                bind_host="127.0.0.1",
                bind_port=0,
                relay_host="hle.world",
                relay_port=443,
            )


class TestForwardRule:
    def test_exact_host_and_port(self):
        rule = ForwardRule(host="localhost", port=22)
        assert rule.matches("localhost", 22)
        assert not rule.matches("localhost", 23)
        assert not rule.matches("nas", 22)

    def test_wildcard_port(self):
        rule = ForwardRule(host="nas")
        assert rule.matches("nas", 22)
        assert rule.matches("nas", 8096)
        assert not rule.matches("other", 22)

    @pytest.mark.parametrize("alias", ["127.0.0.1", "LOCALHOST", "::1", "[::1]", "localhost."])
    def test_loopback_aliases_are_one_target(self, alias):
        # An allowlist of localhost:22 must not be bypassable (or accidentally
        # missed) by spelling loopback differently.
        assert ForwardRule(host="localhost", port=22).matches(alias, 22)

    def test_loopback_alias_in_rule_matches_plain(self):
        assert ForwardRule(host="127.0.0.1", port=22).matches("localhost", 22)

    def test_str_renders_wildcard(self):
        assert str(ForwardRule(host="nas")) == "nas:*"
        assert str(ForwardRule(host="nas", port=22)) == "nas:22"


class TestIsAllowed:
    def test_default_is_loopback_only(self):
        rules = default_rules()
        assert is_allowed(rules, "localhost", 22)
        assert is_allowed(rules, "127.0.0.1", 5432)
        assert not is_allowed(rules, "192.168.1.50", 22)

    def test_empty_rules_allow_nothing(self):
        # "Not configured" must read as closed, not open.
        assert not is_allowed([], "localhost", 22)


class TestAgentSideAuthorization:
    async def test_refuses_target_outside_allowlist(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=[ForwardRule(host="localhost", port=22)])

        await agent.handle(
            FpOpen(stream_id="s1", target_host="192.168.1.50", target_port=5432).model_dump()
        )

        err = out.first(FpMsgType.ERROR)
        assert err is not None
        assert err["code"] == FpErrorCode.NOT_ALLOWED
        assert "192.168.1.50:5432" in err["message"]
        assert not out.of_type(FpMsgType.READY)

    async def test_refuses_allowed_host_on_wrong_port(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=[ForwardRule(host="localhost", port=22)])

        await agent.handle(
            FpOpen(stream_id="s1", target_host="localhost", target_port=5432).model_dump()
        )

        assert out.first(FpMsgType.ERROR)["code"] == FpErrorCode.NOT_ALLOWED

    async def test_empty_allowlist_refuses_everything(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=[])

        await agent.handle(
            FpOpen(stream_id="s1", target_host="localhost", target_port=22).model_dump()
        )

        assert out.first(FpMsgType.ERROR)["code"] == FpErrorCode.NOT_ALLOWED

    async def test_unreachable_target_reports_cleanly(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=[ForwardRule(host="localhost")])

        # Port 1 on loopback: allowed by the rules, but nothing is listening.
        await agent.handle(
            FpOpen(stream_id="s1", target_host="localhost", target_port=1).model_dump()
        )

        err = out.first(FpMsgType.ERROR)
        assert err is not None
        assert err["code"] in (FpErrorCode.UNREACHABLE, FpErrorCode.TIMEOUT)

    async def test_malformed_frame_does_not_raise(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=default_rules())
        # Missing required fields — must be reported, not propagated.
        await agent.handle({"type": FpMsgType.OPEN, "stream_id": "s1"})
        assert out.first(FpMsgType.ERROR)["code"] == FpErrorCode.INTERNAL

    async def test_unknown_frame_is_ignored(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=default_rules())
        await agent.handle({"type": "fp_bogus", "stream_id": "s1"})
        assert out.frames == []


class TestAgentSideDataPath:
    async def test_forwards_bytes_both_ways(self):
        """Agent dials a real echo server and relays bytes in both directions."""
        received: list[bytes] = []

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(100)
            received.append(data)
            writer.write(b"PONG:" + data)
            await writer.drain()
            # Must close: from 3.12, `Server.wait_closed()` waits for open
            # connections, so a handler that returns without closing hangs
            # `async with server` forever. On 3.11 it returned immediately and
            # this leak was invisible.
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        out = Collector()
        agent = FpAgentSide(send=out, rules=[ForwardRule(host="localhost", port=port)])

        async with server:
            await agent.handle(
                FpOpen(stream_id="s1", target_host="127.0.0.1", target_port=port).model_dump()
            )
            assert out.first(FpMsgType.READY) is not None

            await agent.handle(FpData.of("s1", b"PING").model_dump())
            await asyncio.sleep(0.1)  # let the echo round-trip

            await agent.close_all()

        assert received == [b"PING"]
        data_frames = out.of_type(FpMsgType.DATA)
        assert data_frames, "expected the echo to come back as fp_data"
        assert FpData.model_validate(data_frames[0]).payload() == b"PONG:PING"

    async def test_data_for_unknown_stream_is_dropped(self):
        out = Collector()
        agent = FpAgentSide(send=out, rules=default_rules())
        await agent.handle(FpData.of("never-opened", b"x").model_dump())
        assert out.frames == []


class TestLocalClient:
    async def test_accepted_connection_opens_a_stream_and_forwards(self):
        out = Collector()
        client = FpLocalClient(send=out, target_host="localhost", target_port=22, connected=True)
        server = await client.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)

            opened = out.first(FpMsgType.OPEN)
            assert opened is not None
            assert opened["target_host"] == "localhost"
            assert opened["target_port"] == 22
            sid = opened["stream_id"]

            # The agent accepts, so local bytes start flowing as fp_data.
            await client.handle({"type": FpMsgType.READY, "stream_id": sid})
            writer.write(b"SSH-2.0-test")
            await writer.drain()
            await asyncio.sleep(0.05)

            sent = out.of_type(FpMsgType.DATA)
            assert sent, "expected local bytes to be forwarded"
            assert FpData.model_validate(sent[0]).payload() == b"SSH-2.0-test"

            # And bytes from the agent reach the local socket.
            await client.handle(FpData.of(sid, b"SSH-2.0-remote").model_dump())
            assert await asyncio.wait_for(reader.read(14), timeout=2) == b"SSH-2.0-remote"

            writer.close()
            await client.close_all()

    async def test_connections_are_refused_while_the_relay_is_down(self):
        """The port stays bound across a reconnect, but doesn't accept blindly.

        Keeping the listener up means `ssh rpi` works again the moment the relay
        returns, without restarting `hle fp`. Accepting connections while
        disconnected would instead leave the caller hanging until the ready
        timeout for a relay we already know isn't there.
        """
        out = Collector()
        client = FpLocalClient(send=out, target_host="localhost", target_port=22, connected=False)
        server = await client.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # Dropped immediately, and no fp_open was sent to a dead connection.
            assert await asyncio.wait_for(reader.read(), timeout=2) == b""
            assert out.frames == []
            writer.close()

    async def test_stream_that_never_gets_ready_times_out(self):
        """A silent agent must not wedge the local connection forever."""
        out = Collector()
        client = FpLocalClient(send=out, target_host="localhost", target_port=22, ready_timeout=0.1)
        server = await client.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # No ready, no error — the handler gives up and drops the socket.
            assert await asyncio.wait_for(reader.read(), timeout=5) == b""
            writer.close()

    async def test_refusal_closes_the_local_connection(self):
        """A refused stream must drop the local socket, not hang the caller."""
        out = Collector()
        seen: list[str] = []
        client = FpLocalClient(
            send=out,
            target_host="nope",
            target_port=5432,
            on_error=seen.append,
            connected=True,
        )
        server = await client.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            sid = out.first(FpMsgType.OPEN)["stream_id"]

            await client.handle(
                {
                    "type": FpMsgType.ERROR,
                    "stream_id": sid,
                    "code": FpErrorCode.NOT_ALLOWED,
                    "message": "nope:5432 is not allowed",
                }
            )
            # The local side should see EOF rather than blocking forever.
            assert await asyncio.wait_for(reader.read(), timeout=2) == b""
            assert seen == ["nope:5432 is not allowed"]

            writer.close()
            await client.close_all()
