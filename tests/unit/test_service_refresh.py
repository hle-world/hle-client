"""A service is rebuilt on upgrade, not just restarted.

The failure this covers looked like nothing at all: a firewall agent whose
service file still pointed at the previous client, restarted faithfully by the
service manager, reporting a healthy pid while the dashboard showed it offline
on a version that was no longer installed. Restarting could not fix it, because
what was being restarted was wrong. So the generated file now records the
arguments it was built from, and an upgrade re-renders from that record.
"""

from __future__ import annotations

from unittest.mock import patch

from hle_client import __version__
from hle_client.errors import HleError
from hle_client.service_cmd import (
    build_agent_args,
    parse_service_spec,
    refresh_service,
    render_launchd_plist,
    render_rc_script,
    render_unit,
    spec_comment,
)
from hle_common.tunnel_spec import TunnelSpec

SPEC = {
    "version": "2608.2",
    "label": "agent",
    "run_args": ["agent", "run"],
    "name": None,
    "run_as": None,
    "description": "HLE agent (dashboard-managed tunnels)",
    "restart": "always",
    "agent_config": "/root/.config/hle/agent.toml",
    "user_mode": False,
}


class TestSpecRoundTrip:
    def test_rc_script_carries_its_spec(self):
        text = render_rc_script(
            label="agent",
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            run_as_user=None,
            spec=SPEC,
        )
        assert parse_service_spec(text) == SPEC

    def test_systemd_unit_carries_its_spec(self):
        text = render_unit(
            label="agent",
            hle_path="/usr/bin/hle",
            run_args=build_agent_args(),
            user_mode=False,
            run_as_user="ian",
            spec=SPEC,
        )
        assert parse_service_spec(text) == SPEC

    def test_launchd_plist_carries_its_spec(self):
        text = render_launchd_plist(
            label="agent",
            plist_label="world.hle.agent",
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            run_as_user=None,
            log_dir="/tmp/logs",
            spec=SPEC,
        )
        assert parse_service_spec(text) == SPEC

    def test_the_spec_comment_is_one_line(self):
        """A multi-line stamp would break rc.d and systemd parsing alike."""
        assert "\n" not in (spec_comment(SPEC) or "")

    def test_no_spec_means_no_stamp(self):
        """Rendering stays exactly as it was when nothing is passed."""
        text = render_rc_script(
            label="agent",
            hle_path="/usr/local/bin/hle",
            run_args=build_agent_args(),
            run_as_user=None,
        )
        assert parse_service_spec(text) is None


class TestParseServiceSpec:
    def test_a_file_without_a_stamp_reads_as_none(self):
        assert parse_service_spec("#!/bin/sh\nname=hle_agent\n") is None

    def test_corrupt_json_reads_as_none_rather_than_raising(self):
        """An unreadable stamp must degrade to a plain restart, not a traceback."""
        assert parse_service_spec("# hle-spec: {not json") is None

    def test_a_non_object_stamp_is_rejected(self):
        assert parse_service_spec("# hle-spec: [1, 2]") is None


class TestRefreshService:
    def test_a_stamped_service_is_rebuilt_against_this_client(self):
        with (
            patch("hle_client.service_cmd.service_spec", return_value=SPEC),
            patch("hle_client.service_cmd.current_platform", return_value="freebsd"),
            patch("hle_client.service_cmd._rcd_install") as install,
        ):
            assert refresh_service("hle_agent", False) == "refreshed"
        # Rebuilt with the arguments it was installed with, stamped with the
        # version doing the rebuilding — not the one that wrote it.
        assert install.call_args.kwargs["run_args"] == ["agent", "run"]
        assert install.call_args.kwargs["spec"]["version"] == __version__

    def test_an_unstamped_service_falls_back_to_a_restart(self):
        """Nothing to rebuild from is a reason to restart, not to invent flags."""
        with (
            patch("hle_client.service_cmd.service_spec", return_value=None),
            patch("hle_client.service_cmd.restart_service", return_value=True) as restart,
        ):
            assert refresh_service("hle_agent", False) == "restarted"
        restart.assert_called_once_with("hle_agent", False)

    def test_a_failed_rebuild_reports_failure(self):
        with (
            patch("hle_client.service_cmd.service_spec", return_value=SPEC),
            patch("hle_client.service_cmd.current_platform", return_value="freebsd"),
            patch("hle_client.service_cmd._rcd_install", side_effect=HleError("boom")),
        ):
            assert refresh_service("hle_agent", False) == "failed"

    def test_a_failed_restart_reports_failure(self):
        with (
            patch("hle_client.service_cmd.service_spec", return_value=None),
            patch("hle_client.service_cmd.restart_service", return_value=False),
        ):
            assert refresh_service("hle_agent", False) == "failed"


# A tunnel stamp as written before TunnelSpec: argv only.
_OLD_TUNNEL_STAMP = {
    "version": "2609.1",
    "label": "ha",
    "run_args": ["expose", "--service", "http://localhost:8123", "--label", "ha"],
    "name": None,
    "run_as": None,
    "description": "HLE tunnel: ha",
    "restart": "on-failure",
    "agent_config": None,
    "user_mode": True,
}


