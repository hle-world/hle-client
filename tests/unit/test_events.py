"""`--events jsonl`: what supervisors read instead of scraping glyphs (plan §2.6b).

hle-webapp, the HA add-on and hle-docker spawn `hle expose` / `hle webhook`
and recover notice levels from the ✓/⚠/✗ at the start of stdout lines. These
pin the replacement: one JSON object per line on stdout, every line parseable,
and nothing else on stdout while it is on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import websockets.exceptions
from click.testing import CliRunner
from websockets.frames import Close

from hle_client import notices
from hle_client.cli import main
from hle_client.tunnel import Tunnel, TunnelConfig, TunnelFatalError
from hle_common.protocol import MessageType, NoticePayload, ProtocolMessage

KEYS = {
    "ts",
    "event",
    "level",
    "source",
    "label",
    "subdomain",
    "public_url",
    "message",
    "code",
}


@pytest.fixture(autouse=True)
def _events_off():
    notices.disable_events()
    yield
    notices.disable_events()


def _lines(text: str) -> list[dict[str, Any]]:
    """Every stdout line as JSON — failing on the first one that is not."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# The emitter
# --------------------------------------------------------------------------- #


class TestEmitter:
    def test_off_by_default_and_silent(self, capsys):
        notices.emit_event("registered", message="x")
        assert capsys.readouterr().out == ""

    def test_one_object_per_line_with_the_documented_keys(self, capsys):
        notices.enable_events("jsonl")
        notices.emit_event(
            "registered",
            level="success",
            label="ha",
            subdomain="ha-x7k",
            public_url="https://ha-x7k.hle.world",
            message="Tunnel registered",
        )
        (event,) = _lines(capsys.readouterr().out)
        assert set(event) == KEYS
        assert event["event"] == "registered"
        assert event["source"] == "tunnel"
        assert event["subdomain"] == "ha-x7k"
        assert event["ts"].endswith("Z")

    def test_a_close_code_is_a_string(self, capsys):
        notices.enable_events("jsonl")
        notices.emit_event("fatal", level="error", code=4003)
        assert _lines(capsys.readouterr().out)[0]["code"] == "4003"

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError):
            notices.enable_events("xml")

    def test_every_documented_event_name_is_in_the_schema_doc(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[2] / "docs" / "events.md").read_text()
        for name in notices.EVENT_NAMES:
            assert f"`{name}`" in doc


class TestNoticeRendering:
    def test_a_notice_is_an_event_on_stdout_and_text_on_stderr(self, capsys):
        notices.enable_events("jsonl")
        notices.render_notice(
            NoticePayload(level="warning", code="auto_protect", message="Protected"),
            label="ha",
            subdomain="ha-x7k",
        )
        captured = capsys.readouterr()
        (event,) = _lines(captured.out)
        assert event["event"] == "notice"
        assert event["level"] == "warning"
        assert event["code"] == "auto_protect"
        assert event["message"] == "Protected"
        assert "⚠" in captured.err
        assert "⚠" not in captured.out

    def test_without_events_the_notice_is_unchanged(self, capsys):
        notices.render_notice(NoticePayload(level="success", code="x", message="done"))
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "{" not in captured.out


# --------------------------------------------------------------------------- #
# The real Tunnel: the transitions it already knows about
# --------------------------------------------------------------------------- #


class _RelayWs:
    """A relay that acks the registration, pushes one NOTICE, then closes."""

    def __init__(self) -> None:
        ack = ProtocolMessage(
            type=MessageType.TUNNEL_ACK,
            payload={
                "tunnel_id": "t-1",
                "subdomain": "ha-x7k",
                "public_url": "https://ha-x7k.hle.world",
                "websocket_enabled": True,
                "user_code": "x7k",
                "service_label": "ha",
            },
        )
        notice = ProtocolMessage(
            type=MessageType.NOTICE,
            payload={"level": "info", "code": "hello", "message": "Welcome"},
        )
        self._ack = ack.model_dump_json()
        self._queue = [notice.model_dump_json()]

    async def send(self, raw: str) -> None:
        return None

    async def recv(self) -> str:
        return self._ack

    async def close(self) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._queue:
            return self._queue.pop(0)
        raise StopAsyncIteration

    async def __aenter__(self) -> _RelayWs:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _tunnel() -> Tunnel:
    t = Tunnel(TunnelConfig(service_url="http://localhost:1", service_label="ha", api_key="k"))
    t._proxy.start = AsyncMock()  # type: ignore[method-assign]
    return t


def _closed(code: int) -> websockets.exceptions.ConnectionClosedError:
    return websockets.exceptions.ConnectionClosedError(Close(code, "test"), None)


