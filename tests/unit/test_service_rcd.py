"""Tests for the FreeBSD rc.d service backend (pfSense / OPNsense).

The rc.d script is generated, never hand-written, so the parts that must be
exactly right — the rcvar name, quoting of arguments, daemon(8) flags — are
covered here rather than discovered on a firewall.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from hle_client import service_cmd
from hle_client.cli import main
from hle_client.service_cmd import (
    build_agent_args,
    current_platform,
    rc_service_name,
    render_rc_script,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class TestCurrentPlatform:
    def test_freebsd_detected(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd14")
        assert current_platform() == "freebsd"

    def test_linux_still_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        assert current_platform() == "linux"


class TestRcServiceName:
    def test_default_name_from_label(self):
        assert rc_service_name("agent") == "hle_agent"

    def test_hyphens_become_underscores(self):
        """rc.d derives `<name>_enable` as a shell variable — hyphens break it."""
        assert rc_service_name("fp-rpi-22") == "hle_fp_rpi_22"

    def test_explicit_name_is_sanitised_too(self):
        assert rc_service_name("agent", "my-svc") == "my_svc"

    def test_result_is_a_valid_sh_identifier(self):
        name = rc_service_name("weird.label/with:chars")
        assert all(c.isalnum() or c == "_" for c in name)


class TestRenderRcScript:
    def _script(self, **kw):
        params = {
            "label": "agent",
            "hle_path": "/usr/local/bin/hle",
            "run_args": build_agent_args(),
            **kw,
        }
        return render_rc_script(**params)

    def test_has_shebang_and_rcorder_tags(self):
        script = self._script()
        assert script.startswith("#!/bin/sh")
        assert "# PROVIDE: hle_agent" in script
        assert "# REQUIRE: NETWORKING DAEMON" in script
        assert "# KEYWORD: shutdown" in script

    def test_sources_rc_subr_and_runs_rc_command(self):
        script = self._script()
        assert ". /etc/rc.subr" in script
        assert 'run_rc_command "$1"' in script

    def test_rcvar_matches_service_name(self):
        script = self._script()
        assert 'name="hle_agent"' in script
        assert 'rcvar="hle_agent_enable"' in script
        assert ': ${hle_agent_enable:="NO"}' in script

    def test_disabled_by_default(self):
        """sysrc enables it explicitly; a stray script must not autostart."""
        assert ': ${hle_agent_enable:="NO"}' in self._script()

    def test_uses_daemon_because_hle_runs_in_foreground(self):
        script = self._script()
        assert 'command="/usr/sbin/daemon"' in script
        assert "-f" in script

    def test_restart_flag_adds_supervision(self):
        assert " -r " in self._script(restart=True)

    def test_restart_can_be_disabled(self):
        script = self._script(restart=False)
        assert " -r " not in script

    def test_run_args_are_quoted(self):
        script = self._script(run_args=["agent", "run", "--relay-host", "hle.world"])
        assert "'agent' 'run' '--relay-host' 'hle.world'" in script

    def test_embedded_quote_cannot_break_out(self):
        script = self._script(run_args=["--label", "it's"])
        assert "'it'\\''s'" in script

    def test_hle_path_is_quoted(self):
        script = self._script(hle_path="/opt/my hle/bin/hle")
        assert "'/opt/my hle/bin/hle'" in script

    def test_runs_as_root_by_default(self):
        assert ': ${hle_agent_user:="root"}' in self._script()

    def test_explicit_user_is_honoured(self):
        assert ': ${hle_agent_user:="hle"}' in self._script(run_as_user="hle")

    def test_home_is_set_explicitly(self):
        """rc.d starts services with almost no environment and daemon(8) adds
        nothing, so without HOME the agent looks for its token in the wrong
        place and fails with "No agent token" on every restart."""
        script = self._script(home="/root")
        assert ": ${hle_agent_home:='/root'}" in script
        assert "/usr/bin/env HOME=${hle_agent_home}" in script

    def test_env_precedes_the_hle_command(self):
        """env(1) has to wrap the command, not follow it."""
        script = self._script(home="/root")
        assert script.index("/usr/bin/env HOME=") < script.index("${hle_command}")

    def test_token_file_is_passed_by_absolute_path(self):
        """HOME alone was not enough.

        Enrollment and the service can run with different environments — on
        pfSense the agent enrolls as ``admin`` and runs from rc.d — and then
        the token is written to one path and read from another. The agent
        restarts forever reporting "No agent token" while the file sits on
        disk. The path is resolved at install time and passed explicitly.
        """
        script = self._script(agent_config="/root/.config/hle/agent.toml")
        assert ": ${hle_agent_config:='/root/.config/hle/agent.toml'}" in script
        assert "HLE_AGENT_CONFIG=${hle_agent_config}" in script

    def test_token_path_is_quoted(self):
        script = self._script(agent_config="/home/user with space/agent.toml")
        assert ": ${hle_agent_config:='/home/user with space/agent.toml'}" in script

    def test_no_token_path_leaves_the_default_in_charge(self):
        """An empty value must not override the agent's own default."""
        script = self._script()
        assert ": ${hle_agent_config:=''}" in script

    def test_home_defaults_to_the_installing_users_home(self):
        script = self._script()
        assert f": ${{hle_agent_home:='{Path.home()}'}}" in script

    def test_home_is_quoted(self):
        script = self._script(home="/home/user with space")
        assert "'/home/user with space'" in script

    def test_no_token_is_baked_into_the_script(self):
        """The agent reads its token at runtime; rc.d scripts are world-readable."""
        script = self._script(run_args=build_agent_args(relay_host="hle.world"))
        assert "hlea_" not in script

    @pytest.mark.parametrize("label", ["agent", "fp-rpi-22", "jellyfin"])
    def test_pidfile_and_log_follow_the_name(self, label):
        script = self._script(label=label)
        assert 'pidfile="/var/run/${name}.pid"' in script
        assert 'logfile="/var/log/${name}.log"' in script


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


