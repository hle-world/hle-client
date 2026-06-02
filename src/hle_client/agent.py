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
from hle_client.tunnel import Tunnel, TunnelConfig
from hle_common.agent_protocol import (
    AgentHello,
    AgentStateSync,
    AgentStatus,
    AgentWelcome,
    EndpointSpec,
    EndpointStatus,
)

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
        self._endpoints: dict[str, _Running] = {}
        self._api_key: str | None = None
        self._base_domain: str | None = None

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
            try:
                await self._connect_once()
                delay = self._reconnect_delay  # reset after a clean session
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — control conn is best-effort
                logger.warning("Agent control connection lost: %s", exc)
            finally:
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
            hello = AgentHello(token=self._token, agent_version=__version__)
            await ws.send(hello.model_dump_json())

            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            welcome = AgentWelcome.model_validate_json(raw)
            self._api_key = welcome.api_key
            self._base_domain = welcome.base_domain
            logger.info(
                "Agent registered: public_id=%s endpoints=%d",
                welcome.agent_public_id,
                len(welcome.endpoints),
            )
            await self.reconcile(welcome.endpoints)

            status_task = asyncio.create_task(self._status_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                status_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await status_task

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("Bad agent control message")
            return
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "state_sync":
            sync = AgentStateSync.model_validate(msg)
            await self.reconcile(sync.endpoints)
        elif mtype == "pong":
            pass
        else:
            logger.debug("Unhandled agent control message: %s", mtype)

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
