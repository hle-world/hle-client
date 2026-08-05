"""Docker discovery — enumerate containers with reachable ports.

Talks to the Docker Engine API over its Unix socket using httpx (already a
dependency), rather than pulling in the Docker SDK for two read-only calls.

Addressing is the hard part, and it is not a property of the container: it
depends on where *this agent* runs. A container name only resolves through
Docker's embedded DNS at ``127.0.0.11``, which exists solely inside a container
attached to the same user-defined network. An agent installed on the host — pipx,
a venv, pfSense — has no such resolver, so ``http://jellyfin:8096`` is NXDOMAIN
and every discovered service fails to expose.

So rather than guess one address, each container produces a ladder of candidates
and we report the first that actually answers a TCP connect. Where the agent
lives decides the *order* only; the probe decides the answer. That way being
wrong about the environment costs a few hundred milliseconds instead of handing
the dashboard a URL that cannot work.

Security note: the Docker socket is root-equivalent on the host — anything that
can *write* to it can start a privileged container. This provider only ever
reads (``GET /containers/json``), and the socket should be mounted read-only,
but mounting it at all is a real trust decision. Discovery stays opt-in.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hle_common.discovery import DiscoveredService

logger = logging.getLogger(__name__)

DEFAULT_SOCKET = "/var/run/docker.sock"
# Pinned rather than "latest": the Engine API is versioned and an unpinned call
# fails outright against an older daemon.
API_VERSION = "v1.41"

# Containers we should never offer to expose. HLE's own containers would be a
# confusing loop, and infra sidecars are noise.
_SKIP_IMAGE_PREFIXES = ("ghcr.io/hle-world/", "hle-server", "hle-docker")

# One candidate's connect attempt. Short: these are LAN or loopback addresses,
# and the common failure (NXDOMAIN, connection refused) returns immediately —
# the timeout only bounds the case where a firewall blackholes the packet.
PROBE_TIMEOUT = 0.4
# Probing is per-candidate but containers go in parallel, so this caps the file
# descriptors and DNS lookups in flight on a host running a hundred containers.
PROBE_CONCURRENCY = 20
# The whole probe phase, well inside the caller's 15s SCAN_TIMEOUT. Blowing that
# budget would discard the entire scan; overrunning this one only falls back to
# reporting unprobed best guesses.
PROBE_BUDGET = 10.0

# Bind addresses that mean "every interface" rather than somewhere to connect to.
_WILDCARD_BINDS = {"", "0.0.0.0", "::", "[::]"}

ProbeFn = Callable[[str, int], Awaitable[bool]]


@dataclass(frozen=True)
class _Candidate:
    """One way the agent might reach a container, and where it came from."""

    host: str
    port: int
    source: str

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}"


class DockerProvider:
    """Lists running containers that publish or expose a usable port."""

    name = "docker"

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        probe: ProbeFn | None = None,
        in_container: bool | None = None,
    ) -> None:
        self._socket = socket_path or os.environ.get("DOCKER_SOCKET", DEFAULT_SOCKET)
        self._probe = probe or _probe_tcp
        self._in_container = running_in_container() if in_container is None else in_container

    def available(self) -> bool:
        try:
            path = Path(self._socket)
            return path.exists() and path.is_socket()
        except OSError:
            return False

    async def scan(self) -> list[DiscoveredService]:
        transport = httpx.AsyncHTTPTransport(uds=self._socket)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://docker", timeout=10.0
        ) as client:
            resp = await client.get(f"/{API_VERSION}/containers/json")
            resp.raise_for_status()
            containers = resp.json()

        return await self.services_from(containers)

    async def services_from(self, containers: list[dict[str, Any]]) -> list[DiscoveredService]:
        """Turn raw ``/containers/json`` output into addressable services.

        Split from the socket call so the address choice — the part that was
        wrong — is testable against real container payloads without a daemon.
        """
        # (container, candidates) for everything worth offering, before probing.
        pending: list[tuple[dict[str, Any], list[_Candidate]]] = []
        for c in containers:
            if not self._wanted(c):
                continue
            candidates = _candidates(c, in_container=self._in_container)
            if candidates:
                pending.append((c, candidates))

        chosen = await self._choose_addresses([cands for _, cands in pending])
        return [
            _to_service(container, candidate, reachable=reachable)
            for (container, _), (candidate, reachable) in zip(pending, chosen, strict=True)
        ]

    def _wanted(self, container: dict[str, Any]) -> bool:
        image = str(container.get("Image", ""))
        if any(image.startswith(p) for p in _SKIP_IMAGE_PREFIXES):
            return False
        return bool(_container_name(container))

    async def _choose_addresses(
        self, candidate_lists: list[list[_Candidate]]
    ) -> list[tuple[_Candidate, bool]]:
        """Pick a reachable candidate per container, falling back to the first.

        The fallback matters: a container we can't reach is still reported, with
        the guess we'd have made anyway and a label saying it didn't answer.
        Dropping it instead would turn "these are unreachable from here" into
        "discovery found nothing", which is the harder problem to debug.
        """
        if not candidate_lists:
            return []

        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def first_reachable(candidates: list[_Candidate]) -> tuple[_Candidate, bool]:
            for candidate in candidates:
                async with sem:
                    if await self._probe(candidate.host, candidate.port):
                        return candidate, True
            return candidates[0], False

        try:
            return await asyncio.wait_for(
                asyncio.gather(*(first_reachable(c) for c in candidate_lists)),
                timeout=PROBE_BUDGET,
            )
        except TimeoutError:
            logger.warning(
                "Discovery: probing container addresses exceeded %.0fs; "
                "reporting unverified addresses",
                PROBE_BUDGET,
            )
            return [(c[0], False) for c in candidate_lists]


def running_in_container() -> bool:
    """Whether this agent is itself containerised.

    Only used to order the candidate ladder, so a wrong answer costs latency
    rather than correctness. ``HLE_IN_DOCKER`` lets the hle-docker image state it
    outright instead of relying on inference.
    """
    env = os.environ.get("HLE_IN_DOCKER", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    try:
        if Path("/.dockerenv").exists():
            return True
        # Present for containerd/podman and for Docker under cgroup v1.
        return "docker" in Path("/proc/self/cgroup").read_text() if os.name == "posix" else False
    except OSError:
        return False


def _candidates(container: dict[str, Any], *, in_container: bool) -> list[_Candidate]:
    """Every way the agent might reach this container, best guess first.

    Three rungs, all derived from what ``/containers/json`` already returned:

    * **published** — the host port Docker mapped. Reachable from the host, and
      authoritative rather than a guess: the daemon told us this port forwards to
      this container.
    * **container_ip** — the container's own address on its bridge network,
      routable from the host on Linux and from containers on the same bridge.
      The only rung that covers a container publishing nothing.
    * **container_dns** — the network alias or container name. Needs Docker's
      embedded DNS, so it works only from inside a container on the same
      user-defined network (not the default bridge, which has no name
      resolution at all).
    """
    private_ports = _usable_ports(container)
    ladder = (
        [_dns_candidates, _ip_candidates, _published_candidates]
        if in_container
        else [_published_candidates, _ip_candidates, _dns_candidates]
    )

    out: list[_Candidate] = []
    seen: set[tuple[str, int]] = set()
    for rung in ladder:
        for candidate in rung(container, private_ports):
            key = (candidate.host, candidate.port)
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def _published_candidates(container: dict[str, Any], _private: list[int]) -> list[_Candidate]:
    out: list[_Candidate] = []
    for p in container.get("Ports") or []:
        if p.get("Type") != "tcp":
            continue
        public = p.get("PublicPort")
        if not isinstance(public, int):
            continue
        bind = str(p.get("IP") or "")
        # A wildcard bind is reachable on loopback; a specific one is not.
        host = "127.0.0.1" if bind in _WILDCARD_BINDS else bind
        out.append(_Candidate(host=host, port=public, source="published_port"))
    return out


def _ip_candidates(container: dict[str, Any], private_ports: list[int]) -> list[_Candidate]:
    settings = container.get("NetworkSettings") or {}
    addresses: list[str] = []
    for net in (settings.get("Networks") or {}).values():
        ip = str((net or {}).get("IPAddress") or "")
        if ip:
            addresses.append(ip)
    # Top-level IPAddress is the legacy default-bridge field, absent from
    # Networks on some daemon versions.
    legacy = str(settings.get("IPAddress") or "")
    if legacy and legacy not in addresses:
        addresses.append(legacy)

    return [
        _Candidate(host=ip, port=port, source="container_ip")
        for ip in addresses
        for port in private_ports
    ]


def _dns_candidates(container: dict[str, Any], private_ports: list[int]) -> list[_Candidate]:
    names = _dns_names(container)
    return [
        _Candidate(host=name, port=port, source="container_dns")
        for name in names
        for port in private_ports
    ]


def _to_service(
    container: dict[str, Any], candidate: _Candidate, *, reachable: bool
) -> DiscoveredService:
    name = _container_name(container) or ""
    ports = _usable_ports(container)
    labels = {
        k: v
        for k, v in (container.get("Labels") or {}).items()
        # Compose metadata is useful context; the rest is mostly build noise.
        if k.startswith("com.docker.compose.") or k.startswith("hle.")
    }
    # Recorded in labels rather than new wire fields: `labels` is already a
    # free-form dict that round-trips, so the dashboard can explain an
    # unreachable service without a coordinated server change.
    labels["hle.discovery.address_source"] = candidate.source
    labels["hle.discovery.reachable"] = "true" if reachable else "false"

    return DiscoveredService(
        provider=DockerProvider.name,
        id=f"{container.get('Id', name)[:12]}:{candidate.port}",
        name=name,
        address=candidate.address,
        ports=ports,
        namespace=labels.get("com.docker.compose.project"),
        labels=labels,
    )


def _container_name(container: dict[str, Any]) -> str | None:
    names = container.get("Names") or []
    if not names:
        return None
    # Docker returns names with a leading slash: "/jellyfin".
    return str(names[0]).lstrip("/") or None


def _dns_names(container: dict[str, Any]) -> list[str]:
    """Names other containers on the same user-defined network can resolve.

    Every network's aliases, not just the first one found: dict ordering here is
    the daemon's, so picking one alias could pick a network this agent isn't
    attached to while the reachable one sat later in the list.
    """
    out: list[str] = []
    networks = ((container.get("NetworkSettings") or {}).get("Networks") or {}).values()
    for net in networks:
        for alias in (net or {}).get("Aliases") or []:
            if alias and str(alias) not in out:
                out.append(str(alias))
    name = _container_name(container)
    if name and name not in out:
        out.append(name)
    return out


def _usable_ports(container: dict[str, Any]) -> list[int]:
    """Container-side TCP ports, published ones first.

    Published ports are listed first because they're the ones the operator
    deliberately made reachable, which makes them the better default.
    """
    published: list[int] = []
    internal: list[int] = []
    for p in container.get("Ports") or []:
        if p.get("Type") != "tcp":
            continue  # firepuncher aside, tunnels are HTTP/TCP over WS
        private = p.get("PrivatePort")
        if not isinstance(private, int):
            continue
        if p.get("PublicPort"):
            published.append(private)
        else:
            internal.append(private)

    ordered = published + [p for p in internal if p not in published]
    # Preserve order while removing duplicates (a port can appear per-protocol).
    seen: set[int] = set()
    out: list[int] = []
    for p in ordered:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


async def _probe_tcp(host: str, port: int) -> bool:
    """Whether a TCP connection to ``host:port`` completes.

    Connect only — no request is sent. An HTTP GET would poke a real application
    endpoint and would have to guess TLS versus plaintext; an accepted
    connection is enough to know the address resolves and something is listening.
    """
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=PROBE_TIMEOUT
        )
        return True
    except OSError:
        # Covers refused, unreachable, DNS failure, and TimeoutError — which is
        # an OSError subclass, and what wait_for raises here.
        return False
    except asyncio.CancelledError:
        raise  # the overall probe budget expiring must actually cancel us
    except Exception as exc:  # noqa: BLE001 — a probe must never break a scan
        logger.debug("Probe of %s:%s failed: %s", host, port, exc)
        return False
    finally:
        if writer is not None:
            writer.close()