_AGENT_SPEC = {
    "version": "2610.5",
    "label": "agent",
    "run_args": ["agent", "run"],
    "name": None,
    "run_as": None,
    "description": "HLE agent (dashboard-managed tunnels)",
    "restart": "always",
    "agent_config": "/root/.config/hle/agent.toml",
    "user_mode": False,
}


class TestIsPfSense:
    def test_a_platform_file_starting_with_pfsense_is_pfsense(self, tmp_path, monkeypatch):
        platform = tmp_path / "platform"
        platform.write_text("pfSense\n")
        monkeypatch.setattr(service_cmd, "_PLATFORM_FILE", platform)
        assert service_cmd.is_pfsense() is True

    def test_another_platform_is_not_pfsense(self, tmp_path, monkeypatch):
        platform = tmp_path / "platform"
        platform.write_text("OPNsense\n")
        monkeypatch.setattr(service_cmd, "_PLATFORM_FILE", platform)
        assert service_cmd.is_pfsense() is False

    def test_no_platform_file_is_not_pfsense(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_PLATFORM_FILE", tmp_path / "absent")
        assert service_cmd.is_pfsense() is False


class TestPfSenseBootHook:
    def _install(self, tmp_path, monkeypatch, *, pfsense: bool) -> None:
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/local/bin/hle")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: pfsense)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: None)
        service_cmd._rcd_install(
            label="agent",
            run_args=build_agent_args(),
            name=None,
            run_as=None,
            start=False,
        )

    def test_boot_hook_body_hands_start_to_the_rc_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        body = service_cmd.render_pfsense_boot_hook("hle_agent")
        assert body == (
            "#!/bin/sh\n"
            "# pfSense runs only rc.d/*.sh at boot; hand off to the rc.d service.\n"
            'case "$1" in\n'
            f'  start|stop|restart) exec {tmp_path / "hle_agent"} "$1" ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"
        )

    def test_install_on_pfsense_writes_both_scripts_and_the_hook_is_executable(
        self, tmp_path, monkeypatch
    ):
        self._install(tmp_path, monkeypatch, pfsense=True)
        assert (tmp_path / "hle_agent").exists()
        hook = tmp_path / "hle_agent.sh"
        assert hook.exists()
        assert hook.stat().st_mode & 0o777 == 0o755
        assert f'exec {tmp_path / "hle_agent"} "$1"' in hook.read_text()

    def test_install_on_pfsense_enables_through_the_local_rc_conf(self, tmp_path, monkeypatch):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/local/bin/hle")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: True)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: calls.append(a))
        service_cmd._rcd_install(
            label="agent", run_args=build_agent_args(), name=None, run_as=None, start=False
        )
        assert ("hle_agent_enable=YES", "-f", str(service_cmd._RC_CONF_LOCAL)) in calls

    def test_install_on_freebsd_writes_no_hook_and_keeps_the_plain_enable(
        self, tmp_path, monkeypatch
    ):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/local/bin/hle")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: False)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: calls.append(a))
        service_cmd._rcd_install(
            label="agent", run_args=build_agent_args(), name=None, run_as=None, start=False
        )
        assert (tmp_path / "hle_agent").exists()
        assert not (tmp_path / "hle_agent.sh").exists()
        assert calls == [("hle_agent_enable=YES",)]

    def test_uninstall_on_pfsense_removes_both_scripts(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, pfsense=True)
        monkeypatch.setattr(service_cmd, "_service_cmd", lambda *a: _Result(0))
        service_cmd._rcd_uninstall(label="agent", name=None)
        assert not (tmp_path / "hle_agent").exists()
        assert not (tmp_path / "hle_agent.sh").exists()

    def test_refresh_rewrites_the_wrapper(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, pfsense=True)
        hook = tmp_path / "hle_agent.sh"
        hook.unlink()
        monkeypatch.setattr(service_cmd, "service_spec", lambda *a, **k: dict(_AGENT_SPEC))
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "_service_cmd", lambda *a: _Result(0))
        monkeypatch.setattr(service_cmd, "_rcd_settles", lambda *a, **k: True)
        monkeypatch.setattr(service_cmd, "_rcd_running", lambda svc: False)
        assert service_cmd.refresh_service("hle_agent", False) == "refreshed"
        assert hook.exists()
        assert "hle_agent" in hook.read_text()

    def test_a_hook_is_not_mistaken_for_an_installed_service(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, pfsense=True)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        assert service_cmd.installed_services() == [("hle_agent", False)]


