"""One model for everything a tunnel can be asked to do.

Before this, the tunnel option set was written out by hand in four click
decorators (``expose``, ``tunnel create`` and both ``daemon install``
variants), and the "options -> TunnelConfig" mapping existed once per entry
point. Each copy drifted on its own: ``--upstream-basic-auth`` worked
interactively and could not be daemonised, and a dashboard endpoint could set
five of the thirteen things a CLI tunnel could.

``TunnelSpec`` is that option set, once. The CLI builds its flags from the
field metadata here (``hle_client.tunnel_options``), the daemon installer
serialises it into the unit file, and the agent protocol's ``EndpointSpec`` is
this plus an endpoint id, so the dashboard can set everything the CLI can.

Wire rules. This model travels inside ``EndpointSpec`` to agents that predate
it, so:

* The first six fields keep the names and defaults ``EndpointSpec`` has always
  had.
* Every field added after them defaults to ``None``, meaning "the tunnel
  default". A newer server that sends nothing new produces bytes an older
  agent already understands, plus null keys it ignores.

The CLI metadata lives in ``field(metadata={"cli": ...})`` as plain data, so
this module stays importable by the server without click.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from hle_common.wire import WireModel

# Upper bound the relay applies to a client-requested response timeout. The
# relay caps it anyway; the CLI rejects values past it so the user hears about
# it up front instead of getting a quieter timeout than they asked for.
MAX_RESPONSE_TIMEOUT = 1200

AUTH_MODES: tuple[str, ...] = ("sso", "none")

# For reconcile_key: the value a null means, where the client knows it.
_NULL_EQUALS: dict[str, Any] = {
    "verify_ssl": False,
    "forward_host": False,
    "apex": False,
    "options": {},
}

# Fields whose values must never be printed: repr masks them.
SECRET_FIELDS: frozenset[str] = frozenset({"upstream_basic_auth"})


def _cli(**kw: Any) -> dict[str, Any]:
    """Metadata describing the click option generated for a field."""
    return {"cli": kw}


@dataclass(kw_only=True, repr=False)
class TunnelSpec(WireModel):
    """The full, declarative description of one tunnel."""

    # -- the EndpointSpec 1.2 fields: names and defaults are frozen ----------
    # No flag: every command spells these differently (`--label`, a positional
    # LABEL, `--service`, a positional URL), so each command keeps its own.
    label: str | None = None
    service_url: str = ""
    zone: str | None = field(
        default=None,
        metadata=_cli(
            opts=("--zone",),
            dest="zone",
            help="Custom zone to publish under (e.g. t00t.us). Required for --apex.",
        ),
    )
    auth_mode: str = field(
        default="sso",
        metadata=_cli(
            opts=("--auth",),
            dest="auth",
            default="sso",
            choices=AUTH_MODES,
            help="Auth mode",
        ),
    )
    # Webhook mode is its own command (`hle tunnel webhook --path`), which also
    # forces auth off and WebSockets off; there is no flag for it here.
    webhook_path: str | None = None
    websocket_enabled: bool = field(
        default=True,
        metadata=_cli(
            opts=("--websocket/--no-websocket",),
            dest="websocket",
            default=True,
            help="Enable WebSocket proxying",
        ),
    )

    # -- 1.3: the rest of the CLI option set. None = the tunnel default. -----
    verify_ssl: bool | None = field(
        default=None,
        metadata=_cli(
            opts=("--verify-ssl",),
            dest="verify_ssl",
            is_flag=True,
            default=False,
            help="Enable SSL certificate verification (by default self-signed certs are accepted)",
        ),
    )
    forward_host: bool | None = field(
        default=None,
        metadata=_cli(
            opts=("--forward-host",),
            dest="forward_host",
            is_flag=True,
            default=False,
            help="Forward the browser's Host header to the local service "
            "(for services that validate Host).",
        ),
    )
    # "user:pass". A secret: masked in repr, and never written into argv by
    # the daemon installer (it goes into the service's environment instead).
    upstream_basic_auth: str | None = field(
        default=None,
        metadata=_cli(
            opts=("--upstream-basic-auth",),
            dest="upstream_basic_auth",
            metavar="USER:PASS",
            envvar="HLE_UPSTREAM_BASIC_AUTH",
            help="Inject Basic Auth into every request to the local service. Format: USER:PASS",
        ),
    )
    apex: bool | None = field(
        default=None,
        metadata=_cli(
            opts=("--apex",),
            dest="apex",
            is_flag=True,
            default=False,
            help="Serve at the bare zone root (e.g. https://t00t.us) instead of a subdomain.",
        ),
    )
    options: dict[str, str] | None = field(
        default=None,
        metadata=_cli(
            opts=("--option",),
            dest="options",
            multiple=True,
            metavar="KEY=VALUE",
            help="Generic server-interpreted parameter, passed through verbatim. "
            "Repeatable. The server defines which keys are valid. Example: --option zone=t00t.us",
        ),
    )
    # Seconds the relay waits for the local service to answer one request.
    # None = the relay's default (30s, or 120s for a webhook); capped at
    # MAX_RESPONSE_TIMEOUT.
    response_timeout: int | None = field(
        default=None,
        metadata=_cli(
            opts=("--response-timeout",),
            dest="response_timeout",
            int_range=(1, MAX_RESPONSE_TIMEOUT),
            metavar="SECONDS",
            help="Seconds the relay waits for the local service to respond "
            f"(default 30, webhooks 120, max {MAX_RESPONSE_TIMEOUT}).",
        ),
    )
    # Which system manages this tunnel (e.g. "hle-operator", "hle-agent").
    # Set by the orchestrator, not by a person, so it has no flag.
    managed_by: str | None = None

    def __repr__(self) -> str:
        parts = []
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            shown = "'***'" if f.name in SECRET_FIELDS and value is not None else repr(value)
            parts.append(f"{f.name}={shown}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def tunnel_fields(self) -> dict[str, Any]:
        """Just the TunnelSpec fields, without anything a subclass adds."""
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(TunnelSpec)}

    def reconcile_key(self) -> tuple[Any, ...]:
        """Everything that, when changed, means the running tunnel must restart.

        That is every field: there is no option a running tunnel picks up live.
        ``options`` is a dict, so it is frozen into sorted pairs to keep the
        key hashable and order-independent.

        Null and the explicit default compare equal (``verify_ssl=None`` is
        ``verify_ssl=False``), so a server that starts sending explicit values
        where it used to send nulls does not restart every endpoint once.
        ``response_timeout`` has no client-side default (the relay owns it), so
        null stays distinct there.
        """
        key: list[Any] = []
        for name, value in self.tunnel_fields().items():
            if name == "label":
                continue  # the label is the identity, not a property of it
            if value is None and name in _NULL_EQUALS:
                value = _NULL_EQUALS[name]
            if isinstance(value, dict):
                value = tuple(sorted(value.items()))
            key.append(value)
        return tuple(key)


def cli_fields() -> list[tuple[str, dict[str, Any]]]:
    """``(field name, cli metadata)`` for every field that has a flag, in order."""
    return [
        (f.name, dict(f.metadata["cli"]))
        for f in dataclasses.fields(TunnelSpec)
        if "cli" in f.metadata
    ]


def parse_basic_auth(value: str | None) -> tuple[str, str] | None:
    """``"user:pass"`` -> ``("user", "pass")``. Raises ValueError without a colon."""
    if not value:
        return None
    if ":" not in value:
        raise ValueError("upstream_basic_auth must be in USER:PASS format")
    user, _, password = value.partition(":")
    return user, password
