"""Services on this machine, whichever manager runs them.

Thin on purpose: the systemd, launchd and rc.d backends in
:mod:`hle_client.service_cmd` already raise typed errors and know the
platform. What they do not do is return anything, so this module is where
"what is installed" and "did that work" become values a dashboard can show.

Every function here is ``async`` for the same reason the rest of ``ops`` is,
even though the work is subprocess calls: a TUI or a webapp awaits them all
the same way, and the blocking part runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from hle_client import service_cmd
from hle_client.errors import HleError
from hle_client.ops.models import Daemon


def _sync_list(scope: bool | None) -> list[Daemon]:
    found: list[Daemon] = []
    for name, user_mode in service_cmd.installed_services():
        if scope is not None and user_mode != scope:
            continue
        spec = service_cmd.service_spec(name, user_mode)
        found.append(
            Daemon.from_spec(name, user_mode=user_mode, spec=spec, state=_state(name, user_mode))
        )
    return found


def _state(name: str, user_mode: bool) -> str | None:
    """The manager's own word for the unit, or None when it cannot be asked."""
    plat = service_cmd.current_platform()
    try:
        if plat == "linux":
            cmd = ["systemctl", *(["--user"] if user_mode else []), "is-active", name]
            result = subprocess.run(  # noqa: S603 — argv built internally
                cmd, check=False, capture_output=True, text=True
            )
            word = result.stdout.strip()
            return word or None
        if plat == "darwin":
            domain = f"gui/{os.getuid()}" if user_mode else "system"
            result = subprocess.run(  # noqa: S603 — argv built internally
                ["launchctl", "print", f"{domain}/{name}"],
                check=False,
                capture_output=True,
                text=True,
            )
            return "loaded" if result.returncode == 0 else "not loaded"
        if plat == "freebsd":
            return "running" if service_cmd._rcd_running(name) else "stopped"
    except (OSError, ValueError):
        return None
    return None


async def list_daemons(*, scope: bool | None = None) -> list[Daemon]:
    """Installed hle services. ``scope`` True/False narrows to user/system."""
    return await asyncio.to_thread(_sync_list, scope)


def service_name(label: str, name: str | None = None, *, plat: str | None = None) -> str:
    """The unit/plist/rc name a label maps to on this platform."""
    plat = plat or service_cmd.current_platform()
    if plat == "freebsd":
        return service_cmd.rc_service_name(label, name)
    if plat == "darwin":
        return service_cmd.launchd_label(label, name)
    return service_cmd.unit_name(label, name)


async def install(spec: dict[str, Any], *, start: bool = True) -> Daemon:
    """Write and start a service from its spec. See ``service_cmd._install_from_spec``.

    The backend prints what it wrote and whether it started — output that the
    CLI's success path promises. Returning the :class:`Daemon` on top is what
    lets a caller that is not the CLI know what it got.
    """
    plat = service_cmd._require_supported()
    await asyncio.to_thread(service_cmd._install_from_spec, spec, plat=plat, start=start)
    name = service_name(str(spec["label"]), spec.get("name"), plat=plat)
    user_mode = bool(spec.get("user_mode"))
    return Daemon.from_spec(name, user_mode=user_mode, spec=spec)


def _sync_uninstall(label: str, name: str | None, user_mode: bool) -> None:
    plat = service_cmd._require_supported()
    if plat == "freebsd":
        service_cmd._rcd_reject_user_mode(user_mode)
        service_cmd._rcd_uninstall(label=label, name=name)
    elif plat == "darwin":
        service_cmd._launchd_uninstall(label=label, name=name, user_mode=user_mode)
    else:
        service_cmd._systemd_uninstall(label=label, name=name, user_mode=user_mode)


async def uninstall(label: str, *, name: str | None = None, user_mode: bool) -> None:
    await asyncio.to_thread(_sync_uninstall, label, name, user_mode)


