"""Tests for `hle service` systemd unit generation."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hle_client import service_cmd
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


class TestServiceListShowsBothScopes:
    """`hle service list` is where people are sent to find a duplicate.

    It listed one scope — system by default — so a per-user unit installed
    beside a system one of the same name did not appear at all. On the host
    that prompted this work, `hle service list` showed two system units and
    said nothing about the per-user `hle-agent.service` that was fighting one
    of them for every tunnel.
    """

    @staticmethod
    def _fake_systemctl(monkeypatch, *, system: str, user: str):
        def fake(cmd, **kwargs):
            body = user if "--user" in cmd else system
            return SimpleNamespace(returncode=0, stdout=body, stderr="")

        monkeypatch.setattr(service_cmd.subprocess, "run", fake)

    def test_both_scopes_are_listed(self, monkeypatch, capsys):
        self._fake_systemctl(
            monkeypatch,
            system="hle-ots.service loaded active running HLE tunnel: ots",
            user="hle-agent.service loaded active running HLE agent",
        )
        service_cmd._systemd_list(user_mode=None)
        out = _ANSI.sub("", capsys.readouterr().out)
        assert "system-wide" in out
        assert "per-user" in out
        assert "hle-ots.service" in out
        assert "hle-agent.service" in out

    def test_a_unit_in_both_scopes_is_called_out(self, monkeypatch, capsys):
        """The duplicate is the thing worth saying out loud."""
        line = "hle-agent.service loaded active running HLE agent"
        self._fake_systemctl(monkeypatch, system=line, user=line)
        service_cmd._systemd_list(user_mode=None)
        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "Installed twice: hle-agent.service" in out
        assert "hle service uninstall --user" in out

    def test_distinct_units_are_not_called_a_duplicate(self, monkeypatch, capsys):
        self._fake_systemctl(
            monkeypatch,
            system="hle-ots.service loaded active running HLE tunnel: ots",
            user="hle-agent.service loaded active running HLE agent",
        )
        service_cmd._systemd_list(user_mode=None)
        assert "Installed twice" not in capsys.readouterr().out

    def test_a_single_scope_can_still_be_asked_for(self, monkeypatch, capsys):
        self._fake_systemctl(
            monkeypatch,
            system="hle-ots.service loaded active running HLE tunnel: ots",
            user="hle-agent.service loaded active running HLE agent",
        )
        service_cmd._systemd_list(user_mode=True)
        out = _ANSI.sub("", capsys.readouterr().out)
        assert "hle-agent.service" in out
        assert "hle-ots.service" not in out


class TestRestartStaysInScope:
    """Restarting must target the unit it was asked about, not a namesake.

    From a real upgrade: a host carried `hle-agent.service` as both a system
    unit and a per-user one. The system restart returned "Access denied", the
    code retried in the user scope, that succeeded, and it printed

        Failed to restart hle-agent.service: Access denied
          restarted hle-agent.service

    The duplicate was restarted onto the new version while the unit actually
    serving stayed on the old one, reported as a success.
    """

    @staticmethod
    def _calls(monkeypatch) -> list[tuple[bool, tuple[str, ...]]]:
        seen: list[tuple[bool, tuple[str, ...]]] = []

        def fake(user_mode: bool, *args: str):
            seen.append((user_mode, args))
            # System scope always denied; user scope always works. This is the
            # exact shape that produced the false success.
            return SimpleNamespace(returncode=0 if user_mode else 1)

        monkeypatch.setattr(service_cmd, "_systemctl", fake)
        monkeypatch.setattr(service_cmd, "current_platform", lambda: "linux")
        return seen

    def test_a_denied_system_restart_is_a_failure_not_a_user_restart(self, monkeypatch, capsys):
        seen = self._calls(monkeypatch)
        assert service_cmd.restart_service("hle-agent.service", False) is False
        # It must not have reached into the other scope at all.
        assert [user for user, _ in seen] == [False]

    def test_a_user_unit_is_restarted_in_the_user_scope(self, monkeypatch):
        seen = self._calls(monkeypatch)
        assert service_cmd.restart_service("hle-agent.service", True) is True
        assert [user for user, _ in seen] == [True]

    def test_a_denied_system_restart_says_how_to_do_it(self, monkeypatch, capsys):
        """`sudo hle` does not work — ~/.local/bin is not on root's PATH."""
        self._calls(monkeypatch)
        monkeypatch.setattr(service_cmd.os, "geteuid", lambda: 1000)
        service_cmd.restart_service("hle-agent.service", False)
        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "sudo systemctl restart hle-agent.service" in out

    def test_root_is_not_told_to_use_sudo(self, monkeypatch, capsys):
        self._calls(monkeypatch)
        monkeypatch.setattr(service_cmd.os, "geteuid", lambda: 0)
        service_cmd.restart_service("hle-agent.service", False)
        assert "sudo" not in capsys.readouterr().out

    def test_an_unknown_scope_still_falls_back(self, monkeypatch):
        """Callers that genuinely cannot know keep the old best-effort behaviour."""
        seen = self._calls(monkeypatch)
        assert service_cmd.restart_service("hle-agent.service", None) is True
        assert [user for user, _ in seen] == [False, True]


