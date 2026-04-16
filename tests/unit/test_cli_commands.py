"""Unit tests for hle_client.cli commands (access, tunnels)."""

from __future__ import annotations

import typing
from unittest.mock import AsyncMock, patch

if typing.TYPE_CHECKING:
    from pathlib import Path

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


class TestBasicAuthSetCommand:
    def test_basic_auth_set_success(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.set_tunnel_basic_auth = AsyncMock(return_value={"message": "ok"})
            mock_client.get_tunnel_pin_status = AsyncMock(return_value={"has_pin": False})
            mock_client.list_access_rules = AsyncMock(return_value=[])
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["basic-auth", "set", "app-x7k", "--api-key", "hle_" + "a" * 32],
                    input="admin\nsecret123\nsecret123\n",
                )
        assert result.exit_code == 0
        assert "Basic Auth set" in result.output
        assert "admin" in result.output

    def test_basic_auth_set_password_mismatch(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            result = runner.invoke(
                main,
                ["basic-auth", "set", "app-x7k", "--api-key", "hle_" + "a" * 32],
                input="admin\nsecret123\nwrongpass\n",
            )
        assert result.exit_code == 1
        assert "do not match" in result.output

    def test_basic_auth_set_short_password(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            result = runner.invoke(
                main,
                ["basic-auth", "set", "app-x7k", "--api-key", "hle_" + "a" * 32],
                input="admin\nshort\nshort\n",
            )
        assert result.exit_code == 1
        assert "8 characters" in result.output

    def test_basic_auth_set_username_with_colon(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            result = runner.invoke(
                main,
                ["basic-auth", "set", "app-x7k", "--api-key", "hle_" + "a" * 32],
                input="user:name\n",
            )
        assert result.exit_code == 1
        assert "':'" in result.output


class TestBasicAuthStatusCommand:
    def test_basic_auth_status_active(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.get_tunnel_basic_auth_status = AsyncMock(
                return_value={
                    "enabled": True,
                    "subdomain": "app-x7k",
                    "username": "admin",
                    "updated_at": "2026-02-28T12:00:00",
                }
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["basic-auth", "status", "app-x7k", "--api-key", "hle_" + "a" * 32],
                )
        assert result.exit_code == 0
        assert "active" in result.output
        assert "admin" in result.output

    def test_basic_auth_status_not_set(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.get_tunnel_basic_auth_status = AsyncMock(
                return_value={
                    "enabled": False,
                    "subdomain": "app-x7k",
                    "username": None,
                    "updated_at": None,
                }
            )
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["basic-auth", "status", "app-x7k", "--api-key", "hle_" + "a" * 32],
                )
        assert result.exit_code == 0
        assert "No Basic Auth" in result.output


class TestBasicAuthRemoveCommand:
    def test_basic_auth_remove_success(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._resolve_api_key", return_value="hle_" + "a" * 32):
            mock_client = AsyncMock()
            mock_client.remove_tunnel_basic_auth = AsyncMock(return_value={"message": "ok"})
            with patch("hle_client.api.ApiClient", return_value=mock_client):
                result = runner.invoke(
                    main,
                    ["basic-auth", "remove", "app-x7k", "--api-key", "hle_" + "a" * 32],
                )
        assert result.exit_code == 0
        assert "removed" in result.output


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
        with patch("hle_client.cli._load_api_key", return_value=None):
            result = runner.invoke(main, ["tunnels"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 1
        assert "No API key found" in result.output
        assert "hle auth login" in result.output


class TestAuthLogin:
    """Tests for ``hle auth login``."""

    def test_auth_login_with_api_key(self, tmp_path: Path) -> None:
        runner = CliRunner()
        config_file = tmp_path / "config.toml"
        with (
            patch("hle_client.tunnel._CONFIG_FILE", config_file),
            patch("hle_client.tunnel._CONFIG_DIR", tmp_path),
        ):
            result = runner.invoke(main, ["auth", "login", "--api-key", "hle_" + "a" * 32])
        assert result.exit_code == 0
        assert "Saved" in result.output
        assert config_file.exists()
        content = config_file.read_text()
        assert "hle_" + "a" * 32 in content

    def test_auth_login_interactive(self, tmp_path: Path) -> None:
        runner = CliRunner()
        config_file = tmp_path / "config.toml"
        valid_key = "hle_" + "b" * 32
        with (
            patch("hle_client.tunnel._CONFIG_FILE", config_file),
            patch("hle_client.tunnel._CONFIG_DIR", tmp_path),
            patch("hle_client.cli.webbrowser.open"),
        ):
            result = runner.invoke(main, ["auth", "login"], input=valid_key + "\n")
        assert result.exit_code == 0
        assert "Saved" in result.output
        assert config_file.exists()
        assert valid_key in config_file.read_text()

    def test_auth_login_invalid_key(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "login", "--api-key", "bad_key"])
        assert result.exit_code == 1
        assert "Invalid API key format" in result.output

    def test_auth_login_invalid_key_too_short(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "login", "--api-key", "hle_abc"])
        assert result.exit_code == 1
        assert "Invalid API key format" in result.output


class TestAuthStatus:
    """Tests for ``hle auth status``."""

    def test_auth_status_from_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        valid_key = "hle_" + "c" * 32
        with patch("hle_client.cli._load_api_key", return_value=valid_key):
            result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 0
        assert "config file" in result.output
        assert "hle_" in result.output
        assert valid_key not in result.output  # masked

    def test_auth_status_from_env(self) -> None:
        runner = CliRunner()
        valid_key = "hle_" + "d" * 32
        result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": valid_key})
        assert result.exit_code == 0
        assert "environment variable" in result.output
        assert valid_key not in result.output  # masked

    def test_auth_status_no_key(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._load_api_key", return_value=None):
            result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 0
        assert "No API key configured" in result.output


class TestAuthLogout:
    """Tests for ``hle auth logout``."""

    def test_auth_logout(self, tmp_path: Path) -> None:
        runner = CliRunner()
        config_file = tmp_path / "config.toml"
        config_file.write_text('api_key = "hle_' + "e" * 32 + '"\nother = "keep"\n')
        with (
            patch("hle_client.tunnel._CONFIG_FILE", config_file),
            patch("hle_client.tunnel._CONFIG_DIR", tmp_path),
        ):
            result = runner.invoke(main, ["auth", "logout"])
        assert result.exit_code == 0
        assert "API key removed" in result.output
        content = config_file.read_text()
        assert "api_key" not in content
        assert "other" in content  # other config preserved

    def test_auth_logout_no_key(self, tmp_path: Path) -> None:
        runner = CliRunner()
        config_file = tmp_path / "nonexistent.toml"
        with patch("hle_client.tunnel._CONFIG_FILE", config_file):
            result = runner.invoke(main, ["auth", "logout"])
        assert result.exit_code == 0
        assert "No API key saved" in result.output


class TestExposeNoAutoSave:
    """Verify that ``hle expose`` no longer auto-saves the API key."""

    def test_expose_does_not_auto_save(self, tmp_path: Path) -> None:
        """The auto-save block was removed; _save_api_key should never be called."""
        runner = CliRunner()
        with patch("hle_client.cli.asyncio.run") as mock_run:
            mock_run.return_value = None
            result = runner.invoke(
                main,
                [
                    "expose",
                    "--service",
                    "http://localhost:8080",
                    "--label",
                    "test",
                    "--api-key",
                    "hle_" + "f" * 32,
                ],
            )
        # expose ran (asyncio.run was called) — it should not call _save_api_key
        assert result.exit_code == 0
        assert mock_run.called
