"""Tests for TunnelFatalError handling in connect()."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import websockets.exceptions
import websockets.frames

from hle_client.tunnel import Tunnel, TunnelConfig, TunnelFatalError


class _AsyncIter:
    """Turn a plain list into an async iterator suitable for ``async for``."""

    def __init__(self, items: list):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _tunnel(service_url: str = "http://localhost:8123", **kw) -> Tunnel:
    """Create a Tunnel with sensible test defaults."""
    return Tunnel(TunnelConfig(service_url=service_url, **kw))


class TestTunnelFatalErrorCleanup:
    """Test that TunnelFatalError (4001/4003) triggers final cleanup and dismantled hook."""

    async def test_4001_sets_final_and_fires_dismantled_hook(self):
        tunnel = _tunnel(api_key="test-key")
        tunnel._tunnel_id = "t-4001"
        tunnel._subdomain = "app-4001"
        tunnel._public_url = "https://app-4001.hle.world"

        cleanup_calls = []
        original_cleanup = tunnel._cleanup

        async def tracking_cleanup(final=False):
            cleanup_calls.append(final)
            await original_cleanup(final)

        tunnel._cleanup = tracking_cleanup

        hook_fired = []

        async def fake_fire(event, **kwargs):
            hook_fired.append((event, kwargs))

        tunnel._hook_runner.fire = fake_fire

        async def mock_connect_once():
            # Simulate a ConnectionClosed with code 4001 that occurs during the receive loop
            close = websockets.exceptions.ConnectionClosed(
                websockets.frames.Close(code=4001, reason="auth failed"),
                None
            )
            raise close

        tunnel._proxy.start = AsyncMock()

        with patch.object(tunnel, '_connect_once', mock_connect_once):
            try:
                await tunnel.connect()
            except TunnelFatalError as e:
                assert "Authentication failed" in str(e)
            else:
                pytest.fail("Expected TunnelFatalError to be raised")

        # Verify cleanup was called with final=True at least once
        assert True in cleanup_calls, f"Expected final=True in cleanup calls, got {cleanup_calls}"

        # Verify tunnel_dismantled hook fired exactly once with correct args
        assert len(hook_fired) == 1
        event, kwargs = hook_fired[0]
        assert event == "tunnel_dismantled"
        assert kwargs["tunnel_id"] == "t-4001"
        assert kwargs["subdomain"] == "app-4001"
        assert kwargs["public_url"] == "https://app-4001.hle.world"

    async def test_4003_sets_final_and_fires_dismantled_hook(self):
        tunnel = _tunnel(api_key="test-key")
        tunnel._tunnel_id = "t-4003"
        tunnel._subdomain = "app-4003"
        tunnel._public_url = "https://app-4003.hle.world"

        cleanup_calls = []
        original_cleanup = tunnel._cleanup

        async def tracking_cleanup(final=False):
            cleanup_calls.append(final)
            await original_cleanup(final)

        tunnel._cleanup = tracking_cleanup

        hook_fired = []

        async def fake_fire(event, **kwargs):
            hook_fired.append((event, kwargs))

        tunnel._hook_runner.fire = fake_fire

        async def mock_connect_once():
            close = websockets.exceptions.ConnectionClosed(
                websockets.frames.Close(code=4003, reason="limit reached"),
                None
            )
            raise close

        tunnel._proxy.start = AsyncMock()

        with patch.object(tunnel, '_connect_once', mock_connect_once):
            try:
                await tunnel.connect()
            except TunnelFatalError as e:
                assert "Tunnel limit reached" in str(e)
            else:
                pytest.fail("Expected TunnelFatalError to be raised")

        assert True in cleanup_calls
        assert len(hook_fired) == 1
        event, kwargs = hook_fired[0]
        assert event == "tunnel_dismantled"
        assert kwargs["tunnel_id"] == "t-4003"
        assert kwargs["subdomain"] == "app-4003"
        assert kwargs["public_url"] == "https://app-4003.hle.world"