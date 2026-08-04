"""Tests for the FreeBSD rc.d service backend (pfSense / OPNsense).

The rc.d script is generated, never hand-written, so the parts that must be
exactly right — the rcvar name, quoting of arguments, daemon(8) flags — are
covered here rather than discovered on a firewall.
"""

from __future__ import annotations

import pytest

from hle_client.service_cmd import (
    build_agent_args,
    current_platform,
    rc_service_name,
    render_rc_script,
)


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

    def test_no_token_is_baked_into_the_script(self):
        """The agent reads its token at runtime; rc.d scripts are world-readable."""
        script = self._script(run_args=build_agent_args(relay_host="hle.world"))
        assert "hlea_" not in script

    @pytest.mark.parametrize("label", ["agent", "fp-rpi-22", "jellyfin"])
    def test_pidfile_and_log_follow_the_name(self, label):
        script = self._script(label=label)
        assert 'pidfile="/var/run/${name}.pid"' in script
        assert 'logfile="/var/log/${name}.log"' in script
