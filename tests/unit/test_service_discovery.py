"""Tests for service discovery (the agent's inventory of nearby services).

Distinct from ``test_discovery.py``, which covers *relay* discovery.

Providers are tested against recorded API payloads so the suite runs without a
Docker socket or a Kubernetes cluster.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hle_client.discovery import active_providers, scan_all
from hle_client.discovery.docker import DockerProvider, _network_alias, _usable_ports
from hle_client.discovery.kubernetes import KubernetesProvider
from hle_common.discovery import DiscoveredService


class FakeProvider:
    def __init__(self, name: str, services: list[DiscoveredService], *, fail: bool = False) -> None:
        self.name = name
        self._services = services
        self._fail = fail

    def available(self) -> bool:
        return True

    async def scan(self) -> list[DiscoveredService]:
        if self._fail:
            raise RuntimeError("socket exploded")
        return self._services


def _svc(name: str, provider: str = "test") -> DiscoveredService:
    return DiscoveredService(
        provider=provider, id=f"{name}:80", name=name, address=f"http://{name}:80", ports=[80]
    )


class TestSuggestedLabel:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("jellyfin", "jellyfin"),
            ("Home Assistant", "home-assistant"),
            ("my_app.v2", "my-app-v2"),
            ("!!!", "service"),  # never produce an empty label
        ],
    )
    def test_label_is_dns_safe(self, name, expected):
        assert _svc(name).suggested_label() == expected

    def test_label_is_truncated(self):
        assert len(_svc("x" * 200).suggested_label()) <= 63


class TestScanAll:
    async def test_collects_from_every_provider(self):
        services, names, error = await scan_all(
            [FakeProvider("a", [_svc("one")]), FakeProvider("b", [_svc("two")])]
        )
        assert {s.name for s in services} == {"one", "two"}
        assert names == ["a", "b"]
        assert error is None

    async def test_one_failing_provider_does_not_discard_the_others(self):
        """A broken Docker socket must not hide the k8s services we can see."""
        services, names, error = await scan_all(
            [FakeProvider("broken", [], fail=True), FakeProvider("ok", [_svc("survivor")])]
        )
        assert [s.name for s in services] == ["survivor"]
        assert "broken" in error
        assert "ok" in names

    async def test_no_providers_is_not_an_error(self):
        services, names, error = await scan_all([])
        assert services == []
        assert names == []
        assert error is None


class TestActiveProviders:
    def test_unavailable_providers_are_skipped(self):
        class Unavailable:
            name = "nope"

            def available(self) -> bool:
                return False

            async def scan(self):
                return []

        assert active_providers([Unavailable()]) == []

    def test_a_raising_availability_check_is_not_fatal(self):
        class Exploding:
            name = "boom"

            def available(self) -> bool:
                raise OSError("no")

            async def scan(self):
                return []

        assert active_providers([Exploding()]) == []


class TestDockerProvider:
    def _container(self, **overrides) -> dict:
        base = {
            "Id": "abc123def456789",
            "Names": ["/jellyfin"],
            "Image": "jellyfin/jellyfin:latest",
            "Labels": {"com.docker.compose.project": "media", "build.irrelevant": "x"},
            "Ports": [{"PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"}],
            "NetworkSettings": {"Networks": {"media_default": {"Aliases": ["jellyfin"]}}},
        }
        base.update(overrides)
        return base

    def test_maps_a_container_to_a_service(self):
        svc = DockerProvider()._to_service(self._container())
        assert svc is not None
        assert svc.provider == "docker"
        assert svc.name == "jellyfin"
        # Reached by network alias, which works whether or not a port is published.
        assert svc.address == "http://jellyfin:8096"
        assert svc.namespace == "media"
        assert "build.irrelevant" not in svc.labels

    def test_skips_containers_without_ports(self):
        assert DockerProvider()._to_service(self._container(Ports=[])) is None

    def test_skips_hle_own_containers(self):
        # Exposing HLE through HLE is a confusing loop.
        assert (
            DockerProvider()._to_service(
                self._container(Image="ghcr.io/hle-world/hle-docker:latest")
            )
            is None
        )

    def test_falls_back_to_container_name_without_alias(self):
        svc = DockerProvider()._to_service(self._container(NetworkSettings={"Networks": {}}))
        assert svc.address == "http://jellyfin:8096"

    def test_unavailable_when_socket_missing(self):
        assert DockerProvider(socket_path="/nonexistent/docker.sock").available() is False


class TestDockerPortSelection:
    def test_published_ports_come_first(self):
        ports = _usable_ports(
            {
                "Ports": [
                    {"PrivatePort": 9000, "Type": "tcp"},
                    {"PrivatePort": 8096, "PublicPort": 8096, "Type": "tcp"},
                ]
            }
        )
        # The operator deliberately published 8096; prefer it as the default.
        assert ports[0] == 8096

    def test_udp_is_ignored(self):
        assert _usable_ports({"Ports": [{"PrivatePort": 53, "Type": "udp"}]}) == []

    def test_duplicates_are_removed(self):
        ports = _usable_ports(
            {"Ports": [{"PrivatePort": 80, "Type": "tcp"}, {"PrivatePort": 80, "Type": "tcp"}]}
        )
        assert ports == [80]

    def test_alias_extraction(self):
        assert _network_alias({"NetworkSettings": {"Networks": {"n": {"Aliases": ["a"]}}}}) == "a"
        assert _network_alias({"NetworkSettings": {"Networks": {}}}) is None


class TestKubernetesProvider:
    def _service(self, **overrides) -> dict:
        base = {
            "metadata": {
                "name": "jellyfin",
                "namespace": "media",
                "labels": {"app": "jellyfin", "helm.sh/chart": "ignored"},
            },
            "spec": {
                "clusterIP": "10.0.0.5",
                "ports": [{"port": 8096, "protocol": "TCP", "name": "http"}],
            },
        }
        base.update(overrides)
        return base

    def test_maps_a_service_to_cluster_dns(self):
        out = KubernetesProvider()._to_services(self._service())
        assert len(out) == 1
        svc = out[0]
        assert svc.address == "http://jellyfin.media.svc.cluster.local:8096"
        assert svc.namespace == "media"
        assert svc.id == "media/jellyfin:8096"
        assert "helm.sh/chart" not in svc.labels

    def test_skips_system_namespaces(self):
        item = self._service()
        item["metadata"]["namespace"] = "kube-system"
        assert KubernetesProvider()._to_services(item) == []

    def test_skips_headless_services(self):
        item = self._service()
        item["spec"]["clusterIP"] = "None"
        assert KubernetesProvider()._to_services(item) == []

    def test_skips_metrics_ports(self):
        item = self._service()
        item["spec"]["ports"] = [{"port": 9090, "protocol": "TCP", "name": "metrics"}]
        assert KubernetesProvider()._to_services(item) == []

    def test_https_port_uses_https_scheme(self):
        item = self._service()
        item["spec"]["ports"] = [{"port": 443, "protocol": "TCP", "name": "https"}]
        assert KubernetesProvider()._to_services(item)[0].address.startswith("https://")

    def test_multi_port_service_yields_one_entry_per_port(self):
        item = self._service()
        item["spec"]["ports"] = [
            {"port": 80, "protocol": "TCP", "name": "http"},
            {"port": 8443, "protocol": "TCP", "name": "alt"},
        ]
        assert len(KubernetesProvider()._to_services(item)) == 2

    def test_udp_ports_are_skipped(self):
        item = self._service()
        item["spec"]["ports"] = [{"port": 53, "protocol": "UDP", "name": "dns"}]
        assert KubernetesProvider()._to_services(item) == []

    def test_unavailable_outside_a_cluster(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        assert KubernetesProvider().available() is False


class TestKubernetesTls:
    """TLS verification must never be skipped.

    The request carries the ServiceAccount bearer token, so an unverified
    connection would hand that credential to anything able to intercept it.
    """

    def test_missing_ca_raises_instead_of_disabling_verification(self, monkeypatch):
        import hle_client.discovery.kubernetes as k8s

        monkeypatch.setattr(k8s, "CA_PATH", Path("/nonexistent/ca.crt"))
        with pytest.raises(RuntimeError, match="refusing to talk to the API server"):
            k8s.KubernetesProvider()._verify()

    def test_verify_returns_the_ca_path_when_present(self, monkeypatch, tmp_path):
        import hle_client.discovery.kubernetes as k8s

        ca = tmp_path / "ca.crt"
        ca.write_text("-----BEGIN CERTIFICATE-----")
        monkeypatch.setattr(k8s, "CA_PATH", ca)
        # A path, never False — httpx treats False as "skip verification".
        assert k8s.KubernetesProvider()._verify() == str(ca)

    def test_unavailable_without_a_ca(self, monkeypatch, tmp_path):
        import hle_client.discovery.kubernetes as k8s

        token = tmp_path / "token"
        token.write_text("t")
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setattr(k8s, "TOKEN_PATH", token)
        monkeypatch.setattr(k8s, "CA_PATH", tmp_path / "missing.crt")
        # Reported unavailable, so scan() is never reached in that state.
        assert k8s.KubernetesProvider().available() is False
