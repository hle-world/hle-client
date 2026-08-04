"""``hle service`` — install and manage a background service.

Generates a service definition that runs either ``hle expose ...`` (a single
tunnel) or ``hle agent run`` (the dashboard-driven multi-tunnel agent), so a
homelab stays reachable across reboots and restarts on failure without the user
hand-writing service files.

Backends:
  * Linux   → systemd unit (``systemctl`` / ``/etc/systemd/system`` or ``--user``)
  * macOS   → launchd plist (``launchctl`` / LaunchDaemons or LaunchAgents)
  * FreeBSD → rc.d script (``service`` / ``/usr/local/etc/rc.d``), which is also
    how this runs on pfSense and OPNsense

Windows is not supported (use Task Scheduler / NSSM manually).
"""

from __future__ import annotations

import getpass
import os
import re
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
# FreeBSD keeps ports/packages rc scripts here, separate from base system rc.d.
_RCD_DIR = Path("/usr/local/etc/rc.d")

# Label used for the agent's unit/plist when the user doesn't override it.
AGENT_LABEL = "agent"


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


def build_agent_args(
    *,
    relay_host: str | None = None,
    relay_port: int | None = None,
) -> list[str]:
    """Build the ``agent run`` argv (no secrets) for the service definition.

    The enrollment token is *not* baked in — the agent reads it at runtime from
    the running user's ``~/.config/hle/agent.toml`` or ``HLE_AGENT_TOKEN``.
    """
    args = ["agent", "run"]
    if relay_host:
        args += ["--relay-host", relay_host]
    if relay_port:
        args += ["--relay-port", str(relay_port)]
    return args


def build_fp_args(
    *,
    agent: str,
    target: str,
    bind_port: int | None = None,
    bind_host: str | None = None,
    relay_host: str | None = None,
    relay_port: int | None = None,
) -> list[str]:
    """Build the ``fp`` argv (no secrets) for the service definition.

    The API key is read at runtime from the running user's config or
    ``HLE_API_KEY``, never written into the unit.
    """
    args = ["fp", "--agent", agent, "--to", target]
    if bind_port:
        args += ["--port", str(bind_port)]
    if bind_host:
        args += ["--bind", bind_host]
    if relay_host:
        args += ["--relay-host", relay_host]
    if relay_port:
        args += ["--relay-port", str(relay_port)]
    return args


def fp_label(agent: str, target: str) -> str:
    """Unit label for a forward, e.g. ``fp-rpi-22`` -> ``hle-fp-rpi-22.service``.

    Includes the port so several forwards through one agent can coexist.
    """
    port = target.rsplit(":", 1)[-1] if ":" in target else target
    safe_agent = "".join(c if c.isalnum() or c in "-_" else "-" for c in agent)
    safe_port = "".join(c for c in port if c.isalnum())
    return f"fp-{safe_agent}-{safe_port}" if safe_port else f"fp-{safe_agent}"


def resolve_user_mode(*, user_flag: bool, system_flag: bool) -> bool:
    """Decide between a per-user and a system service.

    Explicit ``--user`` / ``--system`` always win. Otherwise auto-detect: root
    installs a system service (starts at boot), non-root installs a per-user one
    (no sudo needed).
    """
    if user_flag and system_flag:
        console.print("[red]Pass either --user or --system, not both.[/red]")
        raise SystemExit(1)
    if user_flag:
        return True
    if system_flag:
        return False
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    return not is_root


