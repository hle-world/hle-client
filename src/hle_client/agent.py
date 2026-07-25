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
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from hle_client import __version__
from hle_client.discovery import active_providers, scan_all
from hle_client.firepuncher import FpAgentSide
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.agent_protocol import (
    AgentHello,
    AgentStateSync,
    AgentStatus,
    AgentWelcome,
    EndpointSpec,
    EndpointStatus,
)
from hle_common.discovery import DiscoveryReport
from hle_common.fp_protocol import ForwardRule, default_rules

logger = logging.getLogger(__name__)

# How often the agent reports endpoint status / keepalive to the server.
STATUS_INTERVAL = 15.0
WS_MAX_MESSAGE_SIZE = 4 * 1024 * 1024

# Enrollment token persistence (separate from the API-key config so they don't clash).
AGENT_CONFIG_PATH = Path.home() / ".config" / "hle" / "agent.toml"
AGENT_TOKEN_PREFIX = "hlea_"


def save_agent_token(token: str) -> None:
    """Persist the agent enrollment token (0600)."""
    AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    AGENT_CONFIG_PATH.write_text(f'token = "{token}"\n')
    AGENT_CONFIG_PATH.chmod(0o600)


def load_agent_token() -> str | None:
    if not AGENT_CONFIG_PATH.exists():
        return None
    try:
        with open(AGENT_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("token")
    except (OSError, ValueError):
        logger.debug("Failed to read agent token from %s", AGENT_CONFIG_PATH)
        return None


def remove_agent_token() -> bool:
    if AGENT_CONFIG_PATH.exists():
        AGENT_CONFIG_PATH.unlink()
        return True
    return False


# A tunnel-like object: connect() / disconnect() coroutines + is_connected /
# public_url properties. Real impl is hle_client.tunnel.Tunnel; tests inject fakes.
TunnelFactory = Callable[[TunnelConfig], Any]


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
    ) -> None:
        self._token = token
        self._relay_host = relay_host
        self._relay_port = relay_port
        self._tunnel_factory = tunnel_factory
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._running = False
        # True once the current session reached "registered"; see run().
        self._registered = False
        self._endpoints: dict[str, _Running] = {}
        self._api_key: str | None = None
        self._base_domain: str | None = None
        self._fp: FpAgentSide | None = None
        # Until the server tells us otherwise, only loopback is forwardable.
        self._forward_rules: list[ForwardRule] = default_rules()

    # -- public API ----------------------------------------------------------

    @property
    def control_uri(self) -> str:
        scheme = "ws" if self._relay_host.startswith("localhost") else "wss"
        return f"{scheme}://{self._relay_host}:{self._relay_port}/_hle/agent"

    async def run(self) -> None:
        """Run the control connection with reconnection until stopped."""
        self._running = True
        delay = self._reconnect_delay
        while self._running:
            self._registered = False
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                break
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
                await self._stop_all()
            if not self._running:
                break
            logger.info("Reconnecting agent control in %.1fs ...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

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
            hello = AgentHello(
                token=self._token,
                agent_version=__version__,
                capabilities=capabilities,
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

            status_task = asyncio.create_task(self._status_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_message(raw, ws)
            finally:
                status_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await status_task
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
        elif mtype == "pong":
            pass
        else:
            logger.debug("Unhandled agent control message: %s", mtype)

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
        ]

    # -- reconciler ----------------------------------------------------------

    async def reconcile(self, specs: list[EndpointSpec]) -> None:
        """Converge the running tunnel pool to *specs* (idempotent)."""
        desired = {s.label: s for s in specs}

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
        cfg = TunnelConfig(
            service_url=spec.service_url,
            relay_host=self._relay_host,
            relay_port=self._relay_port,
            auth_mode=spec.auth_mode,
            service_label=spec.label,
            api_key=data_key,
            websocket_enabled=spec.websocket_enabled,
            webhook_path=spec.webhook_path,
            zone=spec.zone,
            managed_by="hle-agent",
        )
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
