"""Which endpoints an agent running inside Kubernetes is allowed to tunnel to.

A homelab agent is invited to reach its own LAN — that is the whole point, and
:func:`hle_client.netinfo.local_networks` is the allowlist it lives by. An agent
put in a cluster is not the same machine. The relay decides ``service_url``, so
a compromised or mistaken dashboard entry could otherwise point the tunnel at
the Kubernetes API server, the cloud metadata service, or the node the pod is
on, and publish any of them to the internet.

This module is the guard that runs before such an endpoint starts. The rules
(the security decision from hle-world/hle-operator#6) are:

* A Kubernetes agent may target Services by name: ``<svc>``, ``<svc>.<ns>``,
  ``<svc>.<ns>.svc`` and ``<svc>.<ns>.svc.<cluster-domain>``. These resolve
  through the pod's DNS search path.
* A raw URL — an IP literal or an ordinary hostname — is refused unless
  ``HLE_ALLOW_RAW_URLS=true``.
* Some targets are refused whatever the settings say: the Kubernetes API
  (by name or by the address in ``KUBERNETES_SERVICE_HOST``), cloud metadata
  (link-local ``169.254.0.0/16``, ``fd00:ec2::254``, ``metadata`` and
  ``metadata.google.internal``), loopback (the agent's own pod) and any node
  address discovered through ``HLE_NODE_IP``.
* Names are resolved with the event loop's ``getaddrinfo`` and every answer is
  checked too, so DNS cannot be used to point a harmless-looking service name
  at a refused address.

Outside Kubernetes none of this runs: a homelab agent legitimately targets LAN
IPs, and its behaviour is unchanged.

Settings (all environment variables, all with safe defaults):

``HLE_INSTALL_METHOD``
    Set to ``kubernetes`` to declare this agent runs in a cluster even when
    ``KUBERNETES_SERVICE_HOST`` is absent (e.g. during enrollment). Also
    overrides :func:`hle_client.agent.detect_install_method` generally.
``KUBERNETES_SERVICE_HOST``
    Presence alone marks the process as in-cluster.
``HLE_CLUSTER_DOMAIN``
    Cluster DNS domain, default ``cluster.local``. Only affects which
    fully-qualified Service names are recognised.
``HLE_ALLOW_RAW_URLS``
    ``true``/``1``/``yes`` to allow IP literals and non-cluster hostnames. The
    always-refused targets above are still refused.
``HLE_NODE_IP``
    The node's address, from the downward API. Honoured when present; the chart
    is what will set it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Env names live here rather than in agent.py so the guard and the hello both
# spell them the same way.
INSTALL_METHOD_ENV = "HLE_INSTALL_METHOD"
KUBERNETES_SERVICE_HOST_ENV = "KUBERNETES_SERVICE_HOST"
CLUSTER_DOMAIN_ENV = "HLE_CLUSTER_DOMAIN"
ALLOW_RAW_URLS_ENV = "HLE_ALLOW_RAW_URLS"
NODE_IP_ENV = "HLE_NODE_IP"

INSTALL_METHOD_KUBERNETES = "kubernetes"
DEFAULT_CLUSTER_DOMAIN = "cluster.local"

# A name that takes longer than this to resolve fails the check rather than
# stalling reconciliation of every other endpoint behind it.
RESOLVE_TIMEOUT = 5.0

# The first half of every refusal, so the dashboard and the log read the same.
NOT_ALLOWED = "target not allowed on kubernetes agents"

# The address the AWS/GCP metadata service answers on. 169.254.169.254 is inside
# the link-local range below; the IPv6 ULA form is not, so it is named here.
_METADATA_IPV6 = ipaddress.ip_network("fd00:ec2::254/128")
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_network("::1/128")

_METADATA_NAMES = frozenset({"metadata", "metadata.google.internal"})

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class TargetDecision:
    """Whether an endpoint target may be started, and why not when it may not."""

    allowed: bool
    reason: str | None = None


_ALLOWED = TargetDecision(allowed=True)


def in_kubernetes(env: Mapping[str, str] | None = None) -> bool:
    """Whether this agent declares itself as running inside a cluster.

    ``KUBERNETES_SERVICE_HOST`` is the marker the kubelet injects into every
    pod. ``HLE_INSTALL_METHOD=kubernetes`` is the explicit opt-in for when the
    agent runs before that variable is in its environment, or outside a pod
    during enrollment.
    """
    if env is None:
        env = os.environ
    return bool(env.get(KUBERNETES_SERVICE_HOST_ENV)) or (
        env.get(INSTALL_METHOD_ENV) == INSTALL_METHOD_KUBERNETES
    )


async def _default_resolver(host: str) -> list[str]:
    """Resolve *host* with the running loop's non-blocking getaddrinfo."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


Resolver = Callable[[str], Awaitable[list[str]]]


