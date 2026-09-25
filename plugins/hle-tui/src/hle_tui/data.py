"""Everything the dashboard shows, read through ``hle_client.ops``.

No HTTP, no paths and no systemd knowledge live here — the CLI's service
layer already owns all of it, and a dashboard that reimplemented any of it
would drift from the commands it is a front end for (it did: this module
once carried its own copy of the relay fetch and of the delete rule). This
module turns ``ops`` results into rows, holds the latest poll in a
:class:`StateStore`, and names the ``hle …`` command each edit stands for.

The one exception is the daemon log tail: ``ops.daemon`` has no log reader
yet, so :func:`daemon_log_tail` finds the log the same way ``hle daemon logs``
does, read-only, through ``service_cmd``'s public helpers.
"""

from __future__ import annotations

import asyncio
import plistlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from hle_client.config import load_api_key
from hle_client.errors import AuthError, HleError, UnreachableError
from hle_client.ops import access as ops_access
from hle_client.ops import agents as ops_agents
from hle_client.ops import auth as ops_auth
from hle_client.ops import daemon as ops_daemon
from hle_client.ops import tunnels as ops_tunnels
from hle_client.ops.models import (
    AccessRule,
    Agent,
    Conflict,
    Daemon,
    ShareLink,
    Tunnel,
    TunnelDetail,
    public_url_for,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hle_client.api import ApiClient

T = TypeVar("T")

LOG_LINES = 200
NO_KEY_LINE = "No API key. Run: hle auth login"


# ---------------------------------------------------------------------------
# One poll
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """One poll of everything, with per-section failure kept separate.

    A section that could not be read is ``None``, which is not the same as an
    empty one: "the relay did not answer" and "you have no tunnels" look
    identical in a table and mean opposite things. Why it could not be read
    rides along in ``*_error``, because "the relay is down" and "the relay
    refused your key" need different advice.
    """

    tunnels: list[Tunnel] | None = None
    agents: list[Agent] | None = None
    daemons: list[Daemon] = field(default_factory=list)
    error: str | None = None
    tunnels_error: HleError | None = None
    agents_error: HleError | None = None


def resolve_api_key(explicit: str | None = None) -> str | None:
    return explicit or load_api_key()


def _client(api_key: str) -> ApiClient:
    # Looked up on the module at call time, so a test that patches
    # ``hle_client.api.ApiClient`` sees the dashboard go through its fake.
    from hle_client import api as api_module

    return api_module.ApiClient(api_module.ApiClientConfig(api_key=api_key))


async def local_daemons() -> list[Daemon]:
    """Installed services, or none when there is no manager to ask."""
    try:
        return await ops_daemon.list_daemons()
    except Exception:  # noqa: BLE001 — no systemd, or no permission, is an answer
        return []


async def _section(awaitable: Awaitable[T]) -> tuple[T | None, HleError | None]:
    try:
        return await awaitable, None
    except HleError as exc:
        return None, exc
    except Exception as exc:  # noqa: BLE001 — ops maps the relay's; this is a bug's
        return None, HleError(str(exc) or exc.__class__.__name__)


def _no_key_line() -> str:
    """What to say when there is no API key, which depends on what there is instead."""
    try:
        local = ops_agents.local_agent_status()
    except Exception:  # noqa: BLE001 — unreadable config is the same as none
        return NO_KEY_LINE
    if local.enrolled:
        # An agent token is a credential, just not one that can read the
        # account. "No API key" alone reads as "you have no credentials".
        return "This machine has an agent token, which cannot read the account. " + NO_KEY_LINE
    return NO_KEY_LINE


async def collect(api_key: str | None = None, *, api: ApiClient | None = None) -> Snapshot:
    """One full poll: tunnels, agents and local daemons, fetched in parallel."""
    key = resolve_api_key(api_key)
    if not key:
        return Snapshot(daemons=await local_daemons(), error=_no_key_line())
    client = api or _client(key)
    (tunnels, tunnels_error), (agents, agents_error), daemons = await asyncio.gather(
        _section(ops_tunnels.list_tunnels(client)),
        _section(ops_agents.list_agents(client)),
        local_daemons(),
    )
    return Snapshot(
        tunnels=tunnels,
        agents=agents,
        daemons=daemons,
        tunnels_error=tunnels_error,
        agents_error=agents_error,
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def agent_key(agent: Agent) -> str:
    return agent.id or agent.name


def daemon_keys(daemons: list[Daemon]) -> list[str]:
    """One key per daemon: its name, or name@scope when both scopes carry it."""
    names = [d.name for d in daemons]
    return [d.name if names.count(d.name) == 1 else f"{d.name}@{d.scope}" for d in daemons]


class StateStore:
    """The latest poll, keyed, with change notification.

    One ``ApiClient`` for the store's life, so every poll and every edit goes
    out with the same credential and the ``user_code`` lookup ``ops`` caches
    per client is made once. Subscribers are called after every refresh,
    successful or not, with the store itself.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = resolve_api_key(api_key)
        self._api: ApiClient | None = None
        self.snapshot = Snapshot()
        self.tunnels: dict[str, Tunnel] = {}
        self.agents: dict[str, Agent] = {}
        self.daemons: dict[str, Daemon] = {}
        self.last_updated: datetime | None = None
        self._subscribers: list[Callable[[StateStore], None]] = []
        self._lock = asyncio.Lock()

    @property
    def api(self) -> ApiClient:
        """The store's client. Raises :class:`AuthError` when there is no key."""
        if not self.api_key:
            raise AuthError(NO_KEY_LINE)
        if self._api is None:
            self._api = _client(self.api_key)
        return self._api

    def client(self) -> ApiClient | None:
        """The store's client, or ``None`` when there is no key to make one with."""
        return self.api if self.api_key else None

    def subscribe(self, callback: Callable[[StateStore], None]) -> Callable[[], None]:
        """Call ``callback(store)`` after each refresh. Returns an unsubscribe."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def refresh(self) -> Snapshot:
        """Poll once. A refresh already in flight is waited for, not doubled."""
        if self._lock.locked():
            async with self._lock:
                return self.snapshot
        async with self._lock:
            snapshot = await collect(self.api_key, api=self.client())
            self._apply(snapshot)
        for callback in list(self._subscribers):
            callback(self)
        return snapshot

    def _apply(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        # A section that failed keeps its last good rows: a relay hiccup
        # should not blank the table the user is acting on. The status line
        # says the data is stale.
        if snapshot.tunnels is not None:
            self.tunnels = {t.subdomain or "?": t for t in snapshot.tunnels}
        if snapshot.agents is not None:
            self.agents = {agent_key(a): a for a in snapshot.agents}
        self.daemons = dict(zip(daemon_keys(snapshot.daemons), snapshot.daemons, strict=True))
        self.last_updated = datetime.now()

    # -- rows, keyed ---------------------------------------------------------
    #
    # Built from the keyed collections rather than the last snapshot, so a
    # section whose refresh failed still shows its last good rows.

    def tunnel_table(self) -> list[tuple[str, tuple[str, ...]]]:
        rows = tunnel_rows(Snapshot(tunnels=list(self.tunnels.values())))
        return list(zip(self.tunnels, rows, strict=True))

    def agent_table(self) -> list[tuple[str, tuple[str, ...]]]:
        rows = agent_rows(Snapshot(agents=list(self.agents.values())))
        return list(zip(self.agents, rows, strict=True))

    def daemon_table(self) -> list[tuple[str, tuple[str, ...]]]:
        rows = daemon_rows(Snapshot(daemons=list(self.daemons.values())))
        return list(zip(self.daemons, rows, strict=True))

    def status_line(self) -> str:
        """The one sentence that says whether the last poll worked."""
        snap = self.snapshot
        if snap.error:
            return snap.error
        if snap.tunnels_error is not None:
            return describe_error(snap.tunnels_error)
        if snap.tunnels is None:
            return "Tunnels could not be read."
        live = sum(1 for t in snap.tunnels if t.online)
        line = f"{live} live of {len(snap.tunnels)} tunnels."
        if snap.agents_error is not None:
            line += f" Agents: {snap.agents_error.message}"
        return line


def describe_error(exc: HleError) -> str:
    """An error as the status line says it: what happened, then what to do."""
    if isinstance(exc, UnreachableError):
        line = "Could not reach the relay."
    elif isinstance(exc, AuthError):
        line = f"The relay refused the API key: {exc.message}"
    else:
        line = exc.message
    hint = exc.hint
    if hint is None and isinstance(exc, AuthError) and exc.status == 401:
        hint = "Run: hle auth login"
    return f"{line} {hint}" if hint else line


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def tunnel_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    if snapshot.tunnels is None:
        return []
    return [
        (
            t.subdomain or "?",
            "live" if t.online else "idle",
            t.service_url,
            t.auth_mode,
        )
        for t in snapshot.tunnels
    ]


def agent_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    """Rows for the agents table.

    ``Agent.state`` decides the word: ``online``, ``offline`` or ``disabled``.
    Reading ``is_online`` off the raw dict once showed every agent as offline,
    which is the most alarming thing this screen can say and was wrong for
    all of them; the mapper in ``ops.models`` is now the only reader.
    """
    if snapshot.agents is None:
        return []
    return [
        (a.name, a.state, str(a.endpoints), a.version or "-", a.last_seen or "-")
        for a in snapshot.agents
    ]


def daemon_rows(snapshot: Snapshot) -> list[tuple[str, ...]]:
    return [(d.name, d.scope, d.kind, d.state or "-") for d in snapshot.daemons]


def public_url(snapshot: Snapshot, subdomain: str) -> str:
    """Where the tunnel answers, from the relay's record rather than a guess."""
    for t in snapshot.tunnels or []:
        if t.subdomain == subdomain:
            return t.public_url
    return public_url_for(subdomain, None)


# Fields the 1.2 agent hello carries. The relay's agent list does not return
# them yet; they are shown the day it does, without a release of this plugin.
AGENT_EXTRA_FIELDS = (
    ("install_method", "Install method"),
    ("platform", "Platform"),
    ("service_manager", "Service manager"),
    ("python_version", "Python"),
    ("created_at", "Enrolled"),
    ("duplicate_hostname", "Also claimed by"),
)


def agent_detail(agent: Agent) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """``(fields, endpoints, gaps)`` for the agent pane.

    ``endpoints`` is the endpoint list when the relay sent one; ``gaps`` names
    what the pane would show but the relay did not say.
    """
    fields = [
        ("Name", agent.name),
        ("State", agent.state),
        ("Version", agent.version or "-"),
        ("Last seen", agent.last_seen or "-"),
        ("Host", agent.hostname or "-"),
        ("Endpoints", str(agent.endpoints)),
    ]
    gaps: list[str] = []
    for key, title in AGENT_EXTRA_FIELDS:
        value = agent.raw.get(key)
        if value not in (None, ""):
            fields.append((title, str(value)))
        elif key in ("install_method", "platform"):
            gaps.append(title.lower())
    endpoints: list[str] = []
    raw_endpoints = agent.raw.get("endpoints")
    if isinstance(raw_endpoints, list):
        for ep in raw_endpoints:
            if isinstance(ep, dict):
                name = ep.get("label") or ep.get("subdomain") or ep.get("name") or "?"
                target = ep.get("service_url") or ep.get("target") or ""
                endpoints.append(f"{name}  {target}".rstrip())
            else:
                endpoints.append(str(ep))
    else:
        gaps.append("endpoint list (the relay sends a count only)")
    return fields, endpoints, gaps


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def delete_tunnel(
    subdomain: str, api_key: str | None = None, *, api: ApiClient | None = None
) -> str:
    """Delete a tunnel's record. Returns a line to show the user."""
    if api is None:
        key = resolve_api_key(api_key)
        if not key:
            return "No API key."
        api = _client(key)
    try:
        result = await ops_tunnels.delete_tunnel(api, subdomain)
    except HleError as exc:
        if exc.exit_code == 4:
            return f"{subdomain} is not in the list any more."
        return describe_error(exc)
    if isinstance(result, Conflict):
        # The same rule the CLI enforces: the record holds the access rules,
        # so removing it under a live connection unprotects a published host.
        return f"{subdomain} is connected — stop it first."
    return f"Deleted {subdomain}."


async def restart_daemon(name: str, user_scope: bool) -> str:
    ok = await ops_daemon.restart(name, user_scope)
    scope = "user" if user_scope else "system"
    return f"Restarted {name} ({scope})." if ok else f"Could not restart {name} ({scope})."


@dataclass
class TunnelGates:
    """Everything the tunnel pane edits, read in one go.

    The allow-list and share links are read from their own endpoints rather
    than from the status payload: removing either needs its id, and the list
    endpoints are where the ids are promised.
    """

    detail: TunnelDetail
    rules: list[AccessRule]
    shares: list[ShareLink] | None
    shares_error: HleError | None = None


async def load_gates(api: ApiClient, subdomain: str) -> TunnelGates:
    detail, rules, (shares, shares_error) = await asyncio.gather(
        ops_tunnels.get_tunnel(api, subdomain),
        ops_access.get_access(api, subdomain),
        _section(ops_auth.list_shares(api, subdomain)),
    )
    return TunnelGates(detail=detail, rules=rules, shares=shares, shares_error=shares_error)


async def add_rule(api: ApiClient, subdomain: str, spec: str) -> AccessRule:
    """Add ``[provider:]email`` — the same spelling ``hle tunnel access replace`` takes."""
    rule = ops_access.parse_spec(spec.strip())
    if not rule.email:
        raise HleError("Type an email address first.")
    return await ops_access.add_rule(api, subdomain, rule.email, rule.provider)


# -- The command each edit stands for ---------------------------------------


def cli_access_add(subdomain: str, spec: str) -> str:
    rule = ops_access.parse_spec(spec.strip())
    provider = "" if rule.provider == "any" else f" --provider {rule.provider}"
    return f"hle tunnel access add {subdomain} {rule.email}{provider}"


def cli_access_remove(subdomain: str, rule_id: int) -> str:
    return f"hle tunnel access remove {subdomain} {rule_id}"


def cli_auth_mode(subdomain: str, mode: str) -> str:
    return f"hle tunnel auth-mode {subdomain} --set {mode}"


def cli_pin(subdomain: str, verb: str) -> str:
    return f"hle tunnel pin {verb} {subdomain}"


def cli_basic_auth(subdomain: str, verb: str) -> str:
    return f"hle tunnel basic-auth {verb} {subdomain}"


def cli_share_create(subdomain: str, duration: str, label: str) -> str:
    extra = f" --label {label!r}" if label else ""
    return f"hle tunnel share create {subdomain} --duration {duration}{extra}"


def cli_share_revoke(subdomain: str, link_id: int) -> str:
    return f"hle tunnel share revoke {subdomain} {link_id}"


def _daemon_target(daemon: Daemon) -> str:
    if daemon.kind == "agent":
        target = "--agent"
    elif daemon.label:
        target = f"--label {daemon.label}"
    else:
        target = f"--name {daemon.name}"
    return f"{target} --{daemon.scope}"


def cli_daemon(verb: str, daemon: Daemon) -> str:
    return f"hle daemon {verb} {_daemon_target(daemon)}"


CLI_CREATE = "hle tunnel create <label> <service-url>"


# ---------------------------------------------------------------------------
# Daemon logs
# ---------------------------------------------------------------------------


def daemon_log_path(daemon: Daemon) -> Path | None:
    """The file a launchd or rc.d service writes to; ``None`` under systemd.

    For launchd the path is read out of the plist the installer wrote, not
    rebuilt, so a service installed by an older client is still found.
    """
    from hle_client import service_cmd

    plat = service_cmd.current_platform()
    if plat == "linux":
        return None
    if plat == "darwin":
        plist = service_cmd.service_file(daemon.name, daemon.user_mode)
        if plist is not None:
            try:
                with plist.open("rb") as fh:
                    out = plistlib.load(fh).get("StandardOutPath")
            except (OSError, plistlib.InvalidFileException, ValueError):
                out = None
            if out:
                return Path(str(out))
        log_dir = Path.home() / "Library" / "Logs" / "hle" if daemon.user_mode else Path("/var/log")
        return log_dir / f"{daemon.label or daemon.name}.log"
    return Path("/var/log") / f"{daemon.name}.log"


def _tail_file(path: Path, lines: int) -> str:
    # Read the end, not the whole file: a service that has logged for months
    # would otherwise be read in full every two seconds.
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 256 * 1024))
        text = fh.read().decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


async def daemon_log_tail(daemon: Daemon, lines: int = LOG_LINES) -> str:
    """The last ``lines`` of a daemon's log. Read-only; raises :class:`HleError`."""
    path = daemon_log_path(daemon)
    if path is None:
        argv = ["journalctl"]
        if daemon.user_mode:
            argv.append("--user")
        argv += ["-u", daemon.name, "-n", str(lines), "--no-pager"]
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
    if not path.exists():
        raise HleError(
            f"No log file at {path}",
            hint=f"The service may never have started. Try: {cli_daemon('status', daemon)}",
        )
    try:
        return await asyncio.to_thread(_tail_file, path, lines)
    except OSError as exc:
        raise HleError(f"Could not read {path}: {exc.strerror or exc}") from None
