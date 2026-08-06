"""Tunnel preflight — shared models for "will this tunnel actually work?".

A tunnel can be connected, authenticated, DNS-correct and still useless. The case
that prompted this: a pfSense box exposed as ``http://192.168.1.1`` reported
healthy everywhere — agent online, upstream answering in 29ms, SSO gate working —
while every page load ended at ``https://192.168.1.1/``, because the device
answered 302 with its own address and nothing rewrites that. Every signal the
product had said fine.

Preflight asks the questions a person would have to know to ask. It runs on the
**agent**, because only the agent can reach ``192.168.1.1``, and reports findings
back over the control channel so the dashboard can show them *before* the tunnel
is created.

Findings never change anything. A finding may carry a ``fix``, which names a
field and a value the UI can offer to apply; applying it is always the user's
choice. A diagnostic that silently rewrites what you typed is a diagnostic you
stop trusting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from hle_common.wire import WireModel

PREFLIGHT_PROTOCOL_VERSION = "1.0"


class PreflightMsgType(StrEnum):
    REQUEST = "preflight_request"  # server -> agent: probe this URL
    REPORT = "preflight_report"  # agent -> server: what we found


class Severity(StrEnum):
    """How much a finding matters.

    ``ERROR`` means the tunnel will not work as configured. ``WARNING`` means it
    will serve pages but something will break in use — a login, an asset, a
    console. ``INFO`` is worth knowing and is not a fault.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(kw_only=True)
class PreflightFix(WireModel):
    """A change that would address a finding, for the UI to *offer*.

    ``field`` names a tunnel setting (``service_url``, ``forward_host``,
    ``verify_ssl``, ``websocket_enabled``) and ``value`` is its new value as a
    string, so this stays a flat, versionable payload.
    """

    field: str
    value: str
    label: str = ""  # human-readable button text, e.g. "Use https://192.168.1.1"


@dataclass(kw_only=True)
class PreflightFinding(WireModel):
    """One thing that will, or might, stop the tunnel working."""

    # Stable identifier, so the UI can special-case or suppress one check
    # without matching on prose: "upstream_redirects_to_private_address".
    id: str
    severity: Severity = Severity.WARNING
    title: str
    detail: str = ""
    # Short, quotable proof — a status line, a header, a snippet. Never a whole
    # body: this crosses the wire and ends up in a browser.
    evidence: str = ""
    fix: PreflightFix | None = None


@dataclass(kw_only=True)
class PreflightRequest(WireModel):
    """Server -> agent: probe ``service_url`` as if it were this tunnel.

    ``tunnel_host`` is the hostname the upstream would eventually see, which is
    what makes the Host-header checks meaningful. It is knowable before the
    tunnel exists because a label's subdomain is a permanent mapping per
    (user, label, zone).
    """

    type: PreflightMsgType = PreflightMsgType.REQUEST
    # Correlates the report. The agent echoes it back unchanged.
    request_id: str
    service_url: str
    tunnel_host: str | None = None
    verify_ssl: bool = False
    websocket_enabled: bool = True
    forward_host: bool = False


@dataclass(kw_only=True)
class PreflightReport(WireModel):
    """Agent -> server: the findings, or why there are none."""

    type: PreflightMsgType = PreflightMsgType.REPORT
    request_id: str
    service_url: str = ""
    findings: list[PreflightFinding] = field(default_factory=list)
    # Set when the run itself failed, as opposed to finding nothing. "No
    # findings" and "could not check" must not look identical.
    error: str | None = None
    elapsed_ms: float = 0.0
    # Which host mode actually served a usable response, when either did:
    # "upstream" (Host stripped, the default) or "tunnel" (Host forwarded).
    # None means neither worked.
    working_host_mode: str | None = None
