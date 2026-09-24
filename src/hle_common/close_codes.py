"""WebSocket close codes used across the HLE control planes.

These were literals scattered through the relay and the client, which is how
the client came to treat some of them as retryable when they were not: the code
meant one thing where it was sent and another where it was read. The vocabulary
belongs to the protocol, so it lives here with both halves importing it.

All codes are in the 4000–4999 private-use range (RFC 6455 §7.4.2).

Whether a code is retryable is part of its meaning, not a client-side policy
choice — see :data:`FATAL_CODES` and :func:`retry_after_seconds`.
"""

from __future__ import annotations

from typing import Final

# -- Handshake ------------------------------------------------------------- #
BAD_FIRST_MESSAGE: Final = 4000
"""Expected TUNNEL_REGISTER as the first message and got something else."""

INVALID_CREDENTIAL: Final = 4001
"""API key or agent token is missing, malformed, inactive, or revoked."""

# -- Revoked while connected ------------------------------------------------ #
DISCONNECTED_BY_OWNER: Final = 4002
"""Closed from the dashboard, or the API key carrying it was revoked."""

TUNNEL_LIMIT: Final = 4003
"""The account has no room for this tunnel under its current plan."""

LABEL_IN_USE: Final = 4004
"""The label belongs to another account, or the account's tunnel code changed."""

# -- One identity, two connections ------------------------------------------ #
REPLACED: Final = 4009
"""A newer connection took this label or agent identity over.

Sent to the connection being replaced. Fatal: reconnecting takes the identity
straight back from whoever just claimed it, and the two ends then trade it
between them roughly once a second.
"""

DUPLICATE_INSTANCE: Final = 4010
"""Another *live* instance already holds this identity, and it is still healthy.

Sent to the connection arriving second, which is the opposite end from
:data:`REPLACED` — the incumbent keeps serving and the newcomer is turned away.
Fatal for the same reason: two copies on one credential cannot both win.
"""

HANDOVER: Final = 4011
"""A successor process took this agent identity over, as arranged.

Sent to the *incumbent* during a remote update (agent protocol 1.2): the new
version connected with ``successor_of`` + the nonce the server issued, the
server admitted it, and this connection is now surplus. Unlike
:data:`REPLACED` nothing went wrong, so it is not fatal — but the old process
must not reconnect either, or it would fight the successor for the endpoints
it just handed over. The right response is to exit 0 without retrying: the
successor is already running, and a service manager does not restart a clean
exit. See :func:`should_reconnect`.
"""

# -- Rate limiting ---------------------------------------------------------- #
TOO_MANY_REGISTRATIONS: Final = 4029
"""This identity is registering far faster than any healthy client needs to.

Not fatal — the client is told to wait rather than to stop — because the cause
is usually a supervisor restarting something, and the right response is to slow
down, not to give up and stay down.
"""

IDLE_TIMEOUT: Final = 4408
"""Nothing was heard from the peer within its keepalive window."""


FATAL_CODES: Final[frozenset[int]] = frozenset(
    {
        INVALID_CREDENTIAL,
        TUNNEL_LIMIT,
        LABEL_IN_USE,
        REPLACED,
        DUPLICATE_INSTANCE,
    }
)
"""Codes a client must not reconnect after.

Every one of these describes a condition that reconnecting cannot improve and
can actively worsen: a bad credential stays bad, and a contested identity turns
into a reconnect storm the moment both ends keep trying.
"""


NO_RECONNECT_CODES: Final[frozenset[int]] = frozenset({HANDOVER})
"""Codes after which a client stops without treating the close as a failure.

The fatal set says "stop, and tell the operator something is wrong". These say
"stop, and that is fine": the relay ended the session on purpose and another
process of ours is carrying on. Disjoint from :data:`FATAL_CODES` so a caller
can tell the two apart.
"""


#: How long to wait before retrying, for codes that are worth retrying at all.
_RETRY_AFTER: Final[dict[int, float]] = {
    TOO_MANY_REGISTRATIONS: 60.0,
}


def is_fatal(code: int | None) -> bool:
    """Whether *code* means "stop", rather than "try again"."""
    return code in FATAL_CODES


def should_reconnect(code: int | None) -> bool:
    """Whether a client should open a new connection after closing with *code*.

    False for fatal codes (reconnecting would make things worse) and for
    :data:`HANDOVER` (reconnecting would undo an intended takeover). True for
    everything else, including ``None`` (no close frame at all).
    """
    return code not in FATAL_CODES and code not in NO_RECONNECT_CODES


def retry_after_seconds(code: int | None) -> float | None:
    """Minimum wait a retryable code asks for, or ``None`` for no specific ask."""
    if code is None:
        return None
    return _RETRY_AFTER.get(code)
