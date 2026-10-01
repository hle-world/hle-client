"""Agent control protocol — shared message models (client + server).

The *control* protocol between a long-running HLE agent (one per homelab) and the
server. It is separate from the tunnel *data* protocol in ``protocol.py``: the data
plane carries proxied HTTP/WS traffic per tunnel; this channel carries only the
desired set of endpoints (server -> agent) and runtime status (agent -> server).

Declarative model: the server sends the full desired set of enabled endpoints and
the agent reconciles its running tunnels to match. Reconnects resend the snapshot.
"""

from __future__ import annotations

import re
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
#
# Stage B (canary handover) is layered on the 1.2 handover fields: a client
# advertises `handover` in its hello capabilities, and the server arms it by
# putting a `successor_nonce` on the UpdateRequest. A peer that knows neither
# keeps doing Stage A, because the extra capability is just a list entry and
# the nonce defaults to null.
#
# 1.4 adds cluster-owned declarations and log reads: `EndpointSpec.target` (a
# Kubernetes Service ref, null for everything a 1.3 server sends), the
# `declared_endpoints` / `declared_ack` frames, `logs_request` / `logs_response`,
# the `k8s:*` and `logs` hello capabilities, and `handover_group`. Additive
# again: every new field is optional with a null/empty default and the new
# message types are simply unhandled by older peers.
#
# 1.5 adds envelope fragmentation, shared with the tunnel channel: the
# `fragmentation` hello capability, `AgentWelcome.capabilities` to echo it, and
# `fragment` frames carrying slices of any oversized control message (the
# welcome included). See hle_common.fragmentation.
AGENT_PROTOCOL_VERSION = "1.5"

# Install methods whose venv the agent owns outright and can stage a new version
# into. Everything else (brew keg, docker image, system pip, PEP 668 interpreter)
# is owned by something outside the process, and the dashboard has to tell the
# operator what to run instead of pretending it can do it for them.
SELF_UPDATE_METHODS: frozenset[str] = frozenset({"venv", "pipx", "uv"})

UPDATE_CAPABILITY_PREFIX = "update:"

# Hello `capabilities` values an agent advertises so the server knows which of
# the 1.4 features it may use with this peer. Kept as constants so the agent and
# the server cannot drift on the spelling.
K8S_SERVICES_CAPABILITY = "k8s:services"
K8S_DECLARED_CAPABILITY = "k8s:declared"
LOGS_CAPABILITY = "logs"

# Upper bound on `logs_request.lines`. The agent clamps to this anyway; naming
# it here lets the server ask for as much as it is allowed to receive.
MAX_LOG_LINES = 500
# Longest single line the agent will put in a `logs_response`. A line past this
# is rejected rather than silently truncated, so both ends agree on the cap.
MAX_LOG_LINE_CHARS = 2000
# Upper bound on the number of entries in a `declared_endpoints` frame: a whole
# cluster's worth, with headroom. More than this is malformed, not merely large.
MAX_DECLARED_ENDPOINTS = 1000

