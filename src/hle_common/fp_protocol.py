"""Firepuncher protocol — forward a remote TCP port to a local one.

Firepuncher ("firewall puncher") lets you reach a TCP service that only a remote
agent can see, without opening a port anywhere::

    laptop                        relay                      rpi
      hle fp --to localhost:22  ──WSS+API key──▶  ◀──WSS──  agent
      listens 127.0.0.1:9922                                  │
                                                              ▼
                                                      127.0.0.1:22

This inverts the normal data plane. In ``protocol.py`` the relay is the *server*
side of every connection: public traffic arrives and is proxied to the agent.
Here an **authenticated client dials in** and the relay pairs it to one of that
user's agents. There is never a public TCP port — both ends authenticate.

One session multiplexes many TCP connections by ``stream_id``: SSH opens one,
but ``scp``, ``rsync``, and SSH's own forwarding open more. Payloads are
base64-encoded because the transport is JSON over WebSocket.

Authorization is enforced twice, deliberately:

1. The **relay** checks that the API key's owner owns the target agent.
2. The **agent** checks the target against its allowlist.

The second check is what stops a leaked API key from turning an agent into a
general-purpose pivot into the private network behind it.
"""

from __future__ import annotations

import base64
from enum import StrEnum

from pydantic import BaseModel, Field

FP_PROTOCOL_VERSION = "1.0"

# Cap on a single fp_data payload (pre-base64). Keeps one noisy stream from
# starving others sharing the control connection.
FP_MAX_CHUNK = 64 * 1024


class FpMsgType(StrEnum):
    OPEN = "fp_open"  # client -> relay -> agent: dial this target
    READY = "fp_ready"  # agent -> relay -> client: connected, send data
    DATA = "fp_data"  # bidirectional: payload bytes
    CLOSE = "fp_close"  # bidirectional: this stream is done
    ERROR = "fp_error"  # agent/relay -> client: dial failed or was refused


class FpErrorCode(StrEnum):
    """Why a stream was refused. Distinct codes so the CLI can explain itself."""

    NOT_ALLOWED = "not_allowed"  # target not in the agent's allowlist
    UNREACHABLE = "unreachable"  # agent could not connect (refused/no route)
    TIMEOUT = "timeout"  # dial exceeded the deadline
    AGENT_OFFLINE = "agent_offline"  # no live control connection for that agent
    FORBIDDEN = "forbidden"  # caller does not own the agent
    INTERNAL = "internal"


class FpOpen(BaseModel):
    """Ask the agent to dial ``target_host:target_port`` for a new stream."""

    type: FpMsgType = FpMsgType.OPEN
    stream_id: str
    target_host: str
    target_port: int


class FpReady(BaseModel):
    type: FpMsgType = FpMsgType.READY
    stream_id: str


class FpData(BaseModel):
    type: FpMsgType = FpMsgType.DATA
    stream_id: str
    data: str  # base64

    @classmethod
    def of(cls, stream_id: str, payload: bytes) -> FpData:
        return cls(stream_id=stream_id, data=base64.b64encode(payload).decode())

    def payload(self) -> bytes:
        return base64.b64decode(self.data)


class FpClose(BaseModel):
    type: FpMsgType = FpMsgType.CLOSE
    stream_id: str
    reason: str | None = None


class FpError(BaseModel):
    type: FpMsgType = FpMsgType.ERROR
    stream_id: str
    code: FpErrorCode = FpErrorCode.INTERNAL
    message: str | None = None


class FpHello(BaseModel):
    """First frame from an ``hle fp`` client to the relay."""

    type: str = "fp_hello"
    api_key: str
    agent: str  # agent public id or name
    fp_version: str = FP_PROTOCOL_VERSION
    client_version: str | None = None


class FpWelcome(BaseModel):
    type: str = "fp_welcome"
    agent_public_id: str
    # Echoed back so the CLI can show what it is actually allowed to reach,
    # rather than failing per-connection with a confusing refusal.
    allowed: list[str] = Field(default_factory=list)


class ForwardRule(BaseModel):
    """One allowlist entry, e.g. ``localhost:22`` or ``192.168.1.50:*``.

    ``port`` of ``None`` means any port on that host.
    """

    host: str
    port: int | None = None

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        return _normalize_host(self.host) == _normalize_host(host)

    def __str__(self) -> str:
        return f"{self.host}:{self.port if self.port is not None else '*'}"


# Hostnames that all mean "the machine the agent runs on". Treated as one target
# so an allowlist of `localhost:22` isn't trivially bypassed (or accidentally
# missed) by asking for `127.0.0.1:22`.
_LOOPBACK_ALIASES = frozenset({"localhost", "127.0.0.1", "::1", "ip6-localhost"})


def _normalize_host(host: str) -> str:
    h = host.strip().lower().rstrip(".")
    # Strip brackets from IPv6 literals like [::1].
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return "localhost" if h in _LOOPBACK_ALIASES else h


def default_rules() -> list[ForwardRule]:
    """Allowlist used when an agent has no explicit rules configured.

    Loopback only: enough for the common "SSH to the box the agent runs on"
    case with zero setup, while anything else on the LAN stays opt-in.
    """
    return [ForwardRule(host="localhost")]


def is_allowed(rules: list[ForwardRule], host: str, port: int) -> bool:
    return any(rule.matches(host, port) for rule in rules)
