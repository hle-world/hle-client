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
  ``<svc>.<ns>.svc`` and ``<svc>.<ns>.svc.<cluster-domain>``. A bare or
  two-label name is rewritten to its absolute in-cluster FQDN before it is
  resolved, so an ordinary public name such as ``attacker.com`` is never
  resolved on the strength of resolving in the pod's search path. If the pod
  namespace is unknown, a bare ``<svc>`` is refused.
* A raw URL — an IP literal or an ordinary hostname — is refused unless
  ``HLE_ALLOW_RAW_URLS=true``.
* Some targets are refused whatever the settings say: the Kubernetes API
  (by name — including a hostname-form service host, compared lower-cased,
  dot-stripped and IDNA-normalised — by the address in
  ``KUBERNETES_SERVICE_HOST``, and by resolving the API Service name and a
  hostname-form service host into the refused set in the background), cloud
  metadata (link-local ``169.254.0.0/16``, ``100.100.100.200``,
  ``192.0.0.192``, ``168.63.129.16``, ``fd00:ec2::254``, ``metadata`` and
  ``metadata.google.internal``), the unspecified address and ``0.0.0.0/8``,
  loopback (the agent's own pod) and any node address discovered through
  ``HLE_NODE_IP``. The refused API set is seeded from the literals in
  ``KUBERNETES_SERVICE_HOST`` and ``KUBERNETES_PORT_443_TCP_ADDR`` when they
  are IPs, and the agent waits — bounded — for the first background refresh
  before its first reconcile. A partial refresh merges the names that did
  answer; the time is stamped only when every name resolved, and a failure is
  retried with a short backoff while the by-name refusal still holds.
* An IPv6 literal carrying a zone/scope id (``fd00::10%1``) is refused, and
  scope is stripped before every address comparison, so a scoped spelling
  cannot compare unequal to the same address and slip past.
* An in-cluster name is resolved as an absolute FQDN (trailing dot), so the
  pod's search domains are never consulted; the canonical name is also what the
  transport is handed. A hostname must be ASCII and inside ``[a-z0-9.-]``;
  unicode and SRV-style ``_`` labels are refused.
* IPv6 addresses that wrap an IPv4 address — ``::ffff:0:0/96``, 6to4, NAT64
  (``64:ff9b::/96`` and ``64:ff9b:1::/48``), Teredo and IPv4-compatible
  ``::a.b.c.d`` — are unwrapped and the embedded address is checked too, so a
  wrapper cannot smuggle a refused IPv4 past the list.
* Names are resolved with the event loop's ``getaddrinfo`` and every answer is
  checked too, so DNS cannot be used to point a harmless-looking service name
  at a refused address.

The check runs at connect time and the resolved name the tunnel is handed is
the same absolute name that was checked; see the README for the limits of what
that can promise (DNS may answer differently later, and an in-cluster name is
only as trustworthy as the cluster's DNS).

Outside Kubernetes none of this runs: a homelab agent legitimately targets LAN
IPs, and its behaviour is unchanged.

Settings (all environment variables, all with safe defaults):

``HLE_INSTALL_METHOD``
    Set to ``kubernetes`` to declare this agent runs in a cluster even when
    ``KUBERNETES_SERVICE_HOST`` is absent (e.g. during enrollment). Also
    overrides :func:`hle_client.agent.detect_install_method` generally.
``KUBERNETES_SERVICE_HOST``
    Presence alone marks the process as in-cluster.
``KUBERNETES_PORT_443_TCP_ADDR``
    The API Service's ClusterIP, as the kubelet exports it. When it is an IP it
    seeds the refused set even if the service host is a hostname.
``HLE_CLUSTER_DOMAIN``
    Cluster DNS domain, default ``cluster.local``. Only affects which
    fully-qualified Service names are recognised.
``HLE_ALLOW_RAW_URLS``
    ``true``/``1``/``yes`` to allow IP literals and non-cluster hostnames. The
    always-refused targets above are still refused.
``HLE_NODE_IP``
    The node's address, from the downward API. Honoured when present; the chart
    is what will set it.
``HLE_POD_NAMESPACE``
    The pod's namespace, used to expand a bare ``<svc>``. Falls back to
    ``/var/run/secrets/kubernetes.io/serviceaccount/namespace``.
``HLE_FIREPUNCHER_ENABLED``
    Firepuncher is disabled on Kubernetes agents unless this is truthy. The
    relay's forward rules allow loopback and link-local, which in a pod include
    the API server and the metadata service.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Env names live here rather than in agent.py so the guard and the hello both
# spell them the same way.
INSTALL_METHOD_ENV = "HLE_INSTALL_METHOD"
KUBERNETES_SERVICE_HOST_ENV = "KUBERNETES_SERVICE_HOST"
KUBERNETES_PORT_443_TCP_ADDR_ENV = "KUBERNETES_PORT_443_TCP_ADDR"
CLUSTER_DOMAIN_ENV = "HLE_CLUSTER_DOMAIN"
ALLOW_RAW_URLS_ENV = "HLE_ALLOW_RAW_URLS"
NODE_IP_ENV = "HLE_NODE_IP"
FIREPUNCHER_ENABLED_ENV = "HLE_FIREPUNCHER_ENABLED"
POD_NAMESPACE_ENV = "HLE_POD_NAMESPACE"

INSTALL_METHOD_KUBERNETES = "kubernetes"
DEFAULT_CLUSTER_DOMAIN = "cluster.local"
SERVICEACCOUNT_NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

# A name that takes longer than this to resolve fails the check rather than
# stalling reconciliation of every other endpoint behind it.
RESOLVE_TIMEOUT = 5.0

# An overall ceiling on one reconciliation's worth of start checks. Every name
# has its own timeout above and the checks run concurrently, so this only bites
# if the resolver itself ignores cancellation.
START_TIMEOUT = 20.0

# How often the API server's address set is refreshed after a full success. The
# set is seeded from ``KUBERNETES_SERVICE_HOST`` and the
# ``kubernetes.default.svc.<domain>`` name at construction; resolving those
# names again is throttled to this interval. A managed cluster can move the
# endpoint behind the name, so the refresh is not permanent.
API_IPS_REFRESH_SECONDS = 300.0

# Retry delays after a refresh that did not resolve every API name. The last
# value repeats. The time is *not* stamped on failure, so checks keep using the
# addresses already known while these retries run.
API_IPS_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0, 60.0)

# How long the agent waits, before its first reconcile, for the bootstrap
# API-address refresh to settle. A hostname-form API host is refused by name
# immediately, but this closes the gap where an address-only route to the API
# would otherwise be usable until the background task finishes. The wait is
# bounded and does not cancel the refresh, which keeps its own retry schedule.
INITIAL_API_REFRESH_TIMEOUT = 5.0

# A legitimate in-cluster name is ASCII and made only of lower-case letters,
# digits, dots and hyphens. Anything else (unicode after IDNA there is no need
# for, an IPv6 zone separator, an SRV-style leading underscore, a stray colon)
# is refused before DNS sees it.
_VALID_HOSTNAME = re.compile(r"[a-z0-9.-]+\Z")

# The first half of every refusal, so the dashboard and the log read the same.
NOT_ALLOWED = "target not allowed on kubernetes agents"

# The address the AWS/GCP metadata service answers on. 169.254.169.254 is inside
# the link-local range below; the IPv6 ULA form is not, so it is named here.
_METADATA_IPV6 = ipaddress.ip_network("fd00:ec2::254/128")
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
_UNSPECIFIED_V4 = ipaddress.ip_network("0.0.0.0/8")

# Cloud metadata services that are not link-local and so would otherwise be
# reachable through a raw URL: Alibaba, Oracle and the Azure WireServer.
_METADATA_V4 = (
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("192.0.0.192/32"),
    ipaddress.ip_network("168.63.129.16/32"),
)

# NAT64 prefixes that carry an IPv4 address in their low bits.
_NAT64_PREFIXES = (
    (ipaddress.ip_network("64:ff9b::/96"), 96),
    (ipaddress.ip_network("64:ff9b:1::/48"), 48),
)

_METADATA_NAMES = frozenset({"metadata", "metadata.google.internal"})

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class TargetDecision:
    """Whether an endpoint target may be started, and why not when it may not.

    ``url`` is the URL the tunnel should use — the absolute in-cluster name —
    and ``address`` is the exact IP that was checked, for callers (firepuncher)
    that dial a socket rather than hand a name to a transport.
    """

    allowed: bool
    reason: str | None = None
    url: str | None = None
    address: str | None = None


@dataclass(frozen=True)
class _Eval:
    """Internal result: the canonical host and the addresses that were checked."""

    allowed: bool
    reason: str | None = None
    host: str | None = None
    addresses: tuple[_IpAddress, ...] = ()


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


def firepuncher_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether firepuncher should run and be advertised on this agent.

    Outside Kubernetes it is always available. Inside Kubernetes it is off
    unless ``HLE_FIREPUNCHER_ENABLED`` is truthy: the relay's forward rules
    allow loopback and link-local addresses, which in a pod include the API
    server and the cloud metadata service, and a relay must not be able to open
    raw TCP to either through that door.
    """
    if env is None:
        env = os.environ
    if not in_kubernetes(env):
        return True
    return _env_true(env.get(FIREPUNCHER_ENABLED_ENV))


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
        kube_port_addr: str | None = None,
        node_ips: list[str] | None = None,
        pod_namespace: str | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._cluster_domain = (cluster_domain or DEFAULT_CLUSTER_DOMAIN).strip(".").lower()
        self._allow_raw_urls = allow_raw_urls
        self._resolver: Resolver = resolver or _default_resolver
        self._kube_ip = _as_ip(kube_service_host)
        self._node_ips = [ip for ip in (_as_ip(n) for n in (node_ips or [])) if ip is not None]
        self._pod_namespace = (pod_namespace or "").strip() or None

        # The always-refused API set: the literal in ``KUBERNETES_SERVICE_HOST``
        # (an IP on most clusters) and in ``KUBERNETES_PORT_443_TCP_ADDR`` (the
        # Service's ClusterIP, present even when the service host is a
        # hostname), plus whatever the API's own DNS name (and the service host,
        # when it is itself a name — AKS and friends) resolves to. The names are
        # refused outright too, so a failed refresh never opens the door; a
        # background task refreshes the resolved addresses and a check never
        # waits on it.
        self._api_ips: set[_IpAddress] = set()
        if self._kube_ip is not None:
            self._api_ips.add(self._kube_ip)
        kube_port_ip = _as_ip(kube_port_addr)
        if kube_port_ip is not None:
            self._api_ips.add(kube_port_ip)
        self._kube_service_host = (kube_service_host or "").strip() or None
        self._kube_host_name: str | None = None
        self._api_names: list[str] = [
            f"kubernetes.default.svc.{self._cluster_domain}.",
        ]
        if self._kube_service_host is not None and _as_ip(self._kube_service_host) is None:
            self._kube_host_name = _normalise_hostname(self._kube_service_host)
            self._api_names.append(self._kube_host_name or self._kube_service_host)
        self._api_ips_refreshed_at: float | None = None
        self._api_refresh_failures = 0
        self._api_refresh_lock = asyncio.Lock()
        self._api_refresh_task: asyncio.Task[None] | None = None
        self._api_refresh_handle: asyncio.TimerHandle | None = None
        # Kick the first resolution off at construction when there is a loop to
        # run it on. Without a loop (import time, a sync caller) the first check
        # starts it instead; either way the resolution runs in the background
        # and never blocks a check.
        self._bootstrap_api_refresh()

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
            kube_port_addr=env.get(KUBERNETES_PORT_443_TCP_ADDR_ENV),
            node_ips=node_ips,
            pod_namespace=_pod_namespace_from_env(env),
            resolver=resolver,
        )

    async def check(self, service_url: str) -> TargetDecision:
        """Whether *service_url* may be tunnelled, resolving names to check them."""
        host = _host_of(service_url)
        if not host:
            return _refuse("unparseable service URL")
        evaluation = await self._evaluate(host)
        if not evaluation.allowed:
            return _refuse(evaluation.reason or "refused")
        return TargetDecision(
            allowed=True,
            url=_replace_host(service_url, evaluation.host),
            address=str(evaluation.addresses[0]) if evaluation.addresses else None,
        )

    async def check_host(self, host: str, port: int) -> TargetDecision:
        """Whether a firepuncher target may be dialled, resolving it exactly once.

        Returns the checked address in ``address`` so the caller connects to the
        IP that was validated instead of resolving the name a second time and
        potentially reaching a different host.
        """
        del port  # the port is not part of the guard's decision
        evaluation = await self._evaluate(host)
        if not evaluation.allowed:
            return _refuse(evaluation.reason or "refused")
        address = _preferred_address(evaluation.addresses)
        if address is None:
            return _refuse(f"cannot resolve {host}")
        return TargetDecision(allowed=True, address=str(address))

    # -- the decision, once --------------------------------------------------

    def _bootstrap_api_refresh(self) -> None:
        """Start the background API-address refresh chain if it is not running.

        Called at construction and, for a guard built outside a loop, from the
        first check. It only *starts* the task; no DNS happens on this call, so
        a check never waits on resolution. After the first run the chain
        reschedules itself.
        """
        if self._api_refresh_task is not None or self._api_refresh_handle is not None:
            return
        self._start_api_refresh()

    def _start_api_refresh(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._api_refresh_task = loop.create_task(self._refresh_api_ips())
        # Consume any exception so a failed refresh cannot surface as an
        # unretrieved task warning; a failed refresh is not fatal.
        self._api_refresh_task.add_done_callback(lambda task: task.cancelled() or task.exception())

    def _schedule_api_refresh(self, delay: float) -> None:
        """Arrange the next background refresh, replacing any pending one."""
        if self._api_refresh_handle is not None:
            self._api_refresh_handle.cancel()
        loop = asyncio.get_running_loop()
        self._api_refresh_handle = loop.call_later(max(delay, 0.0), self._on_api_refresh_timer)

    def _on_api_refresh_timer(self) -> None:
        self._api_refresh_handle = None
        self._start_api_refresh()

    async def _refresh_api_ips(self) -> None:
        """Resolve the API server's names into the always-refused set.

        Every name is attempted and the answers that resolved are merged into
        the refused set, partial or not: a name that answered is a real API
        address even when a sibling did not. The timestamp is stamped only
        after **every** name resolved. A partial or failed refresh leaves the
        time unstamped and schedules a retry with a short backoff (2s, 5s, 15s,
        then 60s). Meanwhile the API names are still refused by name and the
        literals in ``KUBERNETES_SERVICE_HOST``/``KUBERNETES_PORT_443_TCP_ADDR``
        are seeded at construction, so the gap never opens the door.
        """
        async with self._api_refresh_lock:
            resolved: set[_IpAddress] = set()
            failed = False
            for name in self._api_names:
                answers = await self._resolve(name)
                if not answers:
                    failed = True
                    continue
                resolved.update(answers)
            self._api_ips.update(resolved)
            if failed:
                self._api_refresh_failures += 1
                index = min(
                    self._api_refresh_failures - 1,
                    len(API_IPS_RETRY_BACKOFF_SECONDS) - 1,
                )
                self._schedule_api_refresh(API_IPS_RETRY_BACKOFF_SECONDS[index])
                return
            self._api_ips_refreshed_at = asyncio.get_running_loop().time()
            self._api_refresh_failures = 0
            self._schedule_api_refresh(API_IPS_REFRESH_SECONDS)

    async def wait_for_api_refresh(self, timeout: float | None = None) -> None:
        """Wait for the in-flight API-address refresh, if there is one.

        For callers that need the resolved set to be current before making a
        decision; ordinary checks do not wait. With *timeout*, the wait is
        bounded: timing out does **not** cancel the refresh, which keeps its own
        retry schedule, so a later check or the next reconcile still sees a
        populated set.
        """
        task = self._api_refresh_task
        if task is None or task.done():
            return
        if timeout is None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def _evaluate(self, host: str) -> _Eval:
        """Classify *host*, canonicalise it, resolve it once and check every answer."""
        raw = (host or "").strip().strip("[]")
        had_trailing_dot = raw.endswith(".")
        name = raw.rstrip(".").lower()
        if not name:
            return _refuse_eval("unparseable target")

        # An IPv6 zone/scope id makes two spellings of the same address compare
        # unequal (``fd00::10%1 != fd00::10``), so a scoped literal would slip
        # past the ``==`` checks below. Refuse it before anything else; the
        # resolver's answers are refused the same way in ``_refused_address``.
        if _is_scoped_literal(name):
            return _refuse_eval("IPv6 zone/scope id is not allowed")

        # Refused by name before the resolver sees them, so a metadata or API
        # hostname never reaches DNS. The hostname-form service host
        # (``KUBERNETES_SERVICE_HOST`` on AKS and friends) is compared after
        # IDNA normalisation, so a unicode or case variant is still refused by
        # name rather than depending on its resolved address.
        if _is_kube_api_name(name, self._cluster_domain):
            return _refuse_eval("kubernetes API")
        if self._kube_host_name is not None and _normalise_hostname(name) == self._kube_host_name:
            return _refuse_eval("kubernetes API")
        if _is_metadata_name(name):
            return _refuse_eval("cloud metadata")

        address = _as_ip(name)
        if address is not None:
            self._bootstrap_api_refresh()
            refused = self._refused_address(address)
            if refused is not None:
                return _refuse_eval(refused)
            if not self._allow_raw_urls:
                return _refuse_eval("raw URL not allowed")
            return _Eval(allowed=True, host=str(address), addresses=(address,))

        # A hostname must be an ordinary ASCII DNS name. After IDNA there is no
        # legitimate use for unicode inside a cluster, and allowing any other
        # character (a stray colon, a percent escape) invites a spelling that
        # resolves somewhere the checks below do not expect. SRV-style labels
        # (``_https._tcp.…``) are named explicitly: they are the obvious way to
        # make a resolver ask a question the name checks never anticipated.
        if any(label.startswith("_") for label in name.split(".")):
            return _refuse_eval(f"{name} is an SRV-style name")
        if not name.isascii() or not _VALID_HOSTNAME.fullmatch(name):
            return _refuse_eval(f"{name} is not a valid hostname")

        self._bootstrap_api_refresh()
        dots = name.count(".")
        if dots == 0:
            # A bare name resolves through the pod's search path, which could
            # send it anywhere. Only an absolute in-cluster name is resolved.
            if not self._pod_namespace:
                return _refuse_eval(
                    f"cannot resolve {name}: pod namespace unknown for a bare service name"
                )
            fqdn = f"{name}.{self._pod_namespace}.svc.{self._cluster_domain}."
            return await self._resolve_service(name, fqdn)
        if name.endswith(f".svc.{self._cluster_domain}"):
            return await self._resolve_service(name, f"{name}.")
        if name.endswith(".svc") and dots >= 2:
            return await self._resolve_service(name, f"{name}.{self._cluster_domain}.")
        if dots == 1:
            # `<svc>.<ns>` has the same shape as an ordinary two-label domain.
            # It is rewritten to the absolute FQDN and resolved only that way;
            # if it does not exist, and raw URLs are allowed, it may be retried
            # as an external name.
            fqdn = f"{name}.svc.{self._cluster_domain}."
            return await self._resolve_service(name, fqdn, allow_raw_fallback=True)
        # A raw external name is resolved as written. Its trailing dot, if any,
        # was already consumed above; keep the written form for the transport.
        return await self._resolve_raw(name if not had_trailing_dot else f"{name}.")

    async def _resolve_service(
        self, name: str, fqdn: str, *, allow_raw_fallback: bool = False
    ) -> _Eval:
        addresses = await self._resolve(fqdn)
        if addresses:
            refused = self._first_refused(addresses, name)
            if refused is not None:
                return _refuse_eval(refused)
            if not self._allow_raw_urls and any(address.is_global for address in addresses):
                return _refuse_eval(f"raw URL not allowed (via {name})")
            return _Eval(allowed=True, host=fqdn, addresses=tuple(addresses))
        if allow_raw_fallback and self._allow_raw_urls:
            return await self._resolve_raw(name)
        return _refuse_eval(f"cannot resolve {fqdn}")

    async def _resolve_raw(self, name: str) -> _Eval:
        if not self._allow_raw_urls:
            return _refuse_eval("raw URL not allowed")
        addresses = await self._resolve(name)
        if not addresses:
            return _refuse_eval(f"cannot resolve {name}")
        refused = self._first_refused(addresses, name)
        if refused is not None:
            return _refuse_eval(refused)
        return _Eval(allowed=True, host=name, addresses=tuple(addresses))

    async def _resolve(self, name: str) -> list[_IpAddress]:
        try:
            answers = await asyncio.wait_for(self._resolver(name), timeout=RESOLVE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — a resolution failure must not start the tunnel
            logger.debug("Could not resolve %s: %s", name, exc)
            return []
        # Scope is deliberately kept on the parsed address so a scoped DNS
        # answer can be refused rather than silently normalised to its unscoped
        # cousin; comparisons strip it again in ``_same_ip``.
        return [ip for ip in (_as_ip(a) for a in answers) if ip is not None]

    def _first_refused(self, addresses: list[_IpAddress], name: str) -> str | None:
        for address in addresses:
            refused = self._refused_address(address)
            if refused is not None:
                return f"{refused} (via {name})"
        return None

    def _refused_address(self, address: _IpAddress, *, depth: int = 0) -> str | None:
        """The reason *address* is always refused, or None.

        An IPv6 address that embeds an IPv4 address is unwrapped and the
        embedded address is checked too, so ``::ffff:169.254.169.254`` and its
        NAT64, 6to4, Teredo and IPv4-compatible cousins cannot slip past.

        A scoped address (``fe80::1%eth0``) is refused outright: the scope makes
        it compare unequal to the same address without one, and no cluster
        target has a legitimate reason to carry a zone id.
        """
        if getattr(address, "scope_id", None) is not None:
            return f"IPv6 zone/scope id not allowed for {address}"
        if address.is_unspecified:
            return f"unspecified address {address}"
        if address.version == 4 and address in _UNSPECIFIED_V4:
            return f"unspecified address {address}"
        if address.is_loopback:
            return f"loopback address {address}"
        if any(_same_ip(address, api) for api in self._api_ips):
            return f"kubernetes API address {address}"
        if any(_same_ip(address, node) for node in self._node_ips):
            return f"node address {address}"

        if address.version == 4:
            if address in _LINK_LOCAL_V4:
                return f"link-local address {address}"
            if any(address in net for net in _METADATA_V4):
                return f"cloud metadata address {address}"
            return None

        if depth < 2:
            for embedded in _embedded_ipv4(address):
                reason = self._refused_address(embedded, depth=depth + 1)
                if reason is not None:
                    return f"{reason} (via {address})"
        if address in _LINK_LOCAL_V6:
            return f"link-local address {address}"
        if address in _METADATA_IPV6:
            return f"cloud metadata address {address}"
        return None


def _refuse(detail: str) -> TargetDecision:
    return TargetDecision(allowed=False, reason=f"{NOT_ALLOWED}: {detail}")


def _refuse_eval(detail: str) -> _Eval:
    return _Eval(allowed=False, reason=detail)


def _env_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _normalise_hostname(value: str | None) -> str | None:
    """Lower-case, dotless, IDNA-normalised form of a hostname, for comparison.

    Two spellings of the same name must compare equal: ``API.AKS.Example`` and
    ``api.aks.example.`` are one target, and a unicode spelling must not slip
    past the API-name check. Returns None for an empty value; a name IDNA
    rejects is returned lower-cased and dotless rather than dropped.
    """
    if not value:
        return None
    name = value.strip().strip("[]").rstrip(".").lower()
    if not name:
        return None
    try:
        return name.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return name


def _pod_namespace_from_env(env: Mapping[str, str]) -> str | None:
    explicit = (env.get(POD_NAMESPACE_ENV) or "").strip()
    if explicit:
        return explicit
    try:
        with open(SERVICEACCOUNT_NAMESPACE_PATH, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _as_ip(value: str | None) -> _IpAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def _is_scoped_literal(name: str) -> bool:
    """Whether *name* is an IPv6 literal carrying a zone/scope id.

    A percent sign in an ordinary hostname is not a scope id; only treat it as
    one when the part before it parses as an IPv6 address.
    """
    if "%" not in name:
        return False
    address_part, _, _scope = name.partition("%")
    try:
        return ipaddress.ip_address(address_part).version == 6
    except ValueError:
        return False


def _same_ip(left: _IpAddress, right: _IpAddress) -> bool:
    """Compare two addresses ignoring an IPv6 scope id (defence in depth)."""
    return _unscoped(left) == _unscoped(right)


def _unscoped(address: _IpAddress) -> _IpAddress:
    if getattr(address, "scope_id", None) is None:
        return address
    return type(address)(int(address))


def _preferred_address(addresses: tuple[_IpAddress, ...]) -> _IpAddress | None:
    for address in addresses:
        if address.version == 4:
            return address
    return addresses[0] if addresses else None


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address *address* wraps, in the forms the standards allow."""
    found: list[ipaddress.IPv4Address] = []
    for embedded in (address.ipv4_mapped, address.sixtofour):
        if embedded is not None:
            found.append(embedded)
    teredo = address.teredo
    if teredo is not None:
        found.append(teredo[1])
    # IPv4-compatible `::a.b.c.d` / `::a:b` — first 96 bits zero. `::` and `::1`
    # are caught earlier as unspecified/loopback.
    if int(address) >> 32 == 0 and not address.is_unspecified:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    for network, prefixlen in _NAT64_PREFIXES:
        if address in network:
            found.append(_nat64_embedded(address, prefixlen))
    return found


def _nat64_embedded(address: ipaddress.IPv6Address, prefixlen: int) -> ipaddress.IPv4Address:
    """The IPv4 address a NAT64 prefix embeds, per RFC 6052."""
    value = int(address)
    if prefixlen == 96:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    # /48: octets a and b at bits 48-63, the reserved u octet at 64-71, then c
    # and d at bits 72-79 and 80-87.
    a = (value >> 72) & 0xFF
    b = (value >> 64) & 0xFF
    c = (value >> 48) & 0xFF
    d = (value >> 40) & 0xFF
    return ipaddress.IPv4Address((a << 24) | (b << 16) | (c << 8) | d)


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


def authority_of(service_url: str) -> str | None:
    """The ``host:port`` authority of *service_url*, for a preserved Host header."""
    if not service_url:
        return None
    candidate = service_url if "://" in service_url else f"http://{service_url}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    rendered = f"[{host}]" if ":" in host else host
    return f"{rendered}:{parsed.port}" if parsed.port else rendered


def _replace_host(service_url: str, new_host: str | None) -> str:
    """*service_url* with its host replaced, keeping scheme, port, path and query."""
    if not new_host:
        return service_url
    candidate = service_url if "://" in service_url else f"http://{service_url}"
    parsed = urlparse(candidate)
    userinfo = ""
    # ``is not None``: an empty username with a password (``:pass@``) is still
    # userinfo and must survive canonicalisation so the transport can send it.
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    host = f"[{new_host}]" if ":" in new_host else new_host
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse(parsed._replace(netloc=f"{userinfo}{host}{port}"))


def _is_kube_api_name(name: str, cluster_domain: str) -> bool:
    """The API server's own DNS names, in every form it answers to."""
    if name in ("kubernetes", "kubernetes.default", "kubernetes.default.svc"):
        return True
    return name == f"kubernetes.default.svc.{cluster_domain}"


def _is_metadata_name(name: str) -> bool:
    return name in _METADATA_NAMES or name.endswith(".metadata.google.internal")
