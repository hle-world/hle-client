"""Process-wide in-flight byte budget for chunked response streaming.

The chunked HTTP response path runs one task per request and streams chunks
from the upstream service to the relay. Every in-flight chunk briefly exists
several times over — the raw bytes, the base64 string, the model dict and the
serialised JSON frame — so a client that opens many downloads at once grows
memory linearly with no bound and can OOM the process (seen in practice as the
hle-operator pod being OOM-killed under a parallel offline download, dropping
every tunnel it served).

This module holds a single budget shared by every tunnel and endpoint in the
process. A stream reserves bytes before reading the next upstream chunk and
releases them once ``ws.send()`` has returned. A stream that cannot get budget
waits rather than failing; that backpressure is what stops httpx reading ahead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque

logger = logging.getLogger(__name__)

# 8 MiB of raw chunk bytes in flight, process-wide. A send completes once the
# frame is in the local socket buffer (TCP handles the relay RTT), so this is
# not a bandwidth limit: one stream still saturates a fast link. More buffering
# would only add latency and memory. Benchmark (100 parallel downloads): peak
# RSS growth 346 MB with no budget and 512 KiB chunks, 76 MB with this default.
DEFAULT_CHUNK_BUDGET_BYTES = 8 * 1024 * 1024
CHUNK_BUDGET_ENV = "HLE_CHUNK_BUDGET_BYTES"


class ChunkBudget:
    """An asyncio byte budget: at most ``capacity`` bytes held at once.

    Reservations are granted FIFO. A stream that cannot be satisfied waits;
    it is never failed, so the upstream read simply stalls (backpressure).
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._available = capacity
        self._peak = 0
        self._waiters: deque[tuple[int, asyncio.Future[None]]] = deque()

    @property
    def in_flight(self) -> int:
        """Bytes currently reserved across all streams."""
        return self.capacity - self._available

    @property
    def available(self) -> int:
        """Bytes that may still be reserved right now."""
        return self._available

    @property
    def peak(self) -> int:
        """The most bytes ever held at once (observability / tests)."""
        return self._peak

    def reset_peak(self) -> None:
        self._peak = 0

    async def acquire(self, amount: int) -> int:
        """Reserve up to ``amount`` bytes, waiting until they are free.

        A request larger than the whole budget is capped at the capacity so a
        single oversized chunk can still make progress instead of deadlocking
        on budget that can never exist. Returns the bytes reserved.
        """
        if amount <= 0:
            return 0
        amount = min(amount, self.capacity)
        if self._available >= amount and not self._waiters:
            self._available -= amount
            self._note_peak()
            return amount

        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((amount, waiter))
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                # Granted, but the task was cancelled before it resumed: the
                # bytes were already taken off the budget, so hand them back.
                self.release(amount)
            else:
                # Cancelled while queued: withdraw the request.
                with contextlib.suppress(ValueError):
                    self._waiters.remove((amount, waiter))
                # Our place in the queue may have been blocking smaller
                # requests behind it.
                self._grant_waiters()
            raise
        return amount

    def release(self, amount: int) -> None:
        """Return ``amount`` bytes to the budget and wake any queued waiters.

        Synchronous on purpose: it must be callable from a ``finally`` while a
        task is being cancelled, where awaiting is not safe.
        """
        if amount <= 0:
            return
        self._available = min(self.capacity, self._available + amount)
        self._grant_waiters()

    def _grant_waiters(self) -> None:
        while self._waiters:
            amount, waiter = self._waiters[0]
            if waiter.done():
                self._waiters.popleft()
                continue
            if amount > self._available:
                break
            self._waiters.popleft()
            self._available -= amount
            self._note_peak()
            waiter.set_result(None)

    def _note_peak(self) -> None:
        in_flight = self.capacity - self._available
        if in_flight > self._peak:
            self._peak = in_flight


class LayeredBudget:
    """An endpoint's share of the process budget: both must have room.

    The endpoint budget is acquired first, so a busy endpoint's surplus streams
    queue on their own budget instead of in front of other tunnels on the
    shared one. This caps bytes, not connections: any number of streams run,
    but one endpoint can hold at most its share and the rest stays available
    to every other tunnel in the process (a legacy operator serves them all).
    """

    def __init__(self, endpoint: ChunkBudget, shared: ChunkBudget) -> None:
        self.endpoint = endpoint
        self.shared = shared

    async def acquire(self, amount: int) -> int:
        got = await self.endpoint.acquire(amount)
        try:
            return await self.shared.acquire(got)
        except BaseException:
            self.endpoint.release(got)
            raise

    def release(self, amount: int) -> None:
        self.shared.release(amount)
        self.endpoint.release(amount)


def endpoint_budget(shared: ChunkBudget) -> LayeredBudget:
    """A per-endpoint view of ``shared`` capped at half of it."""
    return LayeredBudget(ChunkBudget(max(1, shared.capacity // 2)), shared)


_budget: ChunkBudget | None = None


def chunk_budget_capacity() -> int:
    """The configured budget, from ``HLE_CHUNK_BUDGET_BYTES`` or the default."""
    raw = os.environ.get(CHUNK_BUDGET_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        logger.warning(
            "Ignoring invalid %s=%r; using %d bytes",
            CHUNK_BUDGET_ENV,
            raw,
            DEFAULT_CHUNK_BUDGET_BYTES,
        )
    return DEFAULT_CHUNK_BUDGET_BYTES


def get_chunk_budget() -> ChunkBudget:
    """The process-wide budget, created on first use and shared thereafter."""
    global _budget
    if _budget is None:
        _budget = ChunkBudget(chunk_budget_capacity())
    return _budget


def reset_chunk_budget() -> None:
    """Forget the shared budget so the next use re-reads the environment.

    A running process keeps one budget for its lifetime so the cap is
    genuinely process-wide; tests use this to get a fresh one.
    """
    global _budget
    _budget = None
