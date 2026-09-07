"""Unit tests for hle_client.cli commands (auth, expose) and config subcommands."""

from __future__ import annotations

import typing
from unittest.mock import AsyncMock, patch

if typing.TYPE_CHECKING:
    from pathlib import Path

from datetime import UTC

from click.testing import CliRunner

from hle_client.cli import main

_KEY = "hle_" + "a" * 32


def _patch_client(mock_client: AsyncMock):
    """Patch the ApiClient used inside config_cmd (where the runtime import resolves)."""
    return patch("hle_client.config_cmd.ApiClient", return_value=mock_client)


def _patch_key():
    return patch("hle_client.config_cmd._require_key", return_value=_KEY)


class TestConfigList:
    def test_list_empty(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_tunnels = AsyncMock(return_value=[])
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "list", "--api-key", _KEY])
        assert result.exit_code == 0
        assert "No active tunnels" in result.output

    def test_list_populated(self) -> None:
        runner = CliRunner()
        tunnel_data = [
            {
                "subdomain": "app-x7k",
                "service_url": "http://localhost:8080",
                "websocket_enabled": True,
                "connected_at": "2026-01-01T00:00:00",
            }
        ]
        mock_client = AsyncMock()
        mock_client.list_tunnels = AsyncMock(return_value=tunnel_data)
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "list", "--api-key", _KEY])
        assert result.exit_code == 0
        assert "app-x7k" in result.output


class TestConfigAccessList:
    def test_access_list_empty(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_access_rules = AsyncMock(return_value=[])
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "access", "list", "app-x7k", "--api-key", _KEY])
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
        mock_client = AsyncMock()
        mock_client.list_access_rules = AsyncMock(return_value=rules)
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "access", "list", "app-x7k", "--api-key", _KEY])
        assert result.exit_code == 0
        assert "friend@example.com" in result.output


class TestConfigAccessAdd:
    def test_access_add_default_provider(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.add_access_rule = AsyncMock(
            return_value={"allowed_email": "new@example.com", "provider": "any"}
        )
        mock_client.get_tunnel_basic_auth_status = AsyncMock(return_value={"enabled": False})
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                ["config", "access", "add", "app-x7k", "new@example.com", "--api-key", _KEY],
            )
        assert result.exit_code == 0
        assert "Added" in result.output
        assert "new@example.com" in result.output

    def test_access_add_custom_provider(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.add_access_rule = AsyncMock(
            return_value={"allowed_email": "dev@co.com", "provider": "github"}
        )
        mock_client.get_tunnel_basic_auth_status = AsyncMock(return_value={"enabled": False})
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                [
                    "config",
                    "access",
                    "add",
                    "app-x7k",
                    "dev@co.com",
                    "--provider",
                    "github",
                    "--api-key",
                    _KEY,
                ],
            )
        assert result.exit_code == 0
        assert "Added" in result.output


class TestConfigAccessRemove:
    def test_access_remove_success(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.delete_access_rule = AsyncMock(return_value={"message": "ok"})
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                ["config", "access", "remove", "app-x7k", "1", "--api-key", _KEY],
            )
        assert result.exit_code == 0
        assert "Removed" in result.output


class TestConfigShareCreate:
    def test_share_create(self) -> None:
        runner = CliRunner()
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
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                [
                    "config",
                    "share",
                    "create",
                    "app-x7k",
                    "--label",
                    "for bob",
                    "--api-key",
                    _KEY,
                ],
            )
        assert result.exit_code == 0
        assert "Share link created" in result.output
        assert "token123" in result.output


class TestConfigShareList:
    def test_share_list_empty(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_share_links = AsyncMock(return_value=[])
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "share", "list", "app-x7k", "--api-key", _KEY])
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
        mock_client = AsyncMock()
        mock_client.list_share_links = AsyncMock(return_value=links)
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "share", "list", "app-x7k", "--api-key", _KEY])
        assert result.exit_code == 0
        assert "abc12345" in result.output
        assert "for bob" in result.output


