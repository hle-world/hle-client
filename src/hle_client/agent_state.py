"""Readiness marker a running agent leaves for its Kubernetes readiness probe.

``hle agent status --ready`` runs in a separate process from the agent, so
"is the agent connected?" has to be written down. The agent writes its pid to
``$HLE_HOME/connected`` when the server welcomes it and removes the file when
the control connection ends. The probe passes only while the file exists and
names a live process. Only transitions write, never heartbeats.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKER = "connected"


def marker_path(home: Path) -> Path:
    return Path(home) / MARKER


def write_connected(home: Path, pid: int) -> None:
    """Record that process *pid* holds a welcomed control connection (atomic)."""
    path = marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(pid))
    os.replace(tmp, path)


def read_pid(home: Path) -> int | None:
    """The pid recorded in the marker, or None when absent or unreadable."""
    try:
        pid = int(marker_path(home).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def clear(home: Path, *, only_pid: int | None = None) -> None:
    """Remove the marker; with *only_pid*, only if it names that process.

    During a handover the successor writes its own marker before the incumbent
    closes, so the incumbent must not remove a marker that is not its own.
    """
    if only_pid is not None and read_pid(home) not in (None, only_pid):
        return
    marker_path(home).unlink(missing_ok=True)


def clear_if_stale(home: Path, *, own_pid: int) -> None:
    """Drop a marker a previous run left behind, before this one connects.

    A container restart reuses pid 1, so a leftover marker would pass the probe
    before this run has been welcomed. A marker naming our own pid (we have
    written nothing yet) or a dead process is stale.
    """
    pid = read_pid(home)
    if marker_path(home).exists() and (pid is None or pid == own_pid or not pid_alive(pid)):
        clear(home)


def pid_alive(pid: int | None) -> bool:
    """Whether *pid* names a live process (EPERM still means it exists)."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_ready(home: Path) -> tuple[bool, str]:
    """``(ready, reason)``; the reason is a one-line explanation for exit 1."""
    pid = read_pid(home)
    if pid is None:
        return False, "agent is not connected"
    if not pid_alive(pid):
        return False, f"agent process {pid} is not running"
    return True, "ready"
