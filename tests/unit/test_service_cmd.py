"""Tests for `hle service` systemd unit generation."""

from __future__ import annotations

from hle_client.service_cmd import (
    build_expose_args,
    launchd_label,
    render_launchd_plist,
    render_unit,
    unit_name,
)


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
            expose_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
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
            expose_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
            user_mode=True,
            run_as_user="ian",
        )
        assert "User=" not in unit
        assert "WantedBy=default.target" in unit

    def test_args_with_spaces_are_quoted(self):
        unit = render_unit(
            label="tv",
            hle_path="/opt/hle bin/hle",
            expose_args=["expose", "--option", "note=hello world"],
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
            expose_args=["expose", "--service", "http://localhost:9998", "--label", "tv"],
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
            expose_args=["expose", "--service", "http://localhost:9998"],
            run_as_user=None,
            log_dir="/Users/ian/Library/Logs/hle",
        )
        assert "<key>UserName</key>" not in plist

    def test_xml_special_chars_escaped(self):
        plist = render_launchd_plist(
            label="tv",
            plist_label="world.hle.tv",
            hle_path="/usr/local/bin/hle",
            expose_args=["expose", "--option", "note=a&b<c"],
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
