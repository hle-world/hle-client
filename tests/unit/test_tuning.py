"""Relay-controlled stream tuning (capability "tuning").

The relay may tune a tunnel's read size, flush delay and memory budgets at
registration and live. The client advertises the capability; a relay that echoes
it may send ``StreamTuning`` snapshots, and a relay that does not changes
nothing. These tests cover the shared models, the capability gate, environment
precedence, snapshot replacement, budget resizing, the process-wide max across
tunnels, and the per-stream stats carried on ``HttpResponseEnd``.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from hle_client import chunk_budget
from hle_client.chunk_budget import (
    ChunkBudget,
    clear_tunnel_budget,
    get_chunk_budget,
    reset_chunk_budget,
)
from hle_client.proxy import HTTP_CHUNK_SIZE_ENV, HTTP_FLUSH_MS_ENV, upstream_chunk_size
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.models import (
    HttpResponseChunk,
    HttpResponseEnd,
    ProxiedHttpRequest,
    StreamTuning,
    TUNING_LIMITS,
    TunnelRegistrationResponse,
    TuningUpdate,
    clamp_tuning,
)
from hle_common.protocol import CAPABILITY_TUNING, MessageType, ProtocolMessage

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh_budget():
    """Give every test its own process-wide budget (one per event loop)."""
    reset_chunk_budget()
    yield
    reset_chunk_budget()


def _tunnel() -> Tunnel:
    return Tunnel(TunnelConfig(service_url="http://localhost:8123", service_label="app"))


def _ack(
    *,
    server_capabilities: list[str],
    tuning: StreamTuning | None = None,
) -> TunnelRegistrationResponse:
    return TunnelRegistrationResponse(
        tunnel_id="t",
        subdomain="app-x",
        public_url="https://app-x.hle.world",
        websocket_enabled=True,
        user_code="x",
        service_label="app",
        server_capabilities=list(server_capabilities),
        tuning=tuning,
    )


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class TestModels:
    def test_capability_and_message_type(self):
        assert CAPABILITY_TUNING == "tuning"
        assert MessageType.TUNING_UPDATE == "tuning_update"

    def test_stream_tuning_round_trip(self):
        original = StreamTuning(
            read_size=131072,
            flush_ms=5,
            budget_bytes=8 * MIB,
            endpoint_share_bytes=4 * MIB,
            frame_max=262144,
            ws_window=MIB,
        )
        restored = StreamTuning.model_validate_json(original.model_dump_json())
        assert restored == original

    def test_tuning_update_round_trip_through_a_protocol_message(self):
        update = TuningUpdate(tuning=StreamTuning(read_size=65536, flush_ms=2))
        msg = ProtocolMessage(type=MessageType.TUNING_UPDATE, payload=update.model_dump())
        parsed = ProtocolMessage.model_validate_json(msg.model_dump_json())
        restored = TuningUpdate.model_validate(parsed.payload)
        assert restored == update
        assert restored.tuning.read_size == 65536

    def test_http_response_end_stats_round_trip(self):
        original = HttpResponseEnd(
            request_id="r",
            stats={
                "bytes": 1000,
                "frames": 3,
                "ms": 42,
                "budget_wait_ms": 7,
                "read_size": 262144,
            },
        )
        restored = HttpResponseEnd.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.stats == original.stats

    def test_ack_payload_without_tuning_parses(self):
        # Exactly what a relay that predates the field sends: the key is absent.
        ack = TunnelRegistrationResponse.model_validate(
            {
                "tunnel_id": "t",
                "subdomain": "s",
                "public_url": "u",
                "websocket_enabled": True,
                "user_code": "c",
                "service_label": "l",
            }
        )
        assert ack.tuning is None

    def test_end_payload_without_stats_parses(self):
        end = HttpResponseEnd.model_validate({"request_id": "r"})
        assert end.stats is None

    def test_unknown_tuning_keys_are_ignored(self):
        tuning = StreamTuning.model_validate({"read_size": 131072, "a_1_9_field": {"n": True}})
        assert tuning.read_size == 131072
        assert "a_1_9_field" not in tuning.model_dump()
        assert tuning.model_dump_json() == StreamTuning(read_size=131072).model_dump_json()

    def test_clamp_tuning_lower_bounds(self):
        clamped = clamp_tuning(
            StreamTuning(
                read_size=1,
                flush_ms=-1,
                budget_bytes=1,
                endpoint_share_bytes=1,
                frame_max=1,
                ws_window=1,
            )
        )
        assert clamped.read_size == TUNING_LIMITS["read_size"][0]
        assert clamped.flush_ms == TUNING_LIMITS["flush_ms"][0]
        assert clamped.budget_bytes == TUNING_LIMITS["budget_bytes"][0]
        assert clamped.endpoint_share_bytes == TUNING_LIMITS["endpoint_share_bytes"][0]
        assert clamped.frame_max == TUNING_LIMITS["frame_max"][0]
        assert clamped.ws_window == TUNING_LIMITS["ws_window"][0]

    def test_clamp_tuning_upper_bounds(self):
        huge = 10**9
        clamped = clamp_tuning(
            StreamTuning(
                read_size=huge,
                flush_ms=huge,
                budget_bytes=huge,
                endpoint_share_bytes=huge,
                frame_max=huge,
                ws_window=huge,
            )
        )
        assert clamped.read_size == TUNING_LIMITS["read_size"][1]
        assert clamped.flush_ms == TUNING_LIMITS["flush_ms"][1]
        assert clamped.budget_bytes == TUNING_LIMITS["budget_bytes"][1]
        assert clamped.endpoint_share_bytes == TUNING_LIMITS["endpoint_share_bytes"][1]
        assert clamped.frame_max == TUNING_LIMITS["frame_max"][1]
        assert clamped.ws_window == TUNING_LIMITS["ws_window"][1]

    def test_clamp_tuning_keeps_none(self):
        clamped = clamp_tuning(StreamTuning(read_size=10))
        assert clamped.read_size == TUNING_LIMITS["read_size"][0]
        assert clamped.flush_ms is None
        assert clamped.budget_bytes is None


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    def test_ack_tuning_ignored_without_the_capability(self):
        tunnel = _tunnel()
        tunnel._server_caps = []
        ack = _ack(server_capabilities=[], tuning=StreamTuning(read_size=131072))
        tunnel._apply_server_tuning(ack)
        assert tunnel._tuning.read_size is None

    def test_ack_tuning_applied_with_the_capability(self):
        tunnel = _tunnel()
        tunnel._server_caps = [CAPABILITY_TUNING]
        ack = _ack(
            server_capabilities=[CAPABILITY_TUNING],
            tuning=StreamTuning(read_size=131072),
        )
        tunnel._apply_server_tuning(ack)
        assert tunnel._tuning.read_size == 131072

    def test_live_update_ignored_without_the_capability(self):
        tunnel = _tunnel()
        tunnel._server_caps = []
        update = TuningUpdate(tuning=StreamTuning(read_size=131072))
        tunnel._handle_tuning_update(
            ProtocolMessage(type=MessageType.TUNING_UPDATE, payload=update.model_dump())
        )
        assert tunnel._tuning.read_size is None

    def test_live_update_applied_with_the_capability(self):
        tunnel = _tunnel()
        tunnel._server_caps = [CAPABILITY_TUNING]
        update = TuningUpdate(tuning=StreamTuning(read_size=131072))
        tunnel._handle_tuning_update(
            ProtocolMessage(type=MessageType.TUNING_UPDATE, payload=update.model_dump())
        )
        assert tunnel._tuning.read_size == 131072


# ---------------------------------------------------------------------------
# Environment precedence and snapshot replacement
# ---------------------------------------------------------------------------


class TestEnvPrecedence:
    def test_env_wins_for_that_knob_only(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(HTTP_CHUNK_SIZE_ENV, "131072")
        monkeypatch.delenv(HTTP_FLUSH_MS_ENV, raising=False)
        tunnel = _tunnel()
        tunnel._apply_tuning(StreamTuning(read_size=524288, flush_ms=10))
        assert tunnel._tuning.read_size is None  # environment wins
        assert tunnel._tuning.flush_ms == 10  # relay still wins here

    def test_env_override_is_logged_once(self, monkeypatch: pytest.MonkeyPatch, caplog):
        monkeypatch.setenv(HTTP_CHUNK_SIZE_ENV, "131072")
        tunnel = _tunnel()
        with caplog.at_level("DEBUG", logger="hle_client.tunnel"):
            tunnel._apply_tuning(StreamTuning(read_size=524288))
        assert any("read_size" in record.getMessage() for record in caplog.records)


class TestSnapshotReplaces:
    def test_second_snapshot_with_none_restores_default(self):
        tunnel = _tunnel()
        tunnel._apply_tuning(StreamTuning(read_size=131072))
        assert tunnel._tuning.read_size == 131072
        tunnel._apply_tuning(StreamTuning(read_size=None))
        assert tunnel._tuning.read_size is None

    def test_snapshot_values_are_clamped(self):
        tunnel = _tunnel()
        tunnel._apply_tuning(StreamTuning(read_size=1))
        assert tunnel._tuning.read_size == TUNING_LIMITS["read_size"][0]


# ---------------------------------------------------------------------------
# ChunkBudget.resize
# ---------------------------------------------------------------------------


class TestResize:
    async def test_grow_wakes_a_waiter(self):
        budget = ChunkBudget(1000)
        await budget.acquire(1000)
        waiter = asyncio.create_task(budget.acquire(500))
        await asyncio.sleep(0)
        assert not waiter.done()
        budget.resize(2000)
        assert await asyncio.wait_for(waiter, timeout=1) == 500
        assert budget.capacity == 2000

    async def test_shrink_below_in_flight_does_not_fail(self):
        budget = ChunkBudget(1000)
        await budget.acquire(1000)
        budget.resize(400)  # smaller than what is already granted
        assert budget.in_flight == 1000  # nothing revoked
        budget.release(1000)
        # A later acquisition respects the new capacity.
        assert await budget.acquire(500) == 400
        assert budget.in_flight == 400

    def test_non_positive_resize_rejected(self):
        budget = ChunkBudget(1000)
        with pytest.raises(ValueError):
            budget.resize(0)


# ---------------------------------------------------------------------------
# Process-wide budget: the max across live tunnels
# ---------------------------------------------------------------------------


class TestProcessBudgetMax:
    def test_process_budget_is_the_max_across_tunnels(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(chunk_budget.CHUNK_BUDGET_ENV, raising=False)
        first = _tunnel()
        second = _tunnel()
        first._apply_tuning(StreamTuning(budget_bytes=4 * MIB))
        second._apply_tuning(StreamTuning(budget_bytes=16 * MIB))
        assert get_chunk_budget().capacity == 16 * MIB
        clear_tunnel_budget(second)
        assert get_chunk_budget().capacity == 4 * MIB

    async def test_disconnect_shrinks_the_process_budget(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(chunk_budget.CHUNK_BUDGET_ENV, raising=False)
        first = _tunnel()
        second = _tunnel()
        first._apply_tuning(StreamTuning(budget_bytes=4 * MIB))
        second._apply_tuning(StreamTuning(budget_bytes=16 * MIB))
        assert get_chunk_budget().capacity == 16 * MIB
        await second._cleanup()
        assert get_chunk_budget().capacity == 4 * MIB

    def test_env_budget_wins_over_the_relay(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, str(3 * MIB))
        reset_chunk_budget()
        tunnel = _tunnel()
        tunnel._apply_tuning(StreamTuning(budget_bytes=32 * MIB))
        assert tunnel._tuning.budget_bytes is None
        assert get_chunk_budget().capacity == 3 * MIB


# ---------------------------------------------------------------------------
# Per-endpoint share
# ---------------------------------------------------------------------------


class TestEndpointShare:
    async def test_tuned_endpoint_share_is_applied(self):
        tunnel = _tunnel()
        tunnel._server_caps = [CAPABILITY_TUNING]
        tunnel._apply_tuning(StreamTuning(budget_bytes=5 * MIB, endpoint_share_bytes=6 * MIB))
        await _run(tunnel, [b"x" * 1024])
        assert tunnel._chunk_budget is not None
        assert tunnel._chunk_budget.endpoint.capacity == 6 * MIB


# ---------------------------------------------------------------------------
# Per-stream stats and the coalescer override
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "application/octet-stream"})
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size: int | None = None):
        for chunk in self._chunks:
            yield chunk


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def stream(self, **kwargs: object) -> _FakeStream:
        return _FakeStream(_FakeResponse(list(self._chunks)))


def _request(request_id: str) -> ProtocolMessage:
    return ProtocolMessage(
        type=MessageType.HTTP_REQUEST,
        tunnel_id="t",
        request_id=request_id,
        payload=ProxiedHttpRequest(
            request_id=request_id,
            method="GET",
            path="/big",
            headers={},
            body=None,
            query_string="",
        ).model_dump(),
    )


def _mock_ws() -> tuple[AsyncMock, list[str]]:
    ws = AsyncMock()
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    ws.send = AsyncMock(side_effect=send)
    return ws, sent


def _messages(sent: list[str]) -> list[ProtocolMessage]:
    return [ProtocolMessage.model_validate_json(raw) for raw in sent]


def _end(sent: list[str]) -> HttpResponseEnd:
    return HttpResponseEnd.model_validate(_messages(sent)[-1].payload)


async def _run(tunnel: Tunnel, chunks: list[bytes]) -> list[str]:
    tunnel._tunnel_id = "t"
    tunnel._proxy._http_client = _FakeClient(chunks)
    ws, sent = _mock_ws()
    await tunnel._handle_http_request_chunked(ws, _request("req"))
    return sent


class TestStats:
    async def test_stats_present_when_tuning_was_echoed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(HTTP_CHUNK_SIZE_ENV, raising=False)
        tunnel = _tunnel()
        tunnel._server_caps = [CAPABILITY_TUNING]
        sent = await _run(tunnel, [b"x" * 1024, b"y" * 2048])

        stats = _end(sent).stats
        assert stats is not None
        assert set(stats) == {"bytes", "frames", "ms", "budget_wait_ms", "read_size"}
        assert stats["bytes"] == 3072
        frames = sum(1 for m in _messages(sent) if m.type == MessageType.HTTP_RESPONSE_CHUNK)
        assert stats["frames"] == frames
        assert stats["read_size"] == upstream_chunk_size()
        assert isinstance(stats["ms"], int)
        assert isinstance(stats["budget_wait_ms"], int)

    async def test_stats_absent_when_tuning_was_not_echoed(self):
        tunnel = _tunnel()
        tunnel._server_caps = []
        sent = await _run(tunnel, [b"x" * 1024])
        assert _end(sent).stats is None

    async def test_relay_read_size_slices_the_stream(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(HTTP_CHUNK_SIZE_ENV, raising=False)
        tunnel = _tunnel()
        tunnel._server_caps = [CAPABILITY_TUNING]
        tunnel._apply_tuning(StreamTuning(read_size=16 * 1024))
        sent = await _run(tunnel, [b"x" * (40 * 1024)])

        sizes = [
            len(base64.b64decode(HttpResponseChunk.model_validate(m.payload).data))
            for m in _messages(sent)
            if m.type == MessageType.HTTP_RESPONSE_CHUNK
        ]
        assert sizes == [16 * 1024, 16 * 1024, 8 * 1024]
        assert _end(sent).stats is not None
