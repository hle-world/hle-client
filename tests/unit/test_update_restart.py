"""`hle update` offers to restart what it just made stale.

An upgrade replaces the code on disk, not the process already running it. The
service keeps serving the previous release while `hle --version` reports the new
one, so nothing looks wrong. Printing "restart any running tunnels" left that
gap open until somebody acted on it.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from hle_client import update_cmd
from hle_client.cli import main
from hle_client.errors import HleError

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
        result, restart = self._run([("hle_agent", False)], confirm_reply="y\ny\n")
        assert result.exit_code == 0, result.output
        assert "Still running the previous version" in result.output
        # The scope goes with the name: restarting a system unit in the user
        # scope is how the wrong copy got restarted and reported as success.
        restart.assert_called_once_with("hle_agent", False)
        assert "restarted" in result.output

    def test_declining_says_how_to_do_it_later(self):
        """Not restarting is a valid choice, but it must not be a silent one."""
        result, restart = self._run([("hle_agent", False)], confirm_reply="y\nn\n")
        assert result.exit_code == 0, result.output
        assert "hle daemon restart --all" in result.output
        restart.assert_not_called()

    def test_yes_flag_restarts_without_asking(self):
        result, restart = self._run([], confirm_reply="", extra_args=("--yes",))
        assert result.exit_code == 0, result.output
        restart.assert_not_called()  # nothing installed to restart

    def test_restarts_every_installed_service(self):
        result, restart = self._run(
            [("hle_agent", False), ("hle_jellyfin", True)], confirm_reply="y\ny\n"
        )
        assert result.exit_code == 0, result.output
        assert restart.call_count == 2

    def test_both_scopes_are_named_so_a_duplicate_is_visible(self):
        """A host can carry the same unit twice; "hle-agent restarted" hides which.

        Observed in the field: a system unit and a per-user unit of the same
        name, the system one failing on permissions and the user one — the
        duplicate — restarting and being reported as the success.
        """
        result, _ = self._run(
            [("hle-agent.service", False), ("hle-agent.service", True)], confirm_reply="y\ny\n"
        )
        out = plain(result.output)
        assert "hle-agent.service (system)" in out
        assert "hle-agent.service (user)" in out

    def test_a_failed_restart_is_loud(self):
        """Otherwise the old version keeps serving and the upgrade looks done."""
        result, _ = self._run([("hle_agent", False)], confirm_reply="y\ny\n", restart_ok=False)
        assert result.exit_code == 1
        assert "failed" in result.output
        assert "still on the old" in result.output

    def test_a_denied_system_restart_names_a_command_that_works(self):
        """`sudo hle ...` is the obvious next try and it fails.

        hle lives in ~/.local/bin, which sudo's secure_path drops, so it
        answers "sudo: hle: command not found" — leaving the operator with a
        failure and no way forward.
        """
        with patch("hle_client.update_cmd.os.geteuid", return_value=1000):
            result, _ = self._run(
                [("hle-agent.service", False)], confirm_reply="y\ny\n", restart_ok=False
            )
        out = plain(result.output)
        assert "sudo systemctl restart hle-agent.service" in out

    def test_a_user_scope_failure_does_not_suggest_sudo(self):
        """Root is not the answer for a per-user unit, and saying so misleads."""
        with patch("hle_client.update_cmd.os.geteuid", return_value=1000):
            result, _ = self._run(
                [("hle-agent.service", True)], confirm_reply="y\ny\n", restart_ok=False
            )
        assert "sudo systemctl" not in plain(result.output)

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


class TestRefreshRunsTheNewClient:
    """The rebuild must be done by the new binary, not the process upgrading.

    The old code stamps its own ``__version__`` and writes the old service
    format, so a service-format fix in release N never reaches someone updating
    to N. With a side-by-side layout ``current/bin/hle`` is the new code and can
    do the rebuild correctly.
    """

    def _layout(self, tmp_path):
        exe = tmp_path / "hle" / "current" / "bin" / "hle"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!fake\n")
        return tmp_path / "hle", exe

    def _restart(self, home, services, *, run=None):
        ctx = click.Context(click.Command("update"))
        with (
            patch("hle_client.update_cmd.subprocess.run") as mock_run,
            patch("hle_client.service_cmd.installed_services", return_value=list(services)),
            patch(
                "hle_client.service_cmd.service_spec",
                return_value={"run_args": ["tunnel", "create"]},
            ),
            patch("hle_client.service_cmd.refresh_service") as refresh,
        ):
            if run is None:
                mock_run.return_value.returncode = 0
            else:
                mock_run.side_effect = run
            update_cmd._restart_services(ctx, yes=True, home=home, new_version="9999.1")
        return mock_run, refresh

    def test_each_service_is_refreshed_by_the_new_binary(self, tmp_path):
        home, exe = self._layout(tmp_path)
        services = [("hle-agent.service", False), ("hle_ha", True)]
        mock_run, refresh = self._restart(home, services)
        assert [c.args[0] for c in mock_run.call_args_list] == [
            [str(exe), "daemon", "refresh", "hle-agent.service"],
            [str(exe), "daemon", "refresh", "hle_ha"],
        ]
        # The old process never rebuilds anything on this path.
        refresh.assert_not_called()

    def test_exit_code_zero_is_reported_as_rebuilt(self, tmp_path, capsys):
        home, _ = self._layout(tmp_path)
        self._restart(home, [("hle-agent.service", False)])
        out = plain(capsys.readouterr().out)
        assert "rebuilt and started" in out

    def test_a_nonzero_exit_is_reported_as_failed(self, tmp_path, capsys):
        home, _ = self._layout(tmp_path)
        run = [SimpleNamespace(returncode=1)]
        with pytest.raises(HleError):
            self._restart(home, [("hle-agent.service", False)], run=run)
        out = plain(capsys.readouterr().out)
        assert "failed" in out
        assert "still on the old" in out

    def test_without_a_layout_it_refreshes_in_process(self, tmp_path):
        home, exe = self._layout(tmp_path)
        exe.unlink()  # no current/bin/hle: no layout to run
        mock_run, refresh = self._restart(home, [("hle-agent.service", False)])
        mock_run.assert_not_called()
        refresh.assert_called_once_with("hle-agent.service", False)

    def test_a_new_binary_that_will_not_start_falls_back(self, tmp_path):
        home, _ = self._layout(tmp_path)
        mock_run, refresh = self._restart(
            home, [("hle-agent.service", False)], run=[FileNotFoundError()]
        )
        mock_run.assert_called_once()
        refresh.assert_called_once_with("hle-agent.service", False)

