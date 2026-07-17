"""``hle service`` — install and manage a background service for a tunnel.

Generates a service definition that runs ``hle expose ...`` with the given
options, so a homelab tunnel survives reboots and restarts on failure without
the user hand-writing service files.

Backends:
  * Linux  → systemd unit (``systemctl`` / ``/etc/systemd/system`` or ``--user``)
  * macOS  → launchd plist (``launchctl`` / LaunchDaemons or LaunchAgents)

Windows is not supported (use Task Scheduler / NSSM manually).
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import click
from rich.console import Console

console = Console()

_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
_LAUNCHD_LABEL_PREFIX = "world.hle"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def find_hle_path() -> str:
    """Resolve an absolute path to the ``hle`` executable for the service."""
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


def launchd_label(label: str, name: str | None = None) -> str:
    """launchd Label (reverse-DNS) for a tunnel label (or an explicit name)."""
    if name:
        return name[: -len(".plist")] if name.endswith(".plist") else name
    return f"{_LAUNCHD_LABEL_PREFIX}.{label}"


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
    """Build the ``expose`` argv (no secrets) for the service definition."""
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


def current_platform() -> str:
    """Return ``"linux"``, ``"darwin"``, or the raw ``sys.platform`` value."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return sys.platform


def _require_supported() -> str:
    """Ensure the running platform has a supported service backend.

    Returns the platform key (``"linux"`` / ``"darwin"``) or exits with a
    clear message on Windows / anything else.
    """
    plat = current_platform()
    if plat == "linux":
        if shutil.which("systemctl") is None:
            console.print("[red]`hle service` needs systemd (systemctl not found).[/red]")
            raise SystemExit(1)
        return plat
    if plat == "darwin":
        if shutil.which("launchctl") is None:
            console.print("[red]`hle service` needs launchd (launchctl not found).[/red]")
            raise SystemExit(1)
        return plat
    console.print(
        f"[red]`hle service` is not supported on this platform ({plat}).[/red]\n"
        "Supported: Linux (systemd) and macOS (launchd). On Windows, use "
        "Task Scheduler or NSSM to run `hle expose ...`."
    )
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# systemd backend (Linux)
# --------------------------------------------------------------------------- #
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


def _unit_dir(user_mode: bool) -> Path:
    if user_mode:
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _SYSTEM_UNIT_DIR


def _systemd_install(
    *,
    label: str,
    expose_args: list[str],
    name: str | None,
    user_mode: bool,
    run_as: str | None,
    start: bool,
) -> None:
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


def _systemd_uninstall(*, label: str, name: str | None, user_mode: bool) -> None:
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


def _systemd_status(*, label: str, name: str | None, user_mode: bool) -> None:
    _systemctl(user_mode, "status", "--no-pager", unit_name(label, name))


def _systemd_list(*, user_mode: bool) -> None:
    cmd = ["systemctl"]
    if user_mode:
        cmd.append("--user")
    cmd += ["list-units", "--type=service", "--all", "hle-*"]
    subprocess.run(cmd, check=False)  # noqa: S603 — argv built internally


