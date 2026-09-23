"""SIGTERM unwinds the main task instead of killing the process.

Without a handler, a service stop is a bare TCP close: the relay never gets a
WebSocket close frame and drops every browser socket riding the tunnel.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from unittest.mock import patch

import pytest

from hle_client import shutdown

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")


class TestRunCancellable:
    def test_sigterm_cancels_the_task_and_returns_none(self) -> None:
        cancelled = asyncio.Event()
        unwound = False

        async def work() -> str:
            nonlocal unwound
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                unwound = True  # the close frame goes out here in real code
                cancelled.set()
                raise
            return "never"

        async def scenario() -> str | None:
            loop = asyncio.get_running_loop()
            loop.call_later(0.01, os.kill, os.getpid(), signal.SIGTERM)
            return await shutdown.run_cancellable(work())

        result = asyncio.run(scenario())
        assert result is None
        assert unwound is True
        assert cancelled.is_set()

    def test_sigint_surfaces_as_keyboard_interrupt_after_unwinding(self) -> None:
        unwound = False

        async def work() -> None:
            nonlocal unwound
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                unwound = True
                raise

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            loop.call_later(0.01, os.kill, os.getpid(), signal.SIGINT)
            await shutdown.run_cancellable(work())

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(scenario())
        assert unwound is True

    def test_a_normal_result_passes_through(self) -> None:
        async def work() -> int:
            return 7

        assert asyncio.run(shutdown.run_cancellable(work())) == 7

    def test_handlers_are_removed_afterwards(self) -> None:
        async def scenario() -> None:
            loop = asyncio.get_running_loop()

            async def work() -> None:
                return None

            await shutdown.run_cancellable(work())
            # Removing an unregistered handler returns False; a leftover one True.
            assert loop.remove_signal_handler(signal.SIGTERM) is False

        asyncio.run(scenario())

    def test_a_cancel_from_elsewhere_still_propagates(self) -> None:
        """Only a signal is swallowed; the caller cancelling us is still a cancel."""

        async def scenario() -> None:
            async def work() -> None:
                await asyncio.sleep(30)

            inner = asyncio.ensure_future(shutdown.run_cancellable(work()))
            await asyncio.sleep(0)
            inner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await inner

        asyncio.run(scenario())


class TestInstallSignalHandlers:
    def test_installs_on_a_fresh_loop_and_cancels_the_task(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(asyncio.sleep(30))
            seen: list[str] = []

            def on_signal(name: str) -> None:
                seen.append(name)
                task.cancel()

            hooked = shutdown.install_signal_handlers(loop, on_signal)
            assert signal.SIGTERM in hooked
            loop.call_soon(os.kill, os.getpid(), signal.SIGTERM)
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(task)
            assert seen == ["SIGTERM"]
            shutdown.remove_signal_handlers(loop, hooked)
        finally:
            loop.close()

    def test_unsupported_platform_installs_nothing(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            with patch.object(shutdown, "_SUPPORTED", False):
                assert shutdown.install_signal_handlers(loop, lambda _n: None) == []
        finally:
            loop.close()


class TestWiring:
    """Every long-running command goes through the runner."""

    @pytest.mark.parametrize("module", ["hle_client.cli", "hle_client.fp_cmd"])
    def test_module_uses_shutdown_runner(self, module: str) -> None:
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module))
        assert "shutdown.run(" in src
        assert "asyncio.run(tunnel.connect())" not in src
        assert "asyncio.run(client.run())" not in src