# Kubernetes naming rules, checked in `K8sServiceTarget.__post_init__` because
# dataclasses enforce neither `Literal` nor string shape. DNS-1123 labels are
# lower-case alphanumerics with interior hyphens (namespace, Service name used
# as a label); DNS-1035 labels additionally start with a letter (Service name).
_DNS_1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS_1035_LABEL_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
# IANA service names: lower-case alphanumerics and hyphens, at most 15 chars,
# at least one letter, no leading/trailing/doubled hyphen.
_PORT_NAME_RE = re.compile(r"^[a-z0-9-]+$")
# A declared endpoint's reference to a locally-resolved credential:
# ``namespace/name#key``.
_SECRET_REF_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?/[a-z0-9]([-a-z0-9]*[a-z0-9])?#[A-Za-z0-9._-]+$"
)
# Where a declaration came from: ``hletunnel:<namespace>/<name>`` or
# ``ingress:<namespace>/<name>``.
_SOURCE_REF_RE = re.compile(
    r"^(hletunnel|ingress):[a-z0-9]([-a-z0-9]*[a-z0-9])?/[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
)
# Stage B: a hello capability saying this build can take part in a canary
# handover — spawn a successor for an update, and itself run as one. The
# server only arms Stage B (by putting `successor_nonce` on the request) for
# agents that advertise it; without it the update is Stage A (swap and exit,
# and let the service manager restart) exactly as before.
HANDOVER_CAPABILITY = "handover"


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
    # 1.4 — cluster-owned declarations and log reads.
    # agent -> server: the full set of endpoints the cluster's own manifests
    # say should exist; server -> agent: per-label accept/conflict.
    DECLARED_ENDPOINTS = "declared_endpoints"
    DECLARED_ACK = "declared_ack"
    # server -> agent request, then agent -> server response, by `request_id`.
    LOGS_REQUEST = "logs_request"
    LOGS_RESPONSE = "logs_response"
    # 1.5 — either direction: a slice of an oversized message. Same frame as
    # the tunnel channel's MessageType.FRAGMENT (hle_common.fragmentation),
    # sent only to a peer that advertised the `fragmentation` capability.
    FRAGMENT = "fragment"


def _validate_k8s_port(port: int | str) -> None:
    """Enforce a Service port: a number 1..65535 or an IANA port name.

    A string of digits is rejected: on the wire the number form is an int, so a
    numeric string means the producer was unsure which it held, and silently
    accepting it would paper over that.
    """
    if isinstance(port, bool):
        raise ValueError("port must be a port number or an IANA port name, got a bool")
    if isinstance(port, int):
        if not 1 <= port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {port}")
        return
    if isinstance(port, str):
        if port.isdigit():
            raise ValueError("a string port must be an IANA port name, not digits")
        if len(port) > 15:
            raise ValueError(f"port name must be at most 15 characters, got {port!r}")
        if not _PORT_NAME_RE.match(port):
            raise ValueError(f"port name must contain only [a-z0-9-], got {port!r}")
        if port.startswith("-") or port.endswith("-") or "--" in port:
            raise ValueError(f"port name must not lead, trail or double a hyphen, got {port!r}")
        if not any(c.isalpha() for c in port):
            raise ValueError(f"port name must contain at least one letter, got {port!r}")
        return
    raise ValueError(f"port must be a port number or an IANA port name, got {type(port).__name__}")


def _validate_secret_ref(ref: str) -> None:
    """Enforce the ``namespace/name#key`` form of a locally-resolved secret ref."""
    if not _SECRET_REF_RE.match(ref):
        raise ValueError(
            "upstream_basic_auth_secret must be 'namespace/name#key' with DNS-1123 "
            f"namespace and name, got {ref!r}"
        )


