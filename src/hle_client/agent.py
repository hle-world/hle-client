"""HLE agent — one process, many tunnels, driven by the dashboard.

The agent holds an enrollment token, opens a single control connection to the
server (``/_hle/agent``), receives the desired set of endpoints, and reconciles a
pool of :class:`~hle_client.tunnel.Tunnel` instances to match. Endpoints can be
added/removed/changed from the dashboard at runtime; the agent converges without a
restart. Each endpoint still uses the ordinary tunnel data plane underneath.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform as _platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import websockets
import websockets.exceptions

if TYPE_CHECKING:
    from pathlib import Path

from hle_client import __version__, agent_update, config
from hle_client.discovery import active_providers, scan_all
from hle_client.firepuncher import FpAgentSide
from hle_client.identity import hostname, instance_id
from hle_client.tunnel import Tunnel, TunnelConfig, to_tunnel_config
from hle_common import close_codes
from hle_common.agent_protocol import (
    AgentHello,
    AgentStateSync,
    AgentStatus,
    AgentWelcome,
    EndpointSpec,
    EndpointStatus,
    UpdateAck,
    UpdateProgress,
    UpdateRequest,
    UpdateResult,
    update_capability,
)
from hle_common.discovery import DiscoveryReport
from hle_common.fp_protocol import ForwardRule, default_rules
from hle_common.preflight import PreflightReport, PreflightRequest

logger = logging.getLogger(__name__)

# How often the agent reports endpoint status / keepalive to the server.
STATUS_INTERVAL = 15.0
WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024

# Enrollment token persistence lives in hle_client.config. The names are kept
# here for anything that imported them from this module.
#
# HLE_AGENT_CONFIG is set by `hle daemon install agent` to the file the token
# was actually found in, so the service does not have to reconstruct the path
# from HOME. A service manager starts processes with an environment of its own
# choosing: on pfSense the agent enrolls as `admin` and runs from rc.d, and if
# those two disagree about HOME the token is written to one path and read from
# another. The agent then restarts forever reporting "No agent token" while
# the file it needs is sitting on disk. An absolute path removes the guesswork.
AGENT_CONFIG_PATH = config.AGENT_CONFIG_PATH
AGENT_TOKEN_PREFIX = config.AGENT_TOKEN_PREFIX
AGENT_CONFIG_ENV = config.AGENT_CONFIG_ENV
agent_config_path = config.agent_config_path
save_agent_token = config.save_agent_token
load_agent_token = config.load_agent_token
remove_agent_token = config.remove_agent_token


def detect_service_manager(env: dict[str, str] | None = None) -> str | None:
    """Which service manager started this process, from what it puts in the environment.

    systemd sets ``INVOCATION_ID`` on every unit it starts; launchd sets
    ``XPC_SERVICE_NAME`` (and ``LAUNCH_JOB`` on older releases). rc.d sets
    nothing distinctive, so a pfSense agent reports None even when it is
    supervised — the server treats None as "unknown", not "unsupervised".
    """
    if env is None:
        env = dict(os.environ)
    if env.get("INVOCATION_ID"):
        return "systemd"
    if env.get("XPC_SERVICE_NAME") or env.get("LAUNCH_JOB"):
        return "launchd"
    return None


def detect_install_method() -> str | None:
    """Classify this install for the hello, never raising.

    The classifier lives in the update command; it is imported lazily so an
    agent process does not pay for click at import time, and any failure
    degrades to "unknown" rather than stopping the agent from connecting.
    """
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        from hle_client.update_cmd import detect_install_method as classify

        return classify(sys.prefix, sys.executable)
    except Exception:  # noqa: BLE001 — hello metadata is best-effort
        return None


def _fatal_agent_message(code: int | None, reason: str) -> str:
    """What to tell the operator about a close the agent must not retry.

    The relay's own reason is capped at 123 bytes by RFC 6455, so it can say
    what happened but not what to do next. These supply the second half.
    """
    if code == close_codes.DUPLICATE_INSTANCE:
        return (
            "This agent's token is already in use by another agent that is connected "
            f"and healthy. {reason}\n"
            "This one has stopped rather than take the endpoints off it. Two agents "
            "sharing one token take every tunnel off each other about once a second.\n"
            "Run `hle daemon list` on each machine to find the copy you did not mean "
            "to run, or enroll this machine as its own agent from the dashboard."
        )
    if code == close_codes.REPLACED:
        return (
            f"Another agent took this token over. {reason}\n"
            "This one has stopped rather than take it back — two agents sharing one "
            "token take every tunnel off each other about once a second.\n"
            "If this machine is meant to be the one running it, stop the other copy "
            "and start this service again."
        )
    if code == close_codes.INVALID_CREDENTIAL:
        return (
            f"This agent's enrollment token is invalid or has been revoked. {reason}\n"
            "Re-enroll from the dashboard: https://hle.world/dashboard"
        )
    return f"The relay stopped this agent and asked it not to reconnect (code {code}). {reason}"


# A tunnel-like object: connect() / disconnect() coroutines + is_connected /
# public_url properties. Real impl is hle_client.tunnel.Tunnel; tests inject fakes.
TunnelFactory = Callable[[TunnelConfig], Any]
UpdaterFactory = Callable[[agent_update.UpdateSupport], agent_update.Updater]


def _default_tunnel_factory(cfg: TunnelConfig) -> Tunnel:
    return Tunnel(config=cfg)


@dataclass
class _Running:
    spec: EndpointSpec
    tunnel: Any
    task: asyncio.Task[None]


class AgentClient:
    """Control connection + reconciler over a pool of tunnels."""

    def __init__(
        self,
        token: str,
        relay_host: str = "hle.world",
        relay_port: int = 443,
        *,
        tunnel_factory: TunnelFactory = _default_tunnel_factory,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        home: Path | None = None,
        support_probe: Callable[[], agent_update.UpdateSupport] | None = None,
        updater_factory: UpdaterFactory | None = None,
        health_timeout: float | None = None,
    ) -> None:
        self._token = token
        self._relay_host = relay_host
        self._relay_port = relay_port
        self._tunnel_factory = tunnel_factory
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        # Self-update plumbing. Every external effect — the install classifier,
        # pip, the symlink flip — goes through these so tests can swap them.
        self._home = home or agent_update.hle_home()
        self._support_probe = support_probe or agent_update.can_self_update
        self._updater_factory = updater_factory or (
            lambda support: agent_update.make_updater(support, self._home)
        )
        self._health_timeout = (
            health_timeout if health_timeout is not None else agent_update.health_timeout()
        )
        self._update_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._boot = agent_update.BootCheck("none")
        self._ws: Any = None
        # Non-zero when the process must end so the service manager relaunches
        # it: after a swap (into the new version) or a rollback (into the old).
        self._exit_code = 0
        # Set to cut the reconnect back-off short when the process has to end
        # (a rollback that fired while the control channel was down).
        self._backoff: asyncio.Future[None] | None = None
        agent_update.ensure_log_buffer()
        self._running = False
        # True once the current session reached "registered"; see run().
        self._registered = False
        # Set when the relay closed with a code that must not be retried, so
        # the caller can exit non-zero and print why instead of a service
        # quietly "stopping successfully".
        self._fatal_error: str | None = None
        self._endpoints: dict[str, _Running] = {}
        # Endpoints the server asked for that could not be started: label -> why.
        self._failed: dict[str, str] = {}
        self._api_key: str | None = None
        self._base_domain: str | None = None
        self._fp: FpAgentSide | None = None
        # Until the server tells us otherwise, only loopback is forwardable.
        self._forward_rules: list[ForwardRule] = default_rules()
        # In-flight preflight probes, held so they aren't garbage-collected.
        self._preflight_tasks: set[asyncio.Task[None]] = set()

    # -- public API ----------------------------------------------------------

    @property
    def control_uri(self) -> str:
        scheme = "ws" if self._relay_host.startswith("localhost") else "wss"
        return f"{scheme}://{self._relay_host}:{self._relay_port}/_hle/agent"

    async def run(self) -> None:
        """Run the control connection with reconnection until stopped."""
        self._running = True
        self._arm_watchdog()
        delay = self._reconnect_delay
        while self._running:
            self._registered = False
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                # Explicit shutdown: the `finally` tears the endpoints down,
                # then the cancel propagates so the caller sees it.
                self._running = False
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if close_codes.is_fatal(code):
                    # Reconnecting after one of these cannot help and can do
                    # real harm: two agents on one token that both keep
                    # retrying take each other's endpoints in turn, roughly
                    # once a second, for as long as both are running. The
                    # `finally` below still stops every endpoint on the way out.
                    self._running = False
                    reason = (exc.rcvd.reason if exc.rcvd is not None else "") or ""
                    message = _fatal_agent_message(code, reason)
                    logger.error("%s", message)
                    self._fatal_error = message
                    if self._boot.kind == "watch":
                        # The updated version was turned away for good. Waiting
                        # out the timer would only delay the same rollback.
                        await self._rollback_update(f"relay refused the updated agent: {message}")
                elif not close_codes.should_reconnect(code):
                    # HANDOVER: a successor of ours took the identity over, as
                    # arranged. Not an error — no `_fatal_error`, so the caller
                    # exits 0 — but reconnecting would take the endpoints back
                    # off the process that is supposed to have them now.
                    self._running = False
                    logger.info("Relay handed this agent over to its successor; exiting")
                wait = close_codes.retry_after_seconds(code)
                if wait is not None:
                    logger.warning("Relay asked this agent to slow down (code %s)", code)
                    delay = max(delay, wait)
                    self._registered = False
                logger.warning("Agent control connection lost: %s", exc)
            except Exception as exc:  # noqa: BLE001 — control conn is best-effort
                logger.warning("Agent control connection lost: %s", exc)
            finally:
                # Reset on any session that got as far as registering, not just
                # one that ended cleanly. Relay restarts end the session with an
                # exception, so keying off a clean exit meant the backoff only
                # ever grew: a healthy agent that saw three unrelated blips over
                # a week would then wait 30s to recover from a routine deploy.
                if self._registered:
                    delay = self._reconnect_delay
                # A control blip is not a data-plane outage. The endpoint
                # tunnels hold their own connections to the relay and reconnect
                # on their own, so they keep serving while the control channel
                # comes back. Tearing them down here turned every relay deploy
                # and every dropped control socket into every tunnel going
                # down and re-registering. They stop only when the agent does:
                # a fatal close code, or an explicit shutdown. The welcome on
                # the next session reconciles against the current endpoint
                # list, so anything removed meanwhile is stopped then.
                if not self._running:
                    await self._stop_all()
            if not self._running:
                break
            logger.info("Reconnecting agent control in %.1fs ...", delay)
            # A task so _restart_process() can cut it short; asyncio.wait
            # does not raise when it is cancelled, but an outer cancel of
            # run() still propagates.
            self._backoff = asyncio.ensure_future(asyncio.sleep(delay))
            try:
                await asyncio.wait({self._backoff})
            finally:
                self._backoff.cancel()
                self._backoff = None
            delay = min(delay * 2, self._max_reconnect_delay)
        self._disarm_watchdog()

    @property
    def fatal_error(self) -> str | None:
        """Why the relay stopped this agent for good, if it did."""
        return self._fatal_error

    @property
    def exit_code(self) -> int:
        """Non-zero when the process must exit so its service manager relaunches it.

        Set after a self-update swapped ``current`` (the relaunch is the new
        version) and after a watchdog rollback (the relaunch is the old one).
        Non-zero on purpose: ``Restart=on-failure`` units would not come back
        from a clean exit, and ``Restart=always`` ones do either way.
        """
        return self._exit_code

    async def stop(self) -> None:
        self._running = False
        await self._stop_all()

    # -- control connection --------------------------------------------------

    async def _connect_once(self) -> None:
        logger.info("Connecting agent control to %s", self.control_uri)
        async with websockets.connect(self.control_uri, max_size=WS_MAX_MESSAGE_SIZE) as ws:
            # Advertise what this agent can do so the dashboard only offers
            # features the agent actually supports. Firepuncher is always
            # available; discovery depends on what's detectable here.
            capabilities = ["firepuncher"]
            capabilities += [f"discovery:{p.name}" for p in active_providers()]
            # `update:<method>` is advertised only for installs the agent can
            # stage a new version into itself. The method is sent regardless so
            # the dashboard can tell a brew user what to run.
            install_method = detect_install_method()
            support = self._probe_support()
            update_cap = update_capability(support.method) if support.supported else None
            if update_cap is not None:
                capabilities.append(update_cap)
            hello = AgentHello(
                token=self._token,
                agent_version=__version__,
                capabilities=capabilities,
                instance_id=instance_id(),
                hostname=hostname(),
                install_method=install_method,
                platform=_platform.system().lower() or None,
                python_version=_platform.python_version(),
                service_manager=detect_service_manager(),
            )
            await ws.send(hello.model_dump_json())

            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            welcome = AgentWelcome.model_validate_json(raw)
            self._api_key = welcome.api_key
            self._base_domain = welcome.base_domain
            if welcome.forward_rules:
                self._forward_rules = list(welcome.forward_rules)
            # Firepuncher frames arrive on this same control connection, so the
            # handler is rebuilt per session and torn down with it.
            self._fp = FpAgentSide(
                send=ws.send,
                rules=self._forward_rules,
            )
            # Marks the session as having worked, so the reconnect backoff in
            # run() starts over rather than compounding across the process life.
            self._registered = True
            logger.info(
                "Agent registered: public_id=%s endpoints=%d",
                welcome.agent_public_id,
                len(welcome.endpoints),
            )
            await self.reconcile(welcome.endpoints)
            # Report the inventory once on connect so the dashboard has
            # something to show immediately, then only on request.
            await self._report_discovery(ws)
            self._ws = ws
            await self._after_welcome(ws)

            status_task = asyncio.create_task(self._status_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_message(raw, ws)
            finally:
                self._ws = None
                status_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await status_task
                if self._health_task is not None:
                    self._health_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._health_task
                    self._health_task = None
                if self._fp is not None:
                    await self._fp.close_all()
                    self._fp = None

    async def _handle_message(self, raw: str | bytes, ws: Any = None) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("Bad agent control message")
            return
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "state_sync":
            sync = AgentStateSync.model_validate(msg)
            if sync.forward_rules is not None:
                self._forward_rules = list(sync.forward_rules)
                if self._fp is not None:
                    # Rules are swapped live: revoking access shouldn't require
                    # the operator to restart the agent.
                    self._fp.rules = self._forward_rules
            await self.reconcile(sync.endpoints)
        elif isinstance(mtype, str) and mtype.startswith("fp_"):
            if self._fp is not None:
                await self._fp.handle(msg)
        elif mtype == "discovery_refresh":
            if ws is not None:
                await self._report_discovery(ws)
        elif mtype == "preflight_request":
            if ws is not None:
                # Spawned rather than awaited: a preflight takes seconds, and the
                # control channel has to keep handling state_sync and fp frames
                # meanwhile. Blocking here would stall tunnel reconciliation.
                self._spawn_preflight(msg, ws)
        elif mtype == "pong":
            pass
        elif mtype == "update_request":
            if ws is not None:
                await self._handle_update_request(msg, ws)
        elif isinstance(mtype, str) and mtype.startswith("update_"):
            # The other update_* frames go agent -> server only. Logged at
            # INFO, not debug: the operator pressed a button on the dashboard
            # and this line is the only explanation they will get.
            logger.info("Ignoring %s from the relay: not a request", mtype)
        else:
            logger.debug("Unhandled agent control message: %s", mtype)

    # -- self-update ---------------------------------------------------------

    def _probe_support(self) -> agent_update.UpdateSupport:
        try:
            return self._support_probe()
        except Exception as exc:  # noqa: BLE001 — classification is best-effort
            logger.debug("Could not classify this install: %s", exc)
            return agent_update.UpdateSupport(False, "unknown", "unsupported:unknown")

    def _active_ws_streams(self) -> int:
        """Live browser WebSocket streams across every endpoint tunnel.

        A swap drops them all, so with ``drain_policy == "wait"`` an update is
        refused while any are open. The count comes from what each tunnel
        exposes; a tunnel that tracks nothing counts as idle.
        """
        total = 0
        for running in self._endpoints.values():
            count = getattr(running.tunnel, "active_ws_streams", 0)
            with contextlib.suppress(TypeError, ValueError):
                total += int(count)
        return total

    def _refuse_update_reason(self, req: UpdateRequest) -> str | None:
        """Why *req* cannot be accepted right now, or None to go ahead."""
        if self._update_task is not None and not self._update_task.done():
            return "busy:updating"
        if self._boot.kind == "watch":
            # This process is itself an update still proving it works.
            return "busy:updating"
        support = self._probe_support()
        if not support.supported:
            return support.reason or f"unsupported:{support.method}"
        if req.target_version == __version__:
            return "unsupported:same-version"
        if req.drain_policy == "wait" and self._active_ws_streams() > 0:
            return "busy:draining"
        return None

    async def _handle_update_request(self, msg: dict[str, Any], ws: Any) -> None:
        try:
            req = UpdateRequest.model_validate(msg)
        except ValueError as exc:
            logger.warning("Bad update request: %s", exc)
            return
        # First, before any other check can touch it: the version becomes a
        # directory name and a pip pin in a process that often runs as root.
        try:
            agent_update.validate_version(req.target_version)
        except agent_update.InvalidVersionError:
            logger.warning("Refused update: invalid target_version %r", req.target_version)
            with contextlib.suppress(Exception):
                await ws.send(
                    UpdateAck(
                        request_id=req.request_id,
                        accepted=False,
                        reason="invalid:target_version",
                    ).model_dump_json()
                )
            return
        reason = self._refuse_update_reason(req)
        ack = UpdateAck(request_id=req.request_id, accepted=reason is None, reason=reason)
        with contextlib.suppress(Exception):
            await ws.send(ack.model_dump_json())
        if reason is not None:
            logger.info("Refused update to %s: %s", req.target_version, reason)
            return
        logger.info("Accepted update %s -> %s", __version__, req.target_version)
        # A task, not an await: pip takes a while and the control channel has
        # to keep handling state_sync and pings meanwhile.
        self._update_task = asyncio.create_task(self._run_update(req, ws))

    async def _run_update(self, req: UpdateRequest, ws: Any) -> None:
        async def progress(p: UpdateProgress) -> None:
            with contextlib.suppress(Exception):
                await ws.send(p.model_dump_json())

        try:
            updater = self._updater_factory(self._probe_support())
            await agent_update.run_update(
                req,
                updater=updater,
                home=self._home,
                from_version=__version__,
                send_progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 — every failure is reported the same way
            logger.error("Update to %s failed: %s", req.target_version, exc)
            result = UpdateResult(
                request_id=req.request_id,
                ok=False,
                from_version=__version__,
                to_version=req.target_version,
                log_tail=agent_update.log_tail(),
            )
            with contextlib.suppress(Exception):
                await ws.send(result.model_dump_json())
            return
        # `current` now points at the new version. Nothing more can be
        # reported from here — the result comes from the process that
        # replaces this one. Close cleanly so the relay sees a proper close
        # frame, then let run() unwind and the caller exit non-zero.
        logger.info("Restarting into %s", req.target_version)
        await self._restart_process(ws)

    async def _restart_process(self, ws: Any) -> None:
        self._exit_code = 1
        self._running = False
        if self._backoff is not None:
            self._backoff.cancel()
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    # -- watchdog (the updated process proving itself) -----------------------

    def _arm_watchdog(self) -> None:
        self._boot = agent_update.boot_check(self._home, __version__)
        if self._boot.kind == "watch" and self._boot.state is not None:
            logger.info(
                "Running as update %s (%s -> %s); %.0fs to become healthy",
                self._boot.state.request_id,
                self._boot.state.from_version,
                self._boot.state.to_version,
                self._health_timeout,
            )
            self._watchdog_task = asyncio.create_task(self._watchdog_timer())
        elif self._boot.kind == "report_failed":
            logger.warning("A previous update failed: %s", self._boot.reason)
            self._undo_unverified_swap()

    def _undo_unverified_swap(self) -> None:
        """Repoint ``current`` away from a version that never proved itself.

        Reached when the swap happened but the service manager relaunched
        something other than ``current`` (a unit still naming the flat venv,
        say): this old process came up, so nobody ran the watchdog, and
        ``current`` still names the unverified version. Left alone, the next
        ``daemon refresh`` would start it without a watchdog.
        """
        state = self._boot.state
        if state is None or state.to_version == __version__:
            return
        if agent_update.current_version(self._home) != state.to_version:
            return
        try:
            prev = agent_update.VersionedUpdater(self._home).rollback()
        except agent_update.UpdateError as exc:
            logger.warning("Could not repoint current away from %s: %s", state.to_version, exc)
            return
        logger.warning("Repointed current from unverified %s back to %s", state.to_version, prev)

    def _disarm_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        # The timer itself calls this on its way into a rollback; cancelling
        # the running task would abort that rollback at its next await.
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _watchdog_timer(self) -> None:
        await asyncio.sleep(self._health_timeout)
        if self._boot.kind == "watch":
            await self._rollback_update(f"not healthy within {self._health_timeout:.0f}s")

    async def _after_welcome(self, ws: Any) -> None:
        """Startup bookkeeping that needs a live control connection."""
        if self._boot.kind == "report_failed" and self._boot.state is not None:
            state = self._boot.state
            result = UpdateResult(
                request_id=state.request_id,
                ok=False,
                from_version=state.from_version,
                to_version=state.to_version,
                log_tail=self._boot.log_tail or agent_update.log_tail(),
            )
            if state.request_id != agent_update.LOCAL_REQUEST_ID:
                await ws.send(result.model_dump_json())
            agent_update.clear_failed_marker(self._home)
            agent_update.clear_update_state(self._home)
            self._boot = agent_update.BootCheck("none")
        elif self._boot.kind == "watch":
            self._health_task = asyncio.create_task(self._confirm_update_health(ws))

    def _all_endpoints_connected(self) -> bool:
        return all(bool(r.tunnel.is_connected) for r in self._endpoints.values())

    async def _confirm_update_health(self, ws: Any, poll: float = 0.5) -> None:
        """Declare the update good once every endpoint is up; then report it."""
        while not self._all_endpoints_connected():
            await asyncio.sleep(poll)
        state = self._boot.state
        if self._boot.kind != "watch" or state is None:
            return
        self._boot = agent_update.BootCheck("none")
        self._disarm_watchdog()
        agent_update.clear_update_state(self._home)
        logger.info("Update %s healthy: %s is serving", state.request_id, state.to_version)
        if state.request_id == agent_update.LOCAL_REQUEST_ID:
            return
        result = UpdateResult(
            request_id=state.request_id,
            ok=True,
            from_version=state.from_version,
            to_version=state.to_version,
            log_tail=agent_update.log_tail(),
        )
        with contextlib.suppress(Exception):
            await ws.send(result.model_dump_json())

    async def _rollback_update(self, reason: str) -> None:
        """Put the previous version back and exit so the manager relaunches it."""
        state = self._boot.state
        if self._boot.kind != "watch" or state is None:
            return
        self._boot = agent_update.BootCheck("none")
        self._disarm_watchdog()
        logger.error("Update %s failed: %s — rolling back", state.request_id, reason)
        tail = agent_update.log_tail()
        try:
            updater = self._updater_factory(self._probe_support())
            prev = await asyncio.to_thread(updater.rollback)
            logger.info("Rolled back to %s", prev)
        except Exception as exc:  # noqa: BLE001 — report it, still exit
            reason = f"{reason}; rollback failed: {exc}"
            logger.error("Rollback failed: %s", exc)
        agent_update.write_failed_marker(
            self._home, agent_update.FailedUpdate(state=state, reason=reason, log_tail=tail)
        )
        agent_update.clear_update_state(self._home)
        await self._restart_process(self._ws)

    def _spawn_preflight(self, msg: dict[str, Any], ws: Any) -> None:
        """Run a preflight in the background and reply with the report."""
        task = asyncio.create_task(self._run_preflight(msg, ws))
        # Held so the task isn't garbage-collected mid-flight, and discarded on
        # completion so a long-lived agent doesn't accumulate them.
        self._preflight_tasks.add(task)
        task.add_done_callback(self._preflight_tasks.discard)

    async def _run_preflight(self, msg: dict[str, Any], ws: Any) -> None:
        """Probe a service on the server's behalf and send back what we found.

        A reply is always sent, including on failure: the server is awaiting this
        request_id, and silence would leave the dashboard spinning until timeout
        with nothing to show.
        """
        from hle_client.preflight import run_preflight

        try:
            req = PreflightRequest.model_validate(msg)
        except ValueError as exc:
            logger.warning("Bad preflight request: %s", exc)
            return  # no request_id to answer with

        try:
            report = await run_preflight(
                req.service_url,
                tunnel_host=req.tunnel_host,
                verify_ssl=req.verify_ssl,
                websocket_enabled=req.websocket_enabled,
                forward_host=req.forward_host,
                request_id=req.request_id,
            )
        except Exception as exc:  # noqa: BLE001 — never take the agent down for this
            logger.warning("Preflight failed for %s: %s", req.service_url, exc)
            report = PreflightReport(
                request_id=req.request_id,
                service_url=req.service_url,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )

        with contextlib.suppress(Exception):
            await ws.send(report.model_dump_json())

    async def _report_discovery(self, ws: Any) -> None:
        """Scan every applicable provider and report the inventory.

        Best-effort: discovery failing must never take down the control
        connection, which is what actually keeps tunnels alive.
        """
        try:
            services, providers, error = await scan_all()
        except Exception as exc:  # noqa: BLE001 — discovery is not load-bearing
            logger.warning("Discovery scan failed: %s", exc)
            return
        if not providers:
            return  # nothing to discover here; stay quiet
        report = DiscoveryReport(services=services, providers=providers, error=error)
        with contextlib.suppress(Exception):
            await ws.send(report.model_dump_json())

    async def _status_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            report = AgentStatus(endpoints=self._build_status())
            with contextlib.suppress(Exception):
                await ws.send(report.model_dump_json())

    def _build_status(self) -> list[EndpointStatus]:
        return [
            EndpointStatus(
                label=label,
                connected=bool(r.tunnel.is_connected),
                public_url=r.tunnel.public_url,
            )
            for label, r in self._endpoints.items()
        ] + [
            EndpointStatus(label=label, connected=False, error=reason)
            for label, reason in self._failed.items()
            if label not in self._endpoints
        ]

    # -- reconciler ----------------------------------------------------------

    async def reconcile(self, specs: list[EndpointSpec]) -> None:
        """Converge the running tunnel pool to *specs* (idempotent)."""
        desired = {s.label: s for s in specs}

        # Endpoints that failed to start and are no longer asked for stop
        # being reported.
        for label in list(self._failed):
            if label not in desired:
                del self._failed[label]

        # Remove endpoints no longer desired.
        for label in list(self._endpoints):
            if label not in desired:
                await self._stop_endpoint(label)

        # Add new endpoints; restart changed ones.
        for label, spec in desired.items():
            current = self._endpoints.get(label)
            if current is None:
                self._start_endpoint(spec)
            elif current.spec.reconcile_key() != spec.reconcile_key():
                logger.info("Endpoint %s changed — restarting", label)
                await self._stop_endpoint(label)
                self._start_endpoint(spec)

    def _start_endpoint(self, spec: EndpointSpec) -> None:
        # Data-plane credential: an explicit key from the welcome if the server
        # sent one, otherwise the agent's own token (the server accepts hlea_
        # tokens for tunnel registration). One enrollment, one secret.
        data_key = self._api_key or self._token
        # The whole TunnelSpec (1.3), through the same mapping the CLI uses, so
        # a dashboard endpoint can set everything `hle tunnel create` can. The
        # agent is what manages these tunnels, whatever the spec says.
        try:
            cfg = to_tunnel_config(
                spec,
                api_key=data_key,
                relay_host=self._relay_host,
                relay_port=self._relay_port,
                managed_by="hle-agent",
            )
        except ValueError as exc:
            # One bad endpoint (a malformed basic-auth value, say) must not take
            # the others down with it: skip it, and report it in status so the
            # dashboard shows why instead of showing nothing. The message names
            # the field, never its value.
            reason = f"invalid endpoint: {exc}"
            if self._failed.get(spec.label) != reason:
                logger.error("Endpoint %s not started: %s", spec.label, exc)
            self._failed[spec.label] = reason
            return
        self._failed.pop(spec.label, None)
        tunnel = self._tunnel_factory(cfg)
        task = asyncio.create_task(tunnel.connect())
        self._endpoints[spec.label] = _Running(spec=spec, tunnel=tunnel, task=task)
        logger.info("Endpoint %s started -> %s", spec.label, spec.service_url)

    async def _stop_endpoint(self, label: str) -> None:
        running = self._endpoints.pop(label, None)
        if running is None:
            return
        with contextlib.suppress(Exception):
            await running.tunnel.disconnect()
        running.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running.task
        logger.info("Endpoint %s stopped", label)

    async def _stop_all(self) -> None:
        for label in list(self._endpoints):
            await self._stop_endpoint(label)
