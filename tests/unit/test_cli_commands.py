"""Unit tests for hle_client.cli commands (access, tunnels)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from hle_client.cli import main


class TestTunnelsCommand:
    def test_tunnels_empty(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_tunnels = AsyncMock(return_value=[])
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(main, ["tunnels", "--api-key", "hle_" + "a" * 32])
        assert result.exit_code == 0
        assert "No active tunnels" in result.output

    def test_tunnels_populated(self) -> None:
        runner = CliRunner()
        tunnel_data = [
            {
                "subdomain": "app-x7k",
                "service_url": "http://localhost:8080",
                "websocket_enabled": True,
                "connected_at": "2026-01-01T00:00:00",
            }
        ]
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_tunnels = AsyncMock(return_value=tunnel_data)
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(main, ["tunnels", "--api-key", "hle_" + "a" * 32])
        assert result.exit_code == 0
        assert "app-x7k" in result.output


class TestAccessListCommand:
    def test_access_list_empty(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_access_rules = AsyncMock(return_value=[])
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main, ["access", "list", "app-x7k", "--api-key", "hle_" + "a" * 32]
                )
        assert result.exit_code == 0
        assert "No access rules" in result.output

    def test_access_list_populated(self) -> None:
        runner = CliRunner()
        rules = [
            {
                "id": 1,
                "allowed_email": "friend@example.com",
                "provider": "any",
                "created_at": "2026-01-01T00:00:00",
            }
        ]
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_access_rules = AsyncMock(return_value=rules)
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main, ["access", "list", "app-x7k", "--api-key", "hle_" + "a" * 32]
                )
        assert result.exit_code == 0
        assert "friend@example.com" in result.output


class TestAccessAddCommand:
    def test_access_add_default_provider(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.add_access_rule = AsyncMock(
                return_value={"allowed_email": "new@example.com", "provider": "any"}
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    [
                        "access",
                        "add",
                        "app-x7k",
                        "new@example.com",
                        "--api-key",
                        "hle_" + "a" * 32,
                    ],
                )
        assert result.exit_code == 0
        assert "Added" in result.output
        assert "new@example.com" in result.output

    def test_access_add_custom_provider(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.add_access_rule = AsyncMock(
                return_value={"allowed_email": "dev@co.com", "provider": "github"}
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    [
                        "access",
                        "add",
                        "app-x7k",
                        "dev@co.com",
                        "--provider",
                        "github",
                        "--api-key",
                        "hle_" + "a" * 32,
                    ],
                )
        assert result.exit_code == 0
        assert "Added" in result.output


class TestAccessRemoveCommand:
    def test_access_remove_success(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.delete_access_rule = AsyncMock(return_value={"message": "ok"})
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["access", "remove", "app-x7k", "1", "--api-key", "hle_" + "a" * 32],
                )
        assert result.exit_code == 0
        assert "Removed" in result.output


class TestShareCreateCommand:
    def test_share_create(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.create_share_link = AsyncMock(
                return_value={
                    "share_url": "https://app-x7k.hle.world?_hle_share=token123",
                    "raw_token": "token123",
                    "link": {
                        "id": 1,
                        "label": "for bob",
                        "expires_at": "2026-02-15T00:00:00",
                        "max_uses": None,
                    },
                }
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    [
                        "share",
                        "create",
                        "app-x7k",
                        "--label",
                        "for bob",
                        "--api-key",
                        "hle_" + "a" * 32,
                    ],
                )
        assert result.exit_code == 0
        assert "Share link created" in result.output
        assert "token123" in result.output


class TestShareListCommand:
    def test_share_list_empty(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_share_links = AsyncMock(return_value=[])
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main, ["share", "list", "app-x7k", "--api-key", "hle_" + "a" * 32]
                )
        assert result.exit_code == 0
        assert "No share links" in result.output

    def test_share_list_populated(self) -> None:
        runner = CliRunner()
        links = [
            {
                "id": 1,
                "label": "for bob",
                "token_prefix": "abc12345",
                "expires_at": "2026-02-15T00:00:00",
                "max_uses": 5,
                "use_count": 2,
                "is_active": True,
            }
        ]
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.list_share_links = AsyncMock(return_value=links)
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main, ["share", "list", "app-x7k", "--api-key", "hle_" + "a" * 32]
                )
        assert result.exit_code == 0
        assert "abc12345" in result.output
        assert "for bob" in result.output


class TestShareRevokeCommand:
    def test_share_revoke(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.delete_share_link = AsyncMock(return_value={"message": "ok"})
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["share", "revoke", "app-x7k", "1", "--api-key", "hle_" + "a" * 32],
                )
        assert result.exit_code == 0
        assert "Revoked" in result.output


class TestErrorHandling:
    def test_error_401(self) -> None:
        import httpx

        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_resp = httpx.Response(
                401,
                text="Not authenticated",
                request=httpx.Request("GET", "http://test/api/tunnels"),
            )
            mock_client.list_tunnels = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "401", request=mock_resp.request, response=mock_resp
                )
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(main, ["tunnels", "--api-key", "hle_" + "a" * 32])
        assert result.exit_code == 0  # CLI handles error gracefully
        assert "Invalid or missing API key" in result.output

    def test_error_403(self) -> None:
        import httpx

        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_resp = httpx.Response(
                403,
                text="Forbidden",
                request=httpx.Request("GET", "http://test/api/tunnels/x-abc/access"),
            )
            mock_client.list_access_rules = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "403", request=mock_resp.request, response=mock_resp
                )
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main, ["access", "list", "x-abc", "--api-key", "hle_" + "a" * 32]
                )
        assert result.exit_code == 0
        assert "do not own" in result.output

    def test_no_api_key(self) -> None:
        runner = CliRunner()
        with patch("hle_client.tunnel._load_api_key", return_value=None):
            result = runner.invoke(main, ["tunnels"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 1
        assert "No API key found" in result.output