def current_platform() -> str:
    """Return ``"linux"``, ``"darwin"``, ``"freebsd"``, or the raw platform value."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("freebsd"):
        return "freebsd"
    return sys.platform


def _require_supported() -> str:
    """Ensure the running platform has a supported service backend.

    Returns the platform key (``"linux"`` / ``"darwin"`` / ``"freebsd"``) or
    exits with a clear message on Windows / anything else.
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
    if plat == "freebsd":
        if shutil.which("service") is None:
            console.print("[red]`hle service` needs rc.d (service(8) not found).[/red]")
            raise SystemExit(1)
        return plat
    console.print(
        f"[red]`hle service` is not supported on this platform ({plat}).[/red]\n"
        "Supported: Linux (systemd), macOS (launchd), FreeBSD/pfSense (rc.d). "
        "On Windows, use Task Scheduler or NSSM to run `hle expose ...`."
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
    run_args: list[str],
    user_mode: bool,
    run_as_user: str | None,
    description: str | None = None,
    restart: str = "on-failure",
) -> str:
    """Render the systemd unit file text. Pure function (unit-testable)."""
    exec_start = f"{hle_path} {_quote_exec_args(run_args)}"
    lines = [
        "[Unit]",
        f"Description={description or f'HLE tunnel: {label}'}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        f"Restart={restart}",
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
    run_args: list[str],
    name: str | None,
    user_mode: bool,
    run_as: str | None,
    start: bool,
    description: str | None = None,
    restart: str = "on-failure",
) -> None:
    run_as_user = run_as or (None if user_mode else getpass.getuser())
    unit = render_unit(
        label=label,
        hle_path=find_hle_path(),
        run_args=run_args,
        user_mode=user_mode,
        run_as_user=run_as_user,
        description=description,
        restart=restart,
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

    if user_mode:
        # Per-user units stop when the user logs out unless lingering is on.
        console.print(
            f"[dim]Tip: run `sudo loginctl enable-linger {getpass.getuser()}` so the "
            f"service keeps running after logout and starts at boot.[/dim]"
        )


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
    run_args: list[str],
    run_as_user: str | None,
    log_dir: str,
) -> str:
    """Render a launchd plist. Pure function (unit-testable).

    ``run_as_user`` adds a ``UserName`` key (system daemons only); pass ``None``
    for per-user agents that already run as the invoking user.
    """
    prog = [hle_path, *run_args]
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
    run_args: list[str],
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
        run_args=run_args,
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
# --------------------------------------------------------------------------- #
# rc.d backend (FreeBSD, pfSense, OPNsense)
# --------------------------------------------------------------------------- #
def rc_service_name(label: str, name: str | None = None) -> str:
    """rc.d script name for a label.

    rc.d derives a shell variable (``<name>_enable``) from the script name, so
    the name has to be a valid sh identifier — hyphens become underscores.
    """
    base = name or f"hle_{label}"
    return re.sub(r"[^A-Za-z0-9_]", "_", base)


def _rc_quote(value: str) -> str:
    """Single-quote a value for safe interpolation into the rc script."""
    return "'" + value.replace("'", "'\\''") + "'"


def render_rc_script(
    *,
    label: str,
    hle_path: str,
    run_args: list[str],
    run_as_user: str | None = None,
    name: str | None = None,
    description: str | None = None,
    restart: bool = True,
) -> str:
    """Render the rc.d script text. Pure function (unit-testable).

    ``hle`` runs in the foreground, so daemon(8) does the backgrounding. With
    ``restart`` it also re-launches the process if it exits, which is the rc.d
    equivalent of systemd's ``Restart=on-failure``.
    """
    svc = rc_service_name(label, name)
    args = " ".join(_rc_quote(a) for a in run_args)
    daemon_flags = "-f" + (" -r" if restart else "")
    lines = [
        "#!/bin/sh",
        "#",
        f"# {description or f'HLE tunnel: {label}'}",
        "# Generated by `hle service install` — edits are lost on reinstall.",
        "#",
        f"# PROVIDE: {svc}",
        "# REQUIRE: NETWORKING DAEMON",
        "# KEYWORD: shutdown",
        "",
        ". /etc/rc.subr",
        "",
        f'name="{svc}"',
        f'rcvar="{svc}_enable"',
        "",
        "load_rc_config $name",
        "",
        f': ${{{svc}_enable:="NO"}}',
        f': ${{{svc}_user:="{run_as_user or "root"}"}}',
        "",
        'pidfile="/var/run/${name}.pid"',
        'logfile="/var/log/${name}.log"',
        "",
        f"hle_command={_rc_quote(hle_path)}",
        'command="/usr/sbin/daemon"',
        # -P tracks the daemon(8) supervisor, -p the hle process itself, so
        # `service ... status` reports on the thing that actually matters.
        f'command_args="{daemon_flags} -P ${{pidfile}} -p /var/run/${{name}}.child.pid '
        f'-o ${{logfile}} ${{hle_command}} {args}"',
        'procname="/usr/sbin/daemon"',
        "",
        'run_rc_command "$1"',
        "",
    ]
    return "\n".join(lines)


def _rcd_reject_user_mode(user_mode: bool) -> None:
    """rc.d has no per-user services — say so instead of failing on a write."""
    if user_mode:
        console.print(
            "[red]rc.d has no per-user services.[/red] "
            "Run as root (or pass --system) to manage an hle service on FreeBSD."
        )
        raise SystemExit(1)


def _rcd_path(svc: str) -> Path:
    return _RCD_DIR / svc


def _service_cmd(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["service", *args], check=False)  # noqa: S603 — argv built internally


def _sysrc(assignment: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["sysrc", assignment], check=False)  # noqa: S603 — argv built internally


