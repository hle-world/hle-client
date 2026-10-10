"""``hle update`` — self-upgrade regardless of how the client was installed.

Detects whether the running client lives in a pipx-managed venv, a uv-managed
tool venv, the installer's plain venv, or a system/pip environment, and runs
the matching upgrade. Keeps users from having to remember the install method
(and sidesteps the installer's symlink pitfalls).
"""

from __future__ import annotations

import json
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

# How long the new client gets to rebuild and restart one service. The work is
# a file write and a `systemctl`/`service`/`launchctl` call, so 120s is well
# past generous; it exists so a wedged rebuild cannot hang `hle update`.
_REFRESH_TIMEOUT = 120

# Install-method identifiers.
PIPX = "pipx"
UV = "uv"
VENV = "venv"
PIP = "pip"
BREW = "brew"
# PEP 668: the interpreter's site-packages belong to the OS package manager.
EXTERNALLY_MANAGED = "externally-managed"
# Container/platform-managed installs: the image or chart owns the version, so
# there is nothing for the CLI to upgrade itself.
DOCKER = "docker"
HA_ADDON = "ha-addon"
KUBERNETES = "kubernetes"
# An editable install or a source checkout: upgrading in place would fight git.
EDITABLE = "editable"

# Methods that name themselves through HLE_INSTALL_METHOD. Installers and
# images set it when the install location alone is ambiguous.
_OVERRIDABLE = {PIPX, UV, VENV, PIP, BREW, DOCKER, HA_ADDON, KUBERNETES}


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def is_externally_managed(stdlib: str | None = None) -> bool:
    """True when the interpreter carries a PEP 668 ``EXTERNALLY-MANAGED`` marker."""
    if stdlib is None:
        import sysconfig

        stdlib = sysconfig.get_path("stdlib")
    if not stdlib:
        return False
    return (Path(stdlib) / "EXTERNALLY-MANAGED").is_file()


def is_editable_install(prefix: str) -> bool:
    """True when ``prefix`` holds an editable (PEP 610) install of the client.

    ``pip install -e`` / ``uv pip install -e`` leave a ``direct_url.json`` next
    to the dist-info recording ``dir_info.editable: true``. Upgrading such an
    install replaces the checkout's own files, so the CLI refuses and points at
    git instead.
    """
    root = Path(prefix)
    patterns = (
        "lib/python*/site-packages",
        "lib64/python*/site-packages",
        "Lib/site-packages",
    )
    for site in (d for pat in patterns for d in root.glob(pat)):
        for info in site.glob(f"{_PACKAGE.replace('-', '_')}-*.dist-info"):
            try:
                data = json.loads((info / "direct_url.json").read_text())
            except (OSError, ValueError):
                continue
            if data.get("dir_info", {}).get("editable"):
                return True
    return False


def detect_install_method(
    prefix: str,
    executable: str,
    *,
    base_prefix: str | None = None,
    stdlib: str | None = None,
    env: dict[str, str] | None = None,
    dockerenv: bool = False,
) -> str:
    """Classify the environment the running client lives in.

    ``prefix`` is ``sys.prefix`` (the venv/environment root); ``executable``
    is ``sys.executable``. ``base_prefix`` and ``stdlib`` default to the
    running interpreter's and exist so tests can describe another one.

    ``env`` and ``dockerenv`` describe the container/platform the client runs
    in. They are passed explicitly (rather than read here) so the pure path
    classification stays deterministic for callers and tests that do not care
    about container detection; ``hle update`` passes the real ones.
    """
    if base_prefix is None:
        base_prefix = sys.base_prefix
    env = env or {}

    # An explicit marker wins over any inference. Installers set it when the
    # path is ambiguous (the current images set no marker of their own).
    override = env.get("HLE_INSTALL_METHOD", "").strip().lower()
    if override in _OVERRIDABLE:
        return override

    # The HA add-on container always carries the Supervisor's token.
    if env.get("SUPERVISOR_TOKEN") or env.get("HASSIO"):
        return HA_ADDON

    if is_editable_install(prefix):
        return EDITABLE

    method = _path_method(prefix, executable, base_prefix, stdlib)

    # Generic container markers (/.dockerenv, KUBERNETES_SERVICE_HOST) are only
    # trusted when the client sits in a bare system pip install, which is how
    # the hle-docker image ships it. A dev container or pod running a
    # pipx/uv/venv install keeps its own upgrade path.
    if method in (PIP, EXTERNALLY_MANAGED):
        if env.get("KUBERNETES_SERVICE_HOST"):
            return KUBERNETES
        if dockerenv or _env_truthy(env.get("HLE_IN_DOCKER")):
            return DOCKER
    return method