def _sync_status(label: str, name: str | None, user_mode: bool) -> None:
    plat = service_cmd._require_supported()
    if plat == "freebsd":
        service_cmd._rcd_reject_user_mode(user_mode)
        service_cmd._rcd_status(label=label, name=name)
    elif plat == "darwin":
        service_cmd._launchd_status(label=label, name=name, user_mode=user_mode)
    else:
        service_cmd._systemd_status(label=label, name=name, user_mode=user_mode)


async def status(label: str, *, name: str | None = None, user_mode: bool) -> Daemon:
    """Ask the manager about one service (it prints its own report) and return the record."""
    await asyncio.to_thread(_sync_status, label, name, user_mode)
    unit = service_name(label, name)
    spec = await asyncio.to_thread(service_cmd.service_spec, unit, user_mode)
    state = await asyncio.to_thread(_state, unit, user_mode)
    return Daemon.from_spec(unit, user_mode=user_mode, spec=spec, state=state)


async def restart(name: str, user_mode: bool | None = None) -> bool:
    """Restart one service in the scope it was found in. True on success."""
    return await asyncio.to_thread(service_cmd.restart_service, name, user_mode)


async def refresh(name: str, user_mode: bool) -> str:
    """Rebuild one service against the installed client and start it.

    One of ``"refreshed"``, ``"restarted"`` (no recorded spec to rebuild
    from) or ``"failed"``.
    """
    return await asyncio.to_thread(service_cmd.refresh_service, name, user_mode)


def _launchd_log_path(name: str, user_mode: bool) -> Path:
    """The file a launchd service was told to write to, plist first.

    The path is read out of the plist the installer wrote, not rebuilt, so a
    service installed by an older client is still found.
    """
    plist = service_cmd.service_file(name, user_mode)
    if plist is not None:
        try:
            with plist.open("rb") as fh:
                out = plistlib.load(fh).get("StandardOutPath")
        except (OSError, plistlib.InvalidFileException, ValueError):
            out = None
        if out:
            return Path(str(out))
    label = name.removeprefix(f"{service_cmd._LAUNCHD_LABEL_PREFIX}.")
    return Path(service_cmd._launchd_log_dir(user_mode)) / f"{label}.log"


def log_path(name: str, *, user_mode: bool = False) -> Path | None:
    """Where a service manager keeps its log; ``None`` when it is journald.

    systemd has no file to point at, so the caller asks journalctl instead.
    launchd reads the path out of the plist, rc.d uses the convention the
    installer writes into the script.
    """
    plat = service_cmd.current_platform()
    if plat == "linux":
        return None
    if plat == "darwin":
        return _launchd_log_path(name, user_mode)
    return Path("/var/log") / f"{name}.log"


def _tail_file(path: Path, lines: int) -> str:
    # Read the end, not the whole file: a service that has logged for months
    # would otherwise be read in full every two seconds by a dashboard.
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 256 * 1024))
        text = fh.read().decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


async def _journalctl_tail(name: str, lines: int, user_mode: bool) -> str:
    argv = [
        "journalctl",
        *(["--user"] if user_mode else []),
        "-u",
        name,
        "-n",
        str(lines),
        "--no-pager",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise HleError("journalctl not found.") from None
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise HleError(
            err.decode(errors="replace").strip() or f"journalctl exited {proc.returncode}."
        )
    return "\n".join(out.decode(errors="replace").splitlines()[-lines:])


async def log_tail(
    name: str,
    lines: int = 200,
    *,
    user_mode: bool = False,
    hint: str | None = None,
) -> str:
    """The last ``lines`` of a service's log, journalctl or file.

    The one reader behind ``hle daemon logs`` and the TUI's log pane. On
    systemd the manager's own journal is asked; elsewhere the file the
    installer pointed the service at is read from the end. Raises
    :class:`HleError` when there is nothing to read.
    """
    path = log_path(name, user_mode=user_mode)
    if path is None:
        return await _journalctl_tail(name, lines, user_mode)
    if not path.exists():
        raise HleError(
            f"No log file at {path}",
            hint=hint or "The service may never have started. Try: hle daemon status",
        )
    try:
        return await asyncio.to_thread(_tail_file, path, lines)
    except OSError as exc:
        raise HleError(f"Could not read {path}: {exc.strerror or exc}") from None