@dataclass(kw_only=True)
class K8sServiceTarget(WireModel):
    """A Kubernetes Service to reach, rather than a raw URL.

    The agent resolves this to the cluster's own in-cluster DNS, so a server can
    name what to expose without knowing the cluster's addresses. ``port`` is
    either the Service's port number or its port name; ``scheme`` says how to
    speak to it.

    The server sends ``target`` only to agents that advertised
    ``K8S_SERVICES_CAPABILITY`` (``k8s:services``). ``kind`` is a discriminator
    rather than a decoration: a peer that does not recognise a ``kind`` must
    refuse the target, so any new ``kind`` has to ship with its own ``k8s:*``
    capability rather than reusing ``k8s:services``.
    """

    kind: Literal["k8s_service"] = "k8s_service"
    namespace: str
    name: str
    port: int | str
    scheme: Literal["http", "https"] = "http"

    def __post_init__(self) -> None:
        if self.kind != "k8s_service":
            raise ValueError(f"kind must be 'k8s_service', got {self.kind!r}")
        if self.scheme not in ("http", "https"):
            raise ValueError(f"scheme must be 'http' or 'https', got {self.scheme!r}")
        if len(self.namespace) > 63 or not _DNS_1123_LABEL_RE.match(self.namespace):
            raise ValueError(
                "namespace must be a DNS-1123 label of at most 63 characters, "
                f"got {self.namespace!r}"
            )
        if len(self.name) > 63 or not _DNS_1035_LABEL_RE.match(self.name):
            raise ValueError(
                f"name must be a DNS-1035 label of at most 63 characters, got {self.name!r}"
            )
        _validate_k8s_port(self.port)


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
    # 1.4: the cluster target behind this endpoint, when the server knows it.
    # Null for everything a 1.3 server sends. ``service_url`` stays filled either
    # way, so a 1.3 agent that ignores this key still connects to the resolved
    # address.
    target: K8sServiceTarget | None = None


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
    # -- 1.4 ---------------------------------------------------------------
    # Names the group this process belongs to, so the server can admit a
    # rolling-update successor without a nonce exchange. A Helm release reuses
    # one group (typically ``<namespace>/<release>``) across its pods; the
    # server treats two live members of the same group as one identity taking
    # over rather than as DUPLICATE_INSTANCE. None means "no group".
    handover_group: str | None = None


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
    # -- 1.5 ---------------------------------------------------------------
    # Hello capabilities the server supports in return. Today only
    # `fragmentation` matters: the agent fragments its own large messages only
    # once the server lists it here.
    capabilities: list[str] = field(default_factory=list)


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
    # Stage B: set when the server will admit a successor for this update. The
    # incumbent spawns the canary with `--successor-of` + this nonce, the canary
    # echoes it in its hello, and the server closes the incumbent with close
    # code HANDOVER once the successor is serving. Absent means Stage A: swap,
    # exit, and let the service manager relaunch the new version.
    successor_nonce: str | None = None


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


# -- 1.4: cluster-owned declarations ---------------------------------------- #
# The operator knows what its CRs and Ingresses already say should be exposed.
# It declares that whole set here; the server reconciles it against the
# dashboard-owned endpoints and answers per label. Ownership is disjoint by
# origin: a declaration never overwrites a dashboard endpoint, it reports a
# conflict instead. Pruning is not per-endpoint: each frame is the FULL set, so
# a label present in one frame and absent from the next is gone.


@dataclass(kw_only=True, repr=False)
class DeclaredEndpoint(TunnelSpec):
    """One endpoint a cluster manifest says should exist.

    Unlike ``EndpointSpec`` (server -> agent, carrying an ``id`` the server
    assigns), these travel agent -> server. The auth and access-policy fields
    mirror ``EndpointSpec`` -- it is a ``TunnelSpec`` -- so a declaration can set
    everything a dashboard endpoint can.

    Either a ``target`` (a :class:`K8sServiceTarget` the agent resolves in
    cluster) or a non-empty ``service_url`` is required. ``service_url`` is not
    only a compatibility crutch: a provider that cannot name the cluster's DNS
    fills it instead of a target, and when both are present the target is
    authoritative and ``service_url`` is the address it resolves to.

    ``upstream_basic_auth`` must stay null: a declaration travels agent ->
    server, so a plaintext credential there would expose the value. The agent
    names a local Secret through ``upstream_basic_auth_secret`` and resolves it
    itself, and the server never sees the credential.
    """

    label: str = field()
    target: K8sServiceTarget | None = None
    # ``namespace/name#key`` of a Secret the agent resolves locally. Alternative
    # to ``upstream_basic_auth``, which must not travel in a declaration.
    upstream_basic_auth_secret: str | None = None
    # `strict`: the server rejects dashboard edits to this endpoint's access
    # rules -- the cluster owns them. `initial`: the cluster seeds them and the
    # dashboard may change them afterwards. Neither prunes on its own; a frame
    # is the whole set, so labels missing from the next frame are dropped.
    sync_policy: Literal["strict", "initial"] = "strict"
    # Where it came from, e.g. ``hletunnel:ns/name`` or ``ingress:ns/name``.
    source_ref: str = field()

    def __post_init__(self) -> None:
        if self.target is None and not self.service_url:
            raise ValueError("a declared endpoint needs a target or a non-empty service_url")
        if self.upstream_basic_auth is not None:
            raise ValueError(
                "upstream_basic_auth must not travel in a declaration; use "
                "upstream_basic_auth_secret so the agent resolves it locally"
            )
        if self.upstream_basic_auth_secret is not None:
            _validate_secret_ref(self.upstream_basic_auth_secret)
        if self.sync_policy not in ("strict", "initial"):
            raise ValueError(f"sync_policy must be 'strict' or 'initial', got {self.sync_policy!r}")
        if not _SOURCE_REF_RE.match(self.source_ref):
            raise ValueError(
                "source_ref must be 'hletunnel:<namespace>/<name>' or "
                f"'ingress:<namespace>/<name>', got {self.source_ref!r}"
            )


