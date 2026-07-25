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
from typing import Any

from hle_common.fp_protocol import (
    FP_MAX_CHUNK,
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
    """

    send: Sender
    rules: list[ForwardRule] = field(default_factory=list)
    _streams: dict[str, _Stream] = field(default_factory=dict)

    async def handle(self, msg: dict[str, Any]) -> None:
        """Dispatch one firepuncher frame. Never raises — errors go on the wire."""
        mtype = msg.get("type")
        try:
            if mtype == FpMsgType.OPEN:
                await self._on_open(FpOpen.model_validate(msg))
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

    async def close_all(self) -> None:
        for sid in list(self._streams):
            await self._close_stream(sid, notify=False)

    # -- individual frames ---------------------------------------------------

    async def _on_open(self, msg: FpOpen) -> None:
        if len(self._streams) >= MAX_STREAMS:
            await self._fail(msg.stream_id, FpErrorCode.INTERNAL, "too many open streams")
            return

        # The allowlist check is the whole point: the relay already proved the
        # caller owns this agent, but owning an agent must not imply the right
        # to reach arbitrary hosts on the network behind it.
        if not is_allowed(self.rules, msg.target_host, msg.target_port):
            allowed = ", ".join(str(r) for r in self.rules) or "(nothing configured)"
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

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(msg.target_host, msg.target_port),
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
