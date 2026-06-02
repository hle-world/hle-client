"""Agent control protocol — shared message models (client + server).

The *control* protocol between a long-running HLE agent (one per homelab) and the
server. It is separate from the tunnel *data* protocol in ``protocol.py``: the data
plane carries proxied HTTP/WS traffic per tunnel; this channel carries only the
desired set of endpoints (server -> agent) and runtime status (agent -> server).

Declarative model: the server sends the full desired set of enabled endpoints and
the agent reconciles its running tunnels to match. Reconnects resend the snapshot.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

AGENT_PROTOCOL_VERSION = "1.0"


class AgentMsgType(StrEnum):
    HELLO = "hello"  # agent -> server (first message, authenticates)
    WELCOME = "welcome"  # server -> agent (ack + full desired state)
    STATE_SYNC = "state_sync"  # server -> agent (desired state changed)
    STATUS = "status"  # agent -> server (per-endpoint runtime status)
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


class EndpointSpec(BaseModel):
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


class AgentHello(BaseModel):
    type: AgentMsgType = AgentMsgType.HELLO
    token: str
    agent_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class AgentWelcome(BaseModel):
    type: AgentMsgType = AgentMsgType.WELCOME
    agent_public_id: str
    base_domain: str
    # Data-plane credential the agent uses to register its tunnels. Sent by the
    # server so a single enrollment yields both control + data-plane auth.
    api_key: str | None = None
    endpoints: list[EndpointSpec] = Field(default_factory=list)


class AgentStateSync(BaseModel):
    type: AgentMsgType = AgentMsgType.STATE_SYNC
    endpoints: list[EndpointSpec] = Field(default_factory=list)


class EndpointStatus(BaseModel):
    label: str
    connected: bool = False
    public_url: str | None = None
    error: str | None = None


class AgentStatus(BaseModel):
    type: AgentMsgType = AgentMsgType.STATUS
    endpoints: list[EndpointStatus] = Field(default_factory=list)