class KubernetesTargetGuard:
    """Decide whether one ``service_url`` is a legal target for a cluster agent."""

    def __init__(
        self,
        *,
        cluster_domain: str = DEFAULT_CLUSTER_DOMAIN,
        allow_raw_urls: bool = False,
        kube_service_host: str | None = None,
        node_ips: list[str] | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._cluster_domain = (cluster_domain or DEFAULT_CLUSTER_DOMAIN).strip(".").lower()
        self._allow_raw_urls = allow_raw_urls
        self._resolver: Resolver = resolver or _default_resolver
        self._kube_ip = _as_ip(kube_service_host)
        self._node_ips = [ip for ip in (_as_ip(n) for n in (node_ips or [])) if ip is not None]

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        resolver: Resolver | None = None,
    ) -> KubernetesTargetGuard:
        """Build a guard from the process environment."""
        if env is None:
            env = os.environ
        node_ips = [
            part.strip() for part in (env.get(NODE_IP_ENV) or "").split(",") if part.strip()
        ]
        return cls(
            cluster_domain=env.get(CLUSTER_DOMAIN_ENV) or DEFAULT_CLUSTER_DOMAIN,
            allow_raw_urls=_env_true(env.get(ALLOW_RAW_URLS_ENV)),
            kube_service_host=env.get(KUBERNETES_SERVICE_HOST_ENV),
            node_ips=node_ips,
            resolver=resolver,
        )

    async def check(self, service_url: str) -> TargetDecision:
        """Whether *service_url* may be tunnelled, resolving names to check them."""
        host = _host_of(service_url)
        if not host:
            return _refuse("unparseable service URL")
        name = host.rstrip(".").lower()

        # Names refused whatever the settings say, checked before anything else
        # so a metadata or API hostname never reaches the resolver.
        if _is_kube_api_name(name, self._cluster_domain):
            return _refuse("kubernetes API")
        if _is_metadata_name(name):
            return _refuse("cloud metadata")

        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            address = None

        if address is not None:
            refused = self._refused_address(address)
            if refused is not None:
                return _refuse(refused)
            if not self._allow_raw_urls:
                return _refuse("raw URL not allowed")
            return _ALLOWED

        explicit_cluster_name = _is_explicit_cluster_name(name, self._cluster_domain)
        bare_name = _is_bare_service_name(name)
        if not explicit_cluster_name and not bare_name and not self._allow_raw_urls:
            return _refuse("raw URL not allowed")

        try:
            answers = await asyncio.wait_for(self._resolver(name), timeout=RESOLVE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — a resolution failure must not start the tunnel
            logger.debug("Could not resolve %s: %s", name, exc)
            return _refuse(f"cannot resolve {name}")
        resolved = [ip for ip in (_as_ip(a.split("%")[0]) for a in answers) if ip is not None]
        if not resolved:
            return _refuse(f"cannot resolve {name}")
        for address in resolved:
            refused = self._refused_address(address)
            if refused is not None:
                return _refuse(f"{refused} (via {name})")
        # A two-label name such as ``example.com`` has the same shape as a bare
        # ``<svc>.<ns>``. They are told apart by where they resolve: a cluster
        # service lands on an internal address, an ordinary hostname on a
        # globally-routable one, which is a raw URL by another road.
        if (
            not explicit_cluster_name
            and not self._allow_raw_urls
            and any(address.is_global for address in resolved)
        ):
            return _refuse("raw URL not allowed")
        return _ALLOWED

    def _refused_address(self, address: _IpAddress) -> str | None:
        """The reason *address* is always refused, or None."""
        # `::ffff:169.254.169.254` is the same host as the IPv4 address, so
        # check what it maps to rather than letting the wrapper slip past.
        if address.version == 6 and address.ipv4_mapped is not None:
            return self._refused_address(address.ipv4_mapped)
        if address.is_loopback:
            return f"loopback address {address}"
        if self._kube_ip is not None and address == self._kube_ip:
            return f"kubernetes API address {address}"
        if any(address == node for node in self._node_ips):
            return f"node address {address}"
        if address.version == 4 and address in _LINK_LOCAL_V4:
            return f"link-local address {address}"
        if address.version == 6:
            if address in _LINK_LOCAL_V6:
                return f"link-local address {address}"
            if address in _METADATA_IPV6:
                return f"cloud metadata address {address}"
        return None


def _refuse(detail: str) -> TargetDecision:
    return TargetDecision(allowed=False, reason=f"{NOT_ALLOWED}: {detail}")


def _env_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _as_ip(value: str | None) -> _IpAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def _host_of(service_url: str) -> str | None:
    """The hostname in *service_url*, bare hostnames assumed to be http."""
    if not service_url:
        return None
    candidate = service_url if "://" in service_url else f"http://{service_url}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    return parsed.hostname or None


def _is_kube_api_name(name: str, cluster_domain: str) -> bool:
    """The API server's own DNS names, in every form it answers to."""
    if name in ("kubernetes", "kubernetes.default", "kubernetes.default.svc"):
        return True
    return name == f"kubernetes.default.svc.{cluster_domain}"


def _is_metadata_name(name: str) -> bool:
    return name in _METADATA_NAMES or name.endswith(".metadata.google.internal")


def _is_explicit_cluster_name(name: str, cluster_domain: str) -> bool:
    """A name that says outright it is a Service, not just a two-label host."""
    if name.endswith(".svc"):
        return name.count(".") >= 2
    return bool(cluster_domain) and name.endswith(f".svc.{cluster_domain}")


def _is_bare_service_name(name: str) -> bool:
    """``<svc>`` or ``<svc>.<ns>``, resolved through the pod's search path."""
    return bool(name) and ".." not in name and name.count(".") <= 1