class TestRcdInstallRestartsARunningService:
    """`service <svc> start` is a no-op on a running service.

    So a refresh rewrote the script and left the previous release running.
    Restart when it is up; start when it is not (a fresh install).
    """

    class _Result:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode

    def _install(self, tmp_path, monkeypatch, *, running: bool) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/local/bin/hle")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: False)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: None)
        monkeypatch.setattr(service_cmd, "_rcd_running", lambda svc: running)
        monkeypatch.setattr(service_cmd, "_rcd_settles", lambda svc, **k: True)
        monkeypatch.setattr(
            service_cmd, "_service_cmd", lambda *a: calls.append(a) or self._Result(0)
        )
        monkeypatch.setattr(
            service_cmd,
            "_rcd_restart_outside_session",
            lambda svc: calls.append((svc, "restart")) or self._Result(0),
        )
        service_cmd._rcd_install(
            label="agent",
            run_args=build_agent_args(),
            name=None,
            run_as=None,
            start=True,
        )
        return calls

    def test_a_running_service_is_restarted(self, tmp_path, monkeypatch):
        calls = self._install(tmp_path, monkeypatch, running=True)
        assert ("hle_agent", "restart") in calls
        assert ("hle_agent", "start") not in calls

    def test_a_stopped_service_is_started(self, tmp_path, monkeypatch):
        calls = self._install(tmp_path, monkeypatch, running=False)
        assert ("hle_agent", "start") in calls
        assert ("hle_agent", "restart") not in calls

    def test_the_restart_cannot_be_hung_up_with_the_callers_session(self, monkeypatch):
        # Stopping the agent can drop the operator's session; a restart tied to
        # it would die between stop and start and leave the firewall unreachable.
        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            return self._Result(0)

        monkeypatch.setattr(service_cmd.subprocess, "run", fake_run)
        service_cmd._rcd_restart_outside_session("hle_agent")
        assert seen["argv"] == ["service", "hle_agent", "restart"]
        assert seen["start_new_session"] is True
        for stream in ("stdin", "stdout", "stderr"):
            assert seen[stream] is subprocess.DEVNULL


