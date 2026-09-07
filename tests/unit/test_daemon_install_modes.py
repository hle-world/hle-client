"""`daemon install` is three commands, and the old flag form still works.

It carried twenty flags across three mutually exclusive modes, with help text
that prefixed individual options with "fp mode:" — the code saying, in as many
words, that it wanted to be three commands. Each mode now shows only the
options that apply to it.

The flag form is in unit files, install scripts and every answer ever given
about this tool, so it keeps working unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from hle_client.aliases import ModeGroup
from hle_client.cli import main


@pytest.fixture
def install_group():
    return main.commands["daemon"].commands["install"]


class TestTheModesAreCommands:
    @pytest.mark.parametrize("mode", ["tunnel", "agent", "forward"])
    def test_each_mode_exists(self, mode, install_group):
        assert mode in install_group.commands

    def test_help_lists_them(self, install_group):
        result = CliRunner().invoke(main, ["daemon", "install", "--help"])
        assert result.exit_code == 0
        for mode in ("tunnel", "agent", "forward"):
            assert mode in result.output

    def test_a_mode_only_shows_its_own_options(self):
        """ "fp mode:" prefixes existed because one command carried three."""
        result = CliRunner().invoke(main, ["daemon", "install", "agent", "--help"])
        assert result.exit_code == 0
        # Nothing about tunnels or forwards belongs in the agent's help.
        assert "--service" not in result.output
        assert "--to" not in result.output
        assert "--apex" not in result.output

    def test_tunnel_takes_its_label_and_url_as_arguments(self, install_group):
        params = [p.name for p in install_group.commands["tunnel"].params]
        assert "label" in params
        assert "url" in params


class TestTheOldFlagFormStillWorks:
    def test_an_option_first_reaches_the_legacy_command(self, install_group):
        """`hle daemon install --agent` — the form in every existing unit file."""
        assert ModeGroup.LEGACY in install_group.commands

    def test_the_internal_name_is_never_shown(self):
        """It is a dispatch detail, not something to retype."""
        result = CliRunner().invoke(main, ["daemon", "install", "--agent", "--help"])
        assert ModeGroup.LEGACY not in result.output
        assert "daemon install [OPTIONS]" in result.output

    def test_help_belongs_to_the_group_not_the_legacy_command(self):
        """`--help` is where the three modes are listed."""
        result = CliRunner().invoke(main, ["daemon", "install", "--help"])
        assert "tunnel" in result.output
        assert "agent" in result.output

    def test_an_unknown_mode_is_reported_against_the_group(self):
        result = CliRunner().invoke(main, ["daemon", "install", "nonsense"])
        assert result.exit_code != 0
        assert "nonsense" in result.output


class TestModesForwardToTheSameImplementation:
    """The split is a front door; nothing about what gets installed changed."""

    def _invoke(self, args):
        with patch("hle_client.service_cmd.install.callback") as impl:
            CliRunner().invoke(main, ["daemon", "install", *args])
        return impl

    def test_tunnel_passes_the_url_and_label_through(self):
        impl = self._invoke(["tunnel", "ha", "http://localhost:8123"])
        assert impl.call_args.kwargs["service_url"] == "http://localhost:8123"
        assert impl.call_args.kwargs["label"] == "ha"

    def test_agent_sets_agent_mode(self):
        impl = self._invoke(["agent"])
        assert impl.call_args.kwargs["agent_mode"] is True

    def test_forward_sets_fp_mode_and_its_target(self):
        impl = self._invoke(["forward", "rpi", "22", "--port", "9922"])
        assert impl.call_args.kwargs["fp_mode"] is True
        assert impl.call_args.kwargs["agent_name"] == "rpi"
        assert impl.call_args.kwargs["fp_target"] == "22"
        assert impl.call_args.kwargs["fp_port"] == 9922


class TestLogs:
    """A service reports "active (running)" while doing nothing useful.

    An agent with no token exits and is restarted forever; the only way to see
    that was to know journalctl's flags, or where launchd was told to write.
    """

    def test_it_is_registered(self):
        assert "logs" in main.commands["daemon"].commands

    def test_it_runs_journalctl_for_the_unit(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=False),
            patch("hle_client.service_cmd.subprocess.run") as run,
        ):
            result = CliRunner().invoke(main, ["daemon", "logs", "--label", "ha", "-n", "10"])
        assert result.exit_code == 0, result.output
        cmd = run.call_args.args[0]
        assert cmd[:2] == ["journalctl", "-u"]
        assert "hle-ha.service" in cmd
        assert "10" in cmd

    def test_the_user_scope_asks_the_user_journal(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=True),
            patch("hle_client.service_cmd.subprocess.run") as run,
        ):
            CliRunner().invoke(main, ["daemon", "logs", "--label", "ha"])
        assert "--user" in run.call_args.args[0]

    def test_follow_is_passed_on(self):
        with (
            patch("hle_client.service_cmd._require_supported", return_value="linux"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=False),
            patch("hle_client.service_cmd.subprocess.run") as run,
        ):
            CliRunner().invoke(main, ["daemon", "logs", "--agent", "-f"])
        assert "-f" in run.call_args.args[0]

    def test_a_missing_file_says_where_it_looked(self, tmp_path):
        """Rather than an empty screen that reads like "no problems"."""
        with (
            patch("hle_client.service_cmd._require_supported", return_value="darwin"),
            patch("hle_client.service_cmd.resolve_user_mode", return_value=True),
            patch("hle_client.service_cmd._launchd_log_dir", return_value=str(tmp_path)),
        ):
            result = CliRunner().invoke(main, ["daemon", "logs", "--label", "ha"])
        assert result.exit_code == 1
        # rich hard-wraps a long path mid-token, so compare with all whitespace gone.
        assert str(tmp_path) in "".join(result.output.split())


def test_click_group_shape_is_what_the_root_expects(install_group):
    """A regression guard: install must stay a group for the modes to exist."""
    assert isinstance(install_group, click.Group)
