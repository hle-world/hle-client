"""One vocabulary for what the relay returns.

The relay's JSON uses ``is_active`` for a tunnel and ``online`` for an agent,
``allowed_email`` for a rule's address, ``has_pin`` for whether a PIN is set.
Every consumer used to read those keys straight off the dict, and every
consumer had a chance to spell one wrong: ``hle status`` once read
``is_online`` and reported every agent offline. Now the raw dict is read in
exactly one place per model — its ``from_api`` — and everyone else reads
attributes, which a typo turns into an ``AttributeError`` at the first test
rather than a silent ``None`` in production.

Each ``from_api`` tolerates missing keys: an older relay, or a newer one that
dropped a field, degrades to a default, not a ``KeyError``. The raw payload
rides along as ``raw`` so ``-o json`` can emit exactly what the relay said.

These live in ``hle_client`` rather than ``hle_common`` for now. The server's
Pydantic models are the other half of this contract; folding the two together
is a follow-up that has to land on both sides at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_ZONE = "hle.world"


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def public_url_for(subdomain: str, zone: str | None) -> str:
    """The URL a tunnel answers at, when the relay did not say.

    The list endpoint does not carry ``public_url``; the status endpoint does.
    Deriving it here is what stops the dashboard hard-coding ``hle.world``.
    """
    domain = zone or DEFAULT_ZONE
    scheme = "http" if domain.startswith("localhost") else "https"
    return f"{scheme}://{subdomain}.{domain}"


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tunnel:
    """One tunnel record, live or not.

    ``online`` is the relay's ``is_active``: whether a client is connected
    right now. It is the same word the :class:`Agent` uses, on purpose.
    """

    subdomain: str
    label: str | None = None
    zone: str | None = None
    public_url: str = ""
    service_url: str = ""
    auth_mode: str = ""
    online: bool = False
    websocket_enabled: bool = True
    connected_at: str | None = None
    tunnel_id: str | None = None
    client_version: str | None = None
    managed_by: str | None = None
    webhook_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any], *, user_code: str | None = None) -> Tunnel:
        subdomain = _str(data.get("subdomain"))
        zone = _opt_str(data.get("zone") or data.get("zone_domain"))
        label = _opt_str(data.get("label") or data.get("service_label"))
        if label is None and user_code and subdomain.endswith(f"-{user_code}"):
            label = subdomain[: -len(user_code) - 1] or None
        return cls(
            subdomain=subdomain,
            label=label,
            zone=zone,
            public_url=_str(data.get("public_url")) or public_url_for(subdomain, zone),
            service_url=_str(data.get("service_url")),
            auth_mode=_str(data.get("auth_mode")),
            online=bool(data.get("is_active", data.get("online", False))),
            websocket_enabled=bool(data.get("websocket_enabled", True)),
            connected_at=_opt_str(data.get("connected_at")),
            tunnel_id=_opt_str(data.get("tunnel_id") or data.get("id")),
            client_version=_opt_str(data.get("client_version")),
            managed_by=_opt_str(data.get("managed_by")),
            webhook_path=_opt_str(data.get("webhook_path")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class AccessRule:
    """An SSO allow-list entry: who may sign in, through which provider."""

    email: str
    provider: str = "any"
    id: int | None = None
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AccessRule:
        return cls(
            email=_str(data.get("allowed_email", data.get("email"))),
            provider=_str(data.get("provider"), "any") or "any",
            id=_opt_int(data.get("id")),
            created_at=_opt_str(data.get("created_at")),
            raw=dict(data),
        )

    @property
    def key(self) -> tuple[str, str]:
        """What makes two rules the same rule: address (case-folded) + provider."""
        return (self.email.lower(), self.provider)


@dataclass(frozen=True, slots=True)
class PinStatus:
    enabled: bool = False
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PinStatus:
        return cls(
            enabled=bool(data.get("has_pin", data.get("enabled", False))),
            updated_at=_opt_str(data.get("updated_at")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class BasicAuthStatus:
    enabled: bool = False
    username: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> BasicAuthStatus:
        return cls(
            enabled=bool(data.get("enabled", False)),
            username=_opt_str(data.get("username")),
            updated_at=_opt_str(data.get("updated_at")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class ShareLink:
    """A temporary link past the gate. ``share_url`` is only known at creation."""

    id: int | None = None
    label: str = ""
    token_prefix: str = ""
    expires_at: str | None = None
    max_uses: int | None = None
    use_count: int = 0
    active: bool = True
    share_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ShareLink:
        # ``create`` answers ``{"share_url": ..., "link": {...}}``; ``list``
        # answers the inner shape directly. Accept both.
        inner = data.get("link")
        link: dict[str, Any] = inner if isinstance(inner, dict) else data
        return cls(
            id=_opt_int(link.get("id")),
            label=_str(link.get("label")),
            token_prefix=_str(link.get("token_prefix")),
            expires_at=_opt_str(link.get("expires_at")),
            max_uses=_opt_int(link.get("max_uses")),
            use_count=_int(link.get("use_count")),
            active=bool(link.get("is_active", link.get("active", True))),
            share_url=_opt_str(data.get("share_url")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class TunnelDetail:
    """Everything ``GET /tunnels/{subdomain}/status`` knows: the record plus its gates."""

    tunnel: Tunnel
    access_rules: tuple[AccessRule, ...] = ()
    pin: PinStatus = field(default_factory=PinStatus)
    basic_auth: BasicAuthStatus = field(default_factory=BasicAuthStatus)
    protected: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TunnelDetail:
        rules = data.get("access_rules") or []
        pin: dict[str, Any] = data["pin"] if isinstance(data.get("pin"), dict) else {}
        basic: dict[str, Any] = (
            data["basic_auth"] if isinstance(data.get("basic_auth"), dict) else {}
        )
        return cls(
            tunnel=Tunnel.from_api(data),
            access_rules=tuple(AccessRule.from_api(r) for r in rules if isinstance(r, dict)),
            pin=PinStatus.from_api(pin),
            basic_auth=BasicAuthStatus.from_api(basic),
            protected=bool(data.get("is_protected", False)),
            raw=dict(data),
        )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Agent:
    """One enrolled agent and whether its control channel is up.

    ``online`` is the relay's ``online``. ``enabled`` is its ``is_active``,
    which is a different fact: a disabled agent is not offline, it is off.
    """

    name: str
    id: str | None = None
    online: bool = False
    enabled: bool = True
    last_seen: str | None = None
    version: str | None = None
    endpoints: int = 0
    hostname: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Agent:
        return cls(
            name=_str(data.get("name"), "?") or "?",
            id=_opt_str(data.get("public_id") or data.get("id")),
            online=bool(data.get("online", False)),
            enabled=bool(data.get("is_active", True)),
            last_seen=_opt_str(data.get("last_seen_at") or data.get("last_seen")),
            version=_opt_str(data.get("agent_version") or data.get("version")),
            endpoints=_int(data.get("endpoint_count", data.get("endpoints"))),
            hostname=_opt_str(data.get("hostname")),
            raw=dict(data),
        )

    @property
    def state(self) -> str:
        """``online`` / ``offline`` / ``disabled`` — the word every screen shows."""
        if not self.enabled:
            return "disabled"
        return "online" if self.online else "offline"


# ---------------------------------------------------------------------------
# Daemons (services on this machine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Daemon:
    """A service unit installed here, in whichever manager this OS runs.

    ``kind`` is read from the spec the installer stamped into the file:
    ``tunnel``, ``agent``, ``forward``, or ``unknown`` for a unit that
    predates stamping. ``state`` is the manager's word for it when one was
    asked for (``active``, ``inactive``, ``loaded``, ...) or ``None``.
    """

    name: str
    scope: str = "system"
    kind: str = "unknown"
    state: str | None = None
    label: str | None = None
    spec: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def user_mode(self) -> bool:
        return self.scope == "user"

    @classmethod
    def from_spec(
        cls,
        name: str,
        *,
        user_mode: bool,
        spec: dict[str, Any] | None,
        state: str | None = None,
    ) -> Daemon:
        return cls(
            name=name,
            scope="user" if user_mode else "system",
            kind=kind_from_spec(spec),
            state=state,
            label=_opt_str(spec.get("label")) if spec else None,
            spec=spec,
        )


def kind_from_spec(spec: dict[str, Any] | None) -> str:
    """Which of the three things a service runs, from its recorded argv."""
    if not spec:
        return "unknown"
    args = [str(a) for a in spec.get("run_args") or []]
    if not args:
        return "unknown"
    head = args[0]
    if head == "agent":
        return "agent"
    if head in ("fp", "forward"):
        return "forward"
    if head in ("expose", "tunnel", "webhook"):
        return "tunnel"
    return "unknown"


# ---------------------------------------------------------------------------
# Results that are decisions, not errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conflict:
    """The operation was not performed because the state does not allow it yet.

    Returned rather than raised, so the caller can ask the person and retry
    with ``force=True`` — or turn it into a :class:`ConflictError` when there
    is nobody to ask.
    """

    subject: str
    reason: str
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class Deleted:
    subject: str


@dataclass(frozen=True, slots=True)
class AccessDiff:
    """What ``set_access`` did, or would do: rules added, removed, kept, and
    any that the relay refused (with its status code)."""

    added: tuple[AccessRule, ...] = ()
    removed: tuple[AccessRule, ...] = ()
    kept: tuple[AccessRule, ...] = ()
    failed: tuple[tuple[AccessRule, str, int | None], ...] = ()

    @property
    def in_sync(self) -> bool:
        return not self.added and not self.removed and not self.failed