class TestConfigShareRevoke:
    def test_share_revoke(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.delete_share_link = AsyncMock(return_value={"message": "ok"})
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                ["config", "share", "revoke", "app-x7k", "1", "--api-key", _KEY],
            )
        assert result.exit_code == 0
        assert "Revoked" in result.output


class TestConfigBasicAuthSet:
    def test_set_success(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.set_tunnel_basic_auth = AsyncMock(return_value={"message": "ok"})
        mock_client.get_tunnel_pin_status = AsyncMock(return_value={"has_pin": False})
        mock_client.list_access_rules = AsyncMock(return_value=[])
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main,
                ["config", "basic-auth", "set", "app-x7k", "--api-key", _KEY],
                input="admin\nsecret123\nsecret123\n",
            )
        assert result.exit_code == 0
        assert "Basic Auth set" in result.output
        assert "admin" in result.output

    def test_password_mismatch(self) -> None:
        runner = CliRunner()
        with _patch_key():
            result = runner.invoke(
                main,
                ["config", "basic-auth", "set", "app-x7k", "--api-key", _KEY],
                input="admin\nsecret123\nwrongpass\n",
            )
        assert result.exit_code != 0
        assert "do not match" in result.output

    def test_short_password(self) -> None:
        runner = CliRunner()
        with _patch_key():
            result = runner.invoke(
                main,
                ["config", "basic-auth", "set", "app-x7k", "--api-key", _KEY],
                input="admin\nshort\nshort\n",
            )
        assert result.exit_code != 0
        assert "8 characters" in result.output

    def test_username_with_colon(self) -> None:
        runner = CliRunner()
        with _patch_key():
            result = runner.invoke(
                main,
                ["config", "basic-auth", "set", "app-x7k", "--api-key", _KEY],
                input="user:name\n",
            )
        assert result.exit_code != 0
        assert "':'" in result.output


class TestConfigBasicAuthStatus:
    def test_active(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_tunnel_basic_auth_status = AsyncMock(
            return_value={
                "enabled": True,
                "subdomain": "app-x7k",
                "username": "admin",
                "updated_at": "2026-02-28T12:00:00",
            }
        )
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main, ["config", "basic-auth", "status", "app-x7k", "--api-key", _KEY]
            )
        assert result.exit_code == 0
        assert "active" in result.output
        assert "admin" in result.output

    def test_not_set(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.get_tunnel_basic_auth_status = AsyncMock(
            return_value={"enabled": False, "username": None, "updated_at": None}
        )
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main, ["config", "basic-auth", "status", "app-x7k", "--api-key", _KEY]
            )
        assert result.exit_code == 0
        assert "No Basic Auth" in result.output


class TestConfigBasicAuthRemove:
    def test_remove_success(self) -> None:
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.remove_tunnel_basic_auth = AsyncMock(return_value={"message": "ok"})
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(
                main, ["config", "basic-auth", "remove", "app-x7k", "--api-key", _KEY]
            )
        assert result.exit_code == 0
        assert "removed" in result.output


