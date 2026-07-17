"""``hle service`` — install and manage a systemd service for a tunnel.

Generates a systemd unit that runs ``hle expose ...`` with the given options,
so a homelab tunnel survives reboots and restarts on failure without the user
hand-writing unit files. Linux/systemd only.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


def find_hle_path() -> str:
    """Resolve an absolute path to the ``hle`` executable for ExecStart."""
    found = shutil.which("hle")
    if found:
        return found
    # Fall back to a sibling of the running interpreter (venv/bin/hle).
    candidate = Path(sys.executable).with_name("hle")
    if candidate.exists():
        return str(candidate)
    return "hle"


def unit_name(label: str, name: str | None = None) -> str:
    """Systemd unit filename for a tunnel label (or an explicit name)."""
    base = name or f"hle-{label}"
    return base if base.endswith(".service") else f"{base}.service"


def build_expose_args(
    *,
    service: str,
    label: str | None,
    zone: str | None = None,
    apex: bool = False,
    auth: str = "sso",
    websocket: bool = True,
    verify_ssl: bool = False,
    forward_host: bool = False,
    allow: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
) -> list[str]:
    """Build the ``expose`` argv (no secrets) for the unit's ExecStart."""
    args = ["expose", "--service", service]
    if label:
        args += ["--label", label]
    if zone:
        args += ["--zone", zone]
    if apex:
        args.append("--apex")
    if auth and auth != "sso":
        args += ["--auth", auth]
    if not websocket:
        args.append("--no-websocket")
    if verify_ssl:
        args.append("--verify-ssl")
    if forward_host:
        args.append("--forward-host")
    for email in allow:
        args += ["--allow", email]
    for opt in options:
        args += ["--option", opt]
    return args


def _quote_exec_args(args: list[str]) -> str:
    """Quote ExecStart args that contain whitespace (systemd-safe)."""
    out = []
    for a in args:
        out.append(f'"{a}"' if (" " in a or "\t" in a) else a)
    return " ".join(out)


def render_unit(
    *,
    label: str,
    hle_path: str,
    expose_args: list[str],
    user_mode: bool,
    run_as_user: str | None,
) -> str:
    """Render the systemd unit file text. Pure function (unit-testable)."""
    exec_start = f"{hle_path} {_quote_exec_args(expose_args)}"
    lines = [
        "[Unit]",
        f"Description=HLE tunnel: {label}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=5",
    ]
    # System units run as root by default; pin an explicit user when asked so
    # the tunnel reads that user's ~/.config/hle/config.toml (API key).
    if not user_mode and run_as_user:
        lines.append(f"User={run_as_user}")
    lines += [
        "",
        "[Install]",
        f"WantedBy={'default.target' if user_mode else 'multi-user.target'}",
        "",
    ]
    return "\n".join(lines)


def _systemctl(user_mode: bool, *args: str) -> subprocess.CompletedProcess[bytes]:
    cmd = ["systemctl"]
    if user_mode:
        cmd.append("--user")
    cmd += list(args)
    return subprocess.run(cmd, check=False)  # noqa: S603 — argv built internally


def _require_systemd() -> None:
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        console.print("[red]`hle service` requires Linux with systemd (systemctl not found).[/red]")
        raise SystemExit(1)


def _unit_dir(user_mode: bool) -> Path:
    if user_mode:
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _SYSTEM_UNIT_DIR


@click.group()
def service() -> None:
    """Install and manage a systemd service for a tunnel."""