def _rcd_install(
    *,
    label: str,
    run_args: list[str],
    name: str | None,
    run_as: str | None,
    start: bool,
    description: str | None = None,
    restart: bool = True,
) -> None:
    svc = rc_service_name(label, name)
    script = render_rc_script(
        label=label,
        hle_path=find_hle_path(),
        run_args=run_args,
        run_as_user=run_as,
        name=name,
        description=description,
        restart=restart,
    )
    path = _rcd_path(svc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script)
        path.chmod(0o755)
    except PermissionError:
        console.print(
            f"[red]Permission denied writing {path}.[/red] Re-run as root "
            "(rc.d has no per-user services)."
        )
        raise SystemExit(1) from None

    console.print(f"[green]Wrote[/green] {path}")
    _sysrc(f"{svc}_enable=YES")
    if start:
        result = _service_cmd(svc, "start")
        if result.returncode == 0:
            console.print(f"[green]Started[/green] {svc}")
        else:
            console.print(
                f"[yellow]Installed but failed to start {svc}.[/yellow] Check /var/log/{svc}.log"
            )
    else:
        console.print(f"Run: service {svc} start")


def _rcd_uninstall(*, label: str, name: str | None) -> None:
    svc = rc_service_name(label, name)
    _service_cmd(svc, "stop")
    _sysrc(f"-x {svc}_enable")
    path = _rcd_path(svc)
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        console.print(f"[red]Permission denied removing {path}.[/red] Re-run as root.")
        raise SystemExit(1) from None
    console.print(f"[green]Removed[/green] {path}")


def _rcd_status(*, label: str, name: str | None) -> None:
    svc = rc_service_name(label, name)
    _service_cmd(svc, "status")


def _rcd_list() -> None:
    if not _RCD_DIR.exists():
        console.print("No hle services installed.")
        return
    found = sorted(p.name for p in _RCD_DIR.glob("hle_*"))
    if not found:
        console.print("No hle services installed.")
        return
    for svc in found:
        console.print(svc)


def _resolve_label(label: str | None, agent_mode: bool) -> str:
    """Resolve the label for uninstall/status: --agent implies the agent label."""
    if label:
        return label
    if agent_mode:
        return AGENT_LABEL
    console.print("[red]--label is required[/red] (or pass --agent for the agent service).")
    raise SystemExit(1)


@click.group()
def service() -> None:
    """Install and manage a background service (systemd/launchd)."""


