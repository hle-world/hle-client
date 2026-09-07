"""Agent control protocol — shared message models (client + server).

The *control* protocol between a long-running HLE agent (one per homelab) and the
server. It is separate from the tunnel *data* protocol in ``protocol.py``: the data
plane carries proxied HTTP/WS traffic per tunnel; this channel carries only the
desired set of endpoints (server -> agent) and runtime status (agent -> server).

Declarative model: the server sends the full desired set of enabled endpoints and
the agent reconciles its running tunnels to match. Reconnects resend the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Runtime import, not just typing: pydantic resolves this to build the model.
from hle_common.fp_protocol import ForwardRule  # noqa: TC001
from hle_common.wire import WireModel

# 1.1 adds firepuncher: `forward_rules` on welcome/state_sync, and `fp_*` frames
# multiplexed onto this same control connection.
AGENT_PROTOCOL_VERSION = "1.1"


class AgentMsgType(StrEnum):
    HELLO = "hello"  # agent -> server (first message, authenticates)
    WELCOME = "welcome"  # server -> agent (ack + full desired state)
    STATE_SYNC = "state_sync"  # server -> agent (desired state changed)
    STATUS = "status"  # agent -> server (per-endpoint runtime status)
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


@dataclass(kw_only=True)
class EndpointSpec(WireModel):
    """One endpoint the agent should run."""

    id: int
    label: str
    service_url: str
    zone: str | None = None  # custom-zone domain, or None for the base domain
    auth_mode: str = "sso"
    webhook_path: str | None = None
    websocket_enabled: bool = True

    def reconcile_key(self) -> tuple[str, str | None, str, str | None, bool]:
        """Identity used to detect when a running tunnel must be restarted."""
        return (
            self.service_url,
            self.zone,
            self.auth_mode,
            self.webhook_path,
            self.websocket_enabled,
        )


@dataclass(kw_only=True)
class AgentHello(WireModel):
    type: AgentMsgType = AgentMsgType.HELLO
    token: str
    agent_version: str | None = None
    capabilities: list[str] = field(default_factory=list)
    # Which agent process this is. One enrollment token can be copied onto a
    # second machine, and without an identity per process the relay cannot tell
    # that from the same agent reconnecting — so it evicted the incumbent every
    # time, and two agents took each other's endpoints in turn indefinitely.
    # `hostname` is shown only to the account's own owner, to name where the
    # other copy is running.
    instance_id: str | None = None
    hostname: str | None = None


@dataclass(kw_only=True)
class AgentWelcome(WireModel):
    type: AgentMsgType = AgentMsgType.WELCOME
    agent_public_id: str
    base_domain: str
    # Data-plane credential the agent uses to register its tunnels. Sent by the
    # server so a single enrollment yields both control + data-plane auth.
    api_key: str | None = None
    endpoints: list[EndpointSpec] = field(default_factory=list)
    # Firepuncher allowlist. None means "server said nothing" — the agent keeps
    # its safe default (loopback only) rather than assuming everything is open.
    forward_rules: list[ForwardRule] | None = None


@dataclass(kw_only=True)
class AgentStateSync(WireModel):
    type: AgentMsgType = AgentMsgType.STATE_SYNC
    endpoints: list[EndpointSpec] = field(default_factory=list)
    forward_rules: list[ForwardRule] | None = None


@dataclass(kw_only=True)
class EndpointStatus(WireModel):
    label: str
    connected: bool = False
    public_url: str | None = None
    error: str | None = None


@dataclass(kw_only=True)
class AgentStatus(WireModel):
    type: AgentMsgType = AgentMsgType.STATUS
    endpoints: list[EndpointStatus] = field(default_factory=list)
