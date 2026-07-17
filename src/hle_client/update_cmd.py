"""``hle update`` — self-upgrade regardless of how the client was installed.

Detects whether the running client lives in a pipx-managed venv, a uv-managed
tool venv, the installer's plain venv, or a system/pip environment, and runs
the matching upgrade. Keeps users from having to remember the install method
(and sidesteps the installer's symlink pitfalls).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from hle_client import __version__

console = Console()

_PACKAGE = "hle-client"

# Install-method identifiers.
PIPX = "pipx"
UV = "uv"
VENV = "venv"
PIP = "pip"


def detect_install_method(prefix: str, executable: str) -> str:
    """Classify the environment the running client lives in.

    ``prefix`` is ``sys.prefix`` (the venv/environment root); ``executable``
    is ``sys.executable``. Pure function so it is unit-testable.
    """
    p = prefix.replace("\\", "/")
    if "/pipx/venvs/" in p or "/pipx/venvs" in p:
        return PIPX
    if "/uv/tools/" in p or "/uv/tools" in p:
        return UV
    # The website installer's plain-venv location.
    if p.rstrip("/").endswith(".local/share/hle/venv"):
        return VENV
    # Any other virtualenv: upgrade in place with its own interpreter.
    if executable and (Path(prefix) / "pyvenv.cfg").as_posix() and prefix != sys.base_prefix:
        return VENV
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
def update(check: bool, target_version: str | None, yes: bool) -> None:
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

    target_desc = target_version or latest or "latest"
    if not yes:
        click.confirm(f"Upgrade {_PACKAGE} to {target_desc}?", abort=True)

    cmd = build_upgrade_command(method, sys.executable, version=target_version)
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    try:
        result = subprocess.run(cmd, check=False)  # noqa: S603 — argv built internally
    except FileNotFoundError:
        console.print(
            f"[red]Could not run '{cmd[0]}'.[/red] Install it or upgrade manually with:\n"
            f"  {sys.executable} -m pip install --upgrade {_PACKAGE}"
        )
        raise SystemExit(1) from None

    if result.returncode != 0:
        console.print("[red]Upgrade command failed.[/red]")
        raise SystemExit(result.returncode)

    new_version = _installed_version(sys.executable) or "unknown"
    console.print(f"[green]Updated to {new_version}.[/green]")
    console.print(
        "[yellow]Restart any running tunnels[/yellow] so they pick up the new version "
        "(e.g. restart the systemd service, or stop and re-run 'hle expose ...')."
    )
