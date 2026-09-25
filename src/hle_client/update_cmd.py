"""``hle update`` — self-upgrade regardless of how the client was installed.

Detects whether the running client lives in a pipx-managed venv, a uv-managed
tool venv, the installer's plain venv, or a system/pip environment, and runs
the matching upgrade. Keeps users from having to remember the install method
(and sidesteps the installer's symlink pitfalls).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from hle_client import __version__
from hle_client.context import confirm, confirm_or_abort
from hle_client.errors import HleError
from hle_client.richcompat import Console

console = Console()

_PACKAGE = "hle-client"

# Install-method identifiers.
PIPX = "pipx"
UV = "uv"
VENV = "venv"
PIP = "pip"
BREW = "brew"
# PEP 668: the interpreter's site-packages belong to the OS package manager.
EXTERNALLY_MANAGED = "externally-managed"


def is_externally_managed(stdlib: str | None = None) -> bool:
    """True when the interpreter carries a PEP 668 ``EXTERNALLY-MANAGED`` marker."""
    if stdlib is None:
        import sysconfig

        stdlib = sysconfig.get_path("stdlib")
    if not stdlib:
        return False
    return (Path(stdlib) / "EXTERNALLY-MANAGED").is_file()


def detect_install_method(
    prefix: str,
    executable: str,
    *,
    base_prefix: str | None = None,
    stdlib: str | None = None,
) -> str:
    """Classify the environment the running client lives in.

    ``prefix`` is ``sys.prefix`` (the venv/environment root); ``executable``
    is ``sys.executable``. ``base_prefix`` and ``stdlib`` default to the
    running interpreter's and exist so tests can describe another one.
    """
    if base_prefix is None:
        base_prefix = sys.base_prefix
    p = prefix.replace("\\", "/")
    if "/pipx/venvs/" in p or "/pipx/venvs" in p:
        return PIPX
    if "/uv/tools/" in p or "/uv/tools" in p:
        return UV
    # A Homebrew keg: `<prefix>/Cellar/hle-client/<version>/libexec` is a venv,
    # but one brew owns. `pip install --upgrade` inside it leaves the formula's
    # receipt and the linked bin/ pointing at the old version, so the keg ends
    # up half one release and half another.
    if "/Cellar/hle-client/" in p:
        return BREW
    # The website installer's plain-venv location.
    if p.rstrip("/").endswith(".local/share/hle/venv"):
        return VENV
    # Any other virtualenv: upgrade in place with its own interpreter.
    if executable and (Path(prefix) / "pyvenv.cfg").is_file() and prefix != base_prefix:
        return VENV
    if is_externally_managed(stdlib):
        return EXTERNALLY_MANAGED
    return PIP


def build_upgrade_command(method: str, executable: str, *, version: str | None = None) -> list[str]:
    """Build the argv that upgrades the client for the given install method.

    Pure function so it is unit-testable. ``version`` pins an exact version
    (e.g. ``"2607.2"``); otherwise upgrades to the latest.
    """
    spec = f"{_PACKAGE}=={version}" if version else _PACKAGE
    if method == PIPX:
        if version:
            return ["pipx", "install", "--force", spec]
        return ["pipx", "upgrade", _PACKAGE]
    if method == UV:
        if version:
            return ["uv", "tool", "install", "--force", spec]
        return ["uv", "tool", "upgrade", _PACKAGE]
    # venv / pip: upgrade the package inside the running interpreter.
    return [executable, "-m", "pip", "install", "--upgrade", spec]


def pypi_latest_version(package: str = _PACKAGE, timeout: float = 10.0) -> str | None:
    """Return the latest version on PyPI, or ``None`` if it can't be fetched."""
    try:
        import httpx

        resp = httpx.get(f"https://pypi.org/pypi/{package}/json", timeout=timeout)
        resp.raise_for_status()
        version = resp.json()["info"]["version"]
        return str(version) if version else None
    except Exception:
        return None


