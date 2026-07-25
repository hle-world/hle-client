"""Docker discovery — enumerate containers with reachable ports.

Talks to the Docker Engine API over its Unix socket using httpx (already a
dependency), rather than pulling in the Docker SDK for two read-only calls.

Security note: the Docker socket is root-equivalent on the host — anything that
can *write* to it can start a privileged container. This provider only ever
reads (``GET /containers/json``), and the socket should be mounted read-only,
but mounting it at all is a real trust decision. Discovery stays opt-in.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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


class DockerProvider:
    """Lists running containers that publish or expose a usable port."""

    name = "docker"

    def __init__(self, socket_path: str | None = None) -> None:
        self._socket = socket_path or os.environ.get("DOCKER_SOCKET", DEFAULT_SOCKET)

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

        services: list[DiscoveredService] = []
        for c in containers:
            service = self._to_service(c)
            if service is not None:
                services.append(service)
        return services

    def _to_service(self, container: dict) -> DiscoveredService | None:
        image = str(container.get("Image", ""))
        if any(image.startswith(p) for p in _SKIP_IMAGE_PREFIXES):
            return None

        name = _container_name(container)
        if not name:
            return None

        ports = _usable_ports(container)
        if not ports:
            return None

        labels = {
            k: v
            for k, v in (container.get("Labels") or {}).items()
            # Compose metadata is useful context; the rest is mostly build noise.
            if k.startswith("com.docker.compose.") or k.startswith("hle.")
        }

        # Prefer reaching the container by its network alias rather than a
        # published host port: it works even when nothing is published, and it
        # doesn't depend on how the agent's own networking is set up.
        host = _network_alias(container) or name
        port = ports[0]

        return DiscoveredService(
            provider=self.name,
            id=f"{container.get('Id', name)[:12]}:{port}",
            name=name,
            address=f"http://{host}:{port}",
            ports=ports,
            namespace=labels.get("com.docker.compose.project"),
            labels=labels,
        )


def _container_name(container: dict) -> str | None:
    names = container.get("Names") or []
    if not names:
        return None
    # Docker returns names with a leading slash: "/jellyfin".
    return str(names[0]).lstrip("/") or None


def _network_alias(container: dict) -> str | None:
    """A name other containers on the same network can resolve."""
    networks = ((container.get("NetworkSettings") or {}).get("Networks") or {}).values()
    for net in networks:
        aliases = net.get("Aliases") or []
        if aliases:
            return str(aliases[0])
    return None


def _usable_ports(container: dict) -> list[int]:
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
    return [p for p in ordered if not (p in seen or seen.add(p))]