class TestRcScriptInterpreterGuard:
    """A venv whose interpreter is gone must not respawn every second.

    ``-x`` says the launcher is executable even when its shebang points at a
    python that no longer exists, so daemon(8) -r relaunches it forever while
    the log stays silent. The rc script tests the command once and refuses to
    start with one line naming the fix.
    """

    def _script(self, **kw):
        params = {
            "label": "agent",
            "hle_path": "/opt/hle/current/bin/hle",
            "run_args": build_agent_args(),
            **kw,
        }
        return render_rc_script(**params)

    def test_a_start_precmd_is_declared(self):
        assert 'start_precmd="hle_agent_check_interpreter"' in self._script()

    def test_the_precmd_runs_the_command_and_logs_the_fix(self):
        script = self._script()
        assert "hle_agent_check_interpreter()" in script
        assert '"${hle_command}" --version' in script
        assert "venv interpreter missing — re-run the installer" in script
        assert '>> "${logfile}"' in script

    def test_refusing_to_start_fails_the_precmd(self):
        script = self._script()
        body = script[script.index("hle_agent_check_interpreter()") :]
        assert "return 1" in body
        assert body.index("return 1") < body.index("return 0")

    def test_the_precmd_precedes_run_rc_command(self):
        script = self._script()
        assert script.index("start_precmd=") < script.index('run_rc_command "$1"')


class TestEnsurePfSenseBootHook:
    def _setup(self, tmp_path, monkeypatch) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "_RC_CONF", tmp_path / "rc.conf")
        monkeypatch.setattr(service_cmd, "_RC_CONF_LOCAL", tmp_path / "rc.conf.local")
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: calls.append(a))
        return calls

    def test_a_missing_hook_is_written_and_the_flag_set(self, tmp_path, monkeypatch):
        calls = self._setup(tmp_path, monkeypatch)
        assert service_cmd.ensure_pfsense_boot_hook("hle_agent") is True
        hook = tmp_path / "hle_agent.sh"
        assert hook.exists()
        assert hook.stat().st_mode & 0o777 == 0o755
        assert f'exec {tmp_path / "hle_agent"} "$1"' in hook.read_text()
        assert ("hle_agent_enable=YES", "-f", str(tmp_path / "rc.conf.local")) in calls

    def test_a_current_hook_and_flag_change_nothing(self, tmp_path, monkeypatch):
        calls = self._setup(tmp_path, monkeypatch)
        (tmp_path / "hle_agent.sh").write_text(service_cmd.render_pfsense_boot_hook("hle_agent"))
        (tmp_path / "rc.conf.local").write_text('hle_agent_enable="YES"\n')
        assert service_cmd.ensure_pfsense_boot_hook("hle_agent") is False
        assert calls == []

    def test_a_stale_hook_is_rewritten(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "hle_agent.sh").write_text("#!/bin/sh\n# stale\n")
        assert service_cmd.ensure_pfsense_boot_hook("hle_agent") is True
        assert "hand off to the rc.d service" in (tmp_path / "hle_agent.sh").read_text()

    def test_a_stale_flag_is_moved_out_of_the_global_rc_conf(self, tmp_path, monkeypatch):
        calls = self._setup(tmp_path, monkeypatch)
        (tmp_path / "hle_agent.sh").write_text(service_cmd.render_pfsense_boot_hook("hle_agent"))
        (tmp_path / "rc.conf").write_text('hle_agent_enable="YES"\n')
        (tmp_path / "rc.conf.local").write_text('hle_agent_enable="YES"\n')
        assert service_cmd.ensure_pfsense_boot_hook("hle_agent") is True
        assert ("-x hle_agent_enable",) in calls
        # The local copy is already right, so it is not written again.
        assert ("hle_agent_enable=YES", "-f", str(tmp_path / "rc.conf.local")) not in calls

    def test_it_reports_when_it_changes_something(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        service_cmd.ensure_pfsense_boot_hook("hle_agent")
        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "pfSense boot hook" in out


class TestLegacyAgentSpec:
    def _write_script(self, tmp_path, monkeypatch, **kw):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        script = render_rc_script(
            label="agent",
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            home="/home/admin",
            agent_config="/home/admin/.config/hle/agent.toml",
            **kw,
        )
        (tmp_path / "hle_agent").write_text(script)
        return script

    def test_a_spec_is_read_back_from_the_script(self, tmp_path, monkeypatch):
        self._write_script(tmp_path, monkeypatch)
        spec = service_cmd._legacy_agent_spec("hle_agent", False)
        assert spec is not None
        assert spec["label"] == "agent"
        assert spec["run_args"] == ["agent", "run"]
        assert spec["agent_config"] == "/home/admin/.config/hle/agent.toml"
        assert spec["home"] == "/home/admin"
        assert spec["user_mode"] is False

    def test_relay_arguments_are_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        (tmp_path / "hle_agent").write_text(
            render_rc_script(
                label="agent",
                hle_path="/usr/local/bin/hle",
                run_args=build_agent_args(relay_host="relay.example", relay_port=8443),
            )
        )
        spec = service_cmd._legacy_agent_spec("hle_agent", False)
        assert spec is not None
        assert spec["run_args"] == [
            "agent",
            "run",
            "--relay-host",
            "relay.example",
            "--relay-port",
            "8443",
        ]

    def test_a_stamped_script_is_not_reconstructed(self, tmp_path, monkeypatch):
        self._write_script(tmp_path, monkeypatch, spec=dict(_AGENT_SPEC))
        assert service_cmd._legacy_agent_spec("hle_agent", False) is None

    def test_a_tunnel_script_is_not_reconstructed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        (tmp_path / "hle_ha").write_text(
            render_rc_script(
                label="ha",
                hle_path="/usr/local/bin/hle",
                run_args=["tunnel", "create", "ha", "http://localhost:8123"],
            )
        )
        assert service_cmd._legacy_agent_spec("hle_ha", False) is None

    def test_a_non_freebsd_platform_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "linux")
        assert service_cmd._legacy_agent_spec("hle_agent", False) is None


