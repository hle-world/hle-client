"""Unit tests for relay discovery (ApiClient.discover_relay and Tunnel._discover_relay_uri)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hle_client.api import ApiClient, ApiClientConfig
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.models import RelayDiscoveryResponse

# ---------------------------------------------------------------------------
# ApiClient.discover_relay
# ---------------------------------------------------------------------------


class TestDiscoverRelay:
    @pytest.fixture
    def client(self) -> ApiClient:
        return ApiClient(ApiClientConfig(api_key="hle_testkey"))

    async def test_success(self, client: ApiClient) -> None:
        body = {
            "relay_url": "wss://us-east.hle.world:443/_hle/tunnel",
            "relay_region": "us-east-1",
            "ttl": 600,
            "fallback_urls": ["wss://eu-west.hle.world:443/_hle/tunnel"],
            "metadata": {"version": "2"},
        }
        mock_resp = httpx.Response(
            200,
            json=body,
            request=httpx.Request("GET", "https://hle.world/api/v1/connect"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.discover_relay()

        assert result is not None
        assert result.relay_url == "wss://us-east.hle.world:443/_hle/tunnel"
        assert result.relay_region == "us-east-1"
        assert result.ttl == 600
        assert result.fallback_urls == ["wss://eu-west.hle.world:443/_hle/tunnel"]
        assert result.metadata == {"version": "2"}

    async def test_404_returns_none(self, client: ApiClient) -> None:
        mock_resp = httpx.Response(
            404,
            text="Not Found",
            request=httpx.Request("GET", "https://hle.world/api/v1/connect"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.discover_relay()

        assert result is None

    async def test_network_error_returns_none(self, client: ApiClient) -> None:
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = await client.discover_relay()

        assert result is None

    async def test_timeout_returns_none(self, client: ApiClient) -> None:
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = await client.discover_relay()

        assert result is None


# ---------------------------------------------------------------------------
# Tunnel._discover_relay_uri
# ---------------------------------------------------------------------------


class TestDiscoverRelayUri:
    def _make_tunnel(self) -> Tunnel:
        config = TunnelConfig(
            service_url="http://localhost:8080",
            api_key="hle_testkey",
        )
        return Tunnel(config=config)

    async def test_uses_discovery_url(self) -> None:
        tunnel = self._make_tunnel()
        discovery = RelayDiscoveryResponse(
            relay_url="wss://us-east.hle.world:443/_hle/tunnel",
            relay_region="us-east-1",
        )
        with patch(
            "hle_client.api.ApiClient.discover_relay",
            new_callable=AsyncMock,
            return_value=discovery,
        ):
            uri = await tunnel._discover_relay_uri("hle_testkey")

        assert uri == "wss://us-east.hle.world:443/_hle/tunnel"

    async def test_falls_back_on_none(self) -> None:
        tunnel = self._make_tunnel()
        with patch(
            "hle_client.api.ApiClient.discover_relay",
            new_callable=AsyncMock,
            return_value=None,
        ):
            uri = await tunnel._discover_relay_uri("hle_testkey")

        assert uri == "wss://hle.world:443/_hle/tunnel"

    async def test_falls_back_on_exception(self) -> None:
        tunnel = self._make_tunnel()
        with patch(
            "hle_client.api.ApiClient.discover_relay",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            uri = await tunnel._discover_relay_uri("hle_testkey")

        assert uri == "wss://hle.world:443/_hle/tunnel"