@service.command("install")
@click.option("--service", "service_url", required=True, help="Local service URL")
@click.option("--label", required=True, help="Service label (also names the unit hle-<label>)")
@click.option("--zone", default=None, help="Custom zone to publish under")
@click.option("--apex", is_flag=True, default=False, help="Serve at the bare zone root")
@click.option("--auth", type=click.Choice(["sso", "none"]), default="sso", help="Auth mode")
@click.option("--websocket/--no-websocket", default=True, help="Enable WebSocket proxying")
@click.option("--verify-ssl", is_flag=True, default=False, help="Verify upstream TLS cert")
@click.option("--forward-host", is_flag=True, default=False, help="Forward the browser Host header")
@click.option("--allow", multiple=True, metavar="[PROVIDER:]EMAIL", help="SSO allow rule (repeat)")
@click.option(
    "--option", "options", multiple=True, metavar="KEY=VALUE", help="Passthrough (repeat)"
)
@click.option("--name", default=None, help="Override the unit name (default hle-<label>)")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Install a --user unit")
@click.option("--run-as", "run_as", default=None, help="System unit User= (default: current user)")
@click.option("--start/--no-start", default=True, help="Enable + start the service now")
def install(
    service_url: str,
    label: str,
    zone: str | None,
    apex: bool,
    auth: str,
    websocket: bool,
    verify_ssl: bool,
    forward_host: bool,
    allow: tuple[str, ...],
    options: tuple[str, ...],
    name: str | None,
    user_mode: bool,
    run_as: str | None,
    start: bool,
) -> None:
    """Install (and start) a systemd service running this tunnel.

    The API key is read from the running user's ~/.config/hle/config.toml or
    HLE_API_KEY at runtime — it is never written into the unit file. For a
    system unit, pass --run-as <user> (defaults to the current user) so the
    service reads that user's config.
    """
    _require_systemd()
    expose_args = build_expose_args(
        service=service_url,
        label=label,
        zone=zone,
        apex=apex,
        auth=auth,
        websocket=websocket,
        verify_ssl=verify_ssl,
        forward_host=forward_host,
        allow=allow,
        options=options,
    )
    run_as_user = run_as or (None if user_mode else getpass.getuser())
    unit = render_unit(
        label=label,
        hle_path=find_hle_path(),
        expose_args=expose_args,
        user_mode=user_mode,
        run_as_user=run_as_user,
    )
    uname = unit_name(label, name)
    path = _unit_dir(user_mode) / uname
    try:
        path.write_text(unit)
    except PermissionError:
        console.print(
            f"[red]Permission denied writing {path}.[/red] "
            "Re-run with sudo, or use --user for a per-user service."
        )
        raise SystemExit(1) from None

    console.print(f"[green]Wrote[/green] {path}")
    _systemctl(user_mode, "daemon-reload")
    if start:
        result = _systemctl(user_mode, "enable", "--now", uname)
        if result.returncode == 0:
            console.print(f"[green]Started[/green] {uname}")
        else:
            console.print(f"[yellow]Installed but failed to start {uname}.[/yellow]")
    else:
        console.print(f"Run: systemctl {'--user ' if user_mode else ''}enable --now {uname}")


@service.command("uninstall")
@click.option("--label", required=True, help="Service label")
@click.option("--name", default=None, help="Explicit unit name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a --user unit")
def uninstall(label: str, name: str | None, user_mode: bool) -> None:
    """Stop, disable, and remove a tunnel's systemd service."""
    _require_systemd()
    uname = unit_name(label, name)
    _systemctl(user_mode, "disable", "--now", uname)
    path = _unit_dir(user_mode) / uname
    if path.exists():
        try:
            path.unlink()
            console.print(f"[green]Removed[/green] {path}")
        except PermissionError:
            console.print(f"[red]Permission denied removing {path}[/red] (try sudo).")
            raise SystemExit(1) from None
    _systemctl(user_mode, "daemon-reload")


@service.command("status")
@click.option("--label", required=True, help="Service label")
@click.option("--name", default=None, help="Explicit unit name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a --user unit")
def status(label: str, name: str | None, user_mode: bool) -> None:
    """Show systemctl status for a tunnel's service."""
    _require_systemd()
    _systemctl(user_mode, "status", "--no-pager", unit_name(label, name))


@service.command("list")
@click.option("--user", "user_mode", is_flag=True, default=False, help="List --user units")
def list_services(user_mode: bool) -> None:
    """List installed hle-* services."""
    _require_systemd()
    cmd = ["systemctl"]
    if user_mode:
        cmd.append("--user")
    cmd += ["list-units", "--type=service", "--all", "hle-*"]
    subprocess.run(cmd, check=False)  # noqa: S603 — argv built internally