class TestRefreshServiceHealsTheBootHook:
    def test_a_spec_less_pfsense_agent_heals_before_restarting(self, monkeypatch):
        healed: list[str] = []
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: True)
        monkeypatch.setattr(
            service_cmd, "ensure_pfsense_boot_hook", lambda svc: healed.append(svc) or True
        )
        monkeypatch.setattr(service_cmd, "service_spec", lambda *a, **k: None)
        monkeypatch.setattr(service_cmd, "_legacy_agent_spec", lambda *a, **k: None)
        monkeypatch.setattr(service_cmd, "restart_service", lambda *a, **k: True)
        assert service_cmd.refresh_service("hle_agent", False) == "restarted"
        assert healed == ["hle_agent"]

    def test_a_freebsd_box_that_is_not_pfsense_is_untouched(self, monkeypatch):
        healed: list[str] = []
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: False)
        monkeypatch.setattr(
            service_cmd, "ensure_pfsense_boot_hook", lambda svc: healed.append(svc) or True
        )
        monkeypatch.setattr(service_cmd, "service_spec", lambda *a, **k: None)
        monkeypatch.setattr(service_cmd, "restart_service", lambda *a, **k: True)
        assert service_cmd.refresh_service("hle_ha", False) == "restarted"
        assert healed == []

    def test_a_spec_less_pfsense_agent_is_rebuilt_from_its_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: True)
        monkeypatch.setattr(service_cmd, "_sysrc", lambda *a: None)
        (tmp_path / "hle_agent").write_text(
            render_rc_script(
                label="agent",
                hle_path="/usr/local/bin/hle",
                run_args=build_agent_args(),
                home="/home/admin",
                agent_config="/home/admin/.config/hle/agent.toml",
            )
        )
        built: list[dict] = []
        monkeypatch.setattr(
            service_cmd, "_install_from_spec", lambda spec, **k: built.append(spec)
        )
        assert service_cmd.refresh_service("hle_agent", False) == "refreshed"
        assert built and built[0]["label"] == "agent"
        assert built[0]["agent_config"] == "/home/admin/.config/hle/agent.toml"
        # The heal ran first, so the box survives the next reboot too.
        assert (tmp_path / "hle_agent.sh").exists()


