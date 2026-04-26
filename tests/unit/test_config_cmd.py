"""Unit tests for the ``hle config`` command group."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from hle_client.cli import main

_KEY = "hle_" + "a" * 32


def _patch_client(mock_client: AsyncMock):
    """Patch ApiClient construction in both api module and config_cmd module."""
    return patch("hle_client.config_cmd.ApiClient", return_value=mock_client)


class TestConfigShow:
    def test_show_renders_basic_status(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        mock_client.get_tunnel_status = AsyncMock(
            return_value={
                "subdomain": "ha-x7k",
                "public_url": "https://ha-x7k.hle.world",
                "is_active": False,
                "auth_mode": "sso",
                "access_rules": [{"allowed_email": "alice@example.com", "provider": "google"}],
                "pin": {"has_pin": False},
                "basic_auth": {"enabled": False},
                "is_protected": True,
            }
        )
        with _patch_client(mock_client):
            result = runner.invoke(main, ["config", "show", "ha", "--api-key", _KEY])
        assert result.exit_code == 0, result.output
        assert "ha-x7k" in result.output
        assert "alice@example.com" in result.output
        # user_code lookup happened
        mock_client.get_me.assert_awaited_once()
        mock_client.get_tunnel_status.assert_awaited_once_with("ha-x7k")

    def test_show_passthrough_for_full_subdomain(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock()  # should NOT be called
        mock_client.get_tunnel_status = AsyncMock(
            return_value={
                "subdomain": "ha-x7k",
                "public_url": "https://ha-x7k.hle.world",
                "is_active": False,
                "auth_mode": "sso",
                "access_rules": [],
                "pin": {"has_pin": False},
                "basic_auth": {"enabled": False},
                "is_protected": True,
            }
        )
        with _patch_client(mock_client):
            result = runner.invoke(main, ["config", "show", "ha-x7k", "--api-key", _KEY])
        assert result.exit_code == 0, result.output
        mock_client.get_me.assert_not_called()
        mock_client.get_tunnel_status.assert_awaited_once_with("ha-x7k")


class TestConfigAuthMode:
    def test_auth_mode_set_none(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        mock_client.set_tunnel_auth_mode = AsyncMock(
            return_value={"subdomain": "ha-x7k", "auth_mode": "none"}
        )
        with _patch_client(mock_client):
            result = runner.invoke(
                main,
                ["config", "auth-mode", "ha", "--set", "none", "--api-key", _KEY],
            )
        assert result.exit_code == 0, result.output
        mock_client.set_tunnel_auth_mode.assert_awaited_once_with("ha-x7k", "none")
        assert "auth_mode = none" in result.output

    def test_auth_mode_invalid_value(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["config", "auth-mode", "ha", "--set", "bogus", "--api-key", _KEY]
        )
        assert result.exit_code != 0
        assert "bogus" in result.output


class TestConfigAccessReplace:
    def _status(self, rules: list[dict]) -> dict:
        return rules

    def test_access_reconcile_adds_and_removes(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        mock_client.list_access_rules = AsyncMock(
            return_value=[
                {"id": 1, "allowed_email": "alice@example.com", "provider": "google"},
                {"id": 2, "allowed_email": "bob@example.com", "provider": "github"},
            ]
        )
        mock_client.add_access_rule = AsyncMock(return_value={})
        mock_client.delete_access_rule = AsyncMock(return_value={})

        with _patch_client(mock_client):
            result = runner.invoke(
                main,
                [
                    "config",
                    "access",
                    "ha",
                    "--replace",
                    "google:alice@example.com",
                    "--replace",
                    "github:carol@example.com",
                    "--api-key",
                    _KEY,
                ],
            )

        assert result.exit_code == 0, result.output
        # alice unchanged → no add. bob removed. carol added.
        mock_client.add_access_rule.assert_awaited_once_with(
            "ha-x7k", "carol@example.com", "github"
        )
        mock_client.delete_access_rule.assert_awaited_once_with("ha-x7k", 2)
        assert "carol@example.com" in result.output
        assert "bob@example.com" in result.output

    def test_access_reconcile_already_in_sync(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        mock_client.list_access_rules = AsyncMock(
            return_value=[
                {"id": 1, "allowed_email": "alice@example.com", "provider": "google"},
            ]
        )
        mock_client.add_access_rule = AsyncMock()
        mock_client.delete_access_rule = AsyncMock()

        with _patch_client(mock_client):
            result = runner.invoke(
                main,
                [
                    "config",
                    "access",
                    "ha",
                    "--replace",
                    "google:alice@example.com",
                    "--api-key",
                    _KEY,
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.add_access_rule.assert_not_called()
        mock_client.delete_access_rule.assert_not_called()
        assert "in sync" in result.output

    def test_access_no_replace_flag_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["config", "access", "ha", "--api-key", _KEY])
        assert result.exit_code != 0
        assert "--replace" in result.output
