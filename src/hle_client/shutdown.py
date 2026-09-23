"""Graceful shutdown for the long-running commands.

A tunnel or agent that dies on SIGTERM never sends a WebSocket close frame:
the relay sees a bare TCP close and drops every browser WebSocket riding the
tunnel. systemd, launchd, Docker and ``kill`` all stop a process with SIGTERM,
so this is how most tunnels end. Cancelling the main task instead lets
``Tunnel`` and ``AgentClient`` unwind their ``async with websockets.connect``
blocks, which sends a proper close frame, before the process exits 0.

SIGINT gets the same treatment so Ctrl+C and a service stop behave alike; it
surfaces as :class:`KeyboardInterrupt` so callers' existing handlers still print
their "Shutting down" line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Windows has no loop.add_signal_handler; leave the default behaviour there.
_SUPPORTED = sys.platform != "win32"


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_signal: Callable[[str], None]
) -> list[signal.Signals]:
    """Route SIGTERM and SIGINT to *on_signal*. Returns the signals actually hooked."""
    if not _SUPPORTED:
        return []
    hooked: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, on_signal, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            # Not the main thread, or a loop that cannot do it: default it is.
            continue
        hooked.append(sig)
    return hooked


def remove_signal_handlers(loop: asyncio.AbstractEventLoop, hooked: list[signal.Signals]) -> None:
    for sig in hooked:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def run_cancellable(coro: Coroutine[Any, Any, T]) -> T | None:
    """Await *coro* as a task that SIGTERM/SIGINT cancel instead of killing.

    Returns the coroutine's result, or ``None`` when SIGTERM ended it. SIGINT
    is re-raised as :class:`KeyboardInterrupt` after the task has unwound.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    received: list[str] = []

    def _on_signal(name: str) -> None:
        if received:
            return  # a second signal while unwinding: keep unwinding
        received.append(name)
        logger.info("Received %s — shutting down", name)
        task.cancel()

    hooked = install_signal_handlers(loop, _on_signal)
    try:
        return await task
    except asyncio.CancelledError:
        if not received:
            raise
        if received[0] == "SIGINT":
            raise KeyboardInterrupt from None
        return None
    finally:
        remove_signal_handlers(loop, hooked)


def run(coro: Coroutine[Any, Any, T]) -> T | None:
    """``asyncio.run`` for a long-running command that must unwind on SIGTERM."""
    return asyncio.run(run_cancellable(coro))
