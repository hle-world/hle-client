"""The upstream body coalescer: forward slow bodies as data arrives.

``httpx``'s ``aiter_bytes(chunk_size)`` returns nothing until ``chunk_size``
bytes have accumulated, which holds a trickling body (Server-Sent Events,
long polls, progress streams) until the buffer fills or the upstream closes.
``_coalesce`` reads without a chunk size and forwards as soon as a chunk fills
or the source goes idle.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from hle_client.proxy import (
    LocalProxy,
    ProxyConfig,
    _coalesce,
    upstream_flush_seconds,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _collect(source: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in source]


class TestCoalesce:
    async def test_fast_source_yields_exact_chunk_size(self):
        target = 1024
        data = bytes(range(256)) * 40 + b"tail"
        assert len(data) == 10244

        async def source() -> AsyncIterator[bytes]:
            for start in range(0, len(data), 100):
                yield data[start : start + 100]

        chunks = await _collect(_coalesce(source(), target, 0.005))

        assert b"".join(chunks) == data
        assert all(len(chunk) == target for chunk in chunks[:-1])
        assert len(chunks[-1]) == len(data) - target * (len(chunks) - 1)

    async def test_slow_source_flushes_after_idle(self):
        async def source() -> AsyncIterator[bytes]:
            yield b"first"
            await asyncio.sleep(0.1)
            yield b"second"

        started = time.monotonic()
        chunks: list[tuple[float, bytes]] = []
        async for chunk in _coalesce(source(), 4096, 0.005):
            chunks.append((time.monotonic() - started, chunk))

        assert chunks[0][1] == b"first"
        # Forwarded while the source slept, well before the next read.
        assert chunks[0][0] < 0.05
        assert b"".join(chunk for _, chunk in chunks) == b"firstsecond"

    async def test_source_exception_propagates(self):
        async def source() -> AsyncIterator[bytes]:
            yield b"data"
            raise RuntimeError("boom")

        chunks: list[bytes] = []
        with pytest.raises(RuntimeError, match="boom"):
            async for chunk in _coalesce(source(), 4096, 0.005):
                chunks.append(chunk)

        # The buffered bytes are forwarded before the failure surfaces.
        assert b"".join(chunks) == b"data"

    async def test_aclose_mid_stream_leaves_no_pending_task(self):
        release = asyncio.Event()

        async def source() -> AsyncIterator[bytes]:
            yield b"first"
            await release.wait()
            yield b"second"

        baseline = asyncio.all_tasks()
        coalesced = _coalesce(source(), 4096, 0.005)
        assert await anext(coalesced) == b"first"
        await coalesced.aclose()
        await asyncio.sleep(0)

        assert asyncio.all_tasks() - baseline == set()

    async def test_zero_flush_forwards_every_read(self):
        async def source() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        chunks = await _collect(_coalesce(source(), 4096, 0.0))

        assert chunks == [b"a", b"b"]


class TestFlushConfig:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HLE_HTTP_FLUSH_MS", raising=False)
        assert upstream_flush_seconds() == 0.005

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HLE_HTTP_FLUSH_MS", "20")
        assert upstream_flush_seconds() == 0.02

    def test_clamped_to_0_50_ms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HLE_HTTP_FLUSH_MS", "500")
        assert upstream_flush_seconds() == 0.05
        monkeypatch.setenv("HLE_HTTP_FLUSH_MS", "-5")
        assert upstream_flush_seconds() == 0.0

    def test_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HLE_HTTP_FLUSH_MS", "nope")
        assert upstream_flush_seconds() == 0.005


class _TimedEventStream(httpx.AsyncByteStream):
    """An SSE body: one event every *gap* seconds, recording when produced."""

    def __init__(self, events: int = 5, gap: float = 0.1) -> None:
        self.events = events
        self.gap = gap
        self.produced: list[float] = []

    async def __aiter__(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        for _ in range(self.events):
            await asyncio.sleep(self.gap)
            self.produced.append(loop.time())
            yield b"data: x\n\n"

    async def aclose(self) -> None:
        return None


class TestSlowStreamForwarding:
    async def test_sse_events_reach_the_consumer_as_they_arrive(self):
        body = _TimedEventStream(events=5, gap=0.1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

        proxy = LocalProxy(ProxyConfig(target_url="http://upstream"))
        client = httpx.AsyncClient(
            base_url="http://upstream",
            transport=httpx.MockTransport(handler),
        )
        proxy._http_client = client
        try:
            async with asyncio.timeout(10):
                loop = asyncio.get_running_loop()
                received: list[tuple[float, bytes]] = []
                stream = proxy.stream_http(method="GET", path="/events", headers={})
                status, _, _ = await anext(stream)
                assert status == 200
                async for _status, _headers, chunk in stream:
                    assert chunk is not None
                    received.append((loop.time(), chunk))
        finally:
            await client.aclose()

        # Every event arrived, in order, as its own small chunk.
        assert [chunk for _, chunk in received] == [b"data: x\n\n"] * 5
        # Each one was forwarded right after it was produced, not held until
        # the next event or the end of the stream.
        delays = [received[index][0] - body.produced[index] for index in range(len(received))]
        assert max(delays) < 0.05