class TestTunnelEmits:
    async def test_connected_registered_and_notice(self, capsys):
        notices.enable_events("jsonl")
        tunnel = _tunnel()
        with (
            patch("hle_client.tunnel.websockets.connect", lambda *a, **kw: _RelayWs()),
            patch.object(tunnel, "_discover_relay_uri", AsyncMock(return_value="wss://r/x")),
        ):
            await tunnel._connect_once()
        events = _lines(capsys.readouterr().out)
        assert [e["event"] for e in events] == ["connected", "registered", "notice"]
        registered = events[1]
        assert registered["label"] == "ha"
        assert registered["subdomain"] == "ha-x7k"
        assert registered["public_url"] == "https://ha-x7k.hle.world"
        # The notice knows which tunnel it is about.
        assert events[2]["subdomain"] == "ha-x7k"
        assert events[2]["code"] == "hello"

    async def test_a_fatal_close_is_a_fatal_event(self, capsys):
        notices.enable_events("jsonl")
        tunnel = _tunnel()
        with (
            patch.object(tunnel, "_connect_once", side_effect=_closed(4003)),
            patch.object(tunnel, "_cleanup", AsyncMock()),
            pytest.raises(TunnelFatalError),
        ):
            await tunnel.connect()
        (event,) = _lines(capsys.readouterr().out)
        assert event["event"] == "fatal"
        assert event["level"] == "error"
        assert event["code"] == "4003"
        assert event["message"]

    async def test_a_live_session_dropping_is_disconnected_a_failed_attempt_is_error(self, capsys):
        notices.enable_events("jsonl")
        tunnel = _tunnel()
        outcomes = iter(["live", "fail"])

        async def _attempt() -> None:
            outcome = next(outcomes, None)
            if outcome is None:
                tunnel._running = False
                return
            if outcome == "live":
                tunnel._session_registered = True
                raise _closed(1011)
            raise ConnectionError("refused")

        with (
            patch.object(tunnel, "_connect_once", side_effect=_attempt),
            patch.object(tunnel, "_cleanup", AsyncMock()),
            patch("hle_client.tunnel.asyncio.sleep", AsyncMock()),
        ):
            await tunnel.connect()
        events = _lines(capsys.readouterr().out)
        assert [e["event"] for e in events] == ["disconnected", "error"]
        assert events[0]["code"] == "1011"
        assert events[1]["message"] == "refused"


# --------------------------------------------------------------------------- #
# The CLI: stdout is machine-clean under --events
# --------------------------------------------------------------------------- #


class _FakeTunnel:
    """Stands in for Tunnel: announces itself the way the real one does, then
    is turned away by the relay."""

    def __init__(self, *, config: Any, **_kw: Any) -> None:
        self.config = config

    async def connect(self) -> None:
        label = self.config.service_label
        notices.emit_event("registered", level="success", label=label, subdomain="ha-x7k")
        notices.render_notice(NoticePayload(code="n", message="Heads up"), label=label)
        notices.emit_event("fatal", level="error", label=label, code=4003, message="bad key")
        raise TunnelFatalError("bad key")


def _run(argv: list[str]):
    with (
        patch("hle_client.cli.Tunnel", _FakeTunnel),
        patch("hle_client.cli.shutdown.run", asyncio.run),
    ):
        return CliRunner().invoke(main, argv)


_SPAWNS = {
    "tunnel create": ["tunnel", "create", "ha", "http://localhost:1", "--api-key", "hle_x"],
    "expose": ["expose", "--service", "http://localhost:1", "--label", "ha"],
    "tunnel webhook": [
        "tunnel",
        "webhook",
        "--path",
        "/hook",
        "--forward-to",
        "http://localhost:1",
        "--label",
        "ha",
    ],
    "webhook": ["webhook", "--path", "/h", "--forward-to", "http://localhost:1", "--label", "ha"],
}


class TestCliEvents:
    @pytest.mark.parametrize("name", sorted(_SPAWNS), ids=sorted(_SPAWNS))
    def test_stdout_is_only_json_lines(self, name):
        result = _run([*_SPAWNS[name], "--events", "jsonl"])
        assert result.exit_code == 1  # the relay's fatal close
        events = _lines(result.stdout)  # raises on any line of human text
        assert [e["event"] for e in events] == ["registered", "notice", "fatal"]
        assert all(set(e) == KEYS for e in events)
        # The person still gets everything — on stderr.
        assert "HLE" in result.stderr
        assert "Heads up" in result.stderr
        assert "bad key" in result.stderr

    def test_without_events_nothing_changes(self):
        result = _run(_SPAWNS["tunnel create"])
        assert "Exposing" in result.stdout
        assert "Heads up" in result.stdout
        assert '"event"' not in result.stdout

    def test_events_are_switched_off_after_the_command(self):
        _run([*_SPAWNS["tunnel create"], "--events", "jsonl"])
        assert not notices.events_enabled()

    def test_an_unknown_format_is_a_usage_error(self):
        result = _run([*_SPAWNS["tunnel create"], "--events", "xml"])
        assert result.exit_code == 2


class TestAgentRunEvents:
    def test_agent_run_keeps_stdout_for_events(self, monkeypatch):
        class _FakeAgent:
            control_uri = "wss://hle.world:443/_hle/agent"
            fatal_error = "Agent token revoked."
            exit_code = 0

            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            async def run(self) -> None:
                notices.emit_event("registered", level="success", source="agent")
                notices.emit_event(
                    "fatal", level="error", source="agent", code=4003, message="revoked"
                )

        monkeypatch.setenv("HLE_AGENT_TOKEN", "hlea_" + "a" * 40)
        with (
            patch("hle_client.cli.AgentClient", _FakeAgent),
            patch("hle_client.cli.shutdown.run", asyncio.run),
        ):
            result = CliRunner().invoke(main, ["agent", "run", "--events", "jsonl"])
        assert result.exit_code == 1
        events = _lines(result.stdout)
        assert [(e["event"], e["source"]) for e in events] == [
            ("registered", "agent"),
            ("fatal", "agent"),
        ]
        assert "Agent running" in result.stderr