class TestRefreshTunnelSpecStamps:
    def _refresh(self, stamp):
        with (
            patch("hle_client.service_cmd.service_spec", return_value=stamp),
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch("hle_client.service_cmd._systemd_install") as install,
        ):
            assert refresh_service("hle-ha.service", True) == "refreshed"
        return install.call_args.kwargs

    def test_an_old_stamp_is_rewritten_in_the_current_grammar(self):
        """Plan §2.6a: `daemon refresh` (run by `hle update`) moves old units off
        `expose`. The legacy argv is read back through the real parser and
        rebuilt, so the exec line becomes `tunnel create LABEL URL`."""
        kwargs = self._refresh(_OLD_TUNNEL_STAMP)
        assert kwargs["run_args"] == ["tunnel", "create", "ha", "http://localhost:8123"]
        assert kwargs["env"] == {}
        # The re-stamp gains the spec it was read back to, so the next refresh
        # does not have to parse argv at all.
        assert kwargs["spec"]["tunnel"]["service_url"] == "http://localhost:8123"

    def test_a_new_stamp_is_rebuilt_from_its_tunnel_spec(self):
        tunnel = TunnelSpec(
            label="ha",
            service_url="http://localhost:8123",
            upstream_basic_auth="u:p",
            response_timeout=90,
        )
        stamp = {
            **_OLD_TUNNEL_STAMP,
            # Deliberately stale argv: the spec wins.
            "run_args": ["expose", "--service", "http://old"],
            "tunnel": tunnel.model_dump(),
            "allow": ["a@x.com"],
        }
        kwargs = self._refresh(stamp)
        assert kwargs["run_args"] == [
            "tunnel",
            "create",
            "ha",
            "http://localhost:8123",
            "--response-timeout",
            "90",
            "--allow",
            "a@x.com",
        ]
        assert kwargs["env"] == {"HLE_UPSTREAM_BASIC_AUTH": "u:p"}
        # The re-stamp records the argv it was rebuilt to, and keeps the spec.
        assert kwargs["spec"]["run_args"] == kwargs["run_args"]
        assert kwargs["spec"]["tunnel"] == tunnel.model_dump()

    def test_an_unusable_tunnel_key_falls_back_to_the_argv(self):
        """The spec cannot be used, so the argv is read instead — and rebuilt."""
        for bad in ({"label": "ha"}, {"label": 5, "service_url": []}, "not a dict"):
            kwargs = self._refresh({**_OLD_TUNNEL_STAMP, "tunnel": bad})
            assert kwargs["run_args"] == ["tunnel", "create", "ha", "http://localhost:8123"]

    def test_an_argv_only_stamp_in_the_new_grammar_is_read_too(self):
        stamp = {
            **_OLD_TUNNEL_STAMP,
            "run_args": ["tunnel", "create", "ha", "http://localhost:8123", "--verify-ssl"],
        }
        kwargs = self._refresh(stamp)
        assert kwargs["run_args"] == [
            "tunnel",
            "create",
            "ha",
            "http://localhost:8123",
            "--verify-ssl",
        ]
        assert kwargs["spec"]["tunnel"]["verify_ssl"] is True

    def test_an_unreadable_legacy_argv_is_replayed_unchanged(self):
        """An --api-key on the command line cannot go into a TunnelSpec; rather
        than guess, the argv is kept as it was."""
        argv = ["expose", "--service", "http://x", "--label", "ha", "--api-key", "hle_x"]
        kwargs = self._refresh({**_OLD_TUNNEL_STAMP, "run_args": argv})
        assert kwargs["run_args"] == argv


class TestRefreshForwardStamps:
    def _refresh(self, run_args):
        stamp = {**_OLD_TUNNEL_STAMP, "label": "fp-rpi-22", "run_args": run_args}
        with (
            patch("hle_client.service_cmd.service_spec", return_value=stamp),
            patch("hle_client.service_cmd.current_platform", return_value="linux"),
            patch("hle_client.service_cmd._systemd_install") as install,
        ):
            assert refresh_service("hle-fp-rpi-22.service", True) == "refreshed"
        return install.call_args.kwargs

    def test_a_legacy_fp_unit_is_renamed_to_forward(self):
        """Every start of an `fp` unit wrote the rename note to its log."""
        kwargs = self._refresh(["fp", "--agent", "rpi", "--to", "22", "--port", "9922"])
        assert kwargs["run_args"] == ["forward", "--agent", "rpi", "--to", "22", "--port", "9922"]
        assert kwargs["spec"]["run_args"] == kwargs["run_args"]

    def test_a_forward_unit_is_left_as_it_is(self):
        argv = ["forward", "rpi", "22", "--port", "9922"]
        assert self._refresh(argv)["run_args"] == argv


class TestUnitsSilenceDeprecationNotes:
    """A unit may exec an old spelling until refreshed; its log is no place for
    advice nobody reads (audit: every `fp` unit start wrote the rename note)."""

    def test_systemd(self):
        text = render_unit(
            label="ha",
            hle_path="/usr/bin/hle",
            run_args=["tunnel", "create", "ha", "http://x"],
            user_mode=True,
            run_as_user=None,
        )
        assert "Environment=HLE_NO_DEPRECATION_WARNINGS=1" in text

    def test_launchd(self):
        text = render_launchd_plist(
            label="ha",
            plist_label="world.hle.ha",
            hle_path="/usr/local/bin/hle",
            run_args=["tunnel", "create", "ha", "http://x"],
            run_as_user=None,
            log_dir="/tmp",
        )
        assert "<key>HLE_NO_DEPRECATION_WARNINGS</key>" in text

    def test_rcd(self):
        text = render_rc_script(
            label="ha", hle_path="/usr/local/bin/hle", run_args=["tunnel", "create", "ha", "x"]
        )
        assert "HLE_NO_DEPRECATION_WARNINGS=1" in text
