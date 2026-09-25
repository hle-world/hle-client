"""The versioned layout seen from outside the agent: service units and `hle update`.

A swap of `current` only changes what runs if the service manager starts
`current/bin/hle`, and only brings the agent back if the unit restarts it on
any exit. `hle update` uses the same stage/verify/swap as the dashboard path
when the layout exists, so the running venv is never rewritten underneath a
live process.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hle_client import __version__, service_cmd
from hle_client.agent_update import (
    CURRENT_LINK,
    LOCAL_REQUEST_ID,
    PREVIOUS_FILE,
    VERSIONS_DIR,
    current_version,
    layout_home,
    read_update_state,
    versioned_exec_path,
)
from hle_client.cli import main

OLD = __version__
NEW = "9999.1"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    return " ".join(_ANSI.sub("", output).split())


def make_layout(tmp_path: Path, versions: tuple[str, ...] = (OLD,), current: str = OLD) -> Path:
    home = tmp_path / "hle"
    for v in versions:
        (home / VERSIONS_DIR / v / "bin").mkdir(parents=True)
        (home / VERSIONS_DIR / v / "bin" / "hle").write_text(f"#!fake {v}\n")
    os.symlink(f"{VERSIONS_DIR}/{current}", home / CURRENT_LINK)
    return home


# --------------------------------------------------------------------------- #
# ExecStart resolution
# --------------------------------------------------------------------------- #
class TestExecPath:
    def test_running_from_a_version_dir_names_current(self, tmp_path):
        home = make_layout(tmp_path)
        prefix = str(home / VERSIONS_DIR / OLD)
        assert versioned_exec_path(prefix, home) == home / CURRENT_LINK / "bin" / "hle"

    def test_running_via_the_current_link_names_current(self, tmp_path):
        home = make_layout(tmp_path)
        assert versioned_exec_path(str(home / CURRENT_LINK), home) is not None

    def test_the_layout_is_found_next_to_the_venv_when_home_is_elsewhere(self, tmp_path):
        """`sudo hle daemon install`: HOME is root's, the layout is the user's."""
        home = make_layout(tmp_path)
        elsewhere = tmp_path / "root-home" / ".local" / "share" / "hle"
        assert layout_home(str(home / VERSIONS_DIR / OLD), elsewhere) == home

    def test_a_pipx_client_next_to_a_stale_layout_keeps_its_own_path(self, tmp_path):
        home = make_layout(tmp_path)
        pipx = tmp_path / "pipx" / "venvs" / "hle-client"
        pipx.mkdir(parents=True)
        assert versioned_exec_path(str(pipx), home) is None

    def test_no_layout_no_path(self, tmp_path):
        (tmp_path / "hle" / "venv").mkdir(parents=True)
        assert versioned_exec_path(str(tmp_path / "hle" / "venv"), tmp_path / "hle") is None

    def test_find_hle_path_prefers_current(self, tmp_path, monkeypatch):
        target = tmp_path / "hle" / CURRENT_LINK / "bin" / "hle"
        monkeypatch.setattr("hle_client.agent_update.versioned_exec_path", lambda: target)
        assert service_cmd.find_hle_path() == str(target)

    def test_find_hle_path_falls_back_without_layout(self, monkeypatch):
        monkeypatch.setattr("hle_client.agent_update.versioned_exec_path", lambda: None)
        monkeypatch.setattr(service_cmd.shutil, "which", lambda _: "/usr/local/bin/hle")
        assert service_cmd.find_hle_path() == "/usr/local/bin/hle"


# --------------------------------------------------------------------------- #
# Unit templates
# --------------------------------------------------------------------------- #
class TestAgentUnits:
    CURRENT = "/home/u/.local/share/hle/current/bin/hle"

    def _spec(self, restart: str) -> dict:
        return {
            "version": "2608.1",
            "label": service_cmd.AGENT_LABEL,
            "run_args": ["agent", "run"],
            "name": None,
            "run_as": "u",
            "description": "HLE agent (dashboard-managed tunnels)",
            "restart": restart,
            "agent_config": "/home/u/.config/hle/agent.toml",
            "user_mode": False,
        }

    def _systemd(self, tmp_path, monkeypatch, spec: dict) -> str:
        monkeypatch.setattr(service_cmd, "_SYSTEM_UNIT_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: self.CURRENT)
        monkeypatch.setattr(service_cmd, "_systemctl", lambda *a, **k: None)
        service_cmd._install_from_spec(spec, plat="linux", start=False)
        return (tmp_path / service_cmd.unit_name(service_cmd.AGENT_LABEL, None)).read_text()

    def test_refresh_upgrades_an_old_on_failure_agent_unit_to_always(self, tmp_path, monkeypatch):
        """Exit 1 after a swap comes back either way; a clean exit only with always."""
        unit = self._systemd(tmp_path, monkeypatch, self._spec("on-failure"))
        assert "Restart=always" in unit
        assert f"ExecStart={self.CURRENT} agent run" in unit
        # In [Unit], where systemd reads it: a swap + rollback + retry burst
        # must not trip the start limit and leave the agent dead.
        unit_section = unit.split("[Service]")[0]
        assert "StartLimitIntervalSec=0" in unit_section
        stamped = service_cmd.parse_service_spec(unit)
        assert stamped is not None and stamped["restart"] == "always"

    def test_tunnel_units_keep_their_policy(self, tmp_path, monkeypatch):
        spec = {**self._spec("on-failure"), "label": "ha", "run_args": ["tunnel", "create"]}
        monkeypatch.setattr(service_cmd, "_SYSTEM_UNIT_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: self.CURRENT)
        monkeypatch.setattr(service_cmd, "_systemctl", lambda *a, **k: None)
        service_cmd._install_from_spec(spec, plat="linux", start=False)
        unit = (tmp_path / service_cmd.unit_name("ha", None)).read_text()
        assert "Restart=on-failure" in unit
        assert "StartLimitIntervalSec" not in unit

    def test_launchd_keeps_alive_from_current(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_launchd_dir", lambda user_mode: tmp_path)
        monkeypatch.setattr(service_cmd, "_launchd_log_dir", lambda user_mode: str(tmp_path))
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: self.CURRENT)
        service_cmd._install_from_spec(self._spec("on-failure"), plat="darwin", start=False)
        (plist,) = tmp_path.glob("*.plist")
        text = plist.read_text()
        assert f"<string>{self.CURRENT}</string>" in text
        assert "<key>KeepAlive</key>\n    <true/>" in text

    def test_rcd_relaunches_from_current(self, tmp_path, monkeypatch):
        """daemon(8) -r restarts the child on any exit, so rc.d needs only the path."""
        monkeypatch.setattr(service_cmd, "_rcd_path", lambda svc: tmp_path / svc)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: None)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: self.CURRENT)
        service_cmd._install_from_spec(self._spec("on-failure"), plat="freebsd", start=False)
        script = (tmp_path / service_cmd.rc_service_name(service_cmd.AGENT_LABEL)).read_text()
        assert f"hle_command='{self.CURRENT}'" in script
        assert "-f -r" in script


# --------------------------------------------------------------------------- #
# `hle update` on the versioned layout
# --------------------------------------------------------------------------- #
class FakePip:
    """subprocess.run for the updater: venv, pip install, --version, import check."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        out = ""
        if argv[1:3] == ["-m", "venv"]:
            (Path(argv[3]) / "bin").mkdir(parents=True, exist_ok=True)
        elif argv[1:4] == ["-m", "pip", "install"]:
            version = argv[-1].split("==", 1)[1]
            (Path(argv[0]).parent / "hle").write_text(f"#!fake {version}\n")
        elif argv[-1] == "--version":
            out = f"hle, version {Path(argv[0]).read_text().split()[-1]}\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


