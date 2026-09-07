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

The second check is what bounds where a forward may go. By default that is the
agent's own machine and the private networks around it — the homelab the agent
exists to reach — which is also what ``hle expose`` has always allowed for the
same agent on the same LAN. What it excludes is the public internet, so an
agent cannot be turned into a general-purpose outbound proxy. Narrowing it
further, to specific hosts and ports, is a matter of configuring rules.
"""

from __future__ import annotations

import base64
import ipaddress
from dataclasses import dataclass, field
from enum import StrEnum

from hle_common.wire import WireModel

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


@dataclass(kw_only=True)
class FpOpen(WireModel):
    """Ask the agent to dial ``target_host:target_port`` for a new stream."""

    type: FpMsgType = FpMsgType.OPEN
    stream_id: str
    target_host: str
    target_port: int


@dataclass(kw_only=True)
class FpReady(WireModel):
    type: FpMsgType = FpMsgType.READY
    stream_id: str


@dataclass(kw_only=True)
class FpData(WireModel):
    type: FpMsgType = FpMsgType.DATA
    stream_id: str
    data: str  # base64

    @classmethod
    def of(cls, stream_id: str, payload: bytes) -> FpData:
        return cls(stream_id=stream_id, data=base64.b64encode(payload).decode())

    def payload(self) -> bytes:
        return base64.b64decode(self.data)


@dataclass(kw_only=True)
class FpClose(WireModel):
    type: FpMsgType = FpMsgType.CLOSE
    stream_id: str
    reason: str | None = None


@dataclass(kw_only=True)
class FpError(WireModel):
    type: FpMsgType = FpMsgType.ERROR
    stream_id: str
    code: FpErrorCode = FpErrorCode.INTERNAL
    message: str | None = None


@dataclass(kw_only=True)
class FpHello(WireModel):
    """First frame from an ``hle fp`` client to the relay."""

    type: str = "fp_hello"
    api_key: str
    agent: str  # agent public id or name
    fp_version: str = FP_PROTOCOL_VERSION
    client_version: str | None = None


@dataclass(kw_only=True)
class FpWelcome(WireModel):
    type: str = "fp_welcome"
    agent_public_id: str
    # Echoed back so the CLI can show what it is actually allowed to reach,
    # rather than failing per-connection with a confusing refusal.
    allowed: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ForwardRule(WireModel):
    """One allowlist entry, e.g. ``localhost:22`` or ``192.168.1.50:*``.

    ``port`` of ``None`` means any port on that host. ``host`` may also be a
    CIDR block (``192.168.0.0/16``), which is how a whole network is expressed
    without listing every address on it.
    """

    host: str
    port: int | None = None

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        if _normalize_host(self.host) == _normalize_host(host):
            return True
        return self._network_contains(host)

    def _network_contains(self, host: str) -> bool:
        """Whether *host* is an address inside this rule's CIDR block.

        Only literal addresses are matched against a network. A *name* is never
        resolved to decide this: resolution happens on the agent at connect
        time, so allowing a name here on the strength of what it resolves to
        now would be checking one answer and using another.
        """
        if "/" not in self.host:
            return False
        try:
            network = ipaddress.ip_network(self.host, strict=False)
            return ipaddress.ip_address(_normalize_host(host)) in network
        except ValueError:
            return False

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


LOCAL_NETWORKS = "@local"
"""Rule meaning "every network this agent is directly attached to".

Expanded by the *agent*, against its own interface and route tables, at the
moment a target is checked. It has to work that way round: only the agent can
see that it has a Docker bridge, a Kubernetes CNI, a VPN and a LAN, and the
relay handing out fixed CIDRs would be guessing at all four. Expanding it
anywhere else — on the relay, in a dashboard — would describe the wrong
machine's networks, so everything except the agent treats it as matching
nothing.
"""


def default_rules() -> list[ForwardRule]:
    """Allowlist used when an agent has no explicit rules configured.

    The agent's own networks. An agent exists to reach a homelab, so a default
    that stopped at loopback answered the wrong question: it allowed only the
    one box the agent happened to run on, while ``hle expose --service
    https://192.168.2.200:8006`` — same agent, same LAN, same credential — has
    never been restricted at all. There is no threat model in which one of
    those is safe and the other is not; if anything the tunnel is the more
    exposed, since it publishes a host on the open internet while a forward
    binds to loopback on the operator's own machine.

    Public addresses are still excluded, and that is the line worth keeping: it
    stops an agent being used as a general-purpose outbound proxy, which is
    nobody's homelab use case. Reaching one is a matter of adding a rule.

    The static private ranges accompany the sentinel rather than being replaced
    by it, because an agent too old to understand ``@local`` would otherwise
    see one rule it cannot read and refuse everything — a worse regression than
    the problem being fixed. Old agents get the private ranges, which is
    already broader than the loopback they have today; new agents get both, and
    the sentinel is what covers the addresses no fixed list can (Tailscale on
    CGNAT, a homelab on globally-routable IPv6).
    """
    return [
        ForwardRule(host=LOCAL_NETWORKS),
        ForwardRule(host="localhost"),
        # RFC 1918 — where most homelabs live, and the fallback for any agent
        # that predates the sentinel above.
        ForwardRule(host="10.0.0.0/8"),
        ForwardRule(host="172.16.0.0/12"),
        ForwardRule(host="192.168.0.0/16"),
        # RFC 3927 / RFC 4193 — link-local IPv4 and unique-local IPv6.
        ForwardRule(host="169.254.0.0/16"),
        ForwardRule(host="fc00::/7"),
        ForwardRule(host="fe80::/10"),
    ]


def is_allowed(rules: list[ForwardRule], host: str, port: int) -> bool:
    return any(rule.matches(host, port) for rule in rules)


def parse_rule(text: str) -> ForwardRule | None:
    """Read back a rule from its ``str()`` form, e.g. ``192.168.0.0/16:*``.

    The welcome frame carries the agent's allowlist as rendered strings, which
    is enough for the client to check a target *before* offering a forward it
    already knows will be refused. Anything unparseable returns ``None`` and is
    dropped by the caller: a rule the client cannot read must not silently
    become a rule that matches nothing, or a legitimate target would look
    forbidden.
    """
    raw = text.strip()
    if not raw:
        return None
    host, sep, port = raw.rpartition(":")
    if not sep or not host:
        return ForwardRule(host=raw)
    if port == "*":
        return ForwardRule(host=host)
    try:
        return ForwardRule(host=host, port=int(port))
    except ValueError:
        # A bare IPv6 literal has colons of its own and no port suffix.
        return ForwardRule(host=raw)