class TestErrorHandling:
    def test_error_401(self) -> None:
        import httpx

        runner = CliRunner()
        mock_client = AsyncMock()
        mock_resp = httpx.Response(
            401, text="Not authenticated", request=httpx.Request("GET", "http://t/api/tunnels")
        )
        mock_client.list_tunnels = AsyncMock(
            side_effect=httpx.HTTPStatusError("401", request=mock_resp.request, response=mock_resp)
        )
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "list", "--api-key", _KEY])
        assert result.exit_code != 0
        assert "Invalid or missing API key" in result.output

    def test_error_403(self) -> None:
        import httpx

        runner = CliRunner()
        mock_client = AsyncMock()
        mock_resp = httpx.Response(
            403, text="Forbidden", request=httpx.Request("GET", "http://t/api/tunnels/x-abc/access")
        )
        mock_client.list_access_rules = AsyncMock(
            side_effect=httpx.HTTPStatusError("403", request=mock_resp.request, response=mock_resp)
        )
        with _patch_key(), _patch_client(mock_client):
            result = runner.invoke(main, ["config", "access", "list", "x-abc", "--api-key", _KEY])
        assert result.exit_code != 0
        assert "do not own" in result.output

    def test_no_api_key(self) -> None:
        runner = CliRunner()
        with patch("hle_client.tunnel._load_api_key", return_value=None):
            result = runner.invoke(main, ["config", "list"], env={"HLE_API_KEY": ""})
        assert result.exit_code != 0
        assert "No API key found" in result.output
        assert "hle auth login" in result.output


class TestAuthLogin:
    def test_with_api_key(self, tmp_path: Path) -> None:
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
        assert "hle_" + "a" * 32 in config_file.read_text()

    def test_interactive(self, tmp_path: Path) -> None:
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
        assert valid_key in config_file.read_text()

    def test_invalid_key(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "login", "--api-key", "bad_key"])
        assert result.exit_code == 1
        assert "Invalid API key format" in result.output

    def test_invalid_key_too_short(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "login", "--api-key", "hle_abc"])
        assert result.exit_code == 1
        assert "Invalid API key format" in result.output


