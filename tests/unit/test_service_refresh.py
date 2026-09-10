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
from hle_client.service_cmd import (
    build_agent_args,
    parse_service_spec,
    refresh_service,
    render_launchd_plist,
    render_rc_script,
    render_unit,
    spec_comment,
)

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
            patch("hle_client.service_cmd._rcd_install", side_effect=SystemExit(1)),
        ):
            assert refresh_service("hle_agent", False) == "failed"

    def test_a_failed_restart_reports_failure(self):
        with (
            patch("hle_client.service_cmd.service_spec", return_value=None),
            patch("hle_client.service_cmd.restart_service", return_value=False),
        ):
            assert refresh_service("hle_agent", False) == "failed"
