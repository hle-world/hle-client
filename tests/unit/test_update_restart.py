"""`hle update` offers to restart what it just made stale.

An upgrade replaces the code on disk, not the process already running it. The
service keeps serving the previous release while `hle --version` reports the new
one, so nothing looks wrong. Printing "restart any running tunnels" left that
gap open until somebody acted on it.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from click.testing import CliRunner

from hle_client.cli import main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """Rich output as readable text.

    Rich highlights numbers, so it injects colour codes *inside* a phrase —
    ``Updated to \x1b[1;36m9999.1\x1b[0m.`` — and wraps at the console width.
    Asserting on the raw string fails for reasons that have nothing to do with
    what the user sees.
    """
    return " ".join(_ANSI.sub("", output).split())


class TestUpdateRestartsServices:
    def _run(self, services, confirm_reply, restart_ok=True, extra_args=()):
        runner = CliRunner()
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value="9999.1"),
            patch("hle_client.update_cmd.detect_install_method", return_value="venv"),
            patch("hle_client.update_cmd.subprocess.run") as mock_run,
            patch("hle_client.update_cmd._installed_version", return_value="9999.1"),
            patch("hle_client.service_cmd.installed_services", return_value=services),
            patch("hle_client.service_cmd.restart_service", return_value=restart_ok) as restart,
        ):
            mock_run.return_value.returncode = 0
            result = runner.invoke(main, ["update", *extra_args], input=confirm_reply)
        return result, restart

    def test_offers_to_restart_and_does_it(self):
        result, restart = self._run(["hle_agent"], confirm_reply="y\ny\n")
        assert result.exit_code == 0, result.output
        assert "Still running the previous version" in result.output
        restart.assert_called_once_with("hle_agent")
        assert "restarted" in result.output

    def test_declining_says_how_to_do_it_later(self):
        """Not restarting is a valid choice, but it must not be a silent one."""
        result, restart = self._run(["hle_agent"], confirm_reply="y\nn\n")
        assert result.exit_code == 0, result.output
        assert "hle service restart --all" in result.output
        restart.assert_not_called()

    def test_yes_flag_restarts_without_asking(self):
        result, restart = self._run([], confirm_reply="", extra_args=("--yes",))
        assert result.exit_code == 0, result.output
        restart.assert_not_called()  # nothing installed to restart

    def test_restarts_every_installed_service(self):
        result, restart = self._run(["hle_agent", "hle_jellyfin"], confirm_reply="y\ny\n")
        assert result.exit_code == 0, result.output
        assert restart.call_count == 2

    def test_a_failed_restart_is_loud(self):
        """Otherwise the old version keeps serving and the upgrade looks done."""
        result, _ = self._run(["hle_agent"], confirm_reply="y\ny\n", restart_ok=False)
        assert result.exit_code == 1
        assert "failed" in result.output
        assert "still on the old" in result.output

    def test_no_services_explains_the_manual_case(self):
        result, restart = self._run([], confirm_reply="y\n")
        assert result.exit_code == 0, result.output
        assert "No hle services installed" in result.output
        restart.assert_not_called()


class TestUpgradeIsVerified:
    """Exit code 0 is not the same as "the new version is installed".

    Reported from a live box: `pipx upgrade` answered "hle-client is already at
    latest version 2608.3" and exited 0 while PyPI served 2608.4 — the release
    had finished publishing eleven seconds earlier and the index pipx resolved
    through was still behind. `hle update` printed a green "Updated to 2608.3."
    directly under "Latest on PyPI: 2608.4", then offered to restart services
    onto code it had not replaced.
    """

    def _run(self, installed_after, method="pipx", extra_args=(), returncodes=(0, 0)):
        runner = CliRunner()
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value="9999.1"),
            patch("hle_client.update_cmd.detect_install_method", return_value=method),
            patch("hle_client.update_cmd.subprocess.run") as mock_run,
            patch("hle_client.update_cmd._installed_version", side_effect=list(installed_after)),
            patch("hle_client.service_cmd.installed_services", return_value=[]),
            patch("hle_client.service_cmd.restart_service", return_value=True),
        ):
            mock_run.side_effect = [type("R", (), {"returncode": rc})() for rc in returncodes]
            result = runner.invoke(main, ["update", *extra_args], input="y\ny\n")
        return result, mock_run

    def test_a_no_op_upgrade_is_retried_with_the_version_pinned(self):
        """The pinned form names the version instead of asking if one is due."""
        result, mock_run = self._run(installed_after=["2608.3", "9999.1"])
        assert result.exit_code == 0, result.output
        assert "is not installed (still 2608.3)" in plain(result.output)
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].args[0] == [
            "pipx",
            "install",
            "--force",
            "hle-client==9999.1",
        ]
        assert "Updated to 9999.1" in plain(result.output)

    def test_a_no_op_that_stays_a_no_op_fails_loudly(self):
        result, _ = self._run(installed_after=["2608.3", "2608.3"])
        assert result.exit_code == 1
        out = plain(result.output)
        assert "Still on 2608.3, not 9999.1" in out
        # Says what to do about it, not just that it happened.
        assert "pipx install --force hle-client==9999.1" in out

    def test_no_pointless_retry_when_the_command_was_already_pinned(self):
        """`--version X` already builds the deterministic command."""
        result, mock_run = self._run(installed_after=["2608.3"], extra_args=("--version", "9999.1"))
        assert result.exit_code == 1
        assert mock_run.call_count == 1
        assert "Still on 2608.3" in plain(result.output)

    def test_an_unreadable_version_is_not_reported_as_success(self):
        """No evidence of failure, so don't invent one — but don't claim success."""
        result, _ = self._run(installed_after=[None])
        assert result.exit_code == 0, result.output
        out = plain(result.output)
        assert "could not be read" in out
        assert "Updated to unknown" not in out

    def test_a_successful_upgrade_still_says_so_once(self):
        result, mock_run = self._run(installed_after=["9999.1"])
        assert result.exit_code == 0, result.output
        assert mock_run.call_count == 1
        out = plain(result.output)
        assert "Updated to 9999.1" in out
        assert "is not installed" not in out
