"""Firepuncher — agent-side dialer and local-client listener.

Two halves of the same feature (see ``hle_common.fp_protocol`` for the wire
format and the trust model):

``FpAgentSide``
    Runs inside the agent. On ``fp_open`` it checks the target against the
    allowlist, dials it, and pumps bytes both ways.

``FpLocalClient``
    Runs on your laptop as ``hle fp``. Binds a local port; every accepted TCP
    connection becomes a new multiplexed stream to the agent.

Neither side ever listens on a public port — the relay pairs the two authenticated
WebSocket connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hle_client.netinfo import describe_local_networks, is_local
from hle_common.fp_protocol import (
    FP_MAX_CHUNK,
    LOCAL_NETWORKS,
    ForwardRule,
    FpClose,
    FpData,
    FpError,
    FpErrorCode,
    FpMsgType,
    FpOpen,
    FpReady,
    is_allowed,
)

if TYPE_CHECKING:
    from hle_client.k8s_targets import KubernetesTargetGuard

logger = logging.getLogger(__name__)

# How long to wait for the remote TCP service to accept a connection.
DIAL_TIMEOUT = 10.0

# Bounds the number of concurrent forwarded connections per session, so one
# runaway client can't exhaust the agent's file descriptors.
MAX_STREAMS = 128

Sender = Callable[[str], Awaitable[None]]


@dataclass
class _Stream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    pump: asyncio.Task[None] | None = None


@dataclass
class FpAgentSide:
    """Handles firepuncher streams on the agent.

    ``send`` is how we write a JSON frame back toward the client (through the
    agent's control connection). ``rules`` is the allowlist; an empty list means
    nothing is forwardable, which is the safe reading of "not configured".

    ``enabled`` and ``target_guard`` carry the Kubernetes policy. Outside a
    cluster ``enabled`` is True and ``target_guard`` is None, so the relay rules
    are the only check, exactly as before. In a cluster the agent sets
    ``enabled`` from ``HLE_FIREPUNCHER_ENABLED`` (off by default) and passes its
    endpoint guard so a forward cannot reach the API server, metadata or a node
    that the relay's own rules would otherwise allow.
    """

    send: Sender
    rules: list[ForwardRule] = field(default_factory=list)
    enabled: bool = True
    target_guard: KubernetesTargetGuard | None = None
    _streams: dict[str, _Stream] = field(default_factory=dict)
    # Stream ids whose open is still in flight (resolving the guard, dialling).
    # Counted with ``_streams`` so the cap covers both pending and active opens;
    # without it a burst of concurrent opens all passes the check before any of
    # them registers.
    _pending_opens: set[str] = field(default_factory=set)

    async def handle(self, msg: dict[str, Any]) -> None:
        """Dispatch one firepuncher frame. Never raises — errors go on the wire."""
        mtype = msg.get("type")
        try:
            if mtype == FpMsgType.OPEN:
                open_req = FpOpen.model_validate(msg)
                # Reserve a slot synchronously, before the first await: this runs
                # as the first step of the spawned open task, so concurrent opens
                # see each other's reservations.
                refusal = self._reserve_open(open_req.stream_id)
                if refusal is not None:
                    logger.warning(
                        "Firepuncher refused stream_id=%s: %s", open_req.stream_id, refusal
                    )
                    await self._fail(open_req.stream_id, FpErrorCode.INTERNAL, refusal)
                    return
                try:
                    await self._on_open(open_req)
                finally:
                    self._pending_opens.discard(open_req.stream_id)
            elif mtype == FpMsgType.DATA:
                await self._on_data(FpData.model_validate(msg))
            elif mtype == FpMsgType.CLOSE:
                await self._close_stream(FpClose.model_validate(msg).stream_id)
            else:
                logger.debug("Unhandled firepuncher frame: %s", mtype)
        except Exception as exc:  # noqa: BLE001 — a bad frame must not kill the agent
            logger.warning("Firepuncher frame failed (%s): %s", mtype, exc)
            sid = msg.get("stream_id")
            if isinstance(sid, str):
                await self._fail(sid, FpErrorCode.INTERNAL, str(exc))

    def _reserve_open(self, stream_id: str) -> str | None:
        """Claim a stream slot for an open that has not registered yet.

        Returns None when the slot is claimed, or the reason it could not be.
        A repeated open for a stream id that is already pending or active is
        refused rather than treated as a retry: letting it through opened a
        second socket for the same id, which the dictionary then overwrote —
        orphaning the first connection and letting repeated opens slip past the
        cap. The existing stream is left untouched. Returns a reason rather than
        a bool so the caller can say which limit was hit.
        """
        if stream_id in self._pending_opens or stream_id in self._streams:
            return "stream id already open"
        if len(self._streams) + len(self._pending_opens) >= MAX_STREAMS:
            return "too many open streams"
        self._pending_opens.add(stream_id)
        return None

    async def close_all(self) -> None:
        for sid in list(self._streams):
            await self._close_stream(sid, notify=False)

    # -- individual frames ---------------------------------------------------

    def _target_allowed(self, host: str, port: int) -> bool:
        """Check a target against the allowlist, expanding ``@local`` here.

        The sentinel is expanded on this side because this is the only side
        that can: the relay cannot see that this machine has a Docker bridge, a
        Kubernetes CNI, a VPN and a LAN, and fixed CIDRs sent from there would
        be guessing at all four.
        """
        if is_allowed(self.rules, host, port):
            return True
        local_rules = [r for r in self.rules if r.host == LOCAL_NETWORKS]
        if not local_rules:
            return False
        # A port on the sentinel still narrows it: `@local:22` means "port 22
        # anywhere on my networks", not "anything on my networks".
        if not any(r.port is None or r.port == port for r in local_rules):
            return False
        return is_local(host)

    def describe_rules(self) -> list[str]:
        """The allowlist as a person would read it, with ``@local`` resolved."""
        rendered: list[str] = []
        for rule in self.rules:
            if rule.host != LOCAL_NETWORKS:
                rendered.append(str(rule))
                continue
            suffix = "" if rule.port is None else f":{rule.port}"
            rendered.extend(f"{net}{suffix}" for net in describe_local_networks())
        return rendered

    async def _on_open(self, msg: FpOpen) -> None:
        # In a cluster firepuncher is off unless the operator turned it on: the
        # relay's default rules include loopback and link-local, which inside a
        # pod are the agent itself and the cloud metadata service.
        if not self.enabled:
            logger.warning(
                "Firepuncher refused %s:%s — disabled on kubernetes agents",
                msg.target_host,
                msg.target_port,
            )
            await self._fail(
                msg.stream_id,
                FpErrorCode.NOT_ALLOWED,
                "firepuncher is disabled on kubernetes agents",
            )
            return

        # The allowlist check is the whole point: the relay already proved the
        # caller owns this agent, but owning an agent must not imply the right
        # to reach arbitrary hosts on the network behind it.
        if not self._target_allowed(msg.target_host, msg.target_port):
            # Resolved, not raw: "@local" tells the operator nothing about why
            # their address was refused, while the networks it stands for do.
            allowed = ", ".join(self.describe_rules()) or "(nothing configured)"
            logger.warning(
                "Firepuncher refused %s:%s — not in allowlist",
                msg.target_host,
                msg.target_port,
            )
            await self._fail(
                msg.stream_id,
                FpErrorCode.NOT_ALLOWED,
                f"{msg.target_host}:{msg.target_port} is not allowed. Allowed: {allowed}",
            )
            return

        # In a cluster, the relay rules are not enough: they permit the pod's
        # own network, and the API server and metadata live there. Resolve the
        # target once, check it, and dial the exact address that was checked so
        # a name cannot resolve differently between the two steps.
        connect_host = msg.target_host
        if self.target_guard is not None:
            decision = await self.target_guard.check_host(msg.target_host, msg.target_port)
            if not decision.allowed:
                logger.warning(
                    "Firepuncher refused %s:%s — %s",
                    msg.target_host,
                    msg.target_port,
                    decision.reason,
                )
                await self._fail(
                    msg.stream_id,
                    FpErrorCode.NOT_ALLOWED,
                    decision.reason or "target not allowed",
                )
                return
            connect_host = decision.address or msg.target_host

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(connect_host, msg.target_port),
                timeout=DIAL_TIMEOUT,
            )
        except TimeoutError:
            await self._fail(msg.stream_id, FpErrorCode.TIMEOUT, "dial timed out")
            return
        except OSError as exc:
            await self._fail(msg.stream_id, FpErrorCode.UNREACHABLE, str(exc))
            return

        stream = _Stream(reader=reader, writer=writer)
        self._streams[msg.stream_id] = stream
        await self.send(FpReady(stream_id=msg.stream_id).model_dump_json())
        stream.pump = asyncio.create_task(self._pump(msg.stream_id, reader))
        logger.info(
            "Firepuncher stream %s -> %s:%s open",
            msg.stream_id[:8],
            msg.target_host,
            msg.target_port,
        )

    async def _on_data(self, msg: FpData) -> None:
        stream = self._streams.get(msg.stream_id)
        if stream is None:
            return
        stream.writer.write(msg.payload())
        await stream.writer.drain()

    async def _pump(self, stream_id: str, reader: asyncio.StreamReader) -> None:
        """Forward bytes from the local service back to the client."""
        try:
            while True:
                chunk = await reader.read(FP_MAX_CHUNK)
                if not chunk:
                    break
                await self.send(FpData.of(stream_id, chunk).model_dump_json())
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 — one stream dying is not fatal
            logger.debug("Firepuncher pump %s ended: %s", stream_id[:8], exc)
        finally:
            with contextlib.suppress(Exception):
                await self.send(FpClose(stream_id=stream_id, reason="eof").model_dump_json())
            self._streams.pop(stream_id, None)

    async def _close_stream(self, stream_id: str, *, notify: bool = True) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        if stream.pump is not None:
            stream.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stream.pump
        with contextlib.suppress(Exception):
            stream.writer.close()
            await stream.writer.wait_closed()
        if notify:
            with contextlib.suppress(Exception):
                await self.send(FpClose(stream_id=stream_id).model_dump_json())

    async def _fail(self, stream_id: str, code: FpErrorCode, message: str) -> None:
        with contextlib.suppress(Exception):
            await self.send(
                FpError(stream_id=stream_id, code=code, message=message).model_dump_json()
            )


@dataclass
class FpLocalClient:
    """Local end of a firepuncher session: a TCP listener that tunnels.

    Each accepted connection opens a stream toward the agent and pumps bytes
    until either side closes.
    """

    send: Sender
    target_host: str
    target_port: int
    # How long to wait for the agent to confirm a stream. Defaults to the dial
    # timeout plus headroom for the round trip through the relay.
    ready_timeout: float = DIAL_TIMEOUT + 5.0
    # False while the relay connection is down. The listener stays bound across
    # reconnects so the local port doesn't vanish, but connections arriving in
    # the gap are refused immediately rather than hanging until they time out.
    connected: bool = False
    _streams: dict[str, _Stream] = field(default_factory=dict)
    _ready: dict[str, asyncio.Event] = field(default_factory=dict)
    _errors: dict[str, str] = field(default_factory=dict)
    on_error: Callable[[str], None] | None = None

    async def serve(self, bind_host: str, bind_port: int) -> asyncio.AbstractServer:
        # Default bind is loopback — a firepuncher listener re-exposes a remote
        # service, and binding 0.0.0.0 by accident would share it with the LAN.
        return await asyncio.start_server(self._on_client, bind_host, bind_port)

    async def handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        sid = msg.get("stream_id")
        if not isinstance(sid, str):
            return
        if mtype == FpMsgType.READY:
            self._ready.setdefault(sid, asyncio.Event()).set()
        elif mtype == FpMsgType.DATA:
            stream = self._streams.get(sid)
            if stream is not None:
                stream.writer.write(FpData.model_validate(msg).payload())
                await stream.writer.drain()
        elif mtype == FpMsgType.CLOSE:
            await self._drop(sid)
        elif mtype == FpMsgType.ERROR:
            err = FpError.model_validate(msg)
            detail = err.message or err.code.value
            self._errors[sid] = detail
            if self.on_error is not None:
                self.on_error(detail)
            self._ready.setdefault(sid, asyncio.Event()).set()
            await self._drop(sid)

    async def close_all(self) -> None:
        for sid in list(self._streams):
            await self._drop(sid)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.connected:
            # Fail fast instead of making the caller wait out ready_timeout for
            # a relay we already know isn't there.
            logger.debug("Refusing local connection: relay not connected")
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            return

        stream_id = uuid.uuid4().hex
        ready = asyncio.Event()
        self._ready[stream_id] = ready
        await self.send(
            FpOpen(
                stream_id=stream_id,
                target_host=self.target_host,
                target_port=self.target_port,
            ).model_dump_json()
        )

        try:
            await asyncio.wait_for(ready.wait(), timeout=self.ready_timeout)
        except TimeoutError:
            writer.close()
            self._ready.pop(stream_id, None)
            return

        # An error arrived instead of a ready — the agent refused or couldn't dial.
        if stream_id in self._errors:
            self._errors.pop(stream_id, None)
            self._ready.pop(stream_id, None)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            return

        self._streams[stream_id] = _Stream(reader=reader, writer=writer)
        try:
            while True:
                chunk = await reader.read(FP_MAX_CHUNK)
                if not chunk:
                    break
                await self.send(FpData.of(stream_id, chunk).model_dump_json())
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 — one connection dying is not fatal
            logger.debug("Firepuncher local stream %s ended: %s", stream_id[:8], exc)
        finally:
            with contextlib.suppress(Exception):
                await self.send(FpClose(stream_id=stream_id).model_dump_json())
            await self._drop(stream_id)

    async def _drop(self, stream_id: str) -> None:
        self._ready.pop(stream_id, None)
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        with contextlib.suppress(Exception):
            stream.writer.close()
            await stream.writer.wait_closed()
