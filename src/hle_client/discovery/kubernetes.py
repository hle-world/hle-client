"""Kubernetes discovery — enumerate Services across the cluster.

Uses the in-cluster ServiceAccount credentials and plain httpx rather than the
official client library: this needs one read-only API call, and the k8s client
is a large dependency to carry into every install of the CLI.

Read-only by design. The agent never mutates cluster state, and the chart's
ClusterRole grants only get/list/watch on services and endpoints.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from hle_common.discovery import DiscoveredService

logger = logging.getLogger(__name__)

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_PATH = SA_DIR / "token"
CA_PATH = SA_DIR / "ca.crt"
NAMESPACE_PATH = SA_DIR / "namespace"

# Infrastructure that is never interesting to expose, and in kube-system's case
# actively dangerous to put on the internet.
_SKIP_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})
# Ports whose names/numbers indicate cluster plumbing rather than a UI.
_SKIP_PORT_NAMES = frozenset({"metrics", "telemetry", "health", "probe"})


class KubernetesProvider:
    """Lists Services the agent could tunnel to."""

    name = "k8s"

    def __init__(self, *, skip_namespaces: frozenset[str] = _SKIP_NAMESPACES) -> None:
        self._skip_namespaces = skip_namespaces
        self._host = os.environ.get("KUBERNETES_SERVICE_HOST")
        self._port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")

    def available(self) -> bool:
        # Env, token, *and* CA must all be present. Requiring the CA here means
        # we never reach scan() in a state where we'd have to choose between
        # skipping verification and failing — see _verify().
        try:
            return bool(self._host) and TOKEN_PATH.exists() and CA_PATH.exists()
        except OSError:
            return False

    def _token(self) -> str:
        # Re-read per scan: projected ServiceAccount tokens are rotated, and a
        # cached one silently starts returning 401 after an hour or so.
        return TOKEN_PATH.read_text().strip()

    def _verify(self) -> str:
        """Path to the cluster CA bundle.

        Deliberately fails rather than falling back to an unverified connection:
        we send the ServiceAccount bearer token on this request, and skipping
        verification would hand that credential to anything that can intercept
        the connection. A missing CA means we aren't really in a pod, so there
        is nothing to usefully talk to anyway.
        """
        if not CA_PATH.exists():
            raise RuntimeError(
                f"Kubernetes CA bundle not found at {CA_PATH}; refusing to talk to the "
                "API server without TLS verification."
            )
        return str(CA_PATH)

    async def scan(self) -> list[DiscoveredService]:
        verify = self._verify()
        headers = {"Authorization": f"Bearer {self._token()}"}
        base = f"https://{self._host}:{self._port}"

        async with httpx.AsyncClient(verify=verify, timeout=10.0) as client:
            resp = await client.get(f"{base}/api/v1/services", headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        services: list[DiscoveredService] = []
        for item in payload.get("items", []):
            services.extend(self._to_services(item))
        return services

    def _to_services(self, item: dict) -> list[DiscoveredService]:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        namespace = meta.get("namespace") or "default"
        name = meta.get("name") or ""

        if namespace in self._skip_namespaces or not name:
            return []
        # Headless services have no cluster IP to talk to.
        if spec.get("clusterIP") in ("None", "", None):
            return []

        labels = {
            k: v
            for k, v in (meta.get("labels") or {}).items()
            if k.startswith(("app", "app.kubernetes.io/name", "hle.world/"))
        }

        out: list[DiscoveredService] = []
        for port in spec.get("ports") or []:
            if port.get("protocol", "TCP") != "TCP":
                continue
            port_name = str(port.get("name") or "").lower()
            if port_name in _SKIP_PORT_NAMES:
                continue
            number = port.get("port")
            if not isinstance(number, int):
                continue

            # In-cluster DNS: reachable from the agent's pod regardless of node.
            fqdn = f"{name}.{namespace}.svc.cluster.local"
            scheme = "https" if number == 443 or port_name in ("https", "tls") else "http"
            out.append(
                DiscoveredService(
                    provider=self.name,
                    id=f"{namespace}/{name}:{number}",
                    name=name,
                    address=f"{scheme}://{fqdn}:{number}",
                    ports=[number],
                    namespace=namespace,
                    labels=labels,
                )
            )
        return out
