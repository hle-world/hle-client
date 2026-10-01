"""The Kubernetes endpoint target allow-list.

A compromised or mistaken dashboard entry must not turn a cluster agent into a
tunnel to the API server, the cloud metadata service or a node. These are the
unit tests for that refusal; see ``hle_client.k8s_targets``.
"""

from __future__ import annotations

import asyncio

import pytest

from hle_client import k8s_targets
from hle_client.agent import AgentClient
from hle_common.agent_protocol import EndpointSpec

INTERNAL = "10.96.0.7"


def resolver(mapping: dict[str, list[str]] | None = None, default: str | None = INTERNAL):
    """A stand-in for the event loop's getaddrinfo.

    Names not in *mapping* answer with *default* (an internal address, so a
    well-formed service name is allowed); ``default=None`` makes them fail.
    """

    async def resolve(host: str) -> list[str]:
        if mapping is not None and host in mapping:
            return mapping[host]
        if default is None:
            raise OSError(f"name or service not known: {host}")
        return [default]

    return resolve


def guard(*, resolving: dict[str, list[str]] | None = None, default: str | None = INTERNAL, **kw):
    return k8s_targets.KubernetesTargetGuard(
        resolver=resolver(resolving, default),
        **kw,
    )


async def check(url: str, **kw):
    return await guard(**kw).check(url)


class TestAllowedServiceNames:
    """Service names are how a cluster agent is meant to target things."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://ha.default.svc.cluster.local:8123",
            "http://ha.default.svc:8123",
            "https://jellyfin.media.svc.cluster.local:443",
        ],
    )
    async def test_fully_qualified_service_names_are_allowed(self, url):
        decision = await check(url)
        assert decision.allowed, decision.reason

    @pytest.mark.parametrize("url", ["http://ha", "http://ha.default"])
    async def test_bare_names_allowed_when_they_resolve_inside(self, url):
        name = url.removeprefix("http://")
        decision = await check(url, resolving={name: [INTERNAL]})
        assert decision.allowed, decision.reason

    async def test_a_custom_cluster_domain_is_honoured(self):
        decision = await check(
            "http://ha.default.svc.k8s.example.com",
            cluster_domain="k8s.example.com",
        )
        assert decision.allowed, decision.reason

    async def test_a_trailing_dot_still_matches(self):
        decision = await check("http://ha.default.svc.cluster.local.")
        assert decision.allowed, decision.reason


class TestAlwaysRefused:
    """These are refused whatever the settings say."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://kubernetes",
            "https://kubernetes.default",
            "https://kubernetes.default.svc",
            "https://kubernetes.default.svc.cluster.local",
        ],
    )
    async def test_the_kubernetes_api_s_name_is_refused(self, url):
        decision = await check(url)
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    async def test_the_kubernetes_api_s_address_is_refused(self):
        decision = await check("https://10.96.0.1:443", kube_service_host="10.96.0.1")
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://[fd00:ec2::254]/",
        ],
    )
    async def test_metadata_addresses_are_refused(self, url):
        decision = await check(url)
        assert not decision.allowed
        assert "metadata" in decision.reason or "link-local" in decision.reason

    @pytest.mark.parametrize("url", ["http://metadata", "http://metadata.google.internal"])
    async def test_metadata_names_are_refused(self, url):
        decision = await check(url)
        assert not decision.allowed
        assert "metadata" in decision.reason

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://[::1]:8080"])
    async def test_loopback_is_refused(self, url):
        decision = await check(url)
        assert not decision.allowed
        assert "loopback" in decision.reason

    async def test_an_ipv4_mapped_metadata_address_is_refused(self):
        decision = await check("http://[::ffff:169.254.169.254]", allow_raw_urls=True)
        assert not decision.allowed
        assert "link-local" in decision.reason

    async def test_a_node_address_is_refused(self):
        decision = await check("http://192.168.1.10:10250", node_ips=["192.168.1.10"])
        assert not decision.allowed
        assert "node" in decision.reason


class TestRawUrls:
    async def test_an_ip_literal_is_refused_by_default(self):
        decision = await check("http://192.168.1.10:8006")
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_an_ordinary_hostname_is_refused_by_default(self):
        decision = await check("http://example.com", resolving={"example.com": ["93.184.216.34"]})
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_an_ip_literal_is_allowed_with_the_opt_in(self):
        decision = await check("http://192.168.1.10:8006", allow_raw_urls=True)
        assert decision.allowed, decision.reason

    async def test_metadata_is_still_refused_with_raw_urls_allowed(self):
        decision = await check("http://169.254.169.254", allow_raw_urls=True)
        assert not decision.allowed

    async def test_the_kubernetes_api_is_still_refused_with_raw_urls_allowed(self):
        decision = await check(
            "https://10.96.0.1", kube_service_host="10.96.0.1", allow_raw_urls=True
        )
        assert not decision.allowed

    async def test_a_public_hostname_is_allowed_with_the_opt_in(self):
        decision = await check(
            "http://example.com",
            allow_raw_urls=True,
            resolving={"example.com": ["93.184.216.34"]},
        )
        assert decision.allowed, decision.reason


