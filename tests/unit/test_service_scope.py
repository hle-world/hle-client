"""A service is acted on in the scope it lives in, and both scopes are listed.

`hle daemon refresh --label X` assumed the system scope, so a per-user unit
was looked for in /etc and "restarted only", or rebuilt as a second copy in
the wrong place. `hle daemon list` on macOS asked `launchctl list`, which only
answers for the calling session's domain, and called one scope the whole
picture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

from hle_client import service_cmd
from hle_client.cli import main

if TYPE_CHECKING:
    from pathlib import Path


class TestInstalledScope:
    def _files(self, tmp_path: Path, *, user: bool, system: bool):
        user_dir = tmp_path / "user"
        sys_dir = tmp_path / "system"
        user_dir.mkdir(exist_ok=True)
        sys_dir.mkdir(exist_ok=True)
        if user:
            (user_dir / "hle-agent.service").write_text("[Unit]\n")
        if system:
            (sys_dir / "hle-agent.service").write_text("[Unit]\n")

        def unit_dir(user_mode: bool) -> Path:
            return user_dir if user_mode else sys_dir

        return patch.object(service_cmd, "_unit_dir", unit_dir)

    def test_per_user_unit_is_found_in_the_user_scope(self, tmp_path):
        with (
            patch.object(service_cmd, "current_platform", return_value="linux"),
            self._files(tmp_path, user=True, system=False),
        ):
            assert service_cmd.installed_scope("hle-agent.service") is True

    def test_system_unit_is_found_in_the_system_scope(self, tmp_path):
        with (
            patch.object(service_cmd, "current_platform", return_value="linux"),
            self._files(tmp_path, user=False, system=True),
        ):
            assert service_cmd.installed_scope("hle-agent.service") is False

    def test_missing_everywhere_is_none(self, tmp_path):
        with (
            patch.object(service_cmd, "current_platform", return_value="linux"),
            self._files(tmp_path, user=False, system=False),
        ):
            assert service_cmd.installed_scope("hle-agent.service") is None

    def test_both_prefers_the_scope_the_caller_can_act_on(self, tmp_path):
        with (
            patch.object(service_cmd, "current_platform", return_value="linux"),
            self._files(tmp_path, user=True, system=True),
            patch.object(service_cmd.os, "geteuid", return_value=1000),
        ):
            assert service_cmd.installed_scope("hle-agent.service") is True
        with (
            patch.object(service_cmd, "current_platform", return_value="linux"),
            self._files(tmp_path, user=True, system=True),
            patch.object(service_cmd.os, "geteuid", return_value=0),
        ):
            assert service_cmd.installed_scope("hle-agent.service") is False


class TestRefreshRespectsScope:
    def _refresh(self, scope: bool | None):
        with (
            patch.object(service_cmd, "_require_supported", return_value="linux"),
            patch.object(service_cmd, "current_platform", return_value="linux"),
            patch.object(service_cmd, "installed_scope", return_value=scope),
            patch.object(service_cmd, "refresh_service", return_value="refreshed") as refresh,
        ):
            result = CliRunner().invoke(main, ["daemon", "refresh", "--agent"])
        return result, refresh

    def test_a_per_user_unit_is_rebuilt_in_the_user_scope(self):
        result, refresh = self._refresh(True)
        assert result.exit_code == 0, result.output
        refresh.assert_called_once_with("hle-agent.service", True)

    def test_a_system_unit_is_rebuilt_in_the_system_scope(self):
        result, refresh = self._refresh(False)
        assert result.exit_code == 0, result.output
        refresh.assert_called_once_with("hle-agent.service", False)

    def test_an_uninstalled_service_is_not_invented(self):
        """Rebuilding 'in the system scope' for a unit that is nowhere wrote a
        new file. Now it says so and points at the list."""
        result, refresh = self._refresh(None)
        assert result.exit_code == 1
        assert "not installed" in result.output
        refresh.assert_not_called()


class TestLaunchdListsBothScopes:
    def _dirs(self, tmp_path: Path):
        agents = tmp_path / "LaunchAgents"
        daemons = tmp_path / "LaunchDaemons"
        agents.mkdir()
        daemons.mkdir()
        (agents / "world.hle.tunnel.ha.plist").write_text("<plist/>")
        (daemons / "world.hle.agent.plist").write_text("<plist/>")
        (daemons / "com.other.thing.plist").write_text("<plist/>")
        return patch.object(
            service_cmd, "_launchd_plist_dirs", return_value=[(agents, True), (daemons, False)]
        )

    def test_installed_covers_both_scopes(self, tmp_path):
        with self._dirs(tmp_path):
            assert service_cmd._launchd_installed() == [
                ("world.hle.tunnel.ha", True),
                ("world.hle.agent", False),
            ]
            assert service_cmd._launchd_installed(True) == [("world.hle.tunnel.ha", True)]
            assert service_cmd._launchd_installed(False) == [("world.hle.agent", False)]

    def test_installed_services_reports_the_real_scope_on_darwin(self, tmp_path):
        """`hle update` looks the spec up by scope; 'always system' sent it to /Library."""
        with (
            self._dirs(tmp_path),
            patch.object(service_cmd, "current_platform", return_value="darwin"),
        ):
            assert service_cmd.installed_services() == [
                ("world.hle.agent", False),
                ("world.hle.tunnel.ha", True),
            ]

    def test_daemon_list_shows_both_scopes(self, tmp_path):
        with (
            self._dirs(tmp_path),
            patch.object(service_cmd, "_require_supported", return_value="darwin"),
            patch.object(service_cmd, "_launchctl_loaded_labels", return_value={"world.hle.agent"}),
        ):
            result = CliRunner().invoke(main, ["daemon", "list"])
        assert result.exit_code == 0, result.output
        assert "world.hle.agent" in result.output
        assert "world.hle.tunnel.ha" in result.output
        assert "per-user" in result.output
        assert "system" in result.output
        assert "com.other.thing" not in result.output

    def test_daemon_list_user_only_filters(self, tmp_path):
        with (
            self._dirs(tmp_path),
            patch.object(service_cmd, "_require_supported", return_value="darwin"),
            patch.object(service_cmd, "_launchctl_loaded_labels", return_value=set()),
        ):
            result = CliRunner().invoke(main, ["daemon", "list", "--user"])
        assert result.exit_code == 0, result.output
        assert "world.hle.tunnel.ha" in result.output
        assert "world.hle.agent" not in result.output