class TestAuthStatus:
    def test_from_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        valid_key = "hle_" + "c" * 32
        with patch("hle_client.cli._load_api_key", return_value=valid_key):
            result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 0
        assert "config file" in result.output
        assert valid_key not in result.output  # masked

    def test_from_env(self) -> None:
        runner = CliRunner()
        valid_key = "hle_" + "d" * 32
        result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": valid_key})
        assert result.exit_code == 0
        assert "environment variable" in result.output
        assert valid_key not in result.output

    def test_no_key(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._load_api_key", return_value=None):
            result = runner.invoke(main, ["auth", "status"], env={"HLE_API_KEY": ""})
        assert result.exit_code == 0
        assert "No API key configured" in result.output


class TestAuthLogout:
    def test_logout(self, tmp_path: Path) -> None:
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
        assert "other" in content

    def test_logout_no_key(self, tmp_path: Path) -> None:
        runner = CliRunner()
        config_file = tmp_path / "nonexistent.toml"
        with patch("hle_client.tunnel._CONFIG_FILE", config_file):
            result = runner.invoke(main, ["auth", "logout"])
        assert result.exit_code == 0
        assert "No API key saved" in result.output


class TestAgentCli:
    def test_enroll_saves_token(self, tmp_path: Path) -> None:
        runner = CliRunner()
        cfg = tmp_path / "agent.toml"
        with patch("hle_client.agent.AGENT_CONFIG_PATH", cfg):
            result = runner.invoke(main, ["agent", "enroll", "hlea_" + "a" * 40])
        assert result.exit_code == 0
        assert "Enrolled" in result.output
        assert "token" in cfg.read_text()

    def test_enroll_rejects_bad_token(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "enroll", "hle_notanagent"])
        assert result.exit_code == 1
        assert "Invalid agent token" in result.output

    def test_status_no_token(self, tmp_path: Path) -> None:
        """Exits non-zero, so a script can tell enrolled from not.

        This used to exit 0, which is how the installer could confirm
        enrollment "worked", install a service, and leave it restarting
        forever on a token that was never saved.
        """
        runner = CliRunner()
        cfg = tmp_path / "missing.toml"
        with patch("hle_client.agent.AGENT_CONFIG_PATH", cfg):
            result = runner.invoke(main, ["agent", "status"])
        assert result.exit_code == 1
        assert "No agent token" in result.output

    def test_run_requires_token(self, tmp_path: Path) -> None:
        runner = CliRunner()
        cfg = tmp_path / "missing.toml"
        with patch("hle_client.agent.AGENT_CONFIG_PATH", cfg):
            result = runner.invoke(main, ["agent", "run"])
        assert result.exit_code == 1
        assert "No agent token" in result.output

    def test_run_invokes_client(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # Close the coroutine instead of awaiting it (avoids "never awaited" warning).
        with patch("hle_client.cli.asyncio.run", side_effect=lambda coro: coro.close()) as mock_run:
            result = runner.invoke(main, ["agent", "run", "--token", "hlea_" + "b" * 40])
        assert result.exit_code == 0
        assert mock_run.called


class TestExposeNoAutoSave:
    def test_expose_does_not_auto_save(self, tmp_path: Path) -> None:
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
        assert result.exit_code == 0
        assert mock_run.called


class TestAgentList:
    """`hle agent list` — the lookup for what to pass to `hle fp --agent`."""

    def _invoke(self, agents, extra=None):
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_agents.return_value = agents
        with patch("hle_client.api.ApiClient", return_value=mock_client):
            return runner.invoke(main, ["agent", "list", "--api-key", _KEY, *(extra or [])])

    def test_shows_name_status_and_endpoints(self) -> None:
        result = self._invoke(
            [
                {
                    "name": "trikala",
                    "online": True,
                    "endpoint_count": 2,
                    "agent_version": "2607.6",
                    "last_seen_at": None,
                }
            ]
        )
        assert result.exit_code == 0
        assert "trikala" in result.output
        assert "online" in result.output

    def test_offline_agent_is_marked(self) -> None:
        result = self._invoke([{"name": "nas", "online": False, "endpoint_count": 0}])
        assert result.exit_code == 0
        assert "offline" in result.output

    def test_empty_list_explains_what_to_do(self) -> None:
        result = self._invoke([])
        assert result.exit_code == 0
        assert "No agents yet" in result.output

    def test_on_an_agent_host_it_says_why_the_token_it_has_is_not_enough(self) -> None:
        """ "No API key" on a machine that is plainly set up reads as broken.

        Reported from an enrolled host: `hle agent list` answered "No API key.
        Run hle auth login first." while the machine was happily running an
        agent. It has a credential — just not one allowed to read account data,
        and nothing said so.
        """
        runner = CliRunner()
        with (
            patch("hle_client.cli._load_api_key", return_value=None),
            patch("hle_client.cli.load_agent_token", return_value="hlea_something"),
        ):
            result = runner.invoke(main, ["agent", "list"])

        assert result.exit_code == 1
        out = " ".join(result.output.split())
        assert "enrolled as an agent" in out
        assert "cannot read your account" in out
        # And points at the command that does work here.
        assert "hle agent status" in out

    def test_without_an_agent_token_the_message_stays_short(self) -> None:
        """No agent on this machine means there is nothing extra to explain."""
        runner = CliRunner()
        with (
            patch("hle_client.cli._load_api_key", return_value=None),
            patch("hle_client.cli.load_agent_token", return_value=None),
        ):
            result = runner.invoke(main, ["agent", "list"])

        assert result.exit_code == 1
        assert "enrolled as an agent" not in result.output

    def test_it_reads_the_env_var_its_help_promises(self, monkeypatch) -> None:
        """The help said "else env/config" and only ever read config.

        Found on a host whose API key lives in a systemd EnvironmentFile:
        exporting HLE_API_KEY and running this still answered "No API key",
        because the option never declared `envvar`.
        """
        monkeypatch.setenv("HLE_API_KEY", _KEY)
        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_agents.return_value = []
        with (
            patch("hle_client.cli._load_api_key", return_value=None),
            patch("hle_client.api.ApiClient", return_value=mock_client),
        ):
            result = runner.invoke(main, ["agent", "list"])

        assert result.exit_code == 0, result.output
        assert "No API key" not in result.output

    def test_a_401_prints_what_the_relay_said(self, monkeypatch) -> None:
        """The relay knows which credential arrived; the client just relays it.

        Seen in the field: `/etc/hle/otsiasi.env` set HLE_API_KEY to an `hlea_`
        enrollment token. The tunnel it was installed for ran happily for weeks
        while this command answered "API key rejected. Run hle auth login." —
        advice that could not have helped. The explanation lives on the relay
        so an already-installed client gets it without upgrading.
        """
        import httpx

        request = httpx.Request("GET", "https://hle.world/api/agents")
        response = httpx.Response(
            401,
            json={"detail": "That is an agent enrollment token, not an API key."},
            request=request,
        )
        mock_client = AsyncMock()
        mock_client.list_agents.side_effect = httpx.HTTPStatusError(
            "401", request=request, response=response
        )
        runner = CliRunner()
        with patch("hle_client.api.ApiClient", return_value=mock_client):
            result = runner.invoke(main, ["agent", "list", "--api-key", _KEY])

        out = " ".join(result.output.split())
        assert result.exit_code == 1
        assert "agent enrollment token, not an API key" in out

    def test_a_401_without_a_detail_still_says_something(self, monkeypatch) -> None:
        """An older relay, or an unparseable body, must not print an empty error."""
        import httpx

        request = httpx.Request("GET", "https://hle.world/api/agents")
        response = httpx.Response(401, content=b"nope", request=request)
        mock_client = AsyncMock()
        mock_client.list_agents.side_effect = httpx.HTTPStatusError(
            "401", request=request, response=response
        )
        runner = CliRunner()
        with patch("hle_client.api.ApiClient", return_value=mock_client):
            result = runner.invoke(main, ["agent", "list", "--api-key", _KEY])

        assert result.exit_code == 1
        assert "API key rejected" in result.output

    def test_json_output_is_machine_readable(self) -> None:
        import json

        agents = [{"name": "rpi", "online": True, "endpoint_count": 1}]
        result = self._invoke(agents, ["--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == agents

    def test_missing_key_is_a_clear_error(self) -> None:
        runner = CliRunner()
        with patch("hle_client.cli._load_api_key", return_value=None):
            result = runner.invoke(main, ["agent", "list"])
        assert result.exit_code == 1
        assert "No API key" in result.output

    def test_disabled_feature_is_explained_not_a_traceback(self) -> None:
        import httpx

        runner = CliRunner()
        mock_client = AsyncMock()
        mock_client.list_agents.side_effect = httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "https://hle.world/api/agents"),
            response=httpx.Response(404),
        )
        with patch("hle_client.api.ApiClient", return_value=mock_client):
            result = runner.invoke(main, ["agent", "list", "--api-key", _KEY])
        assert result.exit_code == 1
        assert "not enabled" in result.output


class TestHumanizeLastSeen:
    def test_never(self) -> None:
        from hle_client.cli import _humanize_last_seen

        assert _humanize_last_seen(None) == "never"

    def test_recent_is_seconds(self) -> None:
        from datetime import datetime, timedelta

        from hle_client.cli import _humanize_last_seen

        ts = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
        assert _humanize_last_seen(ts).endswith("s ago")

    def test_hours(self) -> None:
        from datetime import datetime, timedelta

        from hle_client.cli import _humanize_last_seen

        ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        assert _humanize_last_seen(ts) == "5h ago"

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        """The server serialises without a tz suffix; subtracting a naive from
        an aware datetime raises, so this would crash the table not misformat it.
        """
        from datetime import datetime, timedelta

        from hle_client.cli import _humanize_last_seen

        ts = (datetime.now(UTC) - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
        assert _humanize_last_seen(ts) == "10m ago"

    def test_unparseable_falls_back_to_the_raw_value(self) -> None:
        from hle_client.cli import _humanize_last_seen

        assert _humanize_last_seen("not-a-date") == "not-a-date"