@service.command("install")
@click.option(
    "--agent",
    "agent_mode",
    is_flag=True,
    default=False,
    help="Install the dashboard-driven agent (`hle agent run`) instead of a single tunnel",
)
@click.option(
    "--fp",
    "fp_mode",
    is_flag=True,
    default=False,
    help="Install a firepuncher forward (`hle fp`) — needs --agent-name and --to",
)
@click.option("--agent-name", default=None, help="fp mode: agent to forward through")
@click.option("--to", "fp_target", default=None, metavar="HOST:PORT", help="fp mode: target")
@click.option("--port", "fp_port", default=None, type=int, help="fp mode: local port to bind")
@click.option("--bind", "fp_bind", default=None, help="fp mode: local address to bind")
@click.option("--relay-host", default=None, help="Agent/fp mode: relay host (default hle.world)")
@click.option(
    "--relay-port", default=None, type=int, help="Agent/fp mode: relay port (default 443)"
)
@click.option("--service", "service_url", default=None, help="Local service URL")
@click.option("--label", default=None, help="Service label (also names the unit hle-<label>)")
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
@click.option(
    "--system", "system_mode", is_flag=True, default=False, help="Install a system-wide service"
)
@click.option("--run-as", "run_as", default=None, help="System service user (default: current)")
@click.option("--start/--no-start", default=True, help="Enable + start the service now")
def install(
    agent_mode: bool,
    fp_mode: bool,
    agent_name: str | None,
    fp_target: str | None,
    fp_port: int | None,
    fp_bind: str | None,
    relay_host: str | None,
    relay_port: int | None,
    service_url: str | None,
    label: str | None,
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
    system_mode: bool,
    run_as: str | None,
    start: bool,
) -> None:
    """Install (and start) a background service.

    Three modes:

    \b
      (default)  a single tunnel — requires --service and --label
      --agent    the dashboard-driven agent, serving every endpoint you
                 declare in the dashboard from one process
      --fp       a firepuncher forward, so a remote port is always
                 available locally:
                   hle service install --fp --agent-name rpi --to 22 --port 9922

    Credentials are never written into the service file. They are read at
    runtime from the running user's ~/.config/hle/ (config.toml for the API key,
    agent.toml for the agent token) or from HLE_API_KEY / HLE_AGENT_TOKEN. For a
    system service, pass --run-as <user> (defaults to the current user) so the
    service reads that user's config.

    Scope is auto-detected when neither --user nor --system is given: root
    installs a system service, a normal user installs a per-user one.
    """
    plat = _require_supported()
    user_mode = resolve_user_mode(user_flag=user_mode, system_flag=system_mode)

    if agent_mode and fp_mode:
        console.print("[red]Pass either --agent or --fp, not both.[/red]")
        raise SystemExit(1)

    if fp_mode:
        if not agent_name or not fp_target:
            console.print(
                "[red]--agent-name and --to are required with --fp.[/red]\n"
                "Example: hle service install --fp --agent-name rpi --to 22 --port 9922"
            )
            raise SystemExit(1)
        label = label or fp_label(agent_name, fp_target)
        run_args = build_fp_args(
            agent=agent_name,
            target=fp_target,
            bind_port=fp_port,
            bind_host=fp_bind,
            relay_host=relay_host,
            relay_port=relay_port,
        )
        description = f"HLE firepuncher: {agent_name} {fp_target}"
        # A forward is only useful if it's there when you reach for it, so keep
        # it up across relay blips rather than only on failure.
        restart = "always"
    elif agent_mode:
        if service_url:
            console.print("[red]--service is not used with --agent.[/red] Endpoints come from")
            console.print("the dashboard. Drop --service, or drop --agent for a single tunnel.")
            raise SystemExit(1)
        label = label or AGENT_LABEL
        run_args = build_agent_args(relay_host=relay_host, relay_port=relay_port)
        description = "HLE agent (dashboard-managed tunnels)"
        # The agent is the homelab's front door: always bring it back, not just
        # on failure (a clean exit from a dropped control channel still needs a
        # restart).
        restart = "always"
    else:
        if not service_url or not label:
            console.print(
                "[red]--service and --label are required[/red] (or pass --agent to install "
                "the dashboard-managed agent)."
            )
            raise SystemExit(1)
        run_args = build_expose_args(
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
        description = f"HLE tunnel: {label}"
        restart = "on-failure"

    if plat == "freebsd":
        _rcd_reject_user_mode(user_mode)
        _rcd_install(
            label=label,
            run_args=run_args,
            name=name,
            run_as=run_as,
            start=start,
            description=description,
            restart=restart != "no",
        )
    elif plat == "darwin":
        _launchd_install(
            label=label,
            run_args=run_args,
            name=name,
            user_mode=user_mode,
            run_as=run_as,
            start=start,
        )
    else:
        _systemd_install(
            label=label,
            run_args=run_args,
            name=name,
            user_mode=user_mode,
            run_as=run_as,
            start=start,
            description=description,
            restart=restart,
        )

    if agent_mode:
        console.print(
            "\n[dim]Add endpoints at https://hle.world/dashboard — the agent picks them "
            "up within seconds, no restart needed.[/dim]"
        )
    elif fp_mode:
        port_hint = fp_port or "the bound port"
        console.print(
            f"\n[dim]The forward is now always on. Point your client at "
            f"{fp_bind or '127.0.0.1'}:{port_hint}.[/dim]"
        )


@service.command("uninstall")
@click.option("--agent", "agent_mode", is_flag=True, default=False, help="Target the agent service")
@click.option("--label", default=None, help="Service label")
@click.option("--name", default=None, help="Explicit unit/plist name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a per-user service")
@click.option(
    "--system", "system_mode", is_flag=True, default=False, help="Target a system service"
)
def uninstall(
    agent_mode: bool, label: str | None, name: str | None, user_mode: bool, system_mode: bool
) -> None:
    """Stop, disable, and remove a background service."""
    plat = _require_supported()
    label = _resolve_label(label, agent_mode)
    user_mode = resolve_user_mode(user_flag=user_mode, system_flag=system_mode)
    if plat == "freebsd":
        _rcd_reject_user_mode(user_mode)
        _rcd_uninstall(label=label, name=name)
    elif plat == "darwin":
        _launchd_uninstall(label=label, name=name, user_mode=user_mode)
    else:
        _systemd_uninstall(label=label, name=name, user_mode=user_mode)


@service.command("status")
@click.option("--agent", "agent_mode", is_flag=True, default=False, help="Target the agent service")
@click.option("--label", default=None, help="Service label")
@click.option("--name", default=None, help="Explicit unit/plist name")
@click.option("--user", "user_mode", is_flag=True, default=False, help="Target a per-user service")
@click.option(
    "--system", "system_mode", is_flag=True, default=False, help="Target a system service"
)
def status(
    agent_mode: bool, label: str | None, name: str | None, user_mode: bool, system_mode: bool
) -> None:
    """Show status for a background service."""
    plat = _require_supported()
    label = _resolve_label(label, agent_mode)
    user_mode = resolve_user_mode(user_flag=user_mode, system_flag=system_mode)
    if plat == "freebsd":
        _rcd_reject_user_mode(user_mode)
        _rcd_status(label=label, name=name)
    elif plat == "darwin":
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
    elif plat == "freebsd":
        _rcd_list()
    else:
        _systemd_list(user_mode=user_mode)
