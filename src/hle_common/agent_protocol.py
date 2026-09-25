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
from typing import Literal

# Runtime import, not just typing: pydantic resolves this to build the model.
from hle_common.fp_protocol import ForwardRule  # noqa: TC001
from hle_common.tunnel_spec import TunnelSpec
from hle_common.wire import WireModel

# 1.1 adds firepuncher: `forward_rules` on welcome/state_sync, and `fp_*` frames
# multiplexed onto this same control connection.
# 1.2 adds remote update: `update_*` frames, the install/platform fields on hello
# (so the server knows which agents *can* update), the `successor_*` handover
# fields, and close code 4011 HANDOVER. Everything is optional: a 1.1 peer sees
# fields it ignores and message types it does not handle, nothing it rejects.
# 1.3 makes EndpointSpec the full TunnelSpec (verify_ssl, forward_host,
# upstream_basic_auth, apex, options, response_timeout, managed_by). All new
# fields default to null, so 1.2 peers see only keys they ignore.
AGENT_PROTOCOL_VERSION = "1.3"

# Install methods whose venv the agent owns outright and can stage a new version
# into. Everything else (brew keg, docker image, system pip, PEP 668 interpreter)
# is owned by something outside the process, and the dashboard has to tell the
# operator what to run instead of pretending it can do it for them.
SELF_UPDATE_METHODS: frozenset[str] = frozenset({"venv", "pipx", "uv"})

UPDATE_CAPABILITY_PREFIX = "update:"


def update_capability(install_method: str | None) -> str | None:
    """Hello capability for *install_method*, or ``None`` when it cannot self-update.

    ``install_method`` is what ``hle_client.update_cmd.detect_install_method``
    returns (``venv``, ``pipx``, ``uv``, ``brew``, ``pip``, ``externally-managed``)
    or a distribution's own name (``docker``). Kept here rather than next to
    the detector so the server can apply the same rule to a stored value
    without importing the CLI.
    """
    if install_method in SELF_UPDATE_METHODS:
        return f"{UPDATE_CAPABILITY_PREFIX}{install_method}"
    return None


class AgentMsgType(StrEnum):
    HELLO = "hello"  # agent -> server (first message, authenticates)
    WELCOME = "welcome"  # server -> agent (ack + full desired state)
    STATE_SYNC = "state_sync"  # server -> agent (desired state changed)
    STATUS = "status"  # agent -> server (per-endpoint runtime status)
    PING = "ping"
    PONG = "pong"
    ERROR = "error"
    # 1.2 — remote update. server -> agent request, then agent -> server
    # ack / progress / result, all correlated by `request_id`.
    UPDATE_REQUEST = "update_request"
    UPDATE_ACK = "update_ack"
    UPDATE_PROGRESS = "update_progress"
    UPDATE_RESULT = "update_result"


@dataclass(kw_only=True)
class _EndpointIdentity:
    # A base of its own so that dataclass field ordering puts `id` first, where
    # it has always been on the wire: fields are collected base-first, and this
    # base sits after TunnelSpec in EndpointSpec's MRO.
    id: int


@dataclass(kw_only=True, repr=False)
class EndpointSpec(TunnelSpec, _EndpointIdentity):
    """One endpoint the agent should run: a full ``TunnelSpec`` plus its id.

    Up to 1.2 this carried five tunnel fields, so a dashboard endpoint could not
    set verify-ssl, forward-host, upstream basic auth, apex, options or a
    response timeout. 1.3 makes it the whole spec. The 1.2 fields keep their
    names, order and defaults; the new ones default to None, so a 1.2 server
    still produces an EndpointSpec a 1.3 agent reads, and a 1.2 agent reading
    a 1.3 one simply ignores the keys it does not know.

    ``label`` and ``service_url`` stay required here, as they always were on
    the wire; ``reconcile_key()`` comes from ``TunnelSpec`` and covers every
    field, so a dashboard edit of any of them restarts that endpoint.
    """

    # `field()` with no default: a bare annotation would inherit the
    # TunnelSpec class attribute as its default and quietly make them optional.
    label: str = field()
    service_url: str = field()


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
    # -- 1.2 ---------------------------------------------------------------
    # How the client was installed (`venv`, `pipx`, `uv`, `brew`, `docker`,
    # `pip`, ...). The server only offers an update when `capabilities` also
    # carries `update:<install_method>`; the method itself is sent so the
    # dashboard can say what to run when it does not.
    install_method: str | None = None
    platform: str | None = None  # `platform.system().lower()`: linux/darwin/freebsd
    python_version: str | None = None
    # `systemd`, `launchd`, `rc.d`, or None when not running under one. An
    # update ends with a restart, so the server needs to know something will
    # bring the process back.
    service_manager: str | None = None
    # Stage B canary handover: the successor announces which running instance
    # it replaces and proves the server asked for it with the nonce it was
    # handed in `update_request`. The server then admits it despite a live
    # incumbent (normally DUPLICATE_INSTANCE) and closes the old one with
    # close code HANDOVER.
    successor_of: str | None = None
    successor_nonce: str | None = None


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


# -- 1.2: remote update ------------------------------------------------------ #
# Server asks; agent answers at once with an ack, streams progress while it
# stages and swaps, and reports the outcome. The result may arrive on a *later*
# connection than the request (the swap restarts the process), so the server
# matches it by `request_id`, not by session.


@dataclass(kw_only=True)
class UpdateRequest(WireModel):
    type: AgentMsgType = AgentMsgType.UPDATE_REQUEST
    request_id: str
    target_version: str
    # Seconds the agent has to reach a healthy post-update state before it
    # rolls back on its own.
    deadline_s: int = 300
    # `wait`: hold the swap until the agent's tunnels have no live streams.
    # `force`: swap now and drop whatever is in flight.
    drain_policy: Literal["wait", "force"] = "wait"


@dataclass(kw_only=True)
class UpdateAck(WireModel):
    type: AgentMsgType = AgentMsgType.UPDATE_ACK
    request_id: str
    accepted: bool
    # Why not, when `accepted` is False: `unsupported:<install_method>`
    # (brew, docker, pip, ...), `busy:draining`, `busy:updating`.
    reason: str | None = None


@dataclass(kw_only=True)
class UpdateProgress(WireModel):
    type: AgentMsgType = AgentMsgType.UPDATE_PROGRESS
    request_id: str
    phase: Literal["staging", "verifying", "swapping", "restarting"]
    detail: str | None = None


@dataclass(kw_only=True)
class UpdateResult(WireModel):
    type: AgentMsgType = AgentMsgType.UPDATE_RESULT
    request_id: str
    ok: bool
    from_version: str
    to_version: str
    # Last lines of the updater's log, for the dashboard to show on failure.
    log_tail: list[str] = field(default_factory=list)