def _installed_version(executable: str) -> str | None:
    """Best-effort read of the installed version after an upgrade."""
    try:
        out = subprocess.run(
            [executable, "-c", "import hle_client; print(hle_client.__version__)"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        v = out.stdout.strip()
        return v or None
    except Exception:
        return None


@click.command()
@click.option("--check", is_flag=True, help="Only report current vs. latest; don't upgrade.")
@click.option("--version", "target_version", default=None, help="Upgrade/downgrade to this version")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def update(ctx: click.Context, check: bool, target_version: str | None, yes: bool) -> None:
    """Update the HLE client to the latest version (any install method)."""
    method = detect_install_method(sys.prefix, sys.executable)
    console.print(f"Installed: [bold]{__version__}[/bold]  (install method: {method})")

    latest = pypi_latest_version()
    if latest:
        console.print(f"Latest on PyPI: [bold]{latest}[/bold]")
    elif not target_version:
        console.print("[yellow]Could not reach PyPI to check the latest version.[/yellow]")

    if check:
        if latest and latest == __version__:
            console.print("[green]Already up to date.[/green]")
        elif latest:
            console.print(f"[yellow]Update available: {__version__} -> {latest}[/yellow]")
        return

    if not target_version and latest and latest == __version__:
        console.print("[green]Already up to date.[/green]")
        return

    if method == BREW:
        raise HleError(
            "This client was installed by Homebrew. Upgrading inside the keg would corrupt it.",
            hint="Run:\n  brew upgrade hle-client",
        )

    if method == EXTERNALLY_MANAGED:
        raise HleError(
            "This Python is managed by the OS package manager (PEP 668). "
            "pip will refuse to install into it.",
            hint=(
                "Upgrade with the tool that installed the client, or:\n"
                f"  pipx upgrade {_PACKAGE}\n"
                f"  uv tool upgrade {_PACKAGE}"
            ),
        )

    target_desc = target_version or latest or "latest"
    if not yes:
        confirm_or_abort(ctx, f"Upgrade {_PACKAGE} to {target_desc}?", default=False)

    home = _versioned_home(method)
    if home is not None:
        new_version: str | None = _versioned_update(home, target_version or latest)
    else:
        new_version = _in_place_update(method, target_version, latest)

    _restart_services(ctx, yes=yes, home=home, new_version=new_version)


def _versioned_home(method: str) -> Path | None:
    """The installer's versioned layout, when this client runs from it."""
    if method != VENV:
        return None
    from hle_client.agent_update import layout_home

    return layout_home()


def _versioned_update(home: Path, version: str | None) -> str:
    """Install *version* side by side and repoint ``current`` at it.

    The running venv is never touched, so this process keeps its own code
    (no half-old, half-new lazy imports) and ``current`` moves in one rename.
    """
    from hle_client.agent_update import UpdateError, VersionedUpdater, current_version

    if not version:
        raise HleError(
            "Could not tell which version to install.",
            hint="PyPI was unreachable. Name one: hle update --version <version>",
        )
    if current_version(home) == version:
        console.print(f"[green]{version} is already current.[/green]")
        return version
    updater = VersionedUpdater(home)
    try:
        console.print(f"[dim]Installing {_PACKAGE}=={version} into {updater.version_dir(version)}")
        updater.stage(version)
        updater.verify(version)
        updater.swap(version, from_version=__version__)
    except UpdateError as exc:
        raise HleError(
            f"Update to {version} failed; {__version__} is still current.", hint=str(exc)
        ) from None
    console.print(f"[green]Updated to {version}.[/green] {home / 'current'} now points at it.")
    return version


def _in_place_update(method: str, target_version: str | None, latest: str | None) -> str | None:
    """Upgrade the environment this client runs from (pipx, uv, a plain venv)."""
    cmd = build_upgrade_command(method, sys.executable, version=target_version)
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    try:
        result = subprocess.run(cmd, check=False)  # noqa: S603 — argv built internally
    except FileNotFoundError:
        raise HleError(
            f"Could not run '{cmd[0]}'.",
            hint=(
                "Install it or upgrade manually with:\n"
                f"  {sys.executable} -m pip install --upgrade {_PACKAGE}"
            ),
        ) from None

    if result.returncode != 0:
        # pip's own status, so a wrapper sees what pip would have said.
        raise HleError("Upgrade command failed.", exit_code=result.returncode)

    # Exit code 0 is not the same as "the new version is installed". `pipx
    # upgrade` and `uv tool upgrade` both exit 0 while reporting "already at
    # latest version" when their view of the index is behind PyPI's — which
    # happens for minutes after a release, and for as long as pip's HTTP cache
    # holds a stale index page. Trusting the exit code printed a green
    # "Updated to 2608.3." immediately under "Latest on PyPI: 2608.4".
    expected = target_version or latest
    new_version = _installed_version(sys.executable)

    if expected and new_version and new_version != expected:
        console.print(
            f"[yellow]{cmd[0]} reported success but {expected} is not installed "
            f"(still {new_version}).[/yellow]"
        )
        pinned = build_upgrade_command(method, sys.executable, version=expected)
        if pinned != cmd:
            # The pinned form can't no-op: it names the version outright rather
            # than asking the tool whether it thinks an upgrade is due.
            console.print(f"[dim]$ {' '.join(pinned)}[/dim]")
            retry = subprocess.run(pinned, check=False)  # noqa: S603 — argv built internally
            if retry.returncode == 0:
                new_version = _installed_version(sys.executable)

    if expected and new_version and new_version != expected:
        raise HleError(
            f"Still on {new_version}, not {expected}.",
            hint=(
                "The package index this machine sees is behind PyPI — usually a stale "
                "cache, or a release published moments ago. Retry in a minute, or force it:\n"
                f"  {' '.join(build_upgrade_command(method, sys.executable, version=expected))}"
            ),
        )

    if new_version is None:
        # No evidence of failure, so don't claim one — but don't claim success
        # either, which is what "Updated to unknown." amounted to.
        console.print(
            "[yellow]Upgraded, but the installed version could not be read.[/yellow] "
            "Check with: hle --version"
        )
    else:
        console.print(f"[green]Updated to {new_version}.[/green]")
    return new_version


def _agent_services(services: list[tuple[str, bool]]) -> bool:
    from hle_client.service_cmd import service_spec

    for svc, user_mode in services:
        try:
            spec = service_spec(svc, user_mode)
        except Exception:
            continue
        if spec and [str(a) for a in spec.get("run_args", [])][:2] == ["agent", "run"]:
            return True
    return False


def _restart_services(
    ctx: click.Context, *, yes: bool, home: Path | None, new_version: str | None
) -> None:
    # A service started before the upgrade is still running the old code, and
    # nothing about it looks wrong — `hle --version` reports the new one while
    # the process serving traffic is the previous release. Telling people to
    # "restart any running tunnels" left that gap open until they acted on it.
    from hle_client.service_cmd import installed_services, refresh_service

    try:
        services = installed_services()
    except Exception:
        services = []

    if not services:
        console.print(
            "[dim]No hle services installed. Restart anything you started by hand "
            "(e.g. re-run 'hle tunnel create ...') so it picks up the new version.[/dim]"
        )
        return

    listed = ", ".join(f"{svc} ({'user' if user else 'system'})" for svc, user in services)
    console.print(f"Still running the previous version: [bold]{listed}[/bold]")
    if not (yes or confirm(ctx, f"Restart {len(services)} service(s) now?", default=True)):
        console.print("[yellow]Left running the old version.[/yellow] Restart later with:")
        console.print("  hle daemon restart --all")
        return

    if (
        home is not None
        and new_version
        and new_version != __version__
        and _agent_services(services)
    ):
        # The agent service then starts under the same watchdog a dashboard
        # update gets: not healthy in time means `current` goes back. The
        # result is logged, not sent — no server is waiting on a local update.
        import time

        from hle_client.agent_update import LOCAL_REQUEST_ID, UpdateState, write_update_state

        write_update_state(
            home, UpdateState(LOCAL_REQUEST_ID, __version__, new_version, time.time())
        )

    failed = []
    needs_root = False
    for svc, user_mode in services:
        scope = "user" if user_mode else "system"
        # Rebuild, not just restart. The service file records the path the
        # previous client lived at, and an upgrade can move it — a restart
        # then faithfully re-runs a command that is no longer there.
        outcome = refresh_service(svc, user_mode)
        if outcome == "refreshed":
            console.print(f"  [green]rebuilt and started[/green] {svc} ({scope})")
        elif outcome == "restarted":
            console.print(f"  [green]restarted[/green] {svc} ({scope})")
        else:
            failed.append((svc, user_mode))
            needs_root = needs_root or not user_mode
            console.print(f"  [red]failed[/red] {svc} ({scope})")
    if failed:
        hint = "Check 'hle daemon status --agent' and the service log."
        if needs_root and os.geteuid() != 0:
            # `sudo hle daemon restart --all` is the obvious next thing to try
            # and it does not work: hle lives in ~/.local/bin, which sudo's
            # secure_path drops. Give the command that does.
            units = " ".join(svc for svc, user_mode in failed if not user_mode)
            hint += f"\nRun: sudo systemctl restart {units}"
        raise HleError(
            "Some services did not restart. They are still on the old version.", hint=hint
        )
