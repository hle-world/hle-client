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
import re
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import websockets
import websockets.exceptions

from hle_client import __version__, agent_logs, agent_state, agent_update, config, k8s_targets
from hle_client.discovery import active_providers, scan_all
from hle_client.firepuncher import FpAgentSide
from hle_client.fragmenting import FragmentingConnection
from hle_client.identity import hostname, instance_id
from hle_client.notices import emit_event
from hle_client.proxy import sni_hostname_for
from hle_client.tunnel import Tunnel, TunnelConfig, to_tunnel_config
from hle_common import close_codes
from hle_common.agent_protocol import (
    HANDOVER_CAPABILITY,
    K8S_DECLARED_CAPABILITY,
    AgentHello,
    AgentStateSync,
    AgentStatus,
    AgentWelcome,
    DeclaredAck,
    DeclaredEndpoint,
    DeclaredEndpoints,
    EndpointSpec,
    EndpointStatus,
    UpdateAck,
    UpdateProgress,
    UpdateRequest,
    UpdateResult,
    update_capability,
)
from hle_common.discovery import DiscoveryReport
from hle_common.fp_protocol import ForwardRule, FpMsgType, default_rules
from hle_common.fragmentation import CAPABILITY_FRAGMENTATION
from hle_common.preflight import PreflightReport, PreflightRequest

logger = logging.getLogger(__name__)

# How often the agent reports endpoint status / keepalive to the server.
STATUS_INTERVAL = 15.0
WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024

# Names this process's rolling-update group in the hello. Same rule as the server.
HANDOVER_GROUP_ENV = "HLE_HANDOVER_GROUP"
_HANDOVER_GROUP_RE = re.compile(r"^[a-z0-9]([a-z0-9._/-]*[a-z0-9])?$")
_HANDOVER_GROUP_MAX = 128


