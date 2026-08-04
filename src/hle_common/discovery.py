"""Service discovery — shared models for "what can this agent see?".

An agent that lives inside a Kubernetes cluster or on a Docker host already
knows what is running there. Discovery reports that inventory to the server so
you can browse it in the dashboard and expose something with a click, instead of
hand-copying URLs into endpoint forms.

Discovery is **provider-based**. The protocol, storage, API, and UI are shared;
each environment contributes a provider that knows how to enumerate it. An agent
on a plain VM runs no providers, reports nothing, and the UI hides the section.

Reported inventory is *cache-like*, not configuration: the agent sends the full
current set, the server replaces what it had, and nothing survives the agent
being deleted. Turning a discovered service into a tunnel creates an ordinary
endpoint, so everything downstream is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from hle_common.wire import WireModel

DISCOVERY_PROTOCOL_VERSION = "1.0"


class DiscoveryMsgType(StrEnum):
    REPORT = "discovery_report"  # agent -> server: full current inventory
    REFRESH = "discovery_refresh"  # server -> agent: re-scan now


@dataclass(kw_only=True)
class DiscoveredService(WireModel):
    """One service an agent can see and could expose.

    ``address`` is the URL the *agent* would use to reach it — not a public URL.
    It goes straight into an endpoint's ``service_url`` if the user chooses to
    expose it.
    """

    provider: str  # "k8s" | "docker" | ...
    # Stable within a provider, so re-reports update rather than duplicate.
    id: str
    name: str
    address: str
    ports: list[int] = field(default_factory=list)
    namespace: str | None = None  # k8s namespace, compose project, ...
    labels: dict[str, str] = field(default_factory=dict)
    # Set when the agent can tell this is already exposed, so the UI can show
    # "already exposed" instead of offering a duplicate.
    already_exposed: bool = False

    def suggested_label(self) -> str:
        """A tunnel label guess: DNS-safe, derived from the service name."""
        out = []
        for ch in self.name.lower():
            if ch.isalnum():
                out.append(ch)
            elif ch in "-_. " and out and out[-1] != "-":
                out.append("-")
        return "".join(out).strip("-")[:63] or "service"


@dataclass(kw_only=True)
class DiscoveryReport(WireModel):
    type: DiscoveryMsgType = DiscoveryMsgType.REPORT
    services: list[DiscoveredService] = field(default_factory=list)
    # Providers that actually ran, so the server can distinguish "nothing found"
    # from "discovery isn't enabled here" — they look identical otherwise.
    providers: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(kw_only=True)
class DiscoveryRefresh(WireModel):
    type: DiscoveryMsgType = DiscoveryMsgType.REFRESH
