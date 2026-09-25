"""Agent self-update: staged install, atomic swap, watchdog rollback (Stage A).

The dashboard asks an agent to move to a version; the agent installs that
version *next to* the running one, checks it, repoints one symlink, and exits
so the service manager relaunches it. The new process then has a fixed time
to register and bring every endpoint up. If it does not, it repoints the
symlink back and exits, and the previous version comes back on the next
relaunch and reports what happened.

Layout (``HLE_HOME``, default ``~/.local/share/hle``)::

    versions/<v>/        one venv per installed version
    current -> versions/<v>
    previous             the version `current` pointed at before the last swap
    update.json          an update in flight, read by the new process at start
    update.json.failed   a rolled-back update, read by the old process at start

Everything here is plain functions and one coroutine; no click, so the agent
process does not pay for the CLI at import time. Subprocess and network calls
go through injectable hooks so the tests never touch pip or PyPI.

pipx and uv own their venvs, so there is no side-by-side install for them:
see :class:`ToolUpdater` for the (larger) window that leaves open.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from hle_common.agent_protocol import UpdateProgress, UpdateRequest

logger = logging.getLogger(__name__)

_PACKAGE = "hle-client"

HOME_ENV = "HLE_HOME"
HEALTH_TIMEOUT_ENV = "HLE_UPDATE_HEALTH_TIMEOUT"
DEFAULT_HEALTH_TIMEOUT = 90.0
# An update.json older than this is not an update in flight, it is a leftover:
# `hle update` on a machine with no agent service writes one that nothing
# consumes. Rolling back a week later because of it would be absurd.
STALE_AFTER_S = 3600.0

# `request_id` used when `hle update` (the CLI, not the dashboard) drives the
# same machinery. The watchdog still runs; the result is logged, not sent,
# because no server is waiting on it.
LOCAL_REQUEST_ID = "local"

STATE_FILE = "update.json"
FAILED_FILE = "update.json.failed"
PREVIOUS_FILE = "previous"
VERSIONS_DIR = "versions"
CURRENT_LINK = "current"
FLAT_VENV_DIR = "venv"
# Launchers the installer may have written, all of which must follow `current`
# once the versioned layout exists.
LAUNCHER_PATHS = ("~/.local/bin/hle", "/usr/local/bin/hle", "/usr/bin/hle")

Phase = Literal["staging", "verifying", "swapping", "restarting"]


class UpdateError(Exception):
    """Base for everything that can go wrong while updating."""


class StageError(UpdateError):
    """The new version could not be installed or did not pass its checks."""


class SwapError(UpdateError):
    """The new version is installed but could not be made current."""


class RollbackError(UpdateError):
    """The previous version could not be made current again."""


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def hle_home(env: dict[str, str] | None = None) -> Path:
    """Where the versioned layout lives: ``$HLE_HOME`` or ``~/.local/share/hle``."""
    if env is None:
        env = dict(os.environ)
    raw = env.get(HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share" / "hle"


def versioned_layout_present(home: Path | None = None) -> bool:
    """True when ``current`` is a symlink and ``versions/`` exists."""
    home = home or hle_home()
    return (home / CURRENT_LINK).is_symlink() and (home / VERSIONS_DIR).is_dir()


def current_version(home: Path | None = None) -> str | None:
    """The version ``current`` points at, from the link itself (no exec)."""
    home = home or hle_home()
    link = home / CURRENT_LINK
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name or None


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def installer_owns(prefix: str, home: Path | None = None) -> bool:
    """Whether *prefix* (``sys.prefix``) is a venv the installer laid down.

    Either the old flat ``venv/`` or a ``versions/<v>/`` entry. Only those may
    be updated with a side-by-side venv; any other venv belongs to whoever
    made it.
    """
    home = home or hle_home()
    p = Path(prefix)
    return _is_under(p, home / FLAT_VENV_DIR) or _is_under(p, home / VERSIONS_DIR)


def current_exec_path(home: Path | None = None) -> Path:
    """``current/bin/hle`` — the stable path a service unit should run."""
    home = home or hle_home()
    return home / CURRENT_LINK / "bin" / "hle"


# --------------------------------------------------------------------------- #
# Support matrix
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UpdateSupport:
    supported: bool
    method: str
    # `unsupported:<method>` when not supported, matching the protocol's ack
    # reasons, so the caller can send it as-is.
    reason: str | None = None


def can_self_update(
    *,
    prefix: str | None = None,
    executable: str | None = None,
    home: Path | None = None,
    in_docker: bool | None = None,
) -> UpdateSupport:
    """Classify this install and say whether the agent may update it.

    Supported: the installer's venv (flat or versioned), pipx, uv. Everything
    else is owned by something outside the process — a brew keg, a docker
    image, the OS's Python — and the operator has to run that tool instead.
    """
    if in_docker is None:
        in_docker = os.path.exists("/.dockerenv")
    if in_docker:
        return UpdateSupport(False, "docker", "unsupported:docker")

    # The classifier lives in the CLI module; imported here so this module
    # stays click-free at import time.
    from hle_client.update_cmd import PIPX, UV, VENV, detect_install_method

    prefix = prefix or sys.prefix
    executable = executable or sys.executable
    method = detect_install_method(prefix, executable)
    if method in (PIPX, UV):
        return UpdateSupport(True, method)
    if method == VENV:
        if installer_owns(prefix, home):
            return UpdateSupport(True, method)
        return UpdateSupport(False, method, f"unsupported:{method}")
    return UpdateSupport(False, method, f"unsupported:{method}")


# --------------------------------------------------------------------------- #
# State files
# --------------------------------------------------------------------------- #
@dataclass
class UpdateState:
    request_id: str
    from_version: str
    to_version: str
    started_at: float
    phase: str = "restarting"

    def to_json(self) -> str:
        d = asdict(self)
        # The wire spelling from the plan: {request_id, from, to, ...}.
        d["from"] = d.pop("from_version")
        d["to"] = d.pop("to_version")
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> UpdateState:
        d = json.loads(text)
        return cls(
            request_id=str(d["request_id"]),
            from_version=str(d.get("from", d.get("from_version", ""))),
            to_version=str(d.get("to", d.get("to_version", ""))),
            started_at=float(d.get("started_at", 0.0)),
            phase=str(d.get("phase", "restarting")),
        )


@dataclass
class FailedUpdate:
    state: UpdateState
    reason: str
    log_tail: list[str] = field(default_factory=list)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_update_state(home: Path, state: UpdateState) -> None:
    _write_atomic(home / STATE_FILE, state.to_json())


def read_update_state(home: Path) -> UpdateState | None:
    path = home / STATE_FILE
    try:
        return UpdateState.from_json(path.read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_update_state(home: Path) -> None:
    (home / STATE_FILE).unlink(missing_ok=True)


def write_failed_marker(home: Path, failed: FailedUpdate) -> None:
    d = json.loads(failed.state.to_json())
    d["reason"] = failed.reason
    d["log_tail"] = list(failed.log_tail)
    _write_atomic(home / FAILED_FILE, json.dumps(d, sort_keys=True))


def read_failed_marker(home: Path) -> FailedUpdate | None:
    path = home / FAILED_FILE
    try:
        text = path.read_text()
        d = json.loads(text)
        state = UpdateState.from_json(text)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    tail = d.get("log_tail")
    return FailedUpdate(
        state=state,
        reason=str(d.get("reason", "unknown")),
        log_tail=[str(x) for x in tail] if isinstance(tail, list) else [],
    )


def clear_failed_marker(home: Path) -> None:
    (home / FAILED_FILE).unlink(missing_ok=True)


@dataclass
class BootCheck:
    """What the agent finds on disk at startup, and what it must do about it.

    ``watch``: this process *is* the updated one; run the watchdog.
    ``report_failed``: a previous update failed and nobody has told the server.
    ``none``: nothing pending.
    """

    kind: Literal["none", "watch", "report_failed"]
    state: UpdateState | None = None
    reason: str | None = None
    log_tail: list[str] = field(default_factory=list)


def boot_check(home: Path, running_version: str, *, now: float | None = None) -> BootCheck:
    """Decide the startup role from ``update.json`` / ``update.json.failed``.

    Nothing is deleted here: a failure report is cleared only once it has
    been sent, and the in-flight state only once the watchdog has decided.
    """
    now = time.time() if now is None else now
    failed = read_failed_marker(home)
    if failed is not None:
        # A rolled-back update leaves no update.json, but if one is there it
        # is superseded by the marker.
        clear_update_state(home)
        return BootCheck("report_failed", failed.state, failed.reason, failed.log_tail)

    state = read_update_state(home)
    if state is None:
        return BootCheck("none")
    if now - state.started_at > STALE_AFTER_S:
        logger.info("Discarding a stale %s from %.0fs ago", STATE_FILE, now - state.started_at)
        clear_update_state(home)
        return BootCheck("none")
    if state.to_version == running_version:
        return BootCheck("watch", state)
    # `current` was repointed but the process that came up is not the target:
    # a rollback that never got to write its marker, or a swap that did not
    # take. Either way the update did not happen.
    return BootCheck(
        "report_failed",
        state,
        f"expected {state.to_version} to start, but {running_version} did",
        [],
    )


# --------------------------------------------------------------------------- #
# Log ring buffer, for `update_result.log_tail`
# --------------------------------------------------------------------------- #
class RingBufferHandler(logging.Handler):
    """Keeps the last *capacity* formatted log lines in memory."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 — a log handler must never raise
            self.handleError(record)

    def tail(self, n: int = 50) -> list[str]:
        return list(self.lines)[-n:]