class TestDuplicateScopeInstall:
    """Installing a unit that is already live in the other systemd scope.

    The *same* agent installed twice is the fault: both copies read one
    enrollment token, register the same endpoints, and take the tunnel off each
    other about once a second. Observed in the wild — a system unit from August,
    a per-user unit added in September, a tunnel reconnecting every 1.4s for a
    day, and nothing on the host looking wrong.

    Two *different* agents on one machine are not a fault. Someone learning HLE
    ends up with a spare, and one agent per OS user is a reasonable way to keep
    things separate. The unit name cannot tell those cases apart; the token can,
    and where even that is unreadable the benefit of the doubt goes to the user:
    a false refusal blocks a legitimate setup outright, while a duplicate that
    slips through is still caught by the relay.
    """

    @staticmethod
    def _install(
        tmp_path,
        monkeypatch,
        *,
        user_mode: bool,
        existing: str | None,
        existing_token: str | None = "hlea_same",
        new_token: str | None = "hlea_same",
        existing_user: str | None = None,
        run_as: str | None = None,
    ):
        """Attempt an install, having optionally planted a unit in the other scope.

        Tokens are written to separate files so "same token" and "same file"
        can be exercised independently.
        """
        system_dir = tmp_path / "system"
        user_dir = tmp_path / "home" / ".config" / "systemd" / "user"
        system_dir.mkdir(parents=True, exist_ok=True)
        user_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(service_cmd, "_SYSTEM_UNIT_DIR", system_dir)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/bin/hle")
        monkeypatch.setattr(service_cmd, "_systemctl", lambda *a, **k: None)

        new_config = tmp_path / "new-agent.toml"
        if new_token:
            new_config.write_text(f'token = "{new_token}"\n')

        uname = unit_name(AGENT_LABEL, None)
        if existing is not None:
            existing_config = tmp_path / "existing-agent.toml"
            if existing_token:
                existing_config.write_text(f'token = "{existing_token}"\n')
            lines = [
                "[Unit]",
                "[Service]",
                "ExecStart=/usr/bin/hle agent run",
                f"Environment=HLE_AGENT_CONFIG={existing_config}",
            ]
            if existing_user:
                lines.append(f"User={existing_user}")
            target = system_dir if existing == "system" else user_dir
            (target / uname).write_text("\n".join(lines) + "\n")

        return service_cmd._systemd_install(
            label=AGENT_LABEL,
            run_args=["agent", "run"],
            name=None,
            user_mode=user_mode,
            run_as=run_as,
            start=False,
            agent_config=str(new_config),
        )

    # -- the genuine duplicate ------------------------------------------------

    def test_the_same_token_in_the_other_scope_is_refused(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._install(tmp_path, monkeypatch, user_mode=True, existing="system")
        assert excinfo.value.code == 1

        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "same agent is already installed system-wide" in out
        assert "same enrollment token" in out
        assert "hle service uninstall --label agent" in out

    def test_it_works_in_the_other_direction_too(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._install(tmp_path, monkeypatch, user_mode=False, existing="user")
        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "hle service uninstall --user --label agent" in out

    def test_the_same_token_file_read_as_the_same_user_counts(self, tmp_path, monkeypatch):
        """Unreadable tokens, but provably one identity: same file, same user."""
        shared = tmp_path / "shared-agent.toml"  # deliberately never written
        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True)
        user_dir = tmp_path / "home" / ".config" / "systemd" / "user"
        user_dir.mkdir(parents=True)
        monkeypatch.setattr(service_cmd, "_SYSTEM_UNIT_DIR", system_dir)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/bin/hle")
        monkeypatch.setattr(service_cmd, "_systemctl", lambda *a, **k: None)

        # The incumbent lives in the *other* scope from the one being installed.
        (user_dir / unit_name(AGENT_LABEL, None)).write_text(
            f"[Service]\nEnvironment=HLE_AGENT_CONFIG={shared}\nUser=mimos\n"
        )

        with pytest.raises(SystemExit):
            service_cmd._systemd_install(
                label=AGENT_LABEL,
                run_args=["agent", "run"],
                name=None,
                user_mode=False,
                run_as="mimos",
                start=False,
                agent_config=str(shared),
            )

    # -- the legitimate second agent ------------------------------------------

    def test_a_different_token_is_allowed(self, tmp_path, monkeypatch, capsys):
        """Two agents on one machine is a real setup, not a mistake.

        One per OS user, or a beginner with a spare. The credential decides,
        and these are two different enrollments.
        """
        self._install(
            tmp_path,
            monkeypatch,
            user_mode=True,
            existing="system",
            existing_token="hlea_one",
            new_token="hlea_two",
        )
        written = tmp_path / "home" / ".config" / "systemd" / "user" / unit_name(AGENT_LABEL, None)
        assert written.exists()

        out = " ".join(_ANSI.sub("", capsys.readouterr().out).split())
        assert "also exists system-wide" in out
        assert "--name" in out  # how to keep the two distinguishable

    def test_an_unreadable_token_gets_the_benefit_of_the_doubt(self, tmp_path, monkeypatch):
        """A per-user install cannot read root's token file — the normal case.

        Refusing on "cannot tell" would block the legitimate setup outright,
        while a duplicate that slips through is still refused by the relay.
        """
        self._install(
            tmp_path,
            monkeypatch,
            user_mode=True,
            existing="system",
            existing_token=None,  # file never written
            new_token="hlea_two",
        )
        written = tmp_path / "home" / ".config" / "systemd" / "user" / unit_name(AGENT_LABEL, None)
        assert written.exists()

    def test_the_same_file_under_a_different_user_is_not_the_same_agent(
        self, tmp_path, monkeypatch
    ):
        """``~/.config/hle/agent.toml`` resolves per user; same text, different files."""
        assert (
            service_cmd._same_agent(
                "[Service]\nEnvironment=HLE_AGENT_CONFIG=/home/a/.config/hle/agent.toml\nUser=a\n",
                "[Service]\nEnvironment=HLE_AGENT_CONFIG=/home/a/.config/hle/agent.toml\nUser=b\n",
            )
            is None
        )

    def test_non_agent_units_are_not_judged_on_tokens(self, tmp_path):
        """A plain `hle expose` unit carries no token; its label already separates it."""
        assert service_cmd._same_agent("[Service]\nExecStart=/usr/bin/hle expose\n", "") is None

    # -- unchanged behaviour ---------------------------------------------------

    def test_a_clean_host_still_installs(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, user_mode=True, existing=None)
        written = tmp_path / "home" / ".config" / "systemd" / "user" / unit_name(AGENT_LABEL, None)
        assert written.exists()

    def test_reinstalling_in_the_same_scope_is_still_allowed(self, tmp_path, monkeypatch):
        """Overwriting your own unit is an upgrade, not a duplicate."""
        self._install(tmp_path, monkeypatch, user_mode=True, existing="user")
        written = tmp_path / "home" / ".config" / "systemd" / "user" / unit_name(AGENT_LABEL, None)
        assert "ExecStart" in written.read_text()

    def test_asking_the_question_does_not_create_the_user_directory(self, tmp_path, monkeypatch):
        """A system install must not leave a stray ~/.config/systemd/user behind."""
        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True)
        monkeypatch.setattr(service_cmd, "_SYSTEM_UNIT_DIR", system_dir)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.setattr(service_cmd, "find_hle_path", lambda: "/usr/bin/hle")
        monkeypatch.setattr(service_cmd, "_systemctl", lambda *a, **k: None)

        service_cmd._systemd_install(
            label=AGENT_LABEL,
            run_args=["agent", "run"],
            name=None,
            user_mode=False,
            run_as=None,
            start=False,
        )

        assert not (tmp_path / "home" / ".config" / "systemd" / "user").exists()
