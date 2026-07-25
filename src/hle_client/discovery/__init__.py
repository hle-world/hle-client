"""Service discovery providers — enumerate what an agent can see and expose.

Not to be confused with *relay* discovery (``ApiClient.discover_relay``), which
picks which relay a tunnel connects to. This is about inventorying the services
running near an agent.

A provider answers two questions: "am I applicable here?" and "what is running?"
Everything else — the wire format, server storage, dashboard UI — is shared, so
supporting a new environment means implementing one small interface.

Providers are **opt-in and self-detecting**. `available()` must be cheap and
must never raise: an agent on a plain VM asks every provider, gets "no" from all
of them, and reports nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

# Runtime import, not just typing: it appears in Protocol return annotations
# that `runtime_checkable` evaluates.
from hle_common.discovery import DiscoveredService  # noqa: TC001

logger = logging.getLogger(__name__)

# A scan shouldn't hold up the agent's control loop if a socket hangs.
SCAN_TIMEOUT = 15.0


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Enumerates services in one kind of environment."""

    name: str

    def available(self) -> bool:
        """Cheap, non-raising check for whether this provider applies here."""
        ...

    async def scan(self) -> list[DiscoveredService]:
        """Return everything currently visible. May raise; callers isolate it."""
        ...


def default_providers() -> list[DiscoveryProvider]:
    """All known providers, in preference order. Import errors are non-fatal."""
    providers: list[DiscoveryProvider] = []
    try:
        from hle_client.discovery.kubernetes import KubernetesProvider

        providers.append(KubernetesProvider())
    except Exception as exc:  # noqa: BLE001 — a broken provider must not stop the agent
        logger.debug("Kubernetes provider unavailable: %s", exc)
    try:
        from hle_client.discovery.docker import DockerProvider

        providers.append(DockerProvider())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Docker provider unavailable: %s", exc)
    return providers


def active_providers(
    providers: list[DiscoveryProvider] | None = None,
) -> list[DiscoveryProvider]:
    """Providers that apply on this machine."""
    candidates = default_providers() if providers is None else providers
    active = []
    for p in candidates:
        try:
            if p.available():
                active.append(p)
        except Exception as exc:  # noqa: BLE001 — availability checks never fail loudly
            logger.debug("Provider %s availability check failed: %s", p.name, exc)
    return active


async def scan_all(
    providers: list[DiscoveryProvider] | None = None,
) -> tuple[list[DiscoveredService], list[str], str | None]:
    """Scan every applicable provider.

    Returns ``(services, provider_names, error)``. One provider failing doesn't
    discard the others' results — a broken Docker socket shouldn't hide the
    Kubernetes services an agent can still see.
    """
    active = active_providers(providers)
    services: list[DiscoveredService] = []
    names: list[str] = []
    errors: list[str] = []

    for provider in active:
        names.append(provider.name)
        try:
            found = await asyncio.wait_for(provider.scan(), timeout=SCAN_TIMEOUT)
            services.extend(found)
            logger.info("Discovery: %s found %d service(s)", provider.name, len(found))
        except TimeoutError:
            errors.append(f"{provider.name}: timed out")
            logger.warning("Discovery: %s timed out", provider.name)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the agent
            errors.append(f"{provider.name}: {exc}")
            logger.warning("Discovery: %s failed: %s", provider.name, exc)

    return services, names, "; ".join(errors) if errors else None
