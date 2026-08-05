"""Tests for `hle service` systemd unit generation."""

from __future__ import annotations

import re

import pytest

from hle_client.service_cmd import (
    AGENT_LABEL,
    build_agent_args,
    build_expose_args,
    build_fp_args,
    fp_label,
    launchd_label,
    render_launchd_plist,
    render_unit,
    resolve_user_mode,
    unit_name,
)

# Rich colours numbers and wraps at the console width, so raw substring
# assertions on its output fail for reasons the user never sees.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class TestBuildFpArgs:
    def test_minimal(self):
        assert build_fp_args(agent="rpi", target="22") == ["fp", "--agent", "rpi", "--to", "22"]

    def test_full(self):
        assert build_fp_args(
            agent="rpi", target="localhost:22", bind_port=9922, bind_host="127.0.0.1"
        ) == [
            "fp",
            "--agent",
            "rpi",
            "--to",
            "localhost:22",
            "--port",
            "9922",
            "--bind",
            "127.0.0.1",
        ]

    def test_never_contains_a_key(self):
        # The API key is read at runtime, never baked into the unit.
        assert not any(a.startswith("hle_") for a in build_fp_args(agent="rpi", target="22"))


class TestFpLabel:
    def test_includes_port_so_forwards_coexist(self):
        assert fp_label("rpi", "22") == "fp-rpi-22"
        assert fp_label("rpi", "localhost:5432") == "fp-rpi-5432"
        # Two forwards through one agent must not collide on the unit name.
        assert fp_label("rpi", "22") != fp_label("rpi", "5432")

    def test_unit_name(self):
        assert unit_name(fp_label("rpi", "22")) == "hle-fp-rpi-22.service"

    def test_sanitizes_unsafe_characters(self):
        # Agent names are user-supplied; they must not escape into the filename.
        assert "/" not in fp_label("a/b", "22")
        assert fp_label("a/b", "22") == "fp-a-b-22"


class TestBuildAgentArgs:
    def test_minimal(self):
        assert build_agent_args() == ["agent", "run"]

    def test_relay_override(self):
        assert build_agent_args(relay_host="staging.hle.world", relay_port=8443) == [
            "agent",
            "run",
            "--relay-host",
            "staging.hle.world",
            "--relay-port",
            "8443",
        ]

    def test_never_contains_a_token(self):
        # The enrollment token is read at runtime, never baked into the unit.
        assert not any(a.startswith("hlea_") for a in build_agent_args())


