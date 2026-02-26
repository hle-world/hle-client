"""Unit tests for hle_client.api module."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hle_client.api import ApiClient, ApiClientConfig


class TestApiClientConfig:
    def test_defaults(self) -> None:
        config = ApiClientConfig()
        assert config.api_key == ""

    def test_custom_api_key(self) -> None:
        config = ApiClientConfig(api_key="hle_abc")
        assert config.api_key == "hle_abc"


class TestApiClientInit:
    def test_base_url_is_hle_world(self) -> None:
        client = ApiClient(ApiClientConfig(api_key="hle_test"))
        assert client._base_url == "https://hle.world"

    def test_authorization_header(self) -> None:
        client = ApiClient(ApiClientConfig(api_key="hle_mykey123"))
        assert client._headers["Authorization"] == "Bearer hle_mykey123"


class TestApiClientMethods:
    @pytest.fixture
    def client(self) -> ApiClient:
        return ApiClient(ApiClientConfig(api_key="hle_testkey"))

    async def test_list_tunnels(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            200,
            json=[{"subdomain": "app-x7k", "service_url": "http://localhost:8080"}],
            request=httpx.Request("GET", "https://hle.world/api/tunnels"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await client.list_tunnels()
        assert len(result) == 1
        assert result[0]["subdomain"] == "app-x7k"

    async def test_list_access_rules(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            200,
            json=[{"id": 1, "allowed_email": "a@b.com", "provider": "any"}],
            request=httpx.Request("GET", "https://hle.world/api/tunnels/app-x7k/access"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await client.list_access_rules("app-x7k")
        assert len(result) == 1
        assert result[0]["allowed_email"] == "a@b.com"

    async def test_add_access_rule(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            200,
            json={"id": 2, "allowed_email": "new@b.com", "provider": "github"},
            request=httpx.Request("POST", "https://hle.world/api/tunnels/app-x7k/access"),
        )
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.add_access_rule("app-x7k", "new@b.com", "github")
        assert result["allowed_email"] == "new@b.com"
        assert result["provider"] == "github"

    async def test_delete_access_rule(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            200,
            json={"message": "ok"},
            request=httpx.Request("DELETE", "https://hle.world/api/tunnels/app-x7k/access/1"),
        )
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock, return_value=mock_response):
            result = await client.delete_access_rule("app-x7k", 1)
        assert result["message"] == "ok"

    async def test_error_propagation_401(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            401,
            text="Not authenticated",
            request=httpx.Request("GET", "https://hle.world/api/tunnels"),
        )
        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await client.list_tunnels()

    async def test_error_propagation_403(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            403,
            text="You do not own this subdomain",
            request=httpx.Request("GET", "https://hle.world/api/tunnels/other-abc/access"),
        )
        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await client.list_access_rules("other-abc")

    async def test_error_propagation_404(self, client: ApiClient) -> None:
        mock_response = httpx.Response(
            404,
            text="Access rule not found",
            request=httpx.Request("DELETE", "https://hle.world/api/tunnels/app-x7k/access/999"),
        )
        with (
            patch("httpx.AsyncClient.delete", new_callable=AsyncMock, return_value=mock_response),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await client.delete_access_rule("app-x7k", 999)