class TestResolution:
    """A harmless name must not be a way to a refused address."""

    async def test_a_service_name_resolving_to_link_local_is_refused(self):
        decision = await check(
            "http://metadata.default.svc.cluster.local",
            resolving={"metadata.default.svc.cluster.local": ["169.254.169.254"]},
        )
        assert not decision.allowed
        assert "link-local" in decision.reason

    async def test_a_bare_name_resolving_to_a_public_address_is_refused(self):
        decision = await check("http://example.com", resolving={"example.com": ["93.184.216.34"]})
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_a_name_that_does_not_resolve_is_refused(self):
        decision = await check("http://ha", default=None)
        assert not decision.allowed
        assert "cannot resolve" in decision.reason

    async def test_a_private_address_keeps_a_bare_name_allowed(self):
        decision = await check("http://ha", resolving={"ha": ["10.0.0.5"]})
        assert decision.allowed, decision.reason


class TestDetection:
    def test_service_host_means_kubernetes(self):
        assert k8s_targets.in_kubernetes({"KUBERNETES_SERVICE_HOST": "10.96.0.1"})

    def test_install_method_means_kubernetes(self):
        assert k8s_targets.in_kubernetes({"HLE_INSTALL_METHOD": "kubernetes"})

    def test_an_ordinary_host_is_not_kubernetes(self):
        assert not k8s_targets.in_kubernetes({"HLE_INSTALL_METHOD": "venv"})

    def test_from_env_reads_the_settings(self):
        g = k8s_targets.KubernetesTargetGuard.from_env(
            {
                "KUBERNETES_SERVICE_HOST": "10.96.0.1",
                "HLE_CLUSTER_DOMAIN": "k8s.example.com",
                "HLE_ALLOW_RAW_URLS": "true",
                "HLE_NODE_IP": "192.168.1.10",
            }
        )
        assert g._cluster_domain == "k8s.example.com"
        assert g._allow_raw_urls is True
        assert str(g._kube_ip) == "10.96.0.1"
        assert [str(ip) for ip in g._node_ips] == ["192.168.1.10"]

    def test_install_method_override_wins_over_the_classifier(self, monkeypatch):
        import hle_client.agent as agent_mod

        monkeypatch.setenv("HLE_INSTALL_METHOD", "kubernetes")
        monkeypatch.setattr(agent_mod.os.path, "exists", lambda p: True)  # /.dockerenv
        assert agent_mod.detect_install_method() == "kubernetes"


class FakeTunnel:
    def __init__(self, config) -> None:
        self.config = config
        self.connected = False

    async def connect(self) -> None:
        self.connected = True
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def public_url(self) -> str:
        return f"https://{self.config.service_label}.hle.world"


def _client(guard_obj=None) -> tuple[AgentClient, list[FakeTunnel]]:
    created: list[FakeTunnel] = []

    def factory(cfg):
        t = FakeTunnel(cfg)
        created.append(t)
        return t

    client = AgentClient("hlea_test", tunnel_factory=factory, target_guard=guard_obj)
    return client, created


def _spec(label: str, url: str) -> EndpointSpec:
    return EndpointSpec(id=1, label=label, service_url=url)


class TestAgentIntegration:
    """A refused endpoint must not start; it is reported, not raised."""

    async def test_a_refused_endpoint_is_not_started(self):
        client, created = _client(guard())
        await client.reconcile([_spec("api", "https://kubernetes.default.svc")])
        await asyncio.sleep(0)
        assert created == []
        status = client._build_status()
        assert status[0].label == "api"
        assert status[0].connected is False
        assert status[0].error is not None
        assert status[0].error.startswith("refused:")
        assert "target not allowed on kubernetes agents" in status[0].error

    async def test_an_allowed_endpoint_still_starts(self):
        client, created = _client(guard())
        await client.reconcile([_spec("ha", "http://ha.default.svc.cluster.local")])
        await asyncio.sleep(0)
        assert len(created) == 1
        assert created[0].connected is True
        await client._stop_all()

    async def test_a_refusal_is_logged_once_per_reason(self, monkeypatch, caplog):
        client, _ = _client(guard())
        with caplog.at_level("WARNING", logger="hle_client.agent"):
            await client.reconcile([_spec("api", "https://kubernetes.default.svc")])
            await client.reconcile([_spec("api", "https://kubernetes.default.svc")])
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    async def test_no_guard_means_no_validation(self):
        """Outside Kubernetes a LAN IP is still a perfectly good target."""
        client, created = _client(None)
        await client.reconcile([_spec("nas", "http://192.168.1.10:8006")])
        await asyncio.sleep(0)
        assert len(created) == 1
        assert created[0].connected is True
        await client._stop_all()

    async def test_the_guard_is_built_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
        client, created = _client(None)
        assert client._target_guard is not None
        await client.reconcile([_spec("api", "https://10.96.0.1:443")])
        await asyncio.sleep(0)
        assert created == []
