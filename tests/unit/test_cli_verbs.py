"""One set of verbs: list / get / create / set / delete (plan §2.5).

Removal used six verbs, reading three, setting two shapes (a verb, and an
option named `--set`). Each canonical spelling is added here and every old one
kept as a hidden alias, because units, scripts and support answers carry them.
These tests pin both: the new spelling is what help shows and what runs, and
the old spelling still reaches the very same command, silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from hle_client.cli import main

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "hle_" + "a" * 32

# (group path, old spelling, canonical spelling)
_VERB_PAIRS = [
    (("tunnel", "access"), "add", "create"),
    (("tunnel", "access"), "remove", "delete"),
    (("tunnel", "pin"), "status", "get"),
    (("tunnel", "pin"), "remove", "delete"),
    (("tunnel", "basic-auth"), "status", "get"),
    (("tunnel", "basic-auth"), "remove", "delete"),
    (("tunnel", "share"), "revoke", "delete"),
    (("tunnel",), "auth-mode", "set"),
    (("daemon",), "uninstall", "delete"),
]


def _group(path: tuple[str, ...]) -> click.Group:
    cmd: click.Command = main
    for part in path:
        assert isinstance(cmd, click.Group)
        cmd = cmd.commands[part]
    assert isinstance(cmd, click.Group)
    return cmd


def _ids(pairs: list[tuple[tuple[str, ...], str, str]]) -> list[str]:
    return [f"{' '.join(p)} {old}->{new}" for p, old, new in pairs]


class TestEveryOldVerbResolves:
    @pytest.mark.parametrize(("path", "old", "new"), _VERB_PAIRS, ids=_ids(_VERB_PAIRS))
    def test_the_old_verb_is_the_new_command(self, path, old, new):
        group = _group(path)
        ctx = click.Context(group)
        assert group.get_command(ctx, old) is group.commands[new]

    @pytest.mark.parametrize(("path", "old", "new"), _VERB_PAIRS, ids=_ids(_VERB_PAIRS))
    def test_only_the_new_verb_is_advertised(self, path, old, new):
        result = CliRunner().invoke(main, [*path, "--help"])
        assert result.exit_code == 0, result.output
        assert f"  {new} " in result.output
        assert f"  {old} " not in result.output

    @pytest.mark.parametrize(("path", "old", "new"), _VERB_PAIRS, ids=_ids(_VERB_PAIRS))
    def test_the_old_verb_says_nothing(self, path, old, new):
        """Quiet: the stderr note is for the renamed top-level nouns only."""
        from hle_client import aliases

        aliases._warned.clear()
        result = CliRunner().invoke(main, [*path, old, "--help"])
        assert result.exit_code == 0, result.output
        assert "note:" not in result.stderr

    def test_the_renamed_nouns_still_say_what_to_type(self):
        from hle_client import aliases

        aliases._warned.clear()
        result = CliRunner().invoke(main, ["fp", "--help"])
        assert "`hle fp` is now `hle forward`" in result.stderr

    def test_agent_enroll_is_hidden_but_still_there(self):
        agent = _group(("agent",))
        assert agent.commands["enroll"].hidden
        result = CliRunner().invoke(main, ["agent", "--help"])
        assert "enroll" not in result.output


class TestTunnelSet:
    def _run(self, argv: list[str]):
        client = AsyncMock()
        client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        client.set_tunnel_auth_mode = AsyncMock(return_value={})
        with patch("hle_client.api.ApiClient", return_value=client):
            result = CliRunner().invoke(main, [*argv, "--api-key", _KEY])
        return result, client

    def test_auth_sets_the_gate_mode(self):
        result, client = self._run(["tunnel", "set", "ha", "--auth", "none"])
        assert result.exit_code == 0, result.output
        client.set_tunnel_auth_mode.assert_awaited_once_with("ha-x7k", "none")
        assert "auth_mode = none" in result.output

    def test_the_old_spelling_runs_the_same_command(self):
        result, client = self._run(["tunnel", "auth-mode", "ha", "--set", "sso"])
        assert result.exit_code == 0, result.output
        client.set_tunnel_auth_mode.assert_awaited_once_with("ha-x7k", "sso")

    def test_nothing_to_set_is_a_usage_error(self):
        result, client = self._run(["tunnel", "set", "ha"])
        assert result.exit_code == 2
        assert "Nothing to set" in result.output
        client.set_tunnel_auth_mode.assert_not_awaited()

    def test_contradicting_spellings_are_refused(self):
        result, client = self._run(["tunnel", "set", "ha", "--auth", "sso", "--set", "none"])
        assert result.exit_code == 2
        client.set_tunnel_auth_mode.assert_not_awaited()

    def test_an_unknown_mode_is_rejected(self):
        result, _ = self._run(["tunnel", "set", "ha", "--auth", "bogus"])
        assert result.exit_code == 2

    def test_set_is_hidden_as_set_the_option(self):
        """`--set` only exists so the old spelling parses; help teaches `--auth`."""
        result = CliRunner().invoke(main, ["tunnel", "set", "--help"])
        options = [line.strip() for line in result.output.splitlines()]
        assert any(line.startswith("--auth") for line in options)
        assert not any(line.startswith("--set") for line in options)


class TestShareCreateName:
    def _run(self, extra: list[str]):
        with patch("hle_client.config_cmd.ops_auth.create_share", new_callable=AsyncMock) as create:
            create.return_value.share_url = "https://x"
            create.return_value.label = "x"
            create.return_value.expires_at = "soon"
            create.return_value.max_uses = None
            result = CliRunner().invoke(
                main, ["tunnel", "share", "create", "ha", *extra, "--api-key", _KEY]
            )
        return result, create

    @pytest.mark.parametrize("flag", ["--name", "--label"])
    def test_either_spelling_names_the_link(self, flag):
        result, create = self._run([flag, "for-bob"])
        assert result.exit_code == 0, result.output
        assert create.call_args.kwargs["label"] == "for-bob"

    def test_no_name_is_an_empty_one(self):
        result, create = self._run([])
        assert result.exit_code == 0, result.output
        assert create.call_args.kwargs["label"] == ""

    def test_label_is_not_advertised(self):
        result = CliRunner().invoke(main, ["tunnel", "share", "create", "--help"])
        assert "--name" in result.output
        assert "--label" not in result.output


class TestAgentTokenLivesUnderAuthLogin:
    TOKEN = "hlea_" + "a" * 40

    def test_login_with_a_token_enrols(self, tmp_path: Path):
        cfg = tmp_path / "agent.toml"
        with patch("hle_client.config.AGENT_CONFIG_PATH", cfg):
            result = CliRunner().invoke(main, ["auth", "login", "--agent-token", self.TOKEN])
        assert result.exit_code == 0, result.output
        assert "Enrolled" in result.output
        assert self.TOKEN in cfg.read_text()

    def test_a_bare_flag_prompts_for_the_token(self, tmp_path: Path):
        """So it need not be typed into shell history — what `agent enroll` did."""
        cfg = tmp_path / "agent.toml"
        with patch("hle_client.config.AGENT_CONFIG_PATH", cfg):
            result = CliRunner().invoke(
                main, ["auth", "login", "--agent-token"], input=f"{self.TOKEN}\n"
            )
        assert result.exit_code == 0, result.output
        assert self.TOKEN in cfg.read_text()

    def test_a_non_agent_token_is_refused(self, tmp_path: Path):
        cfg = tmp_path / "agent.toml"
        with patch("hle_client.config.AGENT_CONFIG_PATH", cfg):
            result = CliRunner().invoke(main, ["auth", "login", "--agent-token", "hle_nope"])
        assert result.exit_code == 1
        assert not cfg.exists()

    def test_agent_enroll_is_the_same_path(self, tmp_path: Path):
        cfg = tmp_path / "agent.toml"
        with patch("hle_client.config.AGENT_CONFIG_PATH", cfg):
            result = CliRunner().invoke(main, ["agent", "enroll", self.TOKEN])
        assert result.exit_code == 0, result.output
        assert self.TOKEN in cfg.read_text()


class TestDaemonTargetsArePositional:
    """`hle daemon status agent`, not `hle daemon status --agent`."""

    def _status(self, argv: list[str]):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=False),
            patch("hle_client.ops.daemon.status", new_callable=AsyncMock) as status,
        ):
            result = CliRunner().invoke(main, ["daemon", "status", *argv])
        return result, status

    @pytest.mark.parametrize(
        ("argv", "label", "name"),
        [
            (["agent"], "agent", None),
            (["--agent"], "agent", None),
            (["ha"], "ha", None),
            (["--label", "ha"], "ha", None),
            (["ha", "--label", "ha"], "ha", None),
            (["ha", "--name", "custom.service"], "ha", "custom.service"),
            # What `hle daemon list` prints, pasted back.
            (["hle-ha.service"], "ha", "hle-ha.service"),
            (["world.hle.ha"], "ha", "world.hle.ha"),
            (["world.hle.ha.plist"], "ha", "world.hle.ha.plist"),
            # Not a unit name without its suffix: a label that starts with hle-.
            (["hle-ha"], "hle-ha", None),
        ],
    )
    def test_name_resolves_to_a_label(self, argv, label, name):
        result, status = self._status(argv)
        assert result.exit_code == 0, result.output
        assert status.call_args.args == (label,)
        assert status.call_args.kwargs["name"] == name

    @pytest.mark.parametrize("argv", [["ha", "--label", "tv"], ["ha", "--agent"]])
    def test_disagreeing_spellings_are_refused(self, argv):
        result, status = self._status(argv)
        assert result.exit_code == 2
        status.assert_not_awaited()

    def test_nothing_named_says_what_to_pass(self):
        result, _ = self._status([])
        assert result.exit_code == 2
        assert "NAME is required" in " ".join(result.output.split())

    @pytest.mark.parametrize("flag", ["--agent", "--label", "--name"])
    def test_the_flag_spellings_are_hidden(self, flag):
        result = CliRunner().invoke(main, ["daemon", "status", "--help"])
        assert "NAME" in result.output
        assert flag not in result.output

    def test_logs_takes_a_name(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=False),
            patch("hle_client.service_cmd.subprocess.run") as run,
        ):
            result = CliRunner().invoke(main, ["daemon", "logs", "ha", "-n", "5"])
        assert result.exit_code == 0, result.output
        assert "hle-ha.service" in run.call_args.args[0]

    @pytest.mark.parametrize(
        "argv",
        [["daemon", "delete", "ha"], ["daemon", "uninstall", "--label", "ha"]],
        ids=["delete NAME", "uninstall --label"],
    )
    def test_delete_and_its_old_spelling(self, argv):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=False),
            patch("hle_client.ops.daemon.uninstall", new_callable=AsyncMock) as uninstall,
        ):
            result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0, result.output
        assert uninstall.call_args.args == ("ha",)

    @pytest.mark.parametrize("verb", ["restart", "refresh"])
    def test_restart_and_refresh_take_a_name(self, verb):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch("hle_client.service_cmd.installed_scope", return_value=False),
            patch("hle_client.ops.daemon.restart", new_callable=AsyncMock) as restart,
            patch("hle_client.ops.daemon.refresh", new_callable=AsyncMock) as refresh,
        ):
            restart.return_value = True
            refresh.return_value = "refreshed"
            result = CliRunner().invoke(main, ["daemon", verb, "agent"])
        assert result.exit_code == 0, result.output
        called = restart if verb == "restart" else refresh
        assert called.call_args.args[0] == "hle-agent.service"