class TestHleUpdateVersioned:
    def _run(self, home: Path, *, pip: FakePip, services=(), agent=False, args=("--yes",)):
        runner = CliRunner()
        spec = {"run_args": ["agent", "run"]} if agent else {"run_args": ["tunnel", "create"]}
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value=NEW),
            patch("hle_client.update_cmd.detect_install_method", return_value="venv"),
            patch("hle_client.agent_update.layout_home", return_value=home),
            patch("hle_client.agent_update._run", pip),
            patch("hle_client.update_cmd.subprocess.run") as in_place,
            patch("hle_client.service_cmd.installed_services", return_value=list(services)),
            patch("hle_client.service_cmd.service_spec", return_value=spec),
            patch("hle_client.service_cmd.refresh_service", return_value="refreshed") as refresh,
        ):
            result = runner.invoke(main, ["update", *args])
        return result, in_place, refresh

    def test_stages_swaps_and_refreshes_services(self, tmp_path):
        home = make_layout(tmp_path)
        pip = FakePip()
        result, in_place, refresh = self._run(home, pip=pip, services=[("hle-ha.service", False)])
        assert result.exit_code == 0, result.output
        in_place.assert_not_called()  # the running venv is never pip-upgraded
        assert current_version(home) == NEW
        assert (home / PREVIOUS_FILE).read_text().strip() == OLD
        assert [c for c in pip.calls if c[1:4] == ["-m", "pip", "install"]][0][-1] == (
            f"hle-client=={NEW}"
        )
        refresh.assert_called_once_with("hle-ha.service", False)
        assert f"Updated to {NEW}" in plain(result.output)
        # No agent among the services: no watchdog to arm.
        assert read_update_state(home) is None

    def test_an_agent_service_restarts_under_the_watchdog(self, tmp_path):
        home = make_layout(tmp_path)
        result, _, _ = self._run(
            home, pip=FakePip(), services=[("hle-agent.service", False)], agent=True
        )
        assert result.exit_code == 0, result.output
        state = read_update_state(home)
        assert state is not None
        assert (state.request_id, state.from_version, state.to_version) == (
            LOCAL_REQUEST_ID,
            OLD,
            NEW,
        )

    def test_a_failed_stage_leaves_current_alone(self, tmp_path):
        home = make_layout(tmp_path)
        result, _, refresh = self._run(home, pip=FakePip(fail_on="pip install"))
        assert result.exit_code != 0
        assert "still current" in plain(result.output)
        assert current_version(home) == OLD
        assert not (home / VERSIONS_DIR / NEW).exists()
        refresh.assert_not_called()

    def test_without_the_layout_the_in_place_path_runs(self, tmp_path):
        runner = CliRunner()
        with (
            patch("hle_client.update_cmd.pypi_latest_version", return_value=NEW),
            patch("hle_client.update_cmd.detect_install_method", return_value="venv"),
            patch("hle_client.agent_update.layout_home", return_value=None),
            patch("hle_client.update_cmd.subprocess.run") as in_place,
            patch("hle_client.update_cmd._installed_version", return_value=NEW),
            patch("hle_client.service_cmd.installed_services", return_value=[]),
        ):
            in_place.return_value.returncode = 0
            result = runner.invoke(main, ["update", "--yes"])
        assert result.exit_code == 0, result.output
        in_place.assert_called_once()

    @pytest.mark.parametrize("method", ["pipx", "uv"])
    def test_tool_installs_never_take_the_versioned_path(self, method, tmp_path):
        from hle_client.update_cmd import _versioned_home

        assert _versioned_home(method) is None