def _path_method(prefix: str, executable: str, base_prefix: str, stdlib: str | None) -> str:
    """Classify by where the interpreter lives, ignoring container markers."""
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
    method = detect_install_method(
        sys.prefix,
        sys.executable,
        env=dict(os.environ),
        dockerenv=Path("/.dockerenv").exists(),
    )
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

    if method == DOCKER:
        raise HleError(
            "This client runs inside the hle-docker image; the image owns its version.",
            hint=(
                "Pull the new image and recreate the container:\n"
                "  docker compose pull && docker compose up -d"
            ),
        )

    if method == HA_ADDON:
        raise HleError(
            "This client runs inside the Home Assistant add-on; the add-on owns its version.",
            hint="Update the add-on from Settings → Add-ons.",
        )

    if method == KUBERNETES:
        raise HleError(
            "This client runs in Kubernetes; the hle-operator chart owns its version.",
            hint="Upgrade the hle-operator chart:\n  helm upgrade",
        )

    if method == EDITABLE:
        raise HleError(
            "This is a development install (editable or a source checkout).",
            hint="Update it with:\n  git pull",
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


def _new_client_exec(home: Path | None, new_version: str | None) -> str | None:
    """``current/bin/hle`` when the upgrade repointed it at a different release.

    The service file must be rebuilt by the *new* client, not the process
    running the upgrade: the old code would stamp its own ``__version__`` and
    write the old service format, so a fix to that format in release N would
    not apply to anyone updating to N. ``current`` already points at the new
    version, so running it from here is the new code.
    """
    if home is None or not new_version or new_version == __version__:
        return None
    from hle_client.agent_update import current_exec_path

    exe = current_exec_path(home)
    return str(exe) if exe.exists() else None


def _refresh_with_new_client(executable: str, svc: str) -> str | None:
    """Rebuild one service by running the just-installed client.

    Returns ``"refreshed"``/``"failed"`` from the child's exit code, or ``None``
    when the new binary could not be started at all — the caller then rebuilds
    in-process rather than claim a result it never saw.
    """
    argv = [executable, "daemon", "refresh", svc]
    try:
        result = subprocess.run(  # noqa: S603 — argv built internally
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_REFRESH_TIMEOUT,
        )
    except OSError:
        return None
    except subprocess.SubprocessError:
        return "failed"
    return "refreshed" if result.returncode == 0 else "failed"


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

    # Where the upgrade installed a side-by-side version, rebuild through the
    # new binary: it stamps the new version and writes the new service format,
    # which the old code cannot do for itself. pipx, Homebrew and plain-pip
    # installs have no such layout, so they keep the in-process path.
    new_hle = _new_client_exec(home, new_version)

    failed = []
    needs_root = False
    for svc, user_mode in services:
        scope = "user" if user_mode else "system"
        # Rebuild, not just restart. The service file records the path the
        # previous client lived at, and an upgrade can move it — a restart
        # then faithfully re-runs a command that is no longer there.
        outcome = _refresh_with_new_client(new_hle, svc) if new_hle else None
        if outcome is None:
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
        hint = "Check 'hle daemon status agent' and the service log."
        if needs_root and os.geteuid() != 0:
            # `sudo hle daemon restart --all` is the obvious next thing to try
            # and it does not work: hle lives in ~/.local/bin, which sudo's
            # secure_path drops. Give the command that does.
            units = " ".join(svc for svc, user_mode in failed if not user_mode)
            hint += f"\nRun: sudo systemctl restart {units}"
        raise HleError(
            "Some services did not restart. They are still on the old version.", hint=hint
        )