@dataclass(kw_only=True)
class DeclaredEndpoints(WireModel):
    """agent -> server: the full current set of cluster-owned endpoints.

    ``revision`` is monotonic (>= 0) for one agent. The server drops a frame
    older than the one it last applied, and echoes the revision on the matching
    ``declared_ack`` so an ack is bound to the frame it answers.
    """

    type: AgentMsgType = AgentMsgType.DECLARED_ENDPOINTS
    endpoints: list[DeclaredEndpoint] = field(default_factory=list)
    revision: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError(f"revision must be a non-negative integer, got {self.revision!r}")
        if len(self.endpoints) > MAX_DECLARED_ENDPOINTS:
            raise ValueError(
                f"declared_endpoints carries at most {MAX_DECLARED_ENDPOINTS} endpoints, "
                f"got {len(self.endpoints)}"
            )


@dataclass(kw_only=True)
class DeclaredAckEntry(WireModel):
    label: str
    # `accepted`: the declaration owns (or now owns) this label. `conflict`: a
    # dashboard-owned endpoint already holds it, so nothing was changed.
    status: Literal["accepted", "conflict"] = "accepted"
    message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("accepted", "conflict"):
            raise ValueError(f"status must be 'accepted' or 'conflict', got {self.status!r}")


@dataclass(kw_only=True)
class DeclaredAck(WireModel):
    """server -> agent: the outcome per declared label.

    ``revision`` echoes the ``declared_endpoints`` frame being acknowledged, so
    a late ack cannot be mistaken for one answering a newer frame.
    """

    type: AgentMsgType = AgentMsgType.DECLARED_ACK
    endpoints: list[DeclaredAckEntry] = field(default_factory=list)
    revision: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError(f"revision must be a non-negative integer, got {self.revision!r}")


# -- 1.4: log reads --------------------------------------------------------- #
# The dashboard asks the agent for the tail of its own log ring buffer -- the
# redacted agent/client log, not any one endpoint's output, so these frames
# carry no endpoint field. The agent answers on the same control connection.
# `lines` is bounded by the agent, which reports `truncated` when it had to drop
# older lines.


@dataclass(kw_only=True)
class LogsRequest(WireModel):
    type: AgentMsgType = AgentMsgType.LOGS_REQUEST
    request_id: str
    # Server's ask, clamped to 1..MAX_LOG_LINES. The agent caps its answer at
    # MAX_LOG_LINES too and sets `logs_response.truncated` when the real log is
    # longer than what was sent.
    lines: int = 200

    def __post_init__(self) -> None:
        if isinstance(self.lines, bool) or not isinstance(self.lines, int):
            raise ValueError(f"lines must be an integer, got {type(self.lines).__name__}")
        self.lines = max(1, min(self.lines, MAX_LOG_LINES))


@dataclass(kw_only=True)
class LogsResponse(WireModel):
    type: AgentMsgType = AgentMsgType.LOGS_RESPONSE
    request_id: str
    lines: list[str] = field(default_factory=list)
    truncated: bool = False

    def __post_init__(self) -> None:
        if len(self.lines) > MAX_LOG_LINES:
            raise ValueError(
                f"logs_response carries at most {MAX_LOG_LINES} lines, got {len(self.lines)}"
            )
        for line in self.lines:
            if len(line) > MAX_LOG_LINE_CHARS:
                raise ValueError(
                    f"a log line must be at most {MAX_LOG_LINE_CHARS} characters, got {len(line)}"
                )
