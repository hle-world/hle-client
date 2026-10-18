"""Tests for the process-wide chunked-response byte budget.

The budget is what stops many concurrent downloads from growing memory without
bound: every stream reserves a chunk's bytes as soon as it is read and releases
them once the chunk has been sent, and a stream that cannot get budget waits
(reading nothing more upstream) instead of failing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from hle_client import chunk_budget
from hle_client.chunk_budget import (
    DEFAULT_CHUNK_BUDGET_BYTES,
    ChunkBudget,
    chunk_budget_capacity,
    get_chunk_budget,
    reset_chunk_budget,
)
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.models import ProxiedHttpRequest
from hle_common.protocol import MessageType, ProtocolMessage

CHUNK = b"x" * (512 * 1024)


@pytest.fixture(autouse=True)
def _fresh_budget():
    """Give every test its own process-wide budget (one per event loop)."""
    reset_chunk_budget()
    yield
    reset_chunk_budget()


def _tunnel() -> Tunnel:
    return Tunnel(TunnelConfig(service_url="http://localhost:8123"))


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "application/octet-stream"})
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size: int):
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
    """Stands in for httpx.AsyncClient, one fresh response per stream."""

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
        # Yield so concurrent streams actually overlap (and so hold budget at
        # the same time) instead of each running to completion in one go.
        await asyncio.sleep(0)

    ws.send = AsyncMock(side_effect=send)
    return ws, sent


# ---------------------------------------------------------------------------
# ChunkBudget unit tests
# ---------------------------------------------------------------------------


class TestChunkBudgetUnit:
    async def test_acquire_then_release(self):
        budget = ChunkBudget(1000)
        assert await budget.acquire(400) == 400
        assert budget.in_flight == 400
        budget.release(400)
        assert budget.in_flight == 0

    async def test_waits_until_capacity_is_freed(self):
        budget = ChunkBudget(1000)
        await budget.acquire(1000)
        waiter = asyncio.create_task(budget.acquire(500))
        await asyncio.sleep(0)
        assert not waiter.done()
        budget.release(1000)
        assert await waiter == 500
        assert budget.in_flight == 500

    async def test_oversized_request_is_capped_at_capacity(self):
        budget = ChunkBudget(1000)
        assert await budget.acquire(5000) == 1000
        assert budget.in_flight == 1000

    async def test_cancelled_waiter_does_not_leak_budget(self):
        budget = ChunkBudget(1000)
        await budget.acquire(1000)
        waiter = asyncio.create_task(budget.acquire(1000))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.in_flight == 1000

    async def test_cancelled_after_grant_returns_budget(self):
        """Granted, then cancelled before resuming: the bytes come back."""
        budget = ChunkBudget(1000)
        await budget.acquire(1000)
        waiter = asyncio.create_task(budget.acquire(600))
        await asyncio.sleep(0)
        budget.release(1000)  # grants the waiter (600 reserved)...
        waiter.cancel()  # ...but it is cancelled before it runs
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.in_flight == 0

    async def test_cancelled_head_waiter_unblocks_smaller_ones(self):
        budget = ChunkBudget(1000)
        await budget.acquire(800)
        big = asyncio.create_task(budget.acquire(900))
        await asyncio.sleep(0)
        small = asyncio.create_task(budget.acquire(100))
        await asyncio.sleep(0)
        assert not small.done()  # FIFO: queued behind the big request
        big.cancel()
        with pytest.raises(asyncio.CancelledError):
            await big
        assert await asyncio.wait_for(small, timeout=1) == 100

    def test_non_positive_capacity_rejected(self):
        with pytest.raises(ValueError):
            ChunkBudget(0)


class TestChunkBudgetConfig:
    def test_default_capacity(self, monkeypatch):
        monkeypatch.delenv(chunk_budget.CHUNK_BUDGET_ENV, raising=False)
        assert chunk_budget_capacity() == DEFAULT_CHUNK_BUDGET_BYTES

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, "1048576")
        assert chunk_budget_capacity() == 1048576

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, "not-a-number")
        assert chunk_budget_capacity() == DEFAULT_CHUNK_BUDGET_BYTES

    def test_budget_is_process_wide(self):
        assert get_chunk_budget() is get_chunk_budget()


# ---------------------------------------------------------------------------
# Integration: many concurrent streams stay within the budget
# ---------------------------------------------------------------------------


class TestChunkedResponseBudget:
    async def test_100_concurrent_streams_stay_within_budget(self, monkeypatch):
        monkeypatch.setenv("HLE_HTTP_CHUNK_SIZE", str(512 * 1024))
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, str(2 * 1024 * 1024))
        reset_chunk_budget()

        tunnel = _tunnel()
        tunnel._tunnel_id = "t-budget"
        tunnel._proxy._http_client = _FakeClient([CHUNK, CHUNK])

        budget = get_chunk_budget()
        assert budget.capacity == 2 * 1024 * 1024

        async def run_one(index: int) -> list[str]:
            ws, sent = _mock_ws()
            await tunnel._handle_http_request_chunked(ws, _request(f"req-{index}"))
            return sent

        results = await asyncio.gather(*(run_one(i) for i in range(100)))

        # Every stream completed: START, two chunks, END.
        for sent in results:
            assert len(sent) == 4
        assert [ProtocolMessage.model_validate_json(m).type for m in results[0]] == [
            MessageType.HTTP_RESPONSE_START,
            MessageType.HTTP_RESPONSE_CHUNK,
            MessageType.HTTP_RESPONSE_CHUNK,
            MessageType.HTTP_RESPONSE_END,
        ]

        assert 0 < budget.peak <= budget.capacity
        assert budget.in_flight == 0

    async def test_cancelled_stream_releases_its_budget(self, monkeypatch):
        monkeypatch.setenv("HLE_HTTP_CHUNK_SIZE", str(512 * 1024))
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, str(4 * 1024 * 1024))
        reset_chunk_budget()

        tunnel = _tunnel()
        tunnel._tunnel_id = "t-cancel"
        tunnel._proxy._http_client = _FakeClient([CHUNK])

        budget = get_chunk_budget()
        entered = asyncio.Event()
        sent: list[MessageType] = []

        async def send(message: str) -> None:
            parsed = ProtocolMessage.model_validate_json(message)
            sent.append(parsed.type)
            if parsed.type == MessageType.HTTP_RESPONSE_CHUNK:
                # Block inside the send while the budget is held.
                entered.set()
                await asyncio.Event().wait()

        ws = AsyncMock()
        ws.send = AsyncMock(side_effect=send)

        task = asyncio.create_task(tunnel._handle_http_request_chunked(ws, _request("req-cancel")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert budget.in_flight > 0

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert budget.in_flight == 0

    async def test_streams_are_not_capped_per_endpoint(self):
        """Many long-lived streams on one endpoint all start: no connection cap."""
        tunnel = _tunnel()
        tunnel._tunnel_id = "t-nocap"

        release = asyncio.Event()
        active = 0
        max_active = 0

        async def fake_stream(**kwargs: object):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                yield (200, {"content-type": "text/event-stream"}, None)
                await release.wait()
                yield (None, None, b"payload")
            finally:
                active -= 1

        tunnel._proxy.stream_http = fake_stream  # type: ignore[method-assign]

        async def run_one(index: int) -> None:
            ws, _ = _mock_ws()
            await tunnel._handle_http_request_chunked(ws, _request(f"req-{index}"))

        tasks = [asyncio.create_task(run_one(i)) for i in range(200)]
        for _ in range(2000):
            if max_active >= 200:
                break
            await asyncio.sleep(0)
        assert max_active == 200

        release.set()
        await asyncio.gather(*tasks)
        assert active == 0

    async def test_idle_streams_hold_no_budget(self, monkeypatch):
        """An open stream waiting on upstream (SSE, long-poll) reserves nothing."""
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, str(1024 * 1024))
        reset_chunk_budget()
        budget = get_chunk_budget()

        release = asyncio.Event()

        class _IdleResponse(_FakeResponse):
            async def aiter_bytes(self, chunk_size: int):
                await release.wait()
                yield b"late"

        class _IdleClient(_FakeClient):
            def stream(self, **kwargs: object) -> _FakeStream:
                return _FakeStream(_IdleResponse([]))

        tunnel = _tunnel()
        tunnel._tunnel_id = "t-idle"
        tunnel._proxy._http_client = _IdleClient([])

        tasks = []
        for i in range(50):
            ws, _ = _mock_ws()
            tasks.append(
                asyncio.create_task(tunnel._handle_http_request_chunked(ws, _request(f"i-{i}")))
            )
        for _ in range(200):
            await asyncio.sleep(0)
        assert budget.in_flight == 0

        release.set()
        await asyncio.gather(*tasks)
        assert budget.in_flight == 0

    async def test_busy_endpoint_cannot_starve_another(self, monkeypatch):
        """One tunnel with many stuck streams holds at most half the budget."""
        monkeypatch.setenv(chunk_budget.CHUNK_BUDGET_ENV, str(1024 * 1024))
        reset_chunk_budget()
        budget = get_chunk_budget()
        chunk = b"x" * (64 * 1024)

        busy = _tunnel()
        busy._tunnel_id = "t-busy"
        busy._proxy._http_client = _FakeClient([chunk, chunk])
        quiet = _tunnel()
        quiet._tunnel_id = "t-quiet"
        quiet._proxy._http_client = _FakeClient([chunk])

        stuck = asyncio.Event()

        async def stuck_send(message: str) -> None:
            if ProtocolMessage.model_validate_json(message).type == MessageType.HTTP_RESPONSE_CHUNK:
                await stuck.wait()

        busy_tasks = []
        for i in range(100):
            ws = AsyncMock()
            ws.send = AsyncMock(side_effect=stuck_send)
            busy_tasks.append(
                asyncio.create_task(busy._handle_http_request_chunked(ws, _request(f"b-{i}")))
            )
        for _ in range(500):
            await asyncio.sleep(0)
        assert budget.in_flight <= budget.capacity // 2

        ws_quiet, sent = _mock_ws()
        await asyncio.wait_for(
            quiet._handle_http_request_chunked(ws_quiet, _request("q-1")), timeout=2
        )
        assert len(sent) == 3  # START, chunk, END

        stuck.set()
        await asyncio.gather(*busy_tasks)
        assert budget.in_flight == 0

    async def test_layered_budget_releases_endpoint_share_on_cancel(self):
        shared = ChunkBudget(1000)
        await shared.acquire(1000)
        layered = chunk_budget.endpoint_budget(shared)
        waiter = asyncio.create_task(layered.acquire(300))
        await asyncio.sleep(0)
        assert layered.endpoint.in_flight == 300  # endpoint share taken, waiting on shared
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert layered.endpoint.in_flight == 0