class TestResolveUserMode:
    def test_explicit_user_wins(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
        assert resolve_user_mode(user_flag=True, system_flag=False) is True

    def test_explicit_system_wins(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
        assert resolve_user_mode(user_flag=False, system_flag=True) is False

    def test_autodetect_root_is_system(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
        assert resolve_user_mode(user_flag=False, system_flag=False) is False

    def test_autodetect_nonroot_is_user(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
        assert resolve_user_mode(user_flag=False, system_flag=False) is True

    def test_both_flags_rejected(self):
        with pytest.raises(SystemExit):
            resolve_user_mode(user_flag=True, system_flag=True)


class TestAgentUnit:
    def test_unit_name_and_restart_always(self):
        assert unit_name(AGENT_LABEL) == "hle-agent.service"
        unit = render_unit(
            label=AGENT_LABEL,
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            user_mode=False,
            run_as_user="homelab",
            description="HLE agent (dashboard-managed tunnels)",
            restart="always",
        )
        assert "ExecStart=/usr/local/bin/hle agent run" in unit
        assert "Restart=always" in unit
        assert "Description=HLE agent (dashboard-managed tunnels)" in unit
        assert "User=homelab" in unit

    def test_launchd_plist(self):
        plist = render_launchd_plist(
            label=AGENT_LABEL,
            plist_label=launchd_label(AGENT_LABEL),
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            run_as_user=None,
            log_dir="/tmp/logs",
        )
        assert "<string>world.hle.agent</string>" in plist
        assert "<string>agent</string>" in plist
        assert "<string>run</string>" in plist


class TestUnitName:
    def test_default(self):
        assert unit_name("tv") == "hle-tv.service"

    def test_explicit_name(self):
        assert unit_name("tv", "my-tunnel") == "my-tunnel.service"

    def test_explicit_name_with_suffix(self):
        assert unit_name("tv", "my-tunnel.service") == "my-tunnel.service"


class TestBuildExposeArgs:
    def test_minimal(self):
        assert build_expose_args(service="http://localhost:9998", label="tv") == [
            "expose",
            "--service",
            "http://localhost:9998",
            "--label",
            "tv",
        ]

    def test_all_options(self):
        args = build_expose_args(
            service="https://192.168.2.200:8006",
            label="prox",
            zone="pr.t00t.us",
            auth="none",
            websocket=False,
            verify_ssl=True,
            forward_host=True,
            allow=("a@x.com", "github:b@y.com"),
            options=("k=v",),
        )
        assert "--zone" in args and "pr.t00t.us" in args
        assert "--auth" in args and "none" in args
        assert "--no-websocket" in args
        assert "--verify-ssl" in args
        assert "--forward-host" in args
        assert args.count("--allow") == 2
        assert "--option" in args and "k=v" in args

    def test_apex_no_label_flag(self):
        args = build_expose_args(service="http://x", label=None, zone="t00t.us", apex=True)
        assert "--apex" in args
        assert "--label" not in args

    def test_no_service_secrets(self):
        # API key must never appear in the generated args.
        args = build_expose_args(service="http://x", label="tv")
        assert not any("api" in a.lower() or "key" in a.lower() for a in args)


class TestRenderUnit:
    def test_system_unit_has_user_and_multiuser_target(self):
        unit = render_unit(
            label="tv",
            hle_path="/root/.local/bin/hle",
            run_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
            user_mode=False,
            run_as_user="ian",
        )
        assert "Description=HLE tunnel: tv" in unit
        assert (
            "ExecStart=/root/.local/bin/hle expose --service http://localhost:9998 --label tv"
        ) in unit
        assert "User=ian" in unit
        assert "WantedBy=multi-user.target" in unit
        assert "Restart=on-failure" in unit
        assert "After=network-online.target" in unit

    def test_user_unit_omits_user_and_uses_default_target(self):
        unit = render_unit(
            label="tv",
            hle_path="/home/ian/.local/bin/hle",
            run_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
            user_mode=True,
            run_as_user="ian",
        )
        assert "User=" not in unit
        assert "WantedBy=default.target" in unit

    def test_args_with_spaces_are_quoted(self):
        unit = render_unit(
            label="tv",
            hle_path="/opt/hle bin/hle",
            run_args=["expose", "--option", "note=hello world"],
            user_mode=True,
            run_as_user=None,
        )
        assert '"note=hello world"' in unit
        assert '"/opt/hle bin/hle"' not in unit  # only args are quoted, not the leading path


class TestLaunchdLabel:
    def test_default(self):
        assert launchd_label("tv") == "world.hle.tv"

    def test_explicit_name(self):
        assert launchd_label("tv", "com.acme.tunnel") == "com.acme.tunnel"

    def test_explicit_name_strips_plist_suffix(self):
        assert launchd_label("tv", "com.acme.tunnel.plist") == "com.acme.tunnel"


class TestRenderLaunchdPlist:
    def test_system_daemon_has_username(self):
        plist = render_launchd_plist(
            label="tv",
            plist_label="world.hle.tv",
            hle_path="/usr/local/bin/hle",
            run_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
            run_as_user="ian",
            log_dir="/var/log",
        )
        assert "<string>world.hle.tv</string>" in plist
        assert "<key>UserName</key>" in plist
        assert "<string>ian</string>" in plist
        assert "<string>/usr/local/bin/hle</string>" in plist
        assert "<string>--label</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist
        assert "/var/log/tv.log" in plist

    def test_user_agent_omits_username(self):
        plist = render_launchd_plist(
            label="tv",
            plist_label="world.hle.tv",
            hle_path="/opt/homebrew/bin/hle",
            run_args=["expose", "--service", "http://localhost:9998"],
            run_as_user=None,
            log_dir="/Users/ian/Library/Logs/hle",
        )
        assert "<key>UserName</key>" not in plist

    def test_xml_special_chars_escaped(self):
        plist = render_launchd_plist(
            label="tv",
            plist_label="world.hle.tv",
            hle_path="/usr/local/bin/hle",
            run_args=["expose", "--option", "note=a&b<c"],
            run_as_user=None,
            log_dir="/var/log",
        )
        assert "a&amp;b&lt;c" in plist
        assert "a&b<c" not in plist


class TestServiceWiring:
    def test_registered_on_cli(self):
        from hle_client.cli import main

        assert "service" in main.commands
        assert "install" in main.commands["service"].commands


class TestRestartWithoutATarget:
    """The error has to name every way out, or it sends people the long way round.

    Reported from a live box: a bare `hle service restart` answered "--label is
    required (or pass --agent for the agent service)" and never mentioned
    `--all`, which is what somebody restarting after an upgrade actually wants.
    """

    def _run(self, subcommand):
        """Invoke the subcommand with the platform check stubbed out.

        Without this the test only proves what the CI container lacks: the test
        image has no systemctl, so `_require_supported` exits with "needs
        systemd" before argument handling runs. That made the assertion fail on
        Linux and — worse — made the negative test below pass for the wrong
        reason, since the systemd message happens not to contain "--all".
        """
        from unittest.mock import patch

        from click.testing import CliRunner

        from hle_client.cli import main

        with patch("hle_client.service_cmd._require_supported", return_value="linux"):
            return CliRunner().invoke(main, ["service", subcommand])

    def test_mentions_all_and_how_to_look(self):
        result = self._run("restart")
        assert result.exit_code == 1
        out = " ".join(_ANSI.sub("", result.output).split())
        assert "--label is required" in out
        assert "--all" in out
        assert "hle service list" in out

    def test_uninstall_does_not_claim_an_all_flag_it_lacks(self):
        result = self._run("uninstall")
        out = " ".join(_ANSI.sub("", result.output).split())
        # Proves the hint is per-command, and that we got past the platform gate.
        assert "--label is required" in out
        assert "--all" not in out
