"""Client WebSocket connection that speaks envelope fragmentation.

The single send hook and the single receive hook for
:mod:`hle_common.fragmentation`. Passed to ``websockets.connect`` as
``create_connection`` for the relay (tunnel) and control (agent) channels, so
every ``send``/``recv`` on those connections — every message type, now and
later — goes through it without the call sites knowing.

* Receive: always on. A ``fragment`` frame is fed to the connection's
  :class:`~hle_common.fragmentation.Reassembler`; ``recv`` (and so
  ``async for``) only ever returns whole envelopes. A peer only sends
  fragments after this client advertised the capability, so with an older
  server nothing changes.
* Send: off until :meth:`FragmentingConnection.enable_fragmentation`, which the
  caller invokes once the server has echoed the capability. Until then ``send``
  is the stock method and the bytes on the wire are identical to before.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal, overload

from websockets.asyncio.client import ClientConnection

from hle_common.fragmentation import (
    Fragmenter,
    FragmentError,
    Reassembler,
    is_fragment_frame,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Iterable, Iterator

    from websockets.typing import Data, DataLike

logger = logging.getLogger(__name__)


def _consume_exception(task: asyncio.Future[None]) -> None:
    # A run whose caller was cancelled has nobody awaiting it; retrieve its
    # error (typically ConnectionClosed) so asyncio does not log it as lost.
    if not task.cancelled():
        task.exception()


class FragmentingConnection(ClientConnection):
    """A ``ClientConnection`` that reassembles and (once enabled) fragments."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fragmenter: Fragmenter | None = None
        self._reassembler = Reassembler()

    def enable_fragmentation(self) -> None:
        """Start fragmenting large outgoing envelopes (peer echoed the capability)."""
        self._fragmenter = Fragmenter()

    @property
    def fragmentation_enabled(self) -> bool:
        return self._fragmenter is not None

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        # Free half-received messages now, not whenever this object is dropped.
        self._reassembler.clear()

    async def send(
        self,
        message: DataLike | Iterable[DataLike] | AsyncIterable[DataLike],
        *,
        text: bool | None = None,
    ) -> None:
        if self._fragmenter is None or not isinstance(message, str):
            await super().send(message, text=text)
            return
        # Raises MessageTooLargeError before sending anything when over the ceiling.
        frames = self._fragmenter.frames(message)
        if is_fragment_frame(first := next(frames)):
            # A stock send() of one frame is atomic under cancellation; keep a
            # run of fragments atomic too, or the peer is left holding a
            # partial it can never complete. Cancelling the caller only stops
            # the wait — the rest of the run still goes out.
            run = asyncio.ensure_future(self._send_run(first, frames, text))
            run.add_done_callback(_consume_exception)
            await asyncio.shield(run)
        else:
            await super().send(first, text=text)

    async def _send_run(self, first: str, rest: Iterator[str], text: bool | None) -> None:
        await super().send(first, text=text)
        for frame in rest:
            await super().send(frame, text=text)

    @overload
    async def recv(self, decode: Literal[True]) -> str: ...

    @overload
    async def recv(self, decode: Literal[False]) -> bytes: ...

    @overload
    async def recv(self, decode: bool | None = None) -> Data: ...

    async def recv(self, decode: bool | None = None) -> Data:
        while True:
            raw = await super().recv(decode)
            if not (isinstance(raw, str) and is_fragment_frame(raw)):
                return raw
            try:
                whole = self._reassembler.feed_frame(raw)
            except FragmentError as exc:
                # The reassembler has already freed the message; the rest of
                # its fragments are swallowed. Drop it and keep the channel up.
                logger.warning("Dropped fragmented message: %s", exc)
                continue
            if whole is not None:
                return whole
