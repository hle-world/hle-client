"""`hle update` offers to restart what it just made stale.

An upgrade replaces the code on disk, not the process already running it. The
service keeps serving the previous release while `hle --version` reports the new
one, so nothing looks wrong. Printing "restart any running tunnels" left that
gap open until somebody acted on it.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from hle_client.cli import main


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