class TestAgentStartupHealsTheBootHook:
    def _setup(self, tmp_path, monkeypatch, *, euid: int) -> list[str]:
        healed: list[str] = []
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: True)
        monkeypatch.setattr(service_cmd.os, "geteuid", lambda: euid)
        monkeypatch.setattr(
            service_cmd, "ensure_pfsense_boot_hook", lambda svc: healed.append(svc) or True
        )
        return healed

    def test_root_with_the_agent_installed_heals(self, tmp_path, monkeypatch):
        healed = self._setup(tmp_path, monkeypatch, euid=0)
        (tmp_path / "hle_agent").write_text("# service\n")
        service_cmd.ensure_agent_boot_hook()
        assert healed == ["hle_agent"]

    def test_a_non_root_agent_does_not_touch_service_files(self, tmp_path, monkeypatch):
        healed = self._setup(tmp_path, monkeypatch, euid=1000)
        (tmp_path / "hle_agent").write_text("# service\n")
        service_cmd.ensure_agent_boot_hook()
        assert healed == []

    def test_without_the_agent_service_nothing_happens(self, tmp_path, monkeypatch):
        healed = self._setup(tmp_path, monkeypatch, euid=0)
        service_cmd.ensure_agent_boot_hook()
        assert healed == []


class TestPfSenseBootHookWarnings:
    def _pfsense(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: True)
        monkeypatch.setattr(service_cmd, "_service_cmd", lambda *a: _Result(0))

    def test_status_warns_when_the_hook_is_missing(self, tmp_path, monkeypatch):
        self._pfsense(tmp_path, monkeypatch)
        monkeypatch.setattr(service_cmd, "_require_supported", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "resolve_user_mode", lambda **k: False)
        (tmp_path / "hle_agent").write_text("# service\n")
        with patch("hle_client.ops.daemon.status", new_callable=AsyncMock):
            result = CliRunner().invoke(main, ["daemon", "status", "agent"])
        out = " ".join(_ANSI.sub("", result.output).split())
        assert "will not start after a reboot" in out
        assert "hle daemon refresh agent" in out

    def test_status_is_silent_when_the_hook_exists(self, tmp_path, monkeypatch):
        self._pfsense(tmp_path, monkeypatch)
        monkeypatch.setattr(service_cmd, "_require_supported", lambda: "freebsd")
        monkeypatch.setattr(service_cmd, "resolve_user_mode", lambda **k: False)
        (tmp_path / "hle_agent").write_text("# service\n")
        (tmp_path / "hle_agent.sh").write_text("#!/bin/sh\n")
        with patch("hle_client.ops.daemon.status", new_callable=AsyncMock):
            result = CliRunner().invoke(main, ["daemon", "status", "agent"])
        assert "will not start" not in result.output

    def test_list_names_only_the_services_without_a_hook(self, tmp_path, monkeypatch, capsys):
        self._pfsense(tmp_path, monkeypatch)
        (tmp_path / "hle_agent").write_text("# service\n")
        (tmp_path / "hle_ha").write_text("# service\n")
        (tmp_path / "hle_ha.sh").write_text("#!/bin/sh\n")
        service_cmd._rcd_list()
        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "hle_agent will not start after a reboot" in out
        assert "hle_ha will not start" not in out

    def test_a_non_pfsense_box_never_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(service_cmd, "_RCD_DIR", tmp_path)
        monkeypatch.setattr(service_cmd, "is_pfsense", lambda: False)
        (tmp_path / "hle_agent").write_text("# service\n")
        service_cmd._pfsense_boot_hook_warning("hle_agent")
        assert "will not start" not in capsys.readouterr().out

