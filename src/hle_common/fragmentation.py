"""Envelope fragmentation — carry any message past the per-message transport cap.

Every HLE WebSocket channel (the tunnel data plane and the agent control
channel) sends one JSON envelope per WebSocket message, and both ends cap a
WebSocket message at 4 MB. Rather than teach each message type to split itself
(the way ``HTTP_RESPONSE_START/CHUNK/END`` does for HTTP bodies), a sender that
negotiated :data:`CAPABILITY_FRAGMENTATION` cuts any *serialized envelope* over
:data:`FRAGMENT_THRESHOLD` into consecutive slices and sends each one as a
``fragment`` frame. The receiver joins the slices back into the original text
and hands it to its normal parse/dispatch path as if it had arrived whole.

The mechanism knows nothing about what it carries, so every current and future
message type — in either direction, on either channel — gets it for free.

Wire contract (tunnel protocol 1.6, agent protocol 1.5)::

    {"type":"fragment","tunnel_id":null,"request_id":null,
     "payload":{"frag_id":"7","seq":0,"final":false,"data":"{\\"type\\":\\"ws_frame\\",..."}}

* ``frag_id`` — opaque, at most :data:`MAX_FRAG_ID_LENGTH` characters, unique
  per sender per connection for the life of the connection. Fragments of
  different messages may interleave.
* ``seq`` — 0, 1, 2, ... per ``frag_id``, fewer than
  :data:`MAX_FRAGMENTS_PER_MESSAGE` in all. The transport is ordered, so a gap,
  a duplicate or a reordering is a protocol violation, never something to wait
  out.
* ``final`` — true on the last slice; the message is complete once it arrives.
* ``data`` — a contiguous slice of the envelope *text*. Slicing a ``str``
  never splits a character, and an envelope serialized by
  :mod:`hle_common.wire` is ASCII, so a slice costs nothing beyond JSON string
  escaping (no base64 inflation, no decode pass).

Frames are always built with :func:`encode_fragment`, which is what lets a
receiver recognise one with a prefix test (:func:`is_fragment_frame`) instead
of parsing every message twice.

Sizes are counted in characters of envelope text, which equals bytes for the
ASCII JSON this package emits.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from hle_common.protocol import MessageType, ProtocolMessage
from hle_common.wire import WireModel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Capability token, advertised in TunnelRegistration.capabilities /
# AgentHello.capabilities and echoed in TunnelRegistrationResponse.
# server_capabilities / AgentWelcome.capabilities. Advertising it means "I can
# reassemble fragments"; a peer only *sends* fragments once the other side's
# token is known. Without it on both sides nothing on the wire changes.
CAPABILITY_FRAGMENTATION: Final = "fragmentation"

# Envelopes at or below this length are sent whole, exactly as before.
FRAGMENT_THRESHOLD: Final = 1024 * 1024
# Envelope characters per fragment. Even fully JSON-escaped this stays far
# below the 4 MB per-message transport cap.
FRAGMENT_SIZE: Final = 256 * 1024
# Hard ceiling on one reassembled envelope. A sender refuses to fragment past
# it (MessageTooLargeError); a receiver refuses to buffer past it.
MAX_MESSAGE_SIZE: Final = 64 * 1024 * 1024
# Ceiling on partially received envelopes across one connection, so many
# half-sent messages cannot multiply the per-message ceiling.
MAX_IN_FLIGHT: Final = 128 * 1024 * 1024
# Partially received messages allowed at once on one connection.
MAX_PARTIALS: Final = 32
# Slices allowed in one message. The size ceilings count characters, not the
# per-slice bookkeeping, so without this a peer could grow a partial forever
# with empty (or one-character) slices. A ceiling-sized message in default
# slices needs 256; a sender must slice no finer than MAX_MESSAGE_SIZE / this.
MAX_FRAGMENTS_PER_MESSAGE: Final = 4096
# Longest accepted frag_id. Ids are keys in the partial table and the
# abandoned set, outside every size ceiling, so they must be small.
MAX_FRAG_ID_LENGTH: Final = 64
# A partial that has not grown for this long is dropped on the next feed().
PARTIAL_IDLE_TIMEOUT: Final = 60.0
# How many abandoned frag_ids are remembered, so the rest of a rejected
# message is dropped quietly instead of raising once per remaining slice.
_ABANDONED_MAX: Final = 1024

_FRAME_PREFIX: Final = '{"type":"fragment",'


# -- errors --------------------------------------------------------------------


class FragmentError(Exception):
    """Base for every fragmentation failure.

    ``close_code`` is the WebSocket close code that fits the failure, for a
    caller that knows which stream or connection to close with it.
    """

    close_code: int = 1002  # protocol error

    def __init__(self, message: str, *, frag_id: str | None = None) -> None:
        super().__init__(message)
        self.frag_id = frag_id


class FragmentProtocolError(FragmentError):
    """Malformed fragment, or a ``seq`` gap, duplicate or reordering.

    The partial message (if any) has been discarded; drop it and log.
    """


class FragmentLimitError(FragmentError):
    """A size or count ceiling was hit. The partial has been discarded."""

    close_code = 1009  # message too big


class MessageTooLargeError(FragmentLimitError):
    """Sender side: the envelope exceeds :data:`MAX_MESSAGE_SIZE`.

    Raised before anything is sent, so the caller can close the stream that
    produced the message with ``close_code`` (1009) instead.
    """


# -- wire model ----------------------------------------------------------------


@dataclass(kw_only=True)
class Fragment(WireModel):
    """Payload of a ``MessageType.FRAGMENT`` frame. See the module docstring."""

    frag_id: str
    seq: int
    final: bool
    data: str

    def __post_init__(self) -> None:
        # The wire layer does not type-check, and these fields drive buffering.
        if not isinstance(self.frag_id, str) or not 0 < len(self.frag_id) <= MAX_FRAG_ID_LENGTH:
            raise ValueError(f"frag_id must be a string of 1-{MAX_FRAG_ID_LENGTH} characters")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("seq must be a non-negative integer")
        if not isinstance(self.final, bool):
            raise ValueError("final must be a bool")
        if not isinstance(self.data, str):
            raise ValueError("data must be a string")


def encode_fragment(fragment: Fragment) -> str:
    """Serialize *fragment* as a complete ``fragment`` frame."""
    return ProtocolMessage(
        type=MessageType.FRAGMENT, payload=fragment.model_dump()
    ).model_dump_json()


def is_fragment_frame(raw: str | bytes) -> bool:
    """Whether *raw* is a frame built by :func:`encode_fragment` (no parsing)."""
    return isinstance(raw, str) and raw.startswith(_FRAME_PREFIX)


# -- sender --------------------------------------------------------------------


class Fragmenter:
    """Split outgoing envelopes into ``fragment`` frames. One per connection."""

    def __init__(
        self,
        *,
        threshold: int = FRAGMENT_THRESHOLD,
        fragment_size: int = FRAGMENT_SIZE,
        max_message_size: int = MAX_MESSAGE_SIZE,
    ) -> None:
        if fragment_size <= 0:
            raise ValueError("fragment_size must be positive")
        self._threshold = threshold
        self._fragment_size = fragment_size
        self._max_message_size = max_message_size
        self._ids = itertools.count(1)

    def frames(self, envelope: str) -> Iterator[str]:
        """Frames to send for *envelope*, in order.

        A small envelope yields itself, unchanged. A large one yields its
        fragments lazily, so no second copy of the message is ever built.
        Raises :class:`MessageTooLargeError` immediately — before anything is
        sent — when *envelope* exceeds the ceiling.
        """
        size = len(envelope)
        if size <= self._threshold:
            return iter((envelope,))
        if size > self._max_message_size:
            raise MessageTooLargeError(
                f"message of {size} bytes exceeds the {self._max_message_size}-byte ceiling"
            )
        return self._slices(envelope, str(next(self._ids)))

    def _slices(self, envelope: str, frag_id: str) -> Iterator[str]:
        step = self._fragment_size
        size = len(envelope)
        for seq, start in enumerate(range(0, size, step)):
            yield encode_fragment(
                Fragment(
                    frag_id=frag_id,
                    seq=seq,
                    final=start + step >= size,
                    data=envelope[start : start + step],
                )
            )


# -- receiver ------------------------------------------------------------------


@dataclass
class _Partial:
    parts: list[str] = field(default_factory=list)
    size: int = 0
    next_seq: int = 0
    last_seen: float = 0.0


class Reassembler:
    """Rebuild fragmented envelopes. One per connection; drop it on disconnect.

    :meth:`feed` returns the complete envelope text when the final fragment
    arrives, ``None`` while a message is still incomplete, and raises a
    :class:`FragmentError` subclass when the message has to be abandoned. The
    message's state is already freed when it raises; the remaining fragments of
    an abandoned message are then swallowed silently (``None``).

    Every ceiling is checked *before* a slice is appended, so nothing is ever
    buffered past it.
    """

    def __init__(
        self,
        *,
        max_message_size: int = MAX_MESSAGE_SIZE,
        max_in_flight: int = MAX_IN_FLIGHT,
        max_partials: int = MAX_PARTIALS,
        max_fragments: int = MAX_FRAGMENTS_PER_MESSAGE,
        idle_timeout: float = PARTIAL_IDLE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_message_size = max_message_size
        self._max_in_flight = max_in_flight
        self._max_partials = max_partials
        self._max_fragments = max_fragments
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._partials: dict[str, _Partial] = {}
        self._abandoned: dict[str, None] = {}  # insertion-ordered set
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Characters currently buffered across all partial messages."""
        return self._in_flight

    @property
    def partial_count(self) -> int:
        return len(self._partials)

    def feed_frame(self, raw: str) -> str | None:
        """Parse a ``fragment`` frame (the whole WebSocket message) and feed it."""
        try:
            msg = ProtocolMessage.model_validate_json(raw)
        except ValueError as exc:
            raise FragmentProtocolError(f"unparseable fragment frame: {exc}") from exc
        if msg.type != MessageType.FRAGMENT:
            raise FragmentProtocolError(f"expected a fragment frame, got {msg.type}")
        return self.feed_payload(msg.payload)

    def feed_payload(self, payload: Any) -> str | None:
        """Validate a ``FRAGMENT`` message's payload and feed it."""
        try:
            fragment = Fragment.model_validate(payload)
        except ValueError as exc:
            raise FragmentProtocolError(f"malformed fragment: {exc}") from exc
        return self.feed(fragment)

    def feed(self, fragment: Fragment) -> str | None:
        now = self._clock()
        self.expire(now)
        frag_id = fragment.frag_id

        if frag_id in self._abandoned:
            if fragment.final:
                del self._abandoned[frag_id]
            return None

        partial = self._partials.get(frag_id)
        if partial is None:
            if fragment.seq != 0:
                self._abandon(frag_id, fragment.final)
                raise FragmentProtocolError(
                    f"fragment {frag_id} seq={fragment.seq} has no preceding seq=0",
                    frag_id=frag_id,
                )
            if not fragment.final and len(self._partials) >= self._max_partials:
                self._abandon(frag_id, fragment.final)
                raise FragmentLimitError(
                    f"more than {self._max_partials} partial messages in flight",
                    frag_id=frag_id,
                )
            partial = _Partial()
        elif fragment.seq != partial.next_seq:
            self._drop(frag_id)
            self._abandon(frag_id, fragment.final)
            raise FragmentProtocolError(
                f"fragment {frag_id} expected seq={partial.next_seq}, got {fragment.seq}",
                frag_id=frag_id,
            )

        if fragment.seq >= self._max_fragments:
            self._drop(frag_id)
            self._abandon(frag_id, fragment.final)
            raise FragmentLimitError(
                f"fragmented message {frag_id} exceeds {self._max_fragments} slices",
                frag_id=frag_id,
            )

        size = len(fragment.data)
        if partial.size + size > self._max_message_size:
            self._drop(frag_id)
            self._abandon(frag_id, fragment.final)
            raise FragmentLimitError(
                f"fragmented message {frag_id} exceeds {self._max_message_size} bytes",
                frag_id=frag_id,
            )

        if fragment.final:
            # Complete: assemble without counting the last slice as in flight.
            self._drop(frag_id)
            if not partial.parts:
                return fragment.data
            partial.parts.append(fragment.data)
            return "".join(partial.parts)

        if self._in_flight + size > self._max_in_flight:
            self._drop(frag_id)
            self._abandon(frag_id, fragment.final)
            raise FragmentLimitError(
                f"partial messages exceed {self._max_in_flight} bytes in flight",
                frag_id=frag_id,
            )
        partial.parts.append(fragment.data)
        partial.size += size
        partial.next_seq += 1
        partial.last_seen = now
        self._partials[frag_id] = partial
        self._in_flight += size
        return None

    def expire(self, now: float | None = None) -> int:
        """Drop partials idle past the timeout. Returns how many were dropped."""
        if now is None:
            now = self._clock()
        stale = [
            frag_id
            for frag_id, partial in self._partials.items()
            if now - partial.last_seen > self._idle_timeout
        ]
        for frag_id in stale:
            self._drop(frag_id)
            self._abandon(frag_id, final=False)
        return len(stale)

    def clear(self) -> None:
        """Free everything — call on disconnect."""
        self._partials.clear()
        self._abandoned.clear()
        self._in_flight = 0

    def _drop(self, frag_id: str) -> None:
        partial = self._partials.pop(frag_id, None)
        if partial is not None:
            self._in_flight -= partial.size

    def _abandon(self, frag_id: str, final: bool) -> None:
        """Remember *frag_id* so its remaining fragments are swallowed."""
        if final:
            return  # nothing more is coming
        self._abandoned[frag_id] = None
        while len(self._abandoned) > _ABANDONED_MAX:
            del self._abandoned[next(iter(self._abandoned))]