_ring: RingBufferHandler | None = None


def ensure_log_buffer() -> RingBufferHandler:
    """Attach one ring buffer to the root logger (idempotent)."""
    global _ring
    if _ring is None:
        _ring = RingBufferHandler()
        logging.getLogger().addHandler(_ring)
    return _ring


def log_tail(n: int = 50) -> list[str]:
    return ensure_log_buffer().tail(n)


# --------------------------------------------------------------------------- #
# Updaters
# --------------------------------------------------------------------------- #
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv built internally
        argv, capture_output=True, text=True, check=False, **kwargs
    )


def _tail(text: str, n: int = 5) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


class Updater(Protocol):
    def stage(self, version: str) -> Path: ...
    def verify(self, version: str) -> None: ...
    def swap(self, version: str, *, from_version: str) -> None: ...
    def rollback(self) -> str: ...


class VersionedUpdater:
    """The installer layout: one venv per version, ``current`` picks one.

    ``python`` is the interpreter that creates the new venv; the running one
    by default (``venv`` builds on its base interpreter, so a venv can make
    a sibling). ``run`` is ``subprocess.run`` with output captured.
    """

    def __init__(
        self,
        home: Path,
        *,
        python: str | None = None,
        run: Runner = _run,
        pip_timeout: float = 600.0,
    ) -> None:
        self.home = home
        self.python = python or sys.executable
        self._run = run
        self._pip_timeout = pip_timeout

    # -- paths ---------------------------------------------------------------
    def version_dir(self, version: str) -> Path:
        return self.home / VERSIONS_DIR / version

    @property
    def current(self) -> Path:
        return self.home / CURRENT_LINK

    @property
    def previous_file(self) -> Path:
        return self.home / PREVIOUS_FILE

    # -- stage / verify ------------------------------------------------------
    def stage(self, version: str) -> Path:
        """Create ``versions/<v>`` and install exactly ``hle-client==<v>``.

        PyPI only: the request carries no index and none is honoured from it.
        On any failure the directory is removed and :class:`StageError` raised.
        """
        target = self.version_dir(version)
        if self.current.is_symlink() and current_version(self.home) == version:
            raise StageError(f"{version} is already the current version")
        self._remove(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._check(
                [self.python, "-m", "venv", str(target)],
                what="create venv",
                timeout=120.0,
            )
            self._check(
                [
                    str(target / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--quiet",
                    f"{_PACKAGE}=={version}",
                ],
                what="pip install",
                timeout=self._pip_timeout,
            )
        except StageError:
            self._remove(target)
            raise
        return target

    def verify(self, version: str) -> None:
        """The staged copy must report *version* and import both packages."""
        target = self.version_dir(version)
        try:
            out = self._check([str(target / "bin" / "hle"), "--version"], what="hle --version")
            if version not in out:
                raise StageError(f"staged hle reports {out.strip()!r}, not {version}")
            self._check(
                [str(target / "bin" / "python"), "-c", "import hle_client, hle_common"],
                what="import check",
            )
        except StageError:
            self._remove(target)
            raise

    # -- swap / rollback -----------------------------------------------------
    def swap(self, version: str, *, from_version: str) -> None:
        """Point ``current`` at ``versions/<v>`` atomically; remember the old one.

        First swap from a flat ``venv/`` install: the running venv becomes
        ``versions/<from_version>`` (a symlink, nothing is moved) so a rollback
        has somewhere to go, and the installer's launchers are repointed at
        ``current``.
        """
        target = self.version_dir(version)
        if not target.is_dir():
            raise SwapError(f"{target} is not staged")
        old = current_version(self.home)
        if old is None:
            old = from_version
            flat = self.home / FLAT_VENV_DIR
            old_dir = self.version_dir(old)
            if flat.is_dir() and not old_dir.exists():
                old_dir.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.path.join("..", FLAT_VENV_DIR), old_dir)
        self._repoint(target)
        _write_atomic(self.previous_file, old + "\n")
        relink_launchers(self.home)

    def rollback(self) -> str:
        """Point ``current`` back at ``previous``. Returns the version restored."""
        try:
            prev = self.previous_file.read_text().strip()
        except OSError:
            prev = ""
        if not prev:
            raise RollbackError("no previous version recorded")
        target = self.version_dir(prev)
        if not target.exists():
            raise RollbackError(f"previous version {prev} is gone from {target.parent}")
        try:
            self._repoint(target)
        except OSError as exc:
            raise RollbackError(f"could not repoint {self.current}: {exc}") from exc
        return prev

    # -- helpers -------------------------------------------------------------
    def _repoint(self, target: Path) -> None:
        tmp = self.home / (CURRENT_LINK + ".tmp")
        tmp.unlink(missing_ok=True)
        # Relative, so the layout survives a moved or bind-mounted home.
        os.symlink(os.path.join(VERSIONS_DIR, target.name), tmp)
        try:
            os.replace(tmp, self.current)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise SwapError(f"could not repoint {self.current}: {exc}") from exc

    def _check(self, argv: list[str], *, what: str, timeout: float = 60.0) -> str:
        try:
            result = self._run(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise StageError(f"{what} failed: {exc}") from exc
        if result.returncode != 0:
            detail = _tail(result.stderr or result.stdout or "")
            raise StageError(f"{what} failed (exit {result.returncode}): {detail}")
        return result.stdout or ""

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)


def relink_launchers(home: Path, candidates: tuple[str, ...] | None = None) -> list[Path]:
    """Repoint installer-written ``hle`` symlinks at ``current/bin/hle``.

    Only symlinks that already resolve into *home* are touched; a launcher
    someone else put there is not ours to move. Best-effort: a launcher that
    cannot be rewritten (root-owned, say) is skipped, and the unit file's
    ExecStart — which names ``current/bin/hle`` directly — is what a service
    actually runs.
    """
    target = current_exec_path(home)
    done: list[Path] = []
    for raw in LAUNCHER_PATHS if candidates is None else candidates:
        link = Path(raw).expanduser()
        if not link.is_symlink():
            continue
        try:
            dest = Path(os.readlink(link))
            if not dest.is_absolute():
                dest = link.parent / dest
            if not _is_under(dest, home) and not _is_under(Path(os.path.normpath(dest)), home):
                continue
            if os.path.normpath(dest) == os.path.normpath(target):
                continue
            tmp = link.with_name(link.name + ".tmp")
            tmp.unlink(missing_ok=True)
            os.symlink(target, tmp)
            os.replace(tmp, link)
            done.append(link)
        except OSError as exc:
            logger.debug("Left launcher %s alone: %s", link, exc)
    return done


class ToolUpdater:
    """pipx / uv: reinstall in place, pinned, with a network-dependent rollback.

    These tools own their venv, so there is no side-by-side install and no
    symlink to flip. ``swap`` is ``pipx install --force hle-client==<v>``
    (or the uv equivalent), which rewrites the venv the running process is
    executing from. The window is therefore larger than the versioned
    layout's: the old version is gone from disk from the moment the install
    finishes until a rollback reinstalls it, and that rollback needs the
    index to be reachable. ``stage`` is only a dry check that the version
    exists on PyPI, so a typo fails before anything is touched.
    """

    def __init__(
        self,
        tool: str,
        home: Path,
        *,
        run: Runner = _run,
        fetch: Callable[[str, float], int] | None = None,
        hle_path: str | None = None,
        install_timeout: float = 600.0,
    ) -> None:
        if tool not in ("pipx", "uv"):
            raise ValueError(f"not a tool updater: {tool}")
        self.tool = tool
        self.home = home
        self._run = run
        self._fetch = fetch or _http_status
        self._hle_path = hle_path or str(Path(sys.executable).with_name("hle"))
        self._install_timeout = install_timeout

    @property
    def previous_file(self) -> Path:
        return self.home / PREVIOUS_FILE

    def _install_argv(self, version: str) -> list[str]:
        spec = f"{_PACKAGE}=={version}"
        if self.tool == "pipx":
            return ["pipx", "install", "--force", spec]
        return ["uv", "tool", "install", "--force", spec]

    def stage(self, version: str) -> Path:
        url = f"https://pypi.org/pypi/{_PACKAGE}/{version}/json"
        try:
            status = self._fetch(url, 15.0)
        except Exception as exc:  # noqa: BLE001 — any transport error is the same answer
            raise StageError(f"PyPI unreachable: {exc}") from exc
        if status == 404:
            raise StageError(f"{_PACKAGE} {version} is not on PyPI")
        if status != 200:
            raise StageError(f"PyPI answered HTTP {status} for {version}")
        return Path(self._hle_path).parent.parent

    def verify(self, version: str) -> None:
        # Nothing is staged yet; the real check happens after the install.
        return None

    def _installed_version(self) -> str:
        try:
            result = self._run([self._hle_path, "--version"], timeout=30.0)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SwapError(f"could not run {self._hle_path}: {exc}") from exc
        return (result.stdout or "").strip()

    def swap(self, version: str, *, from_version: str) -> None:
        _write_atomic(self.previous_file, from_version + "\n")
        try:
            result = self._run(self._install_argv(version), timeout=self._install_timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SwapError(f"{self.tool} install failed: {exc}") from exc
        if result.returncode != 0:
            raise SwapError(
                f"{self.tool} install failed (exit {result.returncode}): "
                f"{_tail(result.stderr or result.stdout or '')}"
            )
        out = self._installed_version()
        if version not in out:
            # The tool said yes but the venv says otherwise (a stale index
            # view, typically). Put the old pin back before reporting.
            try:
                self.rollback()
            except RollbackError as exc:
                raise SwapError(
                    f"{self.tool} left {out!r} installed, not {version}; rollback failed: {exc}"
                ) from exc
            raise SwapError(f"{self.tool} left {out!r} installed, not {version}; rolled back")

    def rollback(self) -> str:
        try:
            prev = self.previous_file.read_text().strip()
        except OSError:
            prev = ""
        if not prev:
            raise RollbackError("no previous version recorded")
        try:
            result = self._run(self._install_argv(prev), timeout=self._install_timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RollbackError(f"{self.tool} reinstall of {prev} failed: {exc}") from exc
        if result.returncode != 0:
            raise RollbackError(
                f"{self.tool} reinstall of {prev} failed (exit {result.returncode}): "
                f"{_tail(result.stderr or result.stdout or '')}"
            )
        return prev


def _http_status(url: str, timeout: float) -> int:
    import httpx

    return httpx.get(url, timeout=timeout, follow_redirects=True).status_code


def make_updater(
    support: UpdateSupport, home: Path, *, run: Runner = _run
) -> VersionedUpdater | ToolUpdater:
    """The updater for a supported install; raises for an unsupported one."""
    if not support.supported:
        raise UpdateError(support.reason or "unsupported")
    if support.method in ("pipx", "uv"):
        return ToolUpdater(support.method, home, run=run)
    return VersionedUpdater(home, run=run)


# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #
ProgressSender = Callable[[UpdateProgress], Awaitable[None]]


async def run_update(
    req: UpdateRequest,
    *,
    updater: Updater,
    home: Path,
    from_version: str,
    send_progress: ProgressSender,
    now: Callable[[], float] = time.time,
) -> UpdateState:
    """Stage, verify and swap; report each phase; leave ``update.json`` behind.

    Returns once the process should restart. Raises :class:`UpdateError` (or
    whatever the updater raised) with nothing swapped when the new version
    could not be staged, and with ``current`` already repointed when the swap
    itself failed half-way — the latter cannot happen for the versioned
    layout, whose swap is a single ``rename``.

    Blocking work runs in a thread so the control connection keeps answering
    pings and state_sync meanwhile.
    """
    import asyncio

    version = req.target_version

    async def phase(p: Phase, detail: str | None = None) -> None:
        logger.info("Update %s: %s%s", req.request_id, p, f" ({detail})" if detail else "")
        await send_progress(UpdateProgress(request_id=req.request_id, phase=p, detail=detail))

    await phase("staging", f"{_PACKAGE}=={version}")
    await asyncio.to_thread(updater.stage, version)
    await phase("verifying")
    await asyncio.to_thread(updater.verify, version)
    await phase("swapping", f"{from_version} -> {version}")
    await asyncio.to_thread(updater.swap, version, from_version=from_version)
    state = UpdateState(
        request_id=req.request_id,
        from_version=from_version,
        to_version=version,
        started_at=now(),
        phase="restarting",
    )
    write_update_state(home, state)
    await phase("restarting")
    return state


def health_timeout(env: dict[str, str] | None = None) -> float:
    """Seconds the updated process gets to become healthy before rolling back."""
    if env is None:
        env = dict(os.environ)
    raw = env.get(HEALTH_TIMEOUT_ENV)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_HEALTH_TIMEOUT
