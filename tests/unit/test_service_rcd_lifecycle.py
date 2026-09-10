"""Starting and restarting an rc.d service, on a box you reach through it.

Two hazards live here, both discovered on a pfSense firewall.

A restart run in the operator's own shell dies with that shell — and on a
firewall the shell is usually reached *through* the agent being restarted, so
the stop lands, the start never does, and the machine is now unreachable.

And ``service ... start`` exits 0 once ``daemon(8)`` has forked, whether or not
the supervised process can run at all, so a broken install reported success and
a healthy pid while writing nothing to its log.
"""

from __future__ import annotations

from unittest.mock import patch

from hle_client.service_cmd import _rcd_settles, restart_service, service_log_tail


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class TestRestartIsDetached:
    def test_freebsd_restart_goes_through_daemon(self):
        """Not `service restart` directly: that dies with the caller's session."""
        with (
            patch("hle_client.service_cmd.current_platform", return_value="freebsd"),
            patch("hle_client.service_cmd.subprocess.run", return_value=_Result(0)) as run,
            patch("hle_client.service_cmd._rcd_settles", return_value=True),
        ):
            assert restart_service("hle_agent", False) is True
        argv = run.call_args.args[0]
        assert argv[:2] == ["/usr/sbin/daemon", "-f"]
        assert argv[-3:] == ["service", "hle_agent", "restart"]

    def test_restart_reports_failure_when_the_service_does_not_settle(self):
        with (
            patch("hle_client.service_cmd.current_platform", return_value="freebsd"),
            patch("hle_client.service_cmd.subprocess.run", return_value=_Result(0)),
            patch("hle_client.service_cmd._rcd_settles", return_value=False),
        ):
            assert restart_service("hle_agent", False) is False

    def test_a_box_without_daemon_still_gets_restarted(self):
        """daemon(8) is base-system FreeBSD, but never assume it is there."""
        with (
            patch("hle_client.service_cmd.current_platform", return_value="freebsd"),
            patch("hle_client.service_cmd.subprocess.run", return_value=_Result(127)),
            patch("hle_client.service_cmd._service_cmd", return_value=_Result(0)) as direct,
        ):
            assert restart_service("hle_agent", False) is True
        direct.assert_called_once_with("hle_agent", "restart")


class TestSettles:
    def test_a_service_that_exits_immediately_does_not_settle(self):
        with (
            patch("hle_client.service_cmd._rcd_running", return_value=False),
            patch("hle_client.service_cmd.time.sleep"),
        ):
            assert _rcd_settles("hle_agent", timeout=2.0) is False

    def test_a_service_still_up_after_the_wait_settles(self):
        with (
            patch("hle_client.service_cmd._rcd_running", return_value=True),
            patch("hle_client.service_cmd.time.sleep"),
        ):
            assert _rcd_settles("hle_agent", timeout=2.0) is True

    def test_a_service_that_dies_partway_through_does_not_settle(self):
        """The respawn loop looks healthy at any single instant; look twice."""
        with (
            patch("hle_client.service_cmd._rcd_running", side_effect=[True, False]),
            patch("hle_client.service_cmd.time.sleep"),
        ):
            assert _rcd_settles("hle_agent", timeout=3.0) is False


class TestServiceLogTail:
    def test_missing_log_reads_as_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hle_client.service_cmd.Path", lambda _: tmp_path / "nope")
        assert service_log_tail("hle_agent") == ""

    def test_the_last_lines_come_back(self, tmp_path, monkeypatch):
        log = tmp_path / "hle_agent.log"
        log.write_text("\n".join(f"line {i}" for i in range(40)))
        monkeypatch.setattr("hle_client.service_cmd.Path", lambda _: tmp_path)
        tail = service_log_tail("hle_agent", lines=3)
        assert tail.splitlines() == ["line 37", "line 38", "line 39"]
