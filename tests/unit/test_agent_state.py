"""The readiness marker behind ``hle agent status --ready``.

The chart's readinessProbe runs that command in a separate process, so the
running agent leaves a marker under ``HLE_HOME`` and the command checks that
it exists and names a live process. A stale marker must not count as ready.
"""

from __future__ import annotations

import os
import subprocess

from click.testing import CliRunner

from hle_client import agent_state
from hle_client.cli import main


def _dead_pid() -> int:
    """A pid that existed and has been reaped, so it is definitely gone."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class TestMarker:
    def test_round_trips(self, tmp_path):
        agent_state.write_connected(tmp_path, 1234)
        assert agent_state.read_pid(tmp_path) == 1234

    def test_missing_or_corrupt_reads_none(self, tmp_path):
        assert agent_state.read_pid(tmp_path) is None
        agent_state.marker_path(tmp_path).write_text("garbage")
        assert agent_state.read_pid(tmp_path) is None

    def test_no_temp_file_is_left_behind(self, tmp_path):
        agent_state.write_connected(tmp_path, 1)
        assert not (tmp_path / "connected.tmp").exists()

    def test_clear_removes_the_marker(self, tmp_path):
        agent_state.write_connected(tmp_path, 1)
        agent_state.clear(tmp_path)
        assert agent_state.read_pid(tmp_path) is None

    def test_clear_only_pid_leaves_another_process_marker(self, tmp_path):
        agent_state.write_connected(tmp_path, 4242)
        agent_state.clear(tmp_path, only_pid=1)
        assert agent_state.read_pid(tmp_path) == 4242
        agent_state.clear(tmp_path, only_pid=4242)
        assert agent_state.read_pid(tmp_path) is None


class TestPidAlive:
    def test_the_current_process_is_alive(self):
        assert agent_state.pid_alive(os.getpid()) is True

    def test_a_reaped_pid_is_not_alive(self):
        assert agent_state.pid_alive(_dead_pid()) is False

    def test_none_and_nonpositive_are_not_alive(self):
        assert agent_state.pid_alive(None) is False
        assert agent_state.pid_alive(0) is False


class TestIsReady:
    def test_live_marker_is_ready(self, tmp_path):
        agent_state.write_connected(tmp_path, os.getpid())
        assert agent_state.is_ready(tmp_path)[0]

    def test_dead_pid_is_not_ready(self, tmp_path):
        agent_state.write_connected(tmp_path, _dead_pid())
        ok, reason = agent_state.is_ready(tmp_path)
        assert not ok
        assert "not running" in reason

    def test_no_marker_is_not_ready(self, tmp_path):
        ok, reason = agent_state.is_ready(tmp_path)
        assert not ok
        assert "not connected" in reason


class TestClearIfStale:
    def test_a_marker_naming_our_own_pid_is_stale(self, tmp_path):
        agent_state.write_connected(tmp_path, os.getpid())
        agent_state.clear_if_stale(tmp_path, own_pid=os.getpid())
        assert agent_state.read_pid(tmp_path) is None

    def test_a_dead_pid_marker_is_stale(self, tmp_path):
        agent_state.write_connected(tmp_path, _dead_pid())
        agent_state.clear_if_stale(tmp_path, own_pid=os.getpid())
        assert agent_state.read_pid(tmp_path) is None

    def test_another_live_process_marker_is_kept(self, tmp_path):
        agent_state.write_connected(tmp_path, os.getppid())
        agent_state.clear_if_stale(tmp_path, own_pid=os.getpid())
        assert agent_state.read_pid(tmp_path) == os.getppid()


class TestReadyCommand:
    def _run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HLE_HOME", str(tmp_path))
        return CliRunner().invoke(main, ["agent", "status", "--ready"])

    def test_connected_live_agent_exits_zero(self, monkeypatch, tmp_path):
        agent_state.write_connected(tmp_path, os.getpid())
        result = self._run(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output

    def test_stale_marker_from_a_dead_process_exits_one(self, monkeypatch, tmp_path):
        agent_state.write_connected(tmp_path, _dead_pid())
        result = self._run(monkeypatch, tmp_path)
        assert result.exit_code == 1
        combined = result.output + (result.stderr or "")
        assert "not ready:" in combined
        assert "not running" in combined

    def test_no_marker_exits_one(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path)
        assert result.exit_code == 1
        assert "not ready:" in result.output + (result.stderr or "")