# --------------------------------------------------------------------------- #
# launchd backend (macOS)
# --------------------------------------------------------------------------- #
def render_launchd_plist(
    *,
    label: str,
    plist_label: str,
    hle_path: str,
    expose_args: list[str],
    run_as_user: str | None,
    log_dir: str,
) -> str:
    """Render a launchd plist. Pure function (unit-testable).

    ``run_as_user`` adds a ``UserName`` key (system daemons only); pass ``None``
    for per-user agents that already run as the invoking user.
    """
    prog = [hle_path, *expose_args]
    prog_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in prog)
    out_log = f"{log_dir.rstrip('/')}/{label}.log"
    err_log = f"{log_dir.rstrip('/')}/{label}.err.log"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        f"    <string>{_xml_escape(plist_label)}</string>",
        "    <key>ProgramArguments</key>",
        "    <array>",
        prog_xml,
        "    </array>",
        "    <key>RunAtLoad</key>",
        "    <true/>",
        "    <key>KeepAlive</key>",
        "    <true/>",
    ]
    if run_as_user:
        lines += ["    <key>UserName</key>", f"    <string>{_xml_escape(run_as_user)}</string>"]
    lines += [
        "    <key>StandardOutPath</key>",
        f"    <string>{_xml_escape(out_log)}</string>",
        "    <key>StandardErrorPath</key>",
        f"    <string>{_xml_escape(err_log)}</string>",
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(lines)


def _launchctl(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["launchctl", *args], check=False)  # noqa: S603 — argv built internally


def _launchd_dir(user_mode: bool) -> Path:
    if user_mode:
        d = Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path("/Library/LaunchDaemons")


def _launchd_log_dir(user_mode: bool) -> str:
    if user_mode:
        d = Path.home() / "Library" / "Logs" / "hle"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    return "/var/log"


def _launchd_install(
    *,
    label: str,
    expose_args: list[str],
    name: str | None,
    user_mode: bool,
    run_as: str | None,
    start: bool,
) -> None:
    # Per-user agents run as the invoking user already; only system daemons
    # need an explicit UserName so the tunnel reads that user's config.
    run_as_user = None if user_mode else (run_as or getpass.getuser())
    plabel = launchd_label(label, name)
    plist = render_launchd_plist(
        label=label,
        plist_label=plabel,
        hle_path=find_hle_path(),
        expose_args=expose_args,
        run_as_user=run_as_user,
        log_dir=_launchd_log_dir(user_mode),
    )
    path = _launchd_dir(user_mode) / f"{plabel}.plist"
    try:
        path.write_text(plist)
    except PermissionError:
        console.print(
            f"[red]Permission denied writing {path}.[/red] "
            "Re-run with sudo, or use --user for a per-user agent."
        )
        raise SystemExit(1) from None

    console.print(f"[green]Wrote[/green] {path}")
    if start:
        # Reload cleanly: unload first (ignore errors) so re-install re-reads.
        _launchctl("unload", str(path))
        result = _launchctl("load", "-w", str(path))
        if result.returncode == 0:
            console.print(f"[green]Loaded[/green] {plabel}")
        else:
            console.print(f"[yellow]Wrote plist but failed to load {plabel}.[/yellow]")
    else:
        console.print(f"Run: launchctl load -w {path}")


def _launchd_uninstall(*, label: str, name: str | None, user_mode: bool) -> None:
    plabel = launchd_label(label, name)
    path = _launchd_dir(user_mode) / f"{plabel}.plist"
    if path.exists():
        _launchctl("unload", "-w", str(path))
        try:
            path.unlink()
            console.print(f"[green]Removed[/green] {path}")
        except PermissionError:
            console.print(f"[red]Permission denied removing {path}[/red] (try sudo).")
            raise SystemExit(1) from None
    else:
        _launchctl("remove", plabel)
        console.print(f"[yellow]No plist at {path}[/yellow] (attempted launchctl remove).")


def _launchd_status(*, label: str, name: str | None, user_mode: bool) -> None:
    plabel = launchd_label(label, name)
    result = _launchctl("list", plabel)
    if result.returncode != 0:
        console.print(f"[yellow]{plabel} is not loaded.[/yellow]")


def _launchd_list(*, user_mode: bool) -> None:
    # launchctl list has no glob; filter its output for our label prefix.
    result = subprocess.run(  # noqa: S603 — argv built internally
        ["launchctl", "list"], check=False, capture_output=True, text=True
    )
    matched = [
        ln
        for ln in result.stdout.splitlines()
        if _LAUNCHD_LABEL_PREFIX in ln or ln.startswith("PID")
    ]
    console.print("\n".join(matched) if matched else "No hle launchd services loaded.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@click.group()
def service() -> None:
    """Install and manage a background service for a tunnel (systemd/launchd)."""


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
@click.option("--name", default=None, help="Override the unit/plist name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Install a per-user service")
@click.option("--run-as", "run_as", default=None, help="System service user (default: current)")
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
    """Install (and start) a background service running this tunnel.

    The API key is read from the running user's ~/.config/hle/config.toml or
    HLE_API_KEY at runtime — it is never written into the service file. For a
    system service, pass --run-as <user> (defaults to the current user) so the
    service reads that user's config.
    """
    plat = _require_supported()
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
    if plat == "darwin":
        _launchd_install(
            label=label,
            expose_args=expose_args,
            name=name,
            user_mode=user_mode,
            run_as=run_as,
            start=start,
        )
    else:
        _systemd_install(
            label=label,
            expose_args=expose_args,
            name=name,
            user_mode=user_mode,
            run_as=run_as,
            start=start,
        )


@service.command("uninstall")
@click.option("--label", required=True, help="Service label")
@click.option("--name", default=None, help="Explicit unit/plist name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a per-user service")
def uninstall(label: str, name: str | None, user_mode: bool) -> None:
    """Stop, disable, and remove a tunnel's background service."""
    plat = _require_supported()
    if plat == "darwin":
        _launchd_uninstall(label=label, name=name, user_mode=user_mode)
    else:
        _systemd_uninstall(label=label, name=name, user_mode=user_mode)


@service.command("status")
@click.option("--label", required=True, help="Service label")
@click.option("--name", default=None, help="Explicit unit/plist name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a per-user service")
def status(label: str, name: str | None, user_mode: bool) -> None:
    """Show status for a tunnel's background service."""
    plat = _require_supported()
    if plat == "darwin":
        _launchd_status(label=label, name=name, user_mode=user_mode)
    else:
        _systemd_status(label=label, name=name, user_mode=user_mode)


@service.command("list")
@click.option("--user", "user_mode", is_flag=True, default=False, help="List per-user services")
def list_services(user_mode: bool) -> None:
    """List installed hle services."""
    plat = _require_supported()
    if plat == "darwin":
        _launchd_list(user_mode=user_mode)
    else:
        _systemd_list(user_mode=user_mode)
