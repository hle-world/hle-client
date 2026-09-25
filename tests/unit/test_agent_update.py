"""Self-update: layout, staging, atomic swap, rollback, and the agent's part in it.

Nothing here touches pip, PyPI or a real venv. Subprocesses are replaced by a
recorder that also lays down the files a real `python -m venv` + `pip install`
would, so the verification steps have something to look at.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hle_client import __version__, agent_update
from hle_client.agent import AgentClient
from hle_client.agent_update import (
    CURRENT_LINK,
    FAILED_FILE,
    LOCAL_REQUEST_ID,
    PREVIOUS_FILE,
    STATE_FILE,
    VERSIONS_DIR,
    BootCheck,
    FailedUpdate,
    RollbackError,
    StageError,
    SwapError,
    ToolUpdater,
    UpdateState,
    UpdateSupport,
    VersionedUpdater,
    boot_check,
    can_self_update,
    current_version,
    installer_owns,
    read_failed_marker,
    read_update_state,
    relink_launchers,
    run_update,
    versioned_layout_present,
    write_update_state,
)
from hle_common.agent_protocol import UpdateRequest

OLD = "2609.7"
NEW = "2609.9"
SUPPORTED_VENV = UpdateSupport(True, "venv")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Stands in for subprocess.run: records argv, fakes venv/pip/hle output.

    ``fail`` maps a substring of the command to (returncode, stderr) so one
    step can be made to break. ``reported_version`` is what the staged
    ``hle --version`` prints; ``None`` means "what was pip-installed".
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        self.reported_version: str | None = None
        self.raise_on: str | None = None

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.raise_on and self.raise_on in joined:
            raise OSError(f"cannot run {argv[0]}")
        for needle, (code, err) in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(argv, code, "", err)
        out = ""
        if argv[1:3] == ["-m", "venv"]:
            target = Path(argv[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "pyvenv.cfg").write_text("home = /usr/bin\n")
            (target / "bin" / "python").write_text("#!fake\n")
        elif argv[1:4] == ["-m", "pip", "install"]:
            spec = argv[-1]
            version = spec.split("==", 1)[1]
            target = Path(argv[0]).parent.parent
            (target / "bin" / "hle").write_text(f"#!fake {version}\n")
            (target / "installed").write_text(version)
        elif argv[-1] == "--version":
            exe = Path(argv[0])
            if self.reported_version is not None:
                out = f"hle, version {self.reported_version}\n"
            elif exe.exists():
                out = f"hle, version {exe.read_text().split()[-1]}\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


def make_home(tmp_path: Path, *, current: str | None = OLD, flat: bool = False) -> Path:
    home = tmp_path / "hle"
    versions = home / VERSIONS_DIR
    versions.mkdir(parents=True)
    if flat:
        (home / "venv" / "bin").mkdir(parents=True)
        (home / "venv" / "bin" / "hle").write_text(f"#!fake {OLD}\n")
    if current:
        (versions / current / "bin").mkdir(parents=True)
        (versions / current / "bin" / "hle").write_text(f"#!fake {current}\n")
        os.symlink(f"{VERSIONS_DIR}/{current}", home / CURRENT_LINK)
    return home


def updater_for(home: Path) -> tuple[VersionedUpdater, FakeRunner]:
    runner = FakeRunner()
    return VersionedUpdater(home, python="/fake/python", run=runner), runner


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #
class TestLayout:
    def test_hle_home_defaults_and_env_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HLE_HOME", raising=False)
        assert agent_update.hle_home() == Path.home() / ".local" / "share" / "hle"
        assert agent_update.hle_home({"HLE_HOME": str(tmp_path)}) == tmp_path

    def test_versioned_layout_present_needs_link_and_dir(self, tmp_path):
        assert not versioned_layout_present(tmp_path / "nope")
        home = make_home(tmp_path)
        assert versioned_layout_present(home)
        assert current_version(home) == OLD

    def test_installer_owns_flat_and_versioned_but_not_others(self, tmp_path):
        home = make_home(tmp_path, flat=True)
        assert installer_owns(str(home / "venv"), home)
        assert installer_owns(str(home / VERSIONS_DIR / OLD), home)
        assert not installer_owns(str(tmp_path / "elsewhere"), home)


class TestCanSelfUpdate:
    @pytest.mark.parametrize("method", ["pipx", "uv"])
    def test_tool_installs_are_supported(self, method, monkeypatch, tmp_path):
        monkeypatch.setattr("hle_client.update_cmd.detect_install_method", lambda *a, **k: method)
        s = can_self_update(prefix="/x", executable="/x/bin/python", home=tmp_path, in_docker=False)
        assert s == UpdateSupport(True, method)

    def test_installer_venv_is_supported(self, monkeypatch, tmp_path):
        home = make_home(tmp_path)
        monkeypatch.setattr("hle_client.update_cmd.detect_install_method", lambda *a, **k: "venv")
        prefix = str(home / VERSIONS_DIR / OLD)
        s = can_self_update(prefix=prefix, executable=prefix + "/bin/python", home=home)
        assert s.supported and s.method == "venv"

    def test_a_foreign_venv_is_not(self, monkeypatch, tmp_path):
        home = make_home(tmp_path)
        monkeypatch.setattr("hle_client.update_cmd.detect_install_method", lambda *a, **k: "venv")
        s = can_self_update(prefix=str(tmp_path / "other"), executable="p", home=home)
        assert s == UpdateSupport(False, "venv", "unsupported:venv")

    @pytest.mark.parametrize("method", ["brew", "pip", "externally-managed"])
    def test_everything_else_gives_the_protocol_reason(self, method, monkeypatch, tmp_path):
        monkeypatch.setattr("hle_client.update_cmd.detect_install_method", lambda *a, **k: method)
        s = can_self_update(prefix="/x", executable="/x/bin/python", home=tmp_path)
        assert s == UpdateSupport(False, method, f"unsupported:{method}")

    def test_docker_wins(self, tmp_path):
        s = can_self_update(prefix="/x", executable="p", home=tmp_path, in_docker=True)
        assert s.reason == "unsupported:docker"


# --------------------------------------------------------------------------- #
# VersionedUpdater
# --------------------------------------------------------------------------- #
class TestStage:
    def test_creates_venv_installs_pinned_from_pypi_only(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        target = up.stage(NEW)
        assert target == home / VERSIONS_DIR / NEW
        assert runner.calls[0][:3] == ["/fake/python", "-m", "venv"]
        pip = runner.calls[1]
        assert pip[0] == str(target / "bin" / "python")
        assert pip[-1] == f"hle-client=={NEW}"
        assert "--no-cache-dir" in pip
        assert not any(a.startswith("--index-url") or a == "-i" for a in pip)

    def test_verify_checks_version_and_imports(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        up.stage(NEW)
        up.verify(NEW)
        assert runner.calls[-2][-1] == "--version"
        assert runner.calls[-1][-2:] == ["-c", "import hle_client, hle_common"]
        assert (home / VERSIONS_DIR / NEW).is_dir()

    def test_pip_failure_removes_the_staged_dir(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        runner.fail["pip install"] = (1, "ERROR: No matching distribution found")
        with pytest.raises(StageError, match="No matching distribution"):
            up.stage(NEW)
        assert not (home / VERSIONS_DIR / NEW).exists()

    def test_wrong_reported_version_removes_the_staged_dir(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        up.stage(NEW)
        runner.reported_version = OLD  # a stale index gave us the old one
        with pytest.raises(StageError, match=f"not {NEW}"):
            up.verify(NEW)
        assert not (home / VERSIONS_DIR / NEW).exists()

    def test_import_failure_removes_the_staged_dir(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        up.stage(NEW)
        runner.fail["import hle_client"] = (1, "ModuleNotFoundError: hle_common")
        with pytest.raises(StageError, match="import check"):
            up.verify(NEW)
        assert not (home / VERSIONS_DIR / NEW).exists()

    def test_missing_interpreter_is_a_stage_error(self, tmp_path):
        home = make_home(tmp_path)
        up, runner = updater_for(home)
        runner.raise_on = "-m venv"
        with pytest.raises(StageError, match="create venv"):
            up.stage(NEW)

    def test_a_leftover_from_a_failed_stage_is_replaced(self, tmp_path):
        home = make_home(tmp_path)
        leftover = home / VERSIONS_DIR / NEW / "junk"
        leftover.mkdir(parents=True)
        up, _ = updater_for(home)
        up.stage(NEW)
        assert not leftover.exists()

    def test_refuses_to_restage_the_current_version(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        with pytest.raises(StageError, match="already the current"):
            up.stage(OLD)
        assert (home / VERSIONS_DIR / OLD / "bin" / "hle").exists()  # untouched


class TestSwapAndRollback:
    def test_swap_repoints_current_atomically_and_records_previous(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        up.stage(NEW)
        up.swap(NEW, from_version=OLD)
        link = home / CURRENT_LINK
        assert link.is_symlink()
        assert os.readlink(link) == f"{VERSIONS_DIR}/{NEW}"
        assert current_version(home) == NEW
        assert (home / PREVIOUS_FILE).read_text().strip() == OLD
        assert not (home / (CURRENT_LINK + ".tmp")).exists()
        # The old tree is still there for the rollback.
        assert (home / VERSIONS_DIR / OLD / "bin" / "hle").exists()

    def test_swap_refuses_an_unstaged_version(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        with pytest.raises(SwapError, match="not staged"):
            up.swap(NEW, from_version=OLD)
        assert current_version(home) == OLD

    def test_rollback_repoints_at_previous(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        up.stage(NEW)
        up.swap(NEW, from_version=OLD)
        assert up.rollback() == OLD
        assert current_version(home) == OLD

    def test_rollback_without_a_previous_is_an_error(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        with pytest.raises(RollbackError, match="no previous"):
            up.rollback()

    def test_rollback_to_a_deleted_previous_is_an_error(self, tmp_path):
        home = make_home(tmp_path)
        up, _ = updater_for(home)
        (home / PREVIOUS_FILE).write_text("2600.1\n")
        with pytest.raises(RollbackError, match="gone"):
            up.rollback()

    def test_first_swap_from_a_flat_venv_migrates_the_layout(self, tmp_path, monkeypatch):
        """The running flat venv becomes versions/<old> so a rollback has a target."""
        home = make_home(tmp_path, current=None, flat=True)
        launcher_dir = tmp_path / "bin"
        launcher_dir.mkdir()
        launcher = launcher_dir / "hle"
        os.symlink(home / "venv" / "bin" / "hle", launcher)
        monkeypatch.setattr(agent_update, "LAUNCHER_PATHS", (str(launcher),))

        up, _ = updater_for(home)
        up.stage(NEW)
        up.swap(NEW, from_version=OLD)

        assert current_version(home) == NEW
        old_dir = home / VERSIONS_DIR / OLD
        assert old_dir.is_symlink()
        assert (old_dir / "bin" / "hle").exists()
        assert (home / PREVIOUS_FILE).read_text().strip() == OLD
        # The launcher the installer wrote now follows `current`.
        assert os.readlink(launcher) == str(home / CURRENT_LINK / "bin" / "hle")
        assert up.rollback() == OLD
        assert (home / CURRENT_LINK / "bin" / "hle").read_text().endswith(f"{OLD}\n")


class TestRelinkLaunchers:
    def test_only_symlinks_into_home_are_touched(self, tmp_path):
        home = make_home(tmp_path)
        ours = tmp_path / "ours"
        theirs = tmp_path / "theirs"
        plain = tmp_path / "plain"
        os.symlink(home / VERSIONS_DIR / OLD / "bin" / "hle", ours)
        os.symlink(tmp_path / "elsewhere" / "hle", theirs)
        plain.write_text("#!/bin/sh\n")
        done = relink_launchers(home, (str(ours), str(theirs), str(plain), "/nonexistent/hle"))
        assert done == [ours]
        assert os.readlink(ours) == str(home / CURRENT_LINK / "bin" / "hle")
        assert os.readlink(theirs) == str(tmp_path / "elsewhere" / "hle")
        assert plain.read_text() == "#!/bin/sh\n"


# --------------------------------------------------------------------------- #
# ToolUpdater (pipx / uv)
# --------------------------------------------------------------------------- #
class TestToolUpdater:
    def _make(self, tmp_path, tool="pipx", status=200):
        runner = FakeRunner()
        fetched: list[str] = []

        def fetch(url: str, timeout: float) -> int:
            fetched.append(url)
            return status

        hle = tmp_path / "venv" / "bin" / "hle"
        hle.parent.mkdir(parents=True)
        hle.write_text(f"#!fake {OLD}\n")
        up = ToolUpdater(tool, tmp_path / "home", run=runner, fetch=fetch, hle_path=str(hle))
        return up, runner, fetched, hle

    def test_stage_is_a_dry_pypi_check(self, tmp_path):
        up, runner, fetched, _ = self._make(tmp_path)
        up.stage(NEW)
        assert fetched == [f"https://pypi.org/pypi/hle-client/{NEW}/json"]
        assert runner.calls == []

    def test_stage_fails_for_an_unknown_version(self, tmp_path):
        up, *_ = self._make(tmp_path, status=404)
        with pytest.raises(StageError, match="not on PyPI"):
            up.stage("0.0.0")

    def test_stage_fails_when_pypi_is_unreachable(self, tmp_path):
        up, *_ = self._make(tmp_path)
        up._fetch = lambda url, t: (_ for _ in ()).throw(ConnectionError("dns"))
        with pytest.raises(StageError, match="unreachable"):
            up.stage(NEW)

    @pytest.mark.parametrize(
        ("tool", "head"),
        [("pipx", ["pipx", "install", "--force"]), ("uv", ["uv", "tool", "install", "--force"])],
    )
    def test_swap_is_the_pinned_forced_install(self, tmp_path, tool, head):
        up, runner, _, hle = self._make(tmp_path, tool=tool)
        runner.reported_version = NEW
        up.swap(NEW, from_version=OLD)
        assert runner.calls[0] == [*head, f"hle-client=={NEW}"]
        assert runner.calls[1] == [str(hle), "--version"]
        assert (tmp_path / "home" / PREVIOUS_FILE).read_text().strip() == OLD

    def test_swap_that_does_not_stick_rolls_back_at_once(self, tmp_path):
        up, runner, _, _ = self._make(tmp_path)
        runner.reported_version = OLD  # index behind PyPI: tool exits 0, nothing changed
        with pytest.raises(SwapError, match="rolled back"):
            up.swap(NEW, from_version=OLD)
        assert runner.calls[-1] == ["pipx", "install", "--force", f"hle-client=={OLD}"]

    def test_rollback_reinstalls_the_previous_pin(self, tmp_path):
        up, runner, _, _ = self._make(tmp_path, tool="uv")
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / PREVIOUS_FILE).write_text(f"{OLD}\n")
        assert up.rollback() == OLD
        assert runner.calls == [["uv", "tool", "install", "--force", f"hle-client=={OLD}"]]

    def test_failed_reinstall_is_a_rollback_error(self, tmp_path):
        up, runner, _, _ = self._make(tmp_path)
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / PREVIOUS_FILE).write_text(f"{OLD}\n")
        runner.fail["pipx install"] = (1, "network down")
        with pytest.raises(RollbackError, match="network down"):
            up.rollback()


# --------------------------------------------------------------------------- #
# State files and the boot decision
# --------------------------------------------------------------------------- #
class TestStateFiles:
    def test_state_round_trips_with_the_wire_spelling(self, tmp_path):
        state = UpdateState("r1", OLD, NEW, 1000.0)
        write_update_state(tmp_path, state)
        raw = json.loads((tmp_path / STATE_FILE).read_text())
        assert raw == {
            "request_id": "r1",
            "from": OLD,
            "to": NEW,
            "started_at": 1000.0,
            "phase": "restarting",
        }
        assert read_update_state(tmp_path) == state

    def test_failed_marker_round_trips(self, tmp_path):
        failed = FailedUpdate(UpdateState("r1", OLD, NEW, 1.0), "timeout", ["a", "b"])
        agent_update.write_failed_marker(tmp_path, failed)
        assert read_failed_marker(tmp_path) == failed

    def test_garbage_reads_as_nothing(self, tmp_path):
        (tmp_path / STATE_FILE).write_text("{not json")
        (tmp_path / FAILED_FILE).write_text("[]")
        assert read_update_state(tmp_path) is None
        assert read_failed_marker(tmp_path) is None


class TestBootCheck:
    def test_nothing_pending(self, tmp_path):
        assert boot_check(tmp_path, NEW).kind == "none"

    def test_the_updated_process_watches(self, tmp_path):
        write_update_state(tmp_path, UpdateState("r1", OLD, NEW, time.time()))
        check = boot_check(tmp_path, NEW)
        assert check.kind == "watch"
        assert check.state is not None and check.state.request_id == "r1"
        assert (tmp_path / STATE_FILE).exists()  # cleared only once decided

    def test_the_wrong_version_coming_up_is_a_failure(self, tmp_path):
        write_update_state(tmp_path, UpdateState("r1", OLD, NEW, time.time()))
        check = boot_check(tmp_path, OLD)
        assert check.kind == "report_failed"
        assert check.reason is not None and f"expected {NEW}" in check.reason

    def test_a_failed_marker_is_reported_and_supersedes_the_state(self, tmp_path):
        write_update_state(tmp_path, UpdateState("r1", OLD, NEW, time.time()))
        agent_update.write_failed_marker(
            tmp_path, FailedUpdate(UpdateState("r1", OLD, NEW, 1.0), "boom", ["l1"])
        )
        check = boot_check(tmp_path, OLD)
        assert check.kind == "report_failed"
        assert check.reason == "boom"
        assert check.log_tail == ["l1"]
        assert not (tmp_path / STATE_FILE).exists()

    def test_a_stale_state_is_discarded(self, tmp_path):
        write_update_state(tmp_path, UpdateState("r1", OLD, NEW, 0.0))
        check = boot_check(tmp_path, NEW, now=agent_update.STALE_AFTER_S + 1)
        assert check.kind == "none"
        assert not (tmp_path / STATE_FILE).exists()


class TestHealthTimeout:
    def test_default_and_env(self):
        assert agent_update.health_timeout({}) == 90.0
        assert agent_update.health_timeout({"HLE_UPDATE_HEALTH_TIMEOUT": "12"}) == 12.0
        assert agent_update.health_timeout({"HLE_UPDATE_HEALTH_TIMEOUT": "nope"}) == 90.0
        assert agent_update.health_timeout({"HLE_UPDATE_HEALTH_TIMEOUT": "-1"}) == 90.0


class TestLogBuffer:
    def test_tail_keeps_the_last_lines(self):
        import logging

        agent_update.ensure_log_buffer()
        log = logging.getLogger("hle_client.test_ring")
        for i in range(60):
            log.warning("line %d", i)
        tail = agent_update.log_tail(50)
        assert len(tail) == 50
        assert tail[-1].endswith("line 59")
        assert "line 9" not in tail[0]


# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #
class RecordingUpdater:
    def __init__(self, fail_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if name == self.fail_at:
            raise StageError(f"{name} broke")

    def stage(self, version: str) -> Path:
        self._step("stage")
        return Path("/x")

    def verify(self, version: str) -> None:
        self._step("verify")

    def swap(self, version: str, *, from_version: str) -> None:
        self._step("swap")

    def rollback(self) -> str:
        self._step("rollback")
        return OLD


class TestRunUpdate:
    async def test_phases_in_order_then_state_file(self, tmp_path):
        phases: list[tuple[str, str | None]] = []

        async def send(p):
            phases.append((p.phase, p.detail))

        up = RecordingUpdater()
        req = UpdateRequest(request_id="r1", target_version=NEW)
        state = await run_update(
            req, updater=up, home=tmp_path, from_version=OLD, send_progress=send, now=lambda: 5.0
        )
        assert [p for p, _ in phases] == ["staging", "verifying", "swapping", "restarting"]
        assert phases[0][1] == f"hle-client=={NEW}"
        assert up.calls == ["stage", "verify", "swap"]
        assert state == UpdateState("r1", OLD, NEW, 5.0)
        assert read_update_state(tmp_path) == state

    async def test_a_failed_stage_leaves_no_state_file(self, tmp_path):
        async def send(p):
            return None

        up = RecordingUpdater(fail_at="verify")
        req = UpdateRequest(request_id="r1", target_version=NEW)
        with pytest.raises(StageError, match="verify broke"):
            await run_update(req, updater=up, home=tmp_path, from_version=OLD, send_progress=send)
        assert "swap" not in up.calls
        assert read_update_state(tmp_path) is None


# --------------------------------------------------------------------------- #
# The agent's side: ack / progress / restart, watchdog, failure reporting
# --------------------------------------------------------------------------- #
class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def of_type(self, mtype: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == mtype]


class FakeTunnel:
    def __init__(self, config) -> None:
        self.config = config
        self.connected = False
        self.active_ws_streams = 0

    async def connect(self) -> None:
        self.connected = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connected = False
            raise

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def public_url(self) -> str | None:
        return f"https://{self.config.service_label}.hle.world"


def make_agent(
    tmp_path: Path,
    *,
    support: UpdateSupport = SUPPORTED_VENV,
    updater: RecordingUpdater | None = None,
    health_timeout: float = 0.2,
) -> tuple[AgentClient, RecordingUpdater, list[FakeTunnel]]:
    tunnels: list[FakeTunnel] = []
    up = updater or RecordingUpdater()

    def factory(cfg):
        t = FakeTunnel(cfg)
        tunnels.append(t)
        return t

    client = AgentClient(
        "hlea_test",
        tunnel_factory=factory,
        home=tmp_path,
        support_probe=lambda: support,
        updater_factory=lambda s: up,
        health_timeout=health_timeout,
    )
    return client, up, tunnels


def request(**kw) -> str:
    base = {"type": "update_request", "request_id": "r1", "target_version": NEW}
    base.update(kw)
    return json.dumps(base)


async def settle(client: AgentClient) -> None:
    if client._update_task is not None:
        await client._update_task


class TestAckAndProgress:
    async def test_accepts_then_streams_phases_then_restarts(self, tmp_path):
        client, up, _ = make_agent(tmp_path)
        ws = FakeWs()
        await client._handle_message(request(), ws)
        ack = ws.of_type("update_ack")[0]
        assert ack == {"type": "update_ack", "request_id": "r1", "accepted": True, "reason": None}
        await settle(client)
        assert [p["phase"] for p in ws.of_type("update_progress")] == [
            "staging",
            "verifying",
            "swapping",
            "restarting",
        ]
        assert up.calls == ["stage", "verify", "swap"]
        assert ws.closed
        assert client.exit_code == 1
        assert client._running is False
        assert read_update_state(tmp_path) == UpdateState(
            "r1", __version__, NEW, read_update_state(tmp_path).started_at
        )
        assert ws.of_type("update_result") == []  # the successor reports

    async def test_a_failed_stage_reports_a_result_and_keeps_running(self, tmp_path):
        client, up, _ = make_agent(tmp_path, updater=RecordingUpdater(fail_at="stage"))
        ws = FakeWs()
        await client._handle_message(request(), ws)
        await settle(client)
        result = ws.of_type("update_result")[0]
        assert result["ok"] is False
        assert result["request_id"] == "r1"
        assert result["from_version"] == __version__
        assert result["to_version"] == NEW
        assert isinstance(result["log_tail"], list)
        assert not ws.closed
        assert client.exit_code == 0
        assert read_update_state(tmp_path) is None

    @pytest.mark.parametrize(
        ("support", "reason"),
        [
            (UpdateSupport(False, "brew", "unsupported:brew"), "unsupported:brew"),
            (UpdateSupport(False, "docker", "unsupported:docker"), "unsupported:docker"),
            (UpdateSupport(False, "pip", "unsupported:pip"), "unsupported:pip"),
        ],
    )
    async def test_unsupported_installs_refuse_with_the_protocol_reason(
        self, tmp_path, support, reason
    ):
        client, up, _ = make_agent(tmp_path, support=support)
        ws = FakeWs()
        await client._handle_message(request(), ws)
        ack = ws.of_type("update_ack")[0]
        assert ack["accepted"] is False
        assert ack["reason"] == reason
        assert up.calls == []
        assert client._update_task is None

    async def test_same_version_is_refused(self, tmp_path):
        client, up, _ = make_agent(tmp_path)
        ws = FakeWs()
        await client._handle_message(request(target_version=__version__), ws)
        assert ws.of_type("update_ack")[0]["reason"] == "unsupported:same-version"

    async def test_busy_draining_when_streams_are_open_and_policy_is_wait(self, tmp_path):
        from hle_common.agent_protocol import EndpointSpec

        client, up, tunnels = make_agent(tmp_path)
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        tunnels[0].active_ws_streams = 3
        ws = FakeWs()
        await client._handle_message(request(), ws)
        assert ws.of_type("update_ack")[0]["reason"] == "busy:draining"
        assert up.calls == []
        # `force` goes ahead regardless.
        await client._handle_message(request(request_id="r2", drain_policy="force"), ws)
        assert ws.of_type("update_ack")[1] == {
            "type": "update_ack",
            "request_id": "r2",
            "accepted": True,
            "reason": None,
        }
        await settle(client)
        await client._stop_all()

    async def test_a_second_request_while_one_runs_is_busy_updating(self, tmp_path):
        class Slow(RecordingUpdater):
            def stage(self, version: str) -> Path:
                time.sleep(0.05)
                return super().stage(version)

        client, up, _ = make_agent(tmp_path, updater=Slow())
        ws = FakeWs()
        await client._handle_message(request(), ws)
        await asyncio.sleep(0.01)
        await client._handle_message(request(request_id="r2"), ws)
        acks = ws.of_type("update_ack")
        assert acks[1]["request_id"] == "r2"
        assert acks[1]["reason"] == "busy:updating"
        await settle(client)

    async def test_a_malformed_request_is_ignored(self, tmp_path):
        client, _, _ = make_agent(tmp_path)
        ws = FakeWs()
        await client._handle_message(json.dumps({"type": "update_request", "request_id": "r"}), ws)
        assert ws.sent == []


class TestWatchdog:
    """The updated process must prove itself or put the old version back."""

    def _armed(self, tmp_path, **kw) -> tuple[AgentClient, RecordingUpdater, list[FakeTunnel]]:
        write_update_state(tmp_path, UpdateState("r1", OLD, __version__, time.time()))
        client, up, tunnels = make_agent(tmp_path, **kw)
        client._arm_watchdog()
        assert client._boot.kind == "watch"
        return client, up, tunnels

    async def test_healthy_reports_ok_and_clears_the_state(self, tmp_path):
        from hle_common.agent_protocol import EndpointSpec

        client, up, tunnels = self._armed(tmp_path, health_timeout=5.0)
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        ws = FakeWs()
        await client._after_welcome(ws)
        for _ in range(50):
            if ws.of_type("update_result"):
                break
            await asyncio.sleep(0.02)
        result = ws.of_type("update_result")[0]
        assert result["ok"] is True
        assert (result["from_version"], result["to_version"]) == (OLD, __version__)
        assert result["request_id"] == "r1"
        assert isinstance(result["log_tail"], list)
        assert read_update_state(tmp_path) is None
        assert client._boot.kind == "none"
        assert up.calls == []
        assert client.exit_code == 0
        client._disarm_watchdog()
        await client._stop_all()

    async def test_no_endpoints_is_healthy_at_welcome(self, tmp_path):
        client, _, _ = self._armed(tmp_path, health_timeout=5.0)
        ws = FakeWs()
        await client._after_welcome(ws)
        for _ in range(50):
            if ws.of_type("update_result"):
                break
            await asyncio.sleep(0.02)
        assert ws.of_type("update_result")[0]["ok"] is True
        client._disarm_watchdog()

    async def test_timeout_before_health_rolls_back_and_exits(self, tmp_path):
        from hle_common.agent_protocol import EndpointSpec

        client, up, tunnels = self._armed(tmp_path, health_timeout=0.05)
        await client.reconcile([EndpointSpec(id=1, label="ha", service_url="http://x")])
        await asyncio.sleep(0)
        tunnels[0].connected = False  # never comes up
        ws = FakeWs()
        client._ws = ws
        await client._after_welcome(ws)
        await asyncio.sleep(0.2)
        assert up.calls == ["rollback"]
        assert client.exit_code == 1
        assert client._running is False
        assert ws.closed
        assert read_update_state(tmp_path) is None
        failed = read_failed_marker(tmp_path)
        assert failed is not None
        assert "not healthy within" in failed.reason
        assert failed.state.request_id == "r1"
        assert ws.of_type("update_result") == []  # the old version reports after relaunch
        if client._health_task:
            client._health_task.cancel()
        await client._stop_all()

    async def test_timeout_with_no_connection_still_rolls_back(self, tmp_path):
        client, up, _ = self._armed(tmp_path, health_timeout=0.05)
        await asyncio.sleep(0.15)
        assert up.calls == ["rollback"]
        assert client.exit_code == 1
        assert read_failed_marker(tmp_path) is not None

    async def test_a_failed_rollback_is_recorded_and_still_exits(self, tmp_path):
        class NoRollback(RecordingUpdater):
            def rollback(self) -> str:
                raise RollbackError("previous is gone")

        client, _, _ = self._armed(tmp_path, updater=NoRollback(), health_timeout=0.05)
        await asyncio.sleep(0.15)
        failed = read_failed_marker(tmp_path)
        assert failed is not None
        assert "rollback failed: previous is gone" in failed.reason
        assert client.exit_code == 1

    async def test_a_fatal_close_during_the_watch_rolls_back(self, tmp_path, monkeypatch):
        import websockets.exceptions
        from websockets.frames import Close

        from hle_common import close_codes

        client, up, _ = self._armed(tmp_path, health_timeout=60.0)

        async def refused() -> None:
            raise websockets.exceptions.ConnectionClosed(
                Close(close_codes.INVALID_CREDENTIAL, "revoked"), None
            )

        monkeypatch.setattr(client, "_connect_once", refused)
        await client.run()
        assert up.calls == ["rollback"]
        assert client.exit_code == 1
        failed = read_failed_marker(tmp_path)
        assert failed is not None and "relay refused" in failed.reason

    async def test_a_rollback_during_reconnect_backoff_exits_at_once(self, tmp_path, monkeypatch):
        """The relay is unreachable, so run() sits in a long back-off; the
        watchdog firing must end it now, not after the back-off."""
        client, up, _ = self._armed(tmp_path, health_timeout=0.05)
        client._reconnect_delay = 30.0

        async def unreachable() -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(client, "_connect_once", unreachable)
        await asyncio.wait_for(client.run(), timeout=2.0)
        assert up.calls == ["rollback"]
        assert client.exit_code == 1
        assert read_failed_marker(tmp_path) is not None

    async def test_a_new_request_during_the_watch_is_busy(self, tmp_path):
        client, _, _ = self._armed(tmp_path, health_timeout=60.0)
        ws = FakeWs()
        await client._handle_message(request(request_id="r2"), ws)
        assert ws.of_type("update_ack")[0]["reason"] == "busy:updating"
        client._disarm_watchdog()

    async def test_local_updates_are_watched_but_not_reported(self, tmp_path):
        write_update_state(tmp_path, UpdateState(LOCAL_REQUEST_ID, OLD, __version__, time.time()))
        client, _, _ = make_agent(tmp_path, health_timeout=5.0)
        client._arm_watchdog()
        ws = FakeWs()
        await client._after_welcome(ws)
        for _ in range(50):
            if read_update_state(tmp_path) is None:
                break
            await asyncio.sleep(0.02)
        assert read_update_state(tmp_path) is None
        assert ws.of_type("update_result") == []
        client._disarm_watchdog()


class TestFailedMarkerReporting:
    """The old version, relaunched after a rollback, tells the server what happened."""

    async def test_reports_not_ok_with_the_failed_process_log_tail(self, tmp_path):
        agent_update.write_failed_marker(
            tmp_path,
            FailedUpdate(UpdateState("r1", __version__, NEW, 1.0), "not healthy", ["x", "y"]),
        )
        client, up, _ = make_agent(tmp_path)
        client._arm_watchdog()
        assert client._boot.kind == "report_failed"
        assert client._watchdog_task is None
        ws = FakeWs()
        await client._after_welcome(ws)
        result = ws.of_type("update_result")[0]
        assert result == {
            "type": "update_result",
            "request_id": "r1",
            "ok": False,
            "from_version": __version__,
            "to_version": NEW,
            "log_tail": ["x", "y"],
        }
        assert read_failed_marker(tmp_path) is None
        assert client._boot.kind == "none"
        assert up.calls == []

    async def test_wrong_version_at_boot_is_reported_as_failed(self, tmp_path):
        write_update_state(tmp_path, UpdateState("r1", OLD, NEW, time.time()))
        client, _, _ = make_agent(tmp_path)
        client._arm_watchdog()
        assert client._boot.kind == "report_failed"
        ws = FakeWs()
        await client._after_welcome(ws)
        result = ws.of_type("update_result")[0]
        assert result["ok"] is False
        assert result["to_version"] == NEW
        assert read_update_state(tmp_path) is None

    async def test_wrong_version_at_boot_repoints_current_back(self, tmp_path):
        """The unit relaunched the old binary, not `current`: undo the swap."""
        home = tmp_path / "hle"
        for v in (__version__, NEW):
            (home / VERSIONS_DIR / v / "bin").mkdir(parents=True)
        os.symlink(f"{VERSIONS_DIR}/{NEW}", home / CURRENT_LINK)
        (home / PREVIOUS_FILE).write_text(f"{__version__}\n")
        write_update_state(home, UpdateState("r1", __version__, NEW, time.time()))
        client, _, _ = make_agent(home)
        client._arm_watchdog()
        assert client._boot.kind == "report_failed"
        assert current_version(home) == __version__

    async def test_hello_capability_follows_support(self, tmp_path):
        client, _, _ = make_agent(
            tmp_path, support=UpdateSupport(False, "brew", "unsupported:brew")
        )
        assert client._refuse_update_reason(UpdateRequest(request_id="r", target_version=NEW)) == (
            "unsupported:brew"
        )

    def test_boot_check_type_is_exported(self):
        assert BootCheck("none").kind == "none"