def handover_group_from_env(env: dict[str, str] | None = None) -> str | None:
    """``HLE_HANDOVER_GROUP`` if set and valid; invalid -> None plus one warning."""
    value = (os.environ if env is None else env).get(HANDOVER_GROUP_ENV)
    if not value:
        return None
    if len(value) > _HANDOVER_GROUP_MAX or not _HANDOVER_GROUP_RE.match(value):
        logger.warning(
            "Ignoring %s: must be at most %d characters of lowercase letters, digits, "
            "'.', '_', '/' or '-', starting and ending with a letter or digit",
            HANDOVER_GROUP_ENV,
            _HANDOVER_GROUP_MAX,
        )
        return None
    return value


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

    ``HLE_INSTALL_METHOD`` overrides the classifier when set: a Kubernetes pod
    runs from a venv the chart owns, and ``kubernetes`` has to be reported (and
    the self-update capability withheld) rather than the location it happens to
    sit in. Otherwise the classifier lives in the update command; it is imported
    lazily so an agent process does not pay for click at import time, and any
    failure degrades to "unknown" rather than stopping the agent from connecting.
    """
    override = os.environ.get(k8s_targets.INSTALL_METHOD_ENV)
    if override:
        return override
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


def _signal_process(proc: Any, name: str) -> None:
    """Forward a signal to *proc*, or terminate it on Windows. Best-effort."""
    with contextlib.suppress(Exception):
        if os.name == "nt":
            proc.terminate()
            return
        signum = getattr(signal, name, None)
        if signum is not None:
            proc.send_signal(signum)


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
        target_guard: k8s_targets.KubernetesTargetGuard | None = None,
        successor_of: str | None = None,
        successor_nonce: str | None = None,
        successor_spawner: Callable[..., Any] | None = None,
        declares_endpoints: bool = False,
        on_declared_ack: Callable[[DeclaredAck], Any] | None = None,
        spec_resolver: Callable[[EndpointSpec], EndpointSpec] | None = None,
    ) -> None:
        self._token = token
        # Resolve local references the server must never see (e.g. a credential
        # held in a local secret store). Applied to every spec before the
        # reconcile diff; a ValueError marks only that endpoint as failed.
        self._spec_resolver = spec_resolver
        self._handover_group = handover_group_from_env()
        # Opt-in hook for an embedder (the operator) that owns a set of
        # endpoints itself: see send_declared_endpoints(). The capability is
        # advertised only once a caller opts in, never by default.
        self._declares_endpoints = declares_endpoints
        self._on_declared_ack = on_declared_ack
        self._declared: DeclaredEndpoints | None = None
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
        self._canary_task: asyncio.Task[None] | None = None
        self._successor_task: asyncio.Task[None] | None = None
        # Stage B: this process is the canary a running incumbent spawned, and
        # the fields it announces in its hello so the relay admits it as one.
        # ``_canary_probation`` is the live marker: while it is set the hello
        # carries ``successor_of``/``successor_nonce`` and this process must not
        # start an update of its own. Once every endpoint is up the relay knows
        # this process, so the fields are dropped and it is an ordinary agent.
        self._successor_of = successor_of
        self._successor_nonce = successor_nonce
        self._canary_probation = successor_of is not None
        # A handle to the successor the incumbent started (Popen-like: poll()).
        self._successor_proc: Any = None
        self._successor_spawner = successor_spawner or agent_update.spawn_successor
        # True from the moment an incumbent has a successor in flight until it
        # is handed over or the successor fails. While set, a REPLACED close is
        # the expected overlap, not a fight to report as fatal.
        self._update_in_flight = False
        # Set when the relay closed the control channel with REPLACED during a
        # handover: the successor owns control now, so this process holds its
        # tunnels and waits rather than reconnecting.
        self._control_taken = False
        # Set once the relay has closed this incumbent with HANDOVER.
        self._handover_done = False
        # An update_result that could not be sent because the control channel
        # was gone (a handover failing after a REPLACED close). Sent on the
        # next welcome so the dashboard still learns the update failed.
        self._pending_result: UpdateResult | None = None
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
        # In-flight firepuncher opens, keyed by stream id, so data/close for a
        # stream can be ordered behind its own open and a slow open cannot block
        # the control channel.
        self._fp_open_tasks: dict[str, asyncio.Task[None]] = {}
        # In a cluster, an endpoint target has to pass the guard before its
        # tunnel starts. Injected by tests; built from the environment when
        # KUBERNETES_SERVICE_HOST (or HLE_INSTALL_METHOD=kubernetes) says so.
        self._target_guard = target_guard
        if self._target_guard is None and k8s_targets.in_kubernetes():
            self._target_guard = k8s_targets.KubernetesTargetGuard.from_env()
        # True while the on-disk readiness state says this process is connected,
        # so run() writes only on transitions and never per heartbeat.
        self._state_connected = False

    # -- public API ----------------------------------------------------------

    @property
    def control_uri(self) -> str:
        scheme = "ws" if self._relay_host.startswith("localhost") else "wss"
        return f"{scheme}://{self._relay_host}:{self._relay_port}/_hle/agent"

    async def run(self) -> None:
        """Run the control connection with reconnection until stopped."""
        self._running = True
        self._arm_watchdog()
        with contextlib.suppress(OSError):
            agent_state.clear_if_stale(self._home, own_pid=os.getpid())
        delay = self._reconnect_delay
        # Set only when the loop ends normally. A cancel that lands after
        # HANDOVER (e.g. during _stop_all) skips the supervise call below, so
        # the finally must still terminate the successor in that case.
        reached_supervision = False
        try:
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
                    was_live = self._registered
                    # During a Stage B handover the successor briefly holds the
                    # same identity, so the relay may close this incumbent with
                    # REPLACED rather than HANDOVER. That overlap is the
                    # arrangement, not two agents fighting: keep every tunnel up
                    # and wait for the handover to finish, rather than treating
                    # it as fatal.
                    replaced_during_update = code == close_codes.REPLACED and self._update_in_flight
                    if close_codes.is_fatal(code) and not replaced_during_update:
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
                        emit_event(
                            "fatal", level="error", source="agent", message=message, code=code
                        )
                        if self._boot.kind == "watch":
                            # The updated version was turned away for good. Waiting
                            # out the timer would only delay the same rollback.
                            await self._rollback_update(
                                f"relay refused the updated agent: {message}"
                            )
                    elif replaced_during_update:
                        # The successor's control connection took the identity
                        # over while this handover was in flight. The relay talks
                        # to the successor now, so reconnecting here would only
                        # fight it. Hold every tunnel and wait: if the successor
                        # serves, this process becomes its supervisor; if it dies,
                        # resume.
                        self._control_taken = True
                        logger.info(
                            "Successor's control connection took this identity over; "
                            "holding tunnels until the handover settles"
                        )
                    elif not close_codes.should_reconnect(code):
                        # HANDOVER: a successor of ours took the identity over, as
                        # arranged. Stop this process's own tunnels (the `finally`
                        # below does it) and become a thin supervisor of the child
                        # rather than exiting, so the service manager's cgroup/job
                        # stays alive while the successor serves.
                        self._running = False
                        self._update_in_flight = False
                        self._handover_done = True
                        logger.info("Relay handed this agent over to its successor; supervising it")
                    wait = close_codes.retry_after_seconds(code)
                    if wait is not None:
                        logger.warning("Relay asked this agent to slow down (code %s)", code)
                        delay = max(delay, wait)
                        self._registered = False
                    logger.warning("Agent control connection lost: %s", exc)
                    if self._fatal_error is None:
                        self._emit_lost(was_live, str(exc), code)
                except Exception as exc:  # noqa: BLE001 — control conn is best-effort
                    logger.warning("Agent control connection lost: %s", exc)
                    self._emit_lost(self._registered, str(exc), None)
                finally:
                    # Reset on any session that got as far as registering, not
                    # just one that ended cleanly. Relay restarts end the session
                    # with an exception, so keying off a clean exit meant the
                    # backoff only ever grew: a healthy agent that saw three
                    # unrelated blips over a week would then wait 30s to recover
                    # from a routine deploy.
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
                if self._control_taken:
                    # The successor took control. Wait for it to either prove
                    # itself (then this process supervises it) or fail (then this
                    # process reconnects and carries on serving the old version).
                    await self._wait_for_successor_control()
                    if self._handover_done:
                        break
                    self._control_taken = False
                    logger.info("Successor did not take over; reconnecting control to resume")
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
            reached_supervision = self._handover_done
        finally:
            # Runs on every exit, including a stop signal cancelling run() while
            # an update is in flight, during the REPLACED hold, or between
            # HANDOVER and supervision. The successor is in its own session, so
            # the stop never reaches it; unless supervision is about to start,
            # terminate it here or a second agent is left on the same identity.
            self._disarm_watchdog()
            if not reached_supervision:
                await self._terminate_successor()
        if self._handover_done and self._successor_proc is not None:
            await self._supervise_successor()

    @staticmethod
    def _emit_lost(was_live: bool, message: str, code: int | None) -> None:
        """``disconnected`` for a control session that worked, ``error`` for an attempt."""
        if was_live:
            emit_event("disconnected", level="warning", source="agent", message=message, code=code)
        else:
            emit_event("error", level="error", source="agent", message=message, code=code)

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

    def _mark_connected(self) -> None:
        """Record readiness on the first welcome of a session, once."""
        if self._state_connected:
            return
        self._state_connected = True
        try:
            agent_state.write_connected(self._home, os.getpid())
        except OSError as exc:  # noqa: BLE001 — readiness is not load-bearing
            logger.debug("Could not record connection state: %s", exc)

    def _mark_disconnected(self) -> None:
        """Record the end of the session, once, so a probe stops passing."""
        if not self._state_connected:
            return
        self._state_connected = False
        try:
            # Only our own marker: during a handover the successor has already
            # written its own, which the incumbent closing must not remove.
            agent_state.clear(self._home, only_pid=os.getpid())
        except OSError as exc:  # noqa: BLE001
            logger.debug("Could not clear connection state: %s", exc)

    # -- control connection --------------------------------------------------

    async def _connect_once(self) -> None:
        logger.info("Connecting agent control to %s", self.control_uri)
        async with websockets.connect(
            self.control_uri,
            max_size=WS_MAX_MESSAGE_SIZE,
            create_connection=FragmentingConnection,
        ) as ws:
            emit_event("connected", source="agent", message=f"Connected to {self.control_uri}")
            # Advertise what this agent can do so the dashboard only offers
            # features the agent actually supports. Firepuncher is available
            # outside a cluster; inside one it is off unless explicitly enabled,
            # because its forward rules would otherwise permit the API server
            # and the metadata service. Discovery depends on what's detectable.
            capabilities: list[str] = []
            if k8s_targets.firepuncher_enabled():
                capabilities.append("firepuncher")
            capabilities += [f"discovery:{p.name}" for p in active_providers()]
            # The agent answers logs_request from its own ring buffer on every
            # install type.
            capabilities.append(agent_logs.LOGS_CAPABILITY)
            if self._declares_endpoints:
                capabilities.append(K8S_DECLARED_CAPABILITY)
            # `update:<method>` is advertised only for installs the agent can
            # stage a new version into itself. The method is sent regardless so
            # the dashboard can tell a brew user what to run.
            install_method = detect_install_method()
            support = self._probe_support()
            update_cap = update_capability(support.method) if support.supported else None
            if update_cap is not None:
                capabilities.append(update_cap)
                # Stage B is only useful to an install that can be swapped at
                # all: the successor runs from the newly staged version. Without
                # the capability the relay keeps issuing Stage A updates.
                capabilities.append(HANDOVER_CAPABILITY)
            capabilities.append(CAPABILITY_FRAGMENTATION)
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
                # The canary fields are only for the relay to admit this
                # process. Once it is serving they would be a stale claim on an
                # identity that is now its own, so they are dropped.
                successor_of=self._successor_of if self._canary_probation else None,
                successor_nonce=self._successor_nonce if self._canary_probation else None,
                handover_group=self._handover_group,
            )
            await ws.send(hello.model_dump_json())

            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            welcome = AgentWelcome.model_validate_json(raw)
            if CAPABILITY_FRAGMENTATION in welcome.capabilities and isinstance(
                ws, FragmentingConnection
            ):
                ws.enable_fragmentation()
            self._api_key = welcome.api_key
            self._base_domain = welcome.base_domain
            if welcome.forward_rules:
                self._forward_rules = list(welcome.forward_rules)
            # Firepuncher frames arrive on this same control connection, so the
            # handler is rebuilt per session and torn down with it.
            self._fp = FpAgentSide(
                send=ws.send,
                rules=self._forward_rules,
                enabled=k8s_targets.firepuncher_enabled(),
                target_guard=self._target_guard,
            )
            # Marks the session as having worked, so the reconnect backoff in
            # run() starts over rather than compounding across the process life.
            self._registered = True
            self._mark_connected()
            logger.info(
                "Agent registered: public_id=%s endpoints=%d",
                welcome.agent_public_id,
                len(welcome.endpoints),
            )
            emit_event(
                "registered",
                level="success",
                source="agent",
                message=f"Agent registered with {len(welcome.endpoints)} endpoint(s)",
            )
            # A hostname-form API host is refused by name immediately, but the
            # address set it resolves to is filled by a background task. Wait
            # once, bounded, before the first endpoint can start so an
            # address-only route to the API is not briefly open. Timing out
            # does not cancel the refresh, which keeps its own retry schedule.
            if self._target_guard is not None:
                with contextlib.suppress(Exception):
                    await self._target_guard.wait_for_api_refresh(
                        timeout=k8s_targets.INITIAL_API_REFRESH_TIMEOUT
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
                # The control connection is gone, whether it dropped or the
                # process is shutting down. Mark readiness down before the
                # endpoint cleanup can take any time.
                self._mark_disconnected()
                status_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await status_task
                if self._health_task is not None:
                    self._health_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._health_task
                    self._health_task = None
                if self._canary_task is not None:
                    self._canary_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._canary_task
                    self._canary_task = None
                if self._fp is not None:
                    self._cancel_pending_fp_opens()
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
                if mtype == FpMsgType.OPEN:
                    # Resolving and dialling a forward target can take seconds
                    # (the Kubernetes guard resolves first); spawn it so one slow
                    # target cannot stall every other frame on the control
                    # channel. Data and close frames for the same stream wait
                    # for its open below, so per-stream ordering is kept.
                    self._spawn_fp_open(msg)
                else:
                    await self._settle_fp_open(msg)
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
        elif mtype == "logs_request":
            if ws is not None:
                try:
                    await ws.send(agent_logs.build_response(msg).model_dump_json())
                except ValueError:
                    logger.debug("Malformed logs_request ignored")
        elif mtype == "declared_ack":
            await self._handle_declared_ack(msg)
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

    # -- declared endpoints (hook for an embedding operator) ------------------

    async def send_declared_endpoints(
        self, endpoints: list[DeclaredEndpoint], revision: int
    ) -> bool:
        """Declare the full set of endpoints the caller owns.

        The latest declaration is remembered and re-sent after every
        (re)welcome. Calling this opts the agent in to the ``k8s:declared``
        capability from the next hello on (pass ``declares_endpoints=True`` to
        the constructor to have it in the first hello). Returns True when the
        frame went out now, False when it is only queued for the next welcome.
        """
        self._declares_endpoints = True
        self._declared = DeclaredEndpoints(endpoints=list(endpoints), revision=revision)
        return await self._send_declared()

    async def _send_declared(self) -> bool:
        ws, declared = self._ws, self._declared
        if ws is None or declared is None:
            return False
        try:
            await ws.send(declared.model_dump_json())
        except Exception:  # noqa: BLE001 — a dropped socket resends on the next welcome
            logger.debug("declared_endpoints not sent; will resend on reconnect")
            return False
        return True

    async def _handle_declared_ack(self, msg: dict[str, Any]) -> None:
        if self._on_declared_ack is None:
            return
        try:
            ack = DeclaredAck.model_validate(msg)
            result = self._on_declared_ack(ack)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — a bad callback must not kill the control loop
            logger.exception("declared_ack handler failed")

    # -- firepuncher frame ordering ------------------------------------------

    def _spawn_fp_open(self, msg: dict[str, Any]) -> None:
        """Validate and dial one forward in its own task.

        The guard resolves the target before dialling, and the dial itself has a
        timeout; awaiting either here would hold the control channel. Frames
        that follow on the same stream wait behind this task (see
        :meth:`_settle_fp_open`), so a stream's open still happens before its
        data and close are handed to the firepuncher.
        """
        if self._fp is None:
            return
        stream_id = msg.get("stream_id")
        task = asyncio.create_task(self._fp.handle(msg))
        if not isinstance(stream_id, str):
            return
        if stream_id in self._fp_open_tasks:
            # A duplicate open for a stream whose first open is still pending:
            # the firepuncher refuses it, and the first open's task must stay
            # registered so a close can still cancel it.
            return
        self._fp_open_tasks[stream_id] = task

        def _forget(finished: asyncio.Task[None], stream_id: str = stream_id) -> None:
            if self._fp_open_tasks.get(stream_id) is finished:
                self._fp_open_tasks.pop(stream_id, None)

        task.add_done_callback(_forget)

    async def _settle_fp_open(self, msg: dict[str, Any]) -> None:
        """Order a data/close frame behind its stream's in-flight open."""
        stream_id = msg.get("stream_id")
        if not isinstance(stream_id, str):
            return
        task = self._fp_open_tasks.pop(stream_id, None)
        if task is None:
            return
        if msg.get("type") == FpMsgType.CLOSE:
            # No point opening a stream the other end has already closed; the
            # close is delivered to the firepuncher below either way.
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _cancel_pending_fp_opens(self) -> None:
        for task in list(self._fp_open_tasks.values()):
            task.cancel()
        self._fp_open_tasks.clear()

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
        if self._update_in_flight:
            # A successor is already being admitted; a second would race it.
            return "busy:updating"
        if self._canary_probation:
            # This process *is* a canary. Its only job is to prove the new
            # version, so it must not start an update of its own.
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
        if req.successor_nonce is not None:
            # Stage B: the relay armed a canary handover.
            await self._run_handover_update(req, ws)
            return

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

    # -- Stage B: canary handover -------------------------------------------

    async def _run_handover_update(self, req: UpdateRequest, ws: Any) -> None:
        """Stage, verify and swap, then start a successor instead of exiting.

        This process keeps its tunnels and its control connection. The
        successor connects as this instance's successor, takes the endpoints
        over, and the relay closes this connection with HANDOVER once it is
        serving. Until then nothing here is torn down: if the successor cannot
        be started or dies on the way up, the update is reported failed and
        this process carries on from the rolled-back version.
        """

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
        except Exception as exc:  # noqa: BLE001 — same reporting as Stage A
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

        try:
            self._successor_proc = self._spawn_successor(req)
        except Exception as exc:  # noqa: BLE001 — the relay hears why, the tunnels stay up
            logger.error("Could not start successor for %s: %s", req.request_id, exc)
            await self._report_handover_failure(req, ws, f"could not start successor: {exc}")
            return

        self._update_in_flight = True
        logger.info(
            "Update %s: successor started, holding tunnels until the relay hands them over",
            req.request_id,
        )
        self._successor_task = asyncio.create_task(self._watch_successor(req, ws))

    def _successor_executable(self) -> str:
        """The just-swapped ``hle`` a successor should run.

        The versioned layout names it through ``current``; pipx and uv own
        their venvs and install in place, so the running interpreter's sibling
        ``hle`` is the new version there.
        """
        exe = agent_update.versioned_exec_path(home=self._home)
        if exe is not None:
            return str(exe)
        return str(Path(sys.executable).with_name("hle"))

    def _spawn_successor(self, req: UpdateRequest) -> Any:
        argv = agent_update.successor_argv(
            self._successor_executable(),
            successor_of=instance_id(),
            successor_nonce=req.successor_nonce or "",
            relay_host=self._relay_host,
            relay_port=self._relay_port,
        )
        # The token is handed over in the environment, never argv: the
        # incumbent may have been started with `--token` rather than the
        # variable, and the successor must use the same credential.
        env = dict(os.environ)
        env[config.AGENT_TOKEN_ENV] = self._token
        logger.info("Starting successor: %s", " ".join(argv))
        return self._successor_spawner(argv, env=env)

    async def _watch_successor(self, req: UpdateRequest, ws: Any, poll: float = 0.5) -> None:
        """Report failure if the successor dies before the relay hands over.

        A healthy successor stays up and serves, so the only thing to watch for
        is an early exit. HANDOVER ends this loop and turns this process into
        the child's supervisor; a successor that exits first means the update
        failed and the incumbent reports it and carries on.
        """
        proc = self._successor_proc
        while self._update_in_flight and self._running:
            if proc is not None and proc.poll() is not None:
                await self._report_handover_failure(
                    req, ws, f"successor exited with {proc.returncode}"
                )
                return
            await asyncio.sleep(poll)

    async def _report_handover_failure(self, req: UpdateRequest, ws: Any, reason: str) -> None:
        self._update_in_flight = False
        logger.error("Update %s failed during handover: %s", req.request_id, reason)
        try:
            updater = self._updater_factory(self._probe_support())
            prev = await asyncio.to_thread(updater.rollback)
            logger.info("Rolled back to %s", prev)
        except Exception as exc:  # noqa: BLE001 — report it and keep the old version running
            reason = f"{reason}; rollback failed: {exc}"
            logger.error("Rollback after handover failure failed: %s", exc)
        # The canary may have written a marker on its way down; the incumbent is
        # the one the relay is still listening to, so it reports and clears it.
        agent_update.clear_update_state(self._home)
        agent_update.clear_failed_marker(self._home)
        result = UpdateResult(
            request_id=req.request_id,
            ok=False,
            from_version=__version__,
            to_version=req.target_version,
            log_tail=agent_update.log_tail(),
        )
        # If a REPLACED already took the control channel the send fails; keep
        # the result so it goes out on the next welcome instead.
        try:
            await ws.send(result.model_dump_json())
            self._pending_result = None
        except Exception:  # noqa: BLE001 — the channel may already be gone
            self._pending_result = result
        # The successor took some endpoints before it died. Restart them so the
        # incumbent is whole again while it keeps serving the old version.
        await self._retake_endpoints()

    async def _retake_endpoints(self) -> None:
        """Restart only the endpoints a failed successor stood down.

        The successor takes endpoints over one at a time, so a healthy
        incumbent tunnel is left alone; only a tunnel that is not connected or
        whose task has finished was given up and needs starting again.
        """
        stood_down = [
            running
            for running in list(self._endpoints.values())
            if not running.tunnel.is_connected or running.task.done()
        ]
        for running in stood_down:
            await self._stop_endpoint(running.spec.label)
            await self._start_endpoint(running.spec)

    async def _wait_for_successor_control(self, poll: float = 0.5) -> None:
        """Block while the successor owns the control channel after a REPLACED.

        A REPLACED close ends the control channel, so the HANDOVER (4011) that
        would announce the takeover can never arrive on it. The data plane is
        the signal instead: the relay hands each incumbent tunnel to the
        successor and the tunnel stops itself when it sees 4011. Once every
        tunnel task is done the handover has happened, so this process becomes
        the successor's supervisor. If the successor dies first,
        ``_watch_successor`` reports it and this process reconnects instead.

        Returns with ``_handover_done`` set (supervise the child) or clear
        (reconnect and serve the old version).
        """
        task = self._successor_task
        if task is None:
            # Nothing was ever spawned to wait on: the takeover is all there is.
            self._handover_done = True
            return
        while not task.done():
            # Only a LIVE successor can be supervised: if it took the tunnels
            # and died within a poll interval, let the watcher report the
            # failure, roll back and retake instead of supervising a corpse.
            proc = self._successor_proc
            if self._all_tunnels_stood_down() and (proc is None or proc.poll() is None):
                self._handover_done = True
                self._update_in_flight = False
                await self._cancel_successor_watch()
                logger.info(
                    "Every tunnel stood down for the successor after REPLACED; supervising it"
                )
                return
            await asyncio.sleep(poll)
        # The successor exited first, so there is nothing to supervise; the
        # watcher has already reported the failure and this process resumes.

    def _all_tunnels_stood_down(self) -> bool:
        """Whether the relay has taken every incumbent tunnel for the successor.

        An empty pool is not "all stood down": with nothing to hand over there
        is no data-plane signal to wait for, so the control channel (or the
        successor's death) decides. A tunnel counts as stood down when its task
        has finished — a transient blip reconnects in place and leaves the task
        running, so only a deliberate 4011 ends it.
        """
        if not self._endpoints:
            return False
        return all(running.task.done() for running in self._endpoints.values())

    async def _supervise_successor(self, poll: float = 0.5) -> None:
        """Stay alive as a thin supervisor of the successor we spawned.

        A service manager stops a whole control group/job when the process it
        started exits: systemd ``KillMode=control-group`` kills every child on
        the way out, launchd ``KeepAlive`` restarts what it does not see, and a
        Windows service does the same. So after HANDOVER this process must not
        exit. It stops its own tunnels and control connection (done by the
        caller) and then waits on the successor, forwarding SIGTERM/SIGINT (a
        terminate on Windows) to it and exiting with the successor's status
        once it does. The supervised PID stays alive, the cgroup/job stays
        intact, and ``Restart=always`` only fires when the agent really died.
        """
        await self._cancel_successor_watch()
        proc = self._successor_proc
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        hooked: list[signal.Signals] = []

        def _forward(name: str) -> None:
            _signal_process(proc, name)

        if sys.platform != "win32":
            # Overrides the shutdown helper's cancel-the-task handlers: a
            # signal meant for the agent belongs to the successor now.
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, _forward, sig.name)
                    hooked.append(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    continue
        try:
            while proc.poll() is None:
                await asyncio.sleep(poll)
        except asyncio.CancelledError:
            # Never leave the successor orphaned if this supervisor is stopped.
            await self._terminate_successor()
            raise
        finally:
            for sig in hooked:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
        # A child killed by a signal reports a negative code; exit codes are
        # unsigned, so map it to the shell convention 128 + signal number.
        code = proc.returncode or 0
        self._exit_code = code if code >= 0 else 128 - code

    async def _terminate_successor(self, wait: float = 10.0) -> None:
        """Stop a successor this process spawned but is not supervising.

        The successor runs in its own session, so a stop signal sent to this
        process's group does not reach it. On any exit that is not the
        supervisor path — an explicit shutdown while an update is in flight, or
        during the REPLACED hold — leaving it running would orphan a second
        agent on the same identity. SIGTERM (terminate on Windows), then a
        bounded wait, then kill.
        """
        proc = self._successor_proc
        if proc is None or proc.poll() is not None:
            return
        await self._cancel_successor_watch()
        _signal_process(proc, "SIGTERM")
        try:
            await asyncio.to_thread(proc.wait, wait)
        except asyncio.CancelledError:
            # A second stop signal can land while we wait; kill rather than
            # leak the process, then let the cancellation keep unwinding.
            with contextlib.suppress(Exception):
                proc.kill()
            raise
        except Exception:  # noqa: BLE001 — TimeoutExpired, or proc has no wait()
            pass
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, wait)

    async def _cancel_successor_watch(self) -> None:
        task = self._successor_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._successor_task = None

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
        # A canary has to prove itself whether or not the incumbent's
        # update.json was found. Without this deadline a canary that never
        # reaches a welcome — update state missing, or the relay never answering
        # — would sit in the reconnect loop forever while the incumbent serves.
        if self._canary_probation and self._watchdog_task is None:
            logger.info("Canary successor has %.0fs to become healthy", self._health_timeout)
            self._watchdog_task = asyncio.create_task(self._watchdog_timer())

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
        if self._boot.kind == "watch" or self._canary_probation:
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
        # A successor has to prove every endpoint comes up whether or not it
        # finds update state on disk — the incumbent always writes it, so
        # `watch` above is the normal case, not a reason to skip this. On
        # success this also drops the canary identity (fix 1).
        if self._canary_probation:
            self._canary_task = asyncio.create_task(self._confirm_canary_health(ws))
        # A handover that failed after the control channel was gone (REPLACED)
        # left its result behind; the dashboard hears it on this new session.
        if self._pending_result is not None:
            pending, self._pending_result = self._pending_result, None
            with contextlib.suppress(Exception):
                await ws.send(pending.model_dump_json())
        # A declaration made earlier (or before this reconnect) is the truth
        # the server must hold again; self._ws is already this session's.
        await self._send_declared()

    async def _confirm_canary_health(self, ws: Any, poll: float = 0.5) -> None:
        """A successor must bring every endpoint up, or exit without a fuss.

        Exiting non-zero here is the point: this process was only ever an
        experiment. The incumbent is still running and keeps serving; the
        relay, seeing no welcome or no healthy endpoints, never hands over.
        """
        deadline = asyncio.get_running_loop().time() + self._health_timeout
        while not self._all_endpoints_connected():
            if asyncio.get_running_loop().time() >= deadline:
                logger.error(
                    "Canary successor did not bring every endpoint up within %.0fs; exiting",
                    self._health_timeout,
                )
                self._exit_code = 1
                self._running = False
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                return
            await asyncio.sleep(poll)
        # Healthy: the relay knows this process now, so stop claiming to be a
        # successor. A later control reconnect is an ordinary agent's hello.
        self._successor_of = None
        self._successor_nonce = None
        self._canary_probation = False
        logger.info("Canary successor is serving every endpoint")

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
        if self._canary_probation:
            # During a Stage B handover the incumbent owns the rollback. A
            # canary that cannot prove itself just exits non-zero; the
            # incumbent is still running, sees the child die, and recovers.
            # This comes before the boot-kind guard: a canary may have no
            # update.json at all and still has to die on its deadline.
            self._boot = agent_update.BootCheck("none")
            self._disarm_watchdog()
            logger.error(
                "Canary update %s failed: %s — exiting for the incumbent to recover",
                state.request_id if state is not None else agent_update.LOCAL_REQUEST_ID,
                reason,
            )
            self._exit_code = 1
            self._running = False
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            return
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

        # In a cluster the guard runs first and a refusal returns before any
        # network I/O: otherwise a preflight is a port scanner and a metadata
        # reader that reports status, Location, auth headers and a body excerpt.
        probe_url = req.service_url
        sni_hostname: str | None = None
        host_header: str | None = None
        if self._target_guard is not None:
            decision = await self._target_guard.check(req.service_url)
            if not decision.allowed:
                logger.warning("Preflight refused for %s: %s", req.service_url, decision.reason)
                report = PreflightReport(
                    request_id=req.request_id,
                    service_url=req.service_url,
                    error=(decision.reason or "target not allowed")[:200],
                )
                with contextlib.suppress(Exception):
                    await ws.send(report.model_dump_json())
                return
            probe_url = decision.url or req.service_url
            if probe_url != req.service_url:
                sni_hostname = sni_hostname_for(probe_url, req.service_url)
                # The tunnel presents the original authority as Host, not the
                # canonical FQDN the transport dials; the probe must match.
                host_header = k8s_targets.authority_of(req.service_url)

        try:
            report = await run_preflight(
                probe_url,
                tunnel_host=req.tunnel_host,
                verify_ssl=req.verify_ssl,
                websocket_enabled=req.websocket_enabled,
                forward_host=req.forward_host,
                request_id=req.request_id,
                sni_hostname=sni_hostname,
                trust_env=self._target_guard is None,
                host_header=host_header,
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

    def endpoint_statuses(self) -> list[EndpointStatus] | None:
        """Per-endpoint status, or None when this process does not hold the session.

        None means "do not publish": disconnected, on canary probation, or
        control already handed to a successor.
        """
        if self._ws is None or self._canary_probation or self._control_taken or self._handover_done:
            return None
        return self._build_status()

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

        # Resolve before the diff, so a changed resolved value restarts that
        # endpoint through the usual reconcile_key comparison. An unresolvable
        # spec is reported failed and stops running; the message never names
        # the value.
        unresolved: set[str] = set()
        if self._spec_resolver is not None:
            for label, spec in list(desired.items()):
                try:
                    desired[label] = self._spec_resolver(spec)
                except ValueError as exc:
                    self._failed[label] = f"unresolved: {exc}"
                    unresolved.add(label)
                    del desired[label]
                    await self._stop_endpoint(label)

        # Endpoints that failed to start and are no longer asked for stop
        # being reported.
        wanted = desired.keys() | unresolved
        for label in list(self._failed):
            if label not in wanted:
                del self._failed[label]

        # Remove endpoints no longer desired.
        for label in list(self._endpoints):
            if label not in desired:
                await self._stop_endpoint(label)

        # Add new endpoints; restart changed ones. Validation resolves names, so
        # it is run concurrently: one slow or unresponsive DNS lookup must not
        # stall every other endpoint behind it. Each check has its own timeout;
        # the gather is additionally capped so a resolver that ignores
        # cancellation cannot wedge reconciliation.
        to_start: list[EndpointSpec] = []
        for label, spec in desired.items():
            current = self._endpoints.get(label)
            if current is None:
                to_start.append(spec)
            elif current.spec.reconcile_key() != spec.reconcile_key():
                logger.info("Endpoint %s changed — restarting", label)
                await self._stop_endpoint(label)
                to_start.append(spec)
        if not to_start:
            return
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(self._start_endpoint(spec) for spec in to_start),
                    return_exceptions=True,
                ),
                timeout=k8s_targets.START_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Endpoint validation did not finish within %.0fs", k8s_targets.START_TIMEOUT
            )
            return
        for spec, result in zip(to_start, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Endpoint %s failed to start: %s", spec.label, result)

    async def _start_endpoint(self, spec: EndpointSpec) -> None:
        # In a cluster, the relay does not get to name any target it likes: a
        # compromised dashboard could otherwise publish the API server, metadata
        # or a node. The check resolves names too, so it awaits. The tunnel is
        # given the canonical in-cluster name the guard checked, while the
        # original spec is what the reconciler compares against.
        cfg_spec = spec
        upstream_host: str | None = None
        if self._target_guard is not None:
            decision = await self._target_guard.check(spec.service_url)
            if not decision.allowed:
                reason = f"refused: {decision.reason}"
                if self._failed.get(spec.label) != reason:
                    logger.warning("Endpoint %s not started: %s", spec.label, reason)
                    emit_event("error", level="error", label=spec.label, message=reason)
                self._failed[spec.label] = reason
                return
            if decision.url and decision.url != spec.service_url:
                # Keep the authority the dashboard asked for as the upstream
                # Host header: canonicalising ``web`` to its FQDN must not
                # present a Name the upstream's host allow-list rejects.
                upstream_host = k8s_targets.authority_of(spec.service_url)
                cfg_spec = replace(spec, service_url=decision.url)
        # Data-plane credential: an explicit key from the welcome if the server
        # sent one, otherwise the agent's own token (the server accepts hlea_
        # tokens for tunnel registration). One enrollment, one secret.
        data_key = self._api_key or self._token
        # The whole TunnelSpec (1.3), through the same mapping the CLI uses, so
        # a dashboard endpoint can set everything `hle tunnel create` can. The
        # agent is what manages these tunnels, whatever the spec says.
        try:
            cfg = to_tunnel_config(
                cfg_spec,
                api_key=data_key,
                relay_host=self._relay_host,
                relay_port=self._relay_port,
                managed_by="hle-agent",
                upstream_host=upstream_host,
                # A cluster guard is active: env proxies must never carry an
                # in-cluster target (they cannot reach the canonical name and
                # would leak it off the pod's network).
                trust_env=self._target_guard is None,
            )
        except ValueError as exc:
            # One bad endpoint (a malformed basic-auth value, say) must not take
            # the others down with it: skip it, and report it in status so the
            # dashboard shows why instead of showing nothing. The message names
            # the field, never its value.
            reason = f"invalid endpoint: {exc}"
            if self._failed.get(spec.label) != reason:
                logger.error("Endpoint %s not started: %s", spec.label, exc)
                emit_event("error", level="error", label=spec.label, message=reason)
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
