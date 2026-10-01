"""The Kubernetes endpoint target allow-list.

A compromised or mistaken dashboard entry must not turn a cluster agent into a
tunnel to the API server, the cloud metadata service or a node. These are the
unit tests for that refusal; see ``hle_client.k8s_targets``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from hle_client import k8s_targets
from hle_client.agent import AgentClient
from hle_common.agent_protocol import AgentWelcome, EndpointSpec

INTERNAL = "10.96.0.7"
PUBLIC = "93.184.216.34"
NAMESPACE = "default"
# The address the API's own DNS name answers with, distinct from a generic
# service so the refresh the guard does at construction does not taint every
# name that resolves to INTERNAL.
KUBE_API = "10.96.0.1"


def resolver(
    mapping: dict[str, list[str]] | None = None,
    default: str | None = INTERNAL,
    calls=None,
):
    """A stand-in for the event loop's getaddrinfo.

    Names not in *mapping* answer with *default* (an internal address, so a
    well-formed service name is allowed); ``default=None`` makes them fail. The
    API server's own name always answers with :data:`KUBE_API` so the guard's
    startup refresh has something realistic to refuse in every test. Lookups are
    matched with any trailing dot removed, so a mapping key need not know
    whether the guard resolved the relative or absolute spelling. ``calls``
    records every name actually resolved, dot and all.
    """

    async def resolve(host: str) -> list[str]:
        if calls is not None:
            calls.append(host)
        key = host.rstrip(".")
        if key.startswith("kubernetes.default.svc."):
            return [KUBE_API]
        if mapping is not None and key in mapping:
            return mapping[key]
        if default is None:
            raise OSError(f"name or service not known: {host}")
        return [default]

    return resolve


def guard(
    *,
    resolving: dict[str, list[str]] | None = None,
    default: str | None = INTERNAL,
    calls=None,
    **kw,
):
    kw.setdefault("pod_namespace", NAMESPACE)
    return k8s_targets.KubernetesTargetGuard(
        resolver=resolver(resolving, default, calls),
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

    async def test_a_bare_name_is_rewritten_to_its_absolute_fqdn(self):
        calls: list[str] = []
        decision = await check(
            "http://ha", resolving={"ha.default.svc.cluster.local": [INTERNAL]}, calls=calls
        )
        assert decision.allowed, decision.reason
        assert decision.url == "http://ha.default.svc.cluster.local."
        assert calls[-1] == "ha.default.svc.cluster.local."

    async def test_a_two_label_name_is_rewritten_to_its_absolute_fqdn(self):
        calls: list[str] = []
        decision = await check(
            "http://ha.media",
            resolving={"ha.media.svc.cluster.local": [INTERNAL]},
            calls=calls,
        )
        assert decision.allowed, decision.reason
        assert decision.url == "http://ha.media.svc.cluster.local."
        assert calls[-1] == "ha.media.svc.cluster.local."

    async def test_the_svc_suffix_gets_the_cluster_domain(self):
        calls: list[str] = []
        decision = await check(
            "http://ha.media.svc",
            resolving={"ha.media.svc.cluster.local": [INTERNAL]},
            calls=calls,
        )
        assert decision.allowed, decision.reason
        assert decision.url == "http://ha.media.svc.cluster.local."

    async def test_scheme_port_and_path_are_preserved(self):
        decision = await check(
            "https://ha:8443/some/path?q=1",
            resolving={"ha.default.svc.cluster.local": [INTERNAL]},
        )
        assert decision.allowed, decision.reason
        assert decision.url == "https://ha.default.svc.cluster.local.:8443/some/path?q=1"

    async def test_a_custom_cluster_domain_is_honoured(self):
        decision = await check(
            "http://ha.default.svc.k8s.example.com",
            cluster_domain="k8s.example.com",
        )
        assert decision.allowed, decision.reason

    async def test_a_trailing_dot_still_matches(self):
        decision = await check("http://ha.default.svc.cluster.local.")
        assert decision.allowed, decision.reason

    async def test_a_bare_name_is_refused_when_the_pod_namespace_is_unknown(self):
        decision = await check("http://ha", default=None, pod_namespace=None)
        assert not decision.allowed
        assert "namespace" in decision.reason

    async def test_a_name_that_does_not_resolve_is_refused(self):
        decision = await check("http://ha", default=None)
        assert not decision.allowed
        assert "cannot resolve" in decision.reason


class TestSearchPathIsNeverUsed:
    """An ordinary public name must not resolve through the pod search path."""

    async def test_attacker_com_is_refused_by_default(self):
        calls: list[str] = []
        decision = await check(
            "http://attacker.com",
            resolving={"attacker.com": [PUBLIC]},
            default=None,
            calls=calls,
        )
        assert not decision.allowed
        # It was resolved as an in-cluster name, never as the raw external name.
        assert calls[-1] == "attacker.com.svc.cluster.local."
        assert "attacker.com" not in calls

    async def test_a_three_label_external_name_is_a_raw_url(self):
        decision = await check("http://internal.corp.example.com", default=None)
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_a_two_label_name_can_be_opted_in_as_a_raw_url(self):
        calls: list[str] = []
        decision = await check(
            "http://example.com",
            allow_raw_urls=True,
            resolving={"example.com": ["192.168.1.50"]},
            default=None,
            calls=calls,
        )
        assert decision.allowed, decision.reason
        assert calls[-2:] == ["example.com.svc.cluster.local.", "example.com"]


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
        decision = await check(
            "https://10.96.0.1:443", kube_service_host="10.96.0.1", allow_raw_urls=True
        )
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://[fd00:ec2::254]/",
            "http://100.100.100.200/",
            "http://192.0.0.192/",
            "http://168.63.129.16/",
        ],
    )
    async def test_metadata_addresses_are_refused(self, url):
        decision = await check(url, allow_raw_urls=True)
        assert not decision.allowed
        assert "metadata" in decision.reason or "link-local" in decision.reason

    @pytest.mark.parametrize("url", ["http://metadata", "http://metadata.google.internal"])
    async def test_metadata_names_are_refused(self, url):
        decision = await check(url)
        assert not decision.allowed
        assert "metadata" in decision.reason

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://[::1]:8080"])
    async def test_loopback_is_refused(self, url):
        decision = await check(url, allow_raw_urls=True)
        assert not decision.allowed
        assert "loopback" in decision.reason

    @pytest.mark.parametrize("url", ["http://0.0.0.0/", "http://[::]/", "http://0.0.0.1/"])
    async def test_unspecified_and_zero_ranges_are_refused(self, url):
        decision = await check(url, allow_raw_urls=True)
        assert not decision.allowed
        assert "unspecified" in decision.reason

    async def test_the_name_zero_is_refused(self):
        """`0` is not a valid IP literal, so it would otherwise be a service name."""
        decision = await check(
            "http://0/",
            allow_raw_urls=True,
            resolving={"0.default.svc.cluster.local": ["0.0.0.0"]},
        )
        assert not decision.allowed
        assert "unspecified" in decision.reason

    async def test_a_node_address_is_refused(self):
        decision = await check(
            "http://192.168.1.10:10250", node_ips=["192.168.1.10"], allow_raw_urls=True
        )
        assert not decision.allowed
        assert "node" in decision.reason


class TestIpv6Wrappers:
    """A wrapper must not smuggle a refused IPv4 past the list."""

    @pytest.mark.parametrize(
        "host",
        [
            "::ffff:169.254.169.254",  # ipv4-mapped
            "::169.254.169.254",  # ipv4-compatible
            "2002:a9fe:a9fe::",  # 6to4
            "64:ff9b::a9fe:a9fe",  # NAT64 /96
            "64:ff9b:1:a9fe:a9:fe00::",  # NAT64 /48 (RFC 6052)
            "2001:0:4136:e378:8000:63bf:5601:5601",  # Teredo, client 169.254.169.254
        ],
    )
    async def test_a_wrapped_metadata_address_is_refused(self, host):
        decision = await check(f"http://[{host}]/", allow_raw_urls=True)
        assert not decision.allowed, decision.reason
        assert "metadata" in decision.reason or "link-local" in decision.reason
        assert "via" in decision.reason

    async def test_a_wrapped_api_address_is_refused(self):
        decision = await check(
            "http://[::ffff:10.96.0.1]/",
            kube_service_host="10.96.0.1",
            allow_raw_urls=True,
        )
        assert not decision.allowed
        assert "kubernetes API" in decision.reason


class TestRawUrls:
    async def test_an_ip_literal_is_refused_by_default(self):
        decision = await check("http://192.168.1.10:8006")
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_an_ip_literal_is_allowed_with_the_opt_in(self):
        decision = await check("http://192.168.1.10:8006", allow_raw_urls=True)
        assert decision.allowed, decision.reason

    async def test_metadata_is_still_refused_with_raw_urls_allowed(self):
        decision = await check("http://169.254.169.254", allow_raw_urls=True)
        assert not decision.allowed

    async def test_a_public_hostname_is_allowed_with_the_opt_in(self):
        decision = await check(
            "http://203.0.113.10",
            allow_raw_urls=True,
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

    async def test_a_service_name_resolving_to_an_api_address_is_refused(self):
        decision = await check(
            "http://admin.default.svc.cluster.local",
            resolving={"admin.default.svc.cluster.local": ["10.96.0.1"]},
            kube_service_host="10.96.0.1",
        )
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    async def test_a_public_address_for_a_service_name_is_refused(self):
        decision = await check(
            "http://ha.default.svc.cluster.local",
            resolving={"ha.default.svc.cluster.local": [PUBLIC]},
        )
        assert not decision.allowed
        assert "raw URL" in decision.reason

    async def test_a_private_address_keeps_a_bare_name_allowed(self):
        decision = await check(
            "http://ha",
            resolving={"ha.default.svc.cluster.local": ["10.0.0.5"]},
        )
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
                "HLE_POD_NAMESPACE": "media",
            }
        )
        assert g._cluster_domain == "k8s.example.com"
        assert g._allow_raw_urls is True
        assert str(g._kube_ip) == "10.96.0.1"
        assert [str(ip) for ip in g._node_ips] == ["192.168.1.10"]
        assert g._pod_namespace == "media"

    def test_from_env_reads_the_namespace_file(self, tmp_path, monkeypatch):
        namespace_file = tmp_path / "namespace"
        namespace_file.write_text("media\n")
        monkeypatch.setattr(k8s_targets, "SERVICEACCOUNT_NAMESPACE_PATH", str(namespace_file))
        g = k8s_targets.KubernetesTargetGuard.from_env({"KUBERNETES_SERVICE_HOST": "10.96.0.1"})
        assert g._pod_namespace == "media"

    def test_from_env_seeds_the_api_set_from_the_service_port_address(self):
        import ipaddress

        g = k8s_targets.KubernetesTargetGuard.from_env(
            {
                # The hostname form on managed clusters: the address literal is
                # only in the Service port variable until DNS answers.
                "KUBERNETES_SERVICE_HOST": "api.aks.example",
                "KUBERNETES_PORT_443_TCP_ADDR": "10.96.0.1",
            }
        )
        assert ipaddress.ip_address("10.96.0.1") in g._api_ips

    def test_install_method_override_wins_over_the_classifier(self, monkeypatch):
        import hle_client.agent as agent_mod

        monkeypatch.setenv("HLE_INSTALL_METHOD", "kubernetes")
        monkeypatch.setattr(agent_mod.os.path, "exists", lambda p: True)  # /.dockerenv
        assert agent_mod.detect_install_method() == "kubernetes"


class TestFirepuncherGate:
    def test_firepuncher_is_available_outside_kubernetes(self):
        assert k8s_targets.firepuncher_enabled({})

    def test_firepuncher_is_disabled_by_default_in_kubernetes(self):
        assert not k8s_targets.firepuncher_enabled({"KUBERNETES_SERVICE_HOST": "10.96.0.1"})

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_firepuncher_can_be_enabled_in_kubernetes(self, value):
        assert k8s_targets.firepuncher_enabled(
            {"KUBERNETES_SERVICE_HOST": "10.96.0.1", "HLE_FIREPUNCHER_ENABLED": value}
        )


class TestCheckHost:
    """Firepuncher dials a socket, so the guard must return the checked IP."""

    async def test_an_allowed_ip_is_returned(self):
        decision = await guard(allow_raw_urls=True).check_host("192.168.1.10", 22)
        assert decision.allowed, decision.reason
        assert decision.address == "192.168.1.10"

    async def test_a_service_name_is_resolved_to_the_checked_ip(self):
        calls: list[str] = []
        g = guard(resolving={"ha.default.svc.cluster.local": [INTERNAL]}, calls=calls)
        decision = await g.check_host("ha", 22)
        assert decision.allowed, decision.reason
        assert decision.address == INTERNAL
        assert calls[-1] == "ha.default.svc.cluster.local."

    async def test_metadata_is_refused(self):
        decision = await guard(allow_raw_urls=True).check_host("169.254.169.254", 80)
        assert not decision.allowed
        assert "link-local" in decision.reason

    async def test_the_api_address_is_refused(self):
        decision = await guard(allow_raw_urls=True, kube_service_host="10.96.0.1").check_host(
            "10.96.0.1", 443
        )
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    async def test_loopback_is_refused(self):
        decision = await guard(allow_raw_urls=True).check_host("127.0.0.1", 22)
        assert not decision.allowed
        assert "loopback" in decision.reason


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


class _WelcomeWs:
    """A control connection that answers the hello with a welcome, then ends."""

    def __init__(self, welcome: str) -> None:
        self.sent: list[str] = []
        self._welcome = welcome

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def recv(self) -> str:
        return self._welcome

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


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

    async def test_the_tunnel_gets_the_canonicalised_url(self):
        client, created = _client(guard(resolving={"ha.default.svc.cluster.local": [INTERNAL]}))
        await client.reconcile([_spec("ha", "http://ha")])
        await asyncio.sleep(0)
        assert len(created) == 1
        assert created[0].config.service_url == "http://ha.default.svc.cluster.local."
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

    async def test_the_initial_api_refresh_is_awaited_before_the_first_reconcile(self, monkeypatch):
        import hle_client.agent as agent_mod

        events: list[str] = []

        async def slow(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                await asyncio.sleep(0.05)
                return [KUBE_API]
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="api.aks.example", resolver=slow, pod_namespace=NAMESPACE
        )
        original_wait = g.wait_for_api_refresh

        async def spy_wait(timeout: float | None = None) -> None:
            events.append("refresh")
            await original_wait(timeout)

        monkeypatch.setattr(g, "wait_for_api_refresh", spy_wait)

        original_check = g.check

        async def spy_check(url: str):
            events.append("check")
            return await original_check(url)

        monkeypatch.setattr(g, "check", spy_check)

        client, _created = _client(g)
        welcome = AgentWelcome(
            agent_public_id="pub",
            base_domain="hle.world",
            endpoints=[_spec("ha", "http://ha:8000")],
        ).model_dump_json()
        ws = _WelcomeWs(welcome)
        monkeypatch.setattr(agent_mod.websockets, "connect", lambda *a, **kw: ws)

        async def no_discovery(_ws) -> None:
            return None

        monkeypatch.setattr(client, "_report_discovery", no_discovery)
        await client._connect_once()

        assert events[0] == "refresh"  # the wait ran before any endpoint check
        assert "check" in events
        await client._stop_all()

    async def test_validation_runs_concurrently(self):
        """One slow resolution must not serialise every other endpoint."""
        delay = 0.2

        async def slow(host: str) -> list[str]:
            await asyncio.sleep(delay)
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                return [KUBE_API]
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        client, created = _client(g)
        specs = [_spec(f"svc{i}", f"http://svc{i}.default.svc.cluster.local") for i in range(8)]
        started = asyncio.get_running_loop().time()
        await client.reconcile(specs)
        elapsed = asyncio.get_running_loop().time() - started
        assert len(created) == 8
        assert elapsed < delay * len(specs) / 2, elapsed
        await client._stop_all()


class _SendWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


class TestPreflightGuard:
    async def test_a_refused_preflight_does_no_network_io(self, monkeypatch):
        from hle_client import preflight as preflight_mod

        called: list[str] = []

        async def explode(*args, **kwargs):
            called.append("run")
            raise AssertionError("network I/O attempted")

        monkeypatch.setattr(preflight_mod, "run_preflight", explode)

        client, _ = _client(guard())
        ws = _SendWs()
        await client._run_preflight(
            {
                "type": "preflight_request",
                "request_id": "r1",
                "service_url": "https://169.254.169.254/latest/meta-data/",
            },
            ws,
        )

        assert called == []
        report = json.loads(ws.sent[0])
        assert report["request_id"] == "r1"
        assert report["error"]
        assert "target not allowed" in report["error"]

    async def test_an_allowed_preflight_probes_the_canonical_url(self, monkeypatch):
        from hle_client import preflight as preflight_mod
        from hle_common.preflight import PreflightReport

        seen: list[str] = []
        seen_host: list[str | None] = []

        async def fake_run(service_url, **kwargs):
            seen.append(service_url)
            seen_host.append(kwargs.get("host_header"))
            return PreflightReport(request_id=kwargs.get("request_id", ""), service_url=service_url)

        monkeypatch.setattr(preflight_mod, "run_preflight", fake_run)

        client, _ = _client(guard(resolving={"ha.default.svc.cluster.local": [INTERNAL]}))
        ws = _SendWs()
        await client._run_preflight(
            {
                "type": "preflight_request",
                "request_id": "r2",
                "service_url": "http://ha:8000",
            },
            ws,
        )

        assert seen == ["http://ha.default.svc.cluster.local.:8000"]
        # The tunnel presents the original authority as Host, not the FQDN.
        assert seen_host == ["ha:8000"]
        assert json.loads(ws.sent[0])["request_id"] == "r2"


class TestScopedAddresses:
    """An IPv6 zone id must not make a refused address look different."""

    async def test_a_scoped_literal_is_refused_even_when_it_is_the_node(self):
        decision = await check("http://[fd00::10%251]/", node_ips=["fd00::10"], allow_raw_urls=True)
        assert not decision.allowed, decision.reason
        assert "zone" in decision.reason or "scope" in decision.reason

    async def test_a_scoped_firepuncher_target_is_refused(self):
        decision = await guard(allow_raw_urls=True, node_ips=["fd00::10"]).check_host(
            "fd00::10%1", 22
        )
        assert not decision.allowed, decision.reason
        assert "zone" in decision.reason or "scope" in decision.reason

    async def test_a_scoped_dns_answer_is_refused(self):
        decision = await check(
            "http://ha.default.svc.cluster.local",
            resolving={"ha.default.svc.cluster.local": ["fd00::10%1"]},
        )
        assert not decision.allowed, decision.reason
        assert "zone" in decision.reason or "scope" in decision.reason

    def test_comparison_strips_a_scope_as_defence_in_depth(self):
        import ipaddress

        assert k8s_targets._same_ip(
            ipaddress.ip_address("fd00::10%1"), ipaddress.ip_address("fd00::10")
        )


class TestLookAlikeApiNames:
    """A Service name must not reach the API just because of how it resolves."""

    async def test_a_hostname_service_host_resolves_into_the_api_set(self):
        mapping = {
            "api.aks.example": ["203.0.113.9"],
            "kubernetes.default.svc.cluster.local": ["10.96.0.1"],
            "lookalike.default.svc.cluster.local": ["203.0.113.9"],
        }
        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="api.aks.example",
            resolver=resolver(mapping, default=None),
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )
        # The resolved API addresses are refreshed in the background; a check
        # never waits, so wait explicitly for the set to be populated.
        await g.wait_for_api_refresh()
        decision = await g.check("http://lookalike.default.svc.cluster.local")
        assert not decision.allowed, decision.reason
        assert "kubernetes API" in decision.reason

    def test_a_guard_built_outside_a_loop_still_waits_for_the_first_refresh(self):
        """The CLI builds the agent (and its guard) before any loop runs."""
        mapping = {
            "api.aks.example": ["10.240.0.4"],
            "kubernetes.default.svc.cluster.local": ["10.96.0.1"],
            "lookalike.default.svc.cluster.local": ["10.240.0.4"],
        }
        g = k8s_targets.KubernetesTargetGuard(  # no running loop here
            kube_service_host="api.aks.example",
            resolver=resolver(mapping, default=None),
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )

        async def first_reconcile() -> k8s_targets.TargetDecision:
            await g.wait_for_api_refresh(timeout=5)
            return await g.check("http://lookalike.default.svc.cluster.local")

        decision = asyncio.run(first_reconcile())
        assert not decision.allowed, decision.reason

    async def test_the_hostname_form_service_host_is_refused_by_itself(self):
        mapping = {
            "api.aks.example": ["203.0.113.9"],
            "kubernetes.default.svc.cluster.local": ["10.96.0.1"],
        }
        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="api.aks.example",
            resolver=resolver(mapping, default=None),
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )
        decision = await g.check("https://api.aks.example")
        assert not decision.allowed, decision.reason
        assert "kubernetes API" in decision.reason

    async def test_the_service_port_address_is_refused_before_any_refresh(self):
        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="api.aks.example",
            kube_port_addr="10.96.0.1",
            resolver=resolver({}, default=None),
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )
        decision = await g.check("https://10.96.0.1:443")
        assert not decision.allowed, decision.reason
        assert "kubernetes API" in decision.reason

    async def test_fullwidth_names_are_refused(self):
        decision = await check("http://ｋｕｂｅｒｎｅｔｅｓ.default.svc.cluster.local")
        assert not decision.allowed, decision.reason
        assert "valid hostname" in decision.reason

    async def test_srv_style_names_are_refused(self):
        decision = await check("http://_https._tcp.web.default.svc.cluster.local")
        assert not decision.allowed, decision.reason
        assert "SRV" in decision.reason


class TestTrailingDot:
    """Fully-qualified names are resolved absolutely, and handed on as such."""

    async def test_the_resolver_is_given_an_absolute_name(self):
        calls: list[str] = []
        decision = await check(
            "http://ha",
            resolving={"ha.default.svc.cluster.local": [INTERNAL]},
            calls=calls,
        )
        assert decision.allowed, decision.reason
        assert calls[-1] == "ha.default.svc.cluster.local."
        assert decision.url == "http://ha.default.svc.cluster.local."

    async def test_the_transports_accept_a_trailing_dot_hostname(self):
        import httpx
        import websockets

        async def http_handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
        http_port = http_server.sockets[0].getsockname()[1]

        async def ws_handler(ws):
            await ws.send("ok")
            await ws.close()

        ws_server = await websockets.serve(ws_handler, "127.0.0.1", 0)
        ws_port = ws_server.sockets[0].getsockname()[1]

        try:
            async with http_server:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://localhost.:{http_port}/")
                assert response.status_code == 200
            async with websockets.connect(f"ws://localhost.:{ws_port}") as ws:
                assert await ws.recv() == "ok"
        finally:
            ws_server.close()
            await ws_server.wait_closed()


class TestUpstreamHost:
    """Canonicalising the name must not change the Host the upstream sees."""

    async def test_the_original_authority_is_kept_as_the_upstream_host(self):
        client, created = _client(guard(resolving={"ha.default.svc.cluster.local": [INTERNAL]}))
        await client.reconcile([_spec("ha", "http://ha:8000")])
        await asyncio.sleep(0)
        cfg = created[0].config
        assert cfg.service_url == "http://ha.default.svc.cluster.local.:8000"
        assert cfg.upstream_host == "ha:8000"
        # A cluster guard is active: environment proxies are never consulted.
        assert cfg.trust_env is False
        await client._stop_all()


class _FakeFpReader:
    async def read(self, _n: int) -> bytes:
        return b""


class _FakeFpWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class TestFirepuncherOpenIsNonBlocking:
    async def test_a_slow_open_does_not_stall_the_control_loop(self, monkeypatch):
        from hle_client import firepuncher
        from hle_client.firepuncher import FpAgentSide
        from hle_common.fp_protocol import ForwardRule

        delay = 0.2

        async def slow(host: str) -> list[str]:
            await asyncio.sleep(delay)
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                return [KUBE_API]
            return [INTERNAL]

        async def fake_open(_host, _port):
            return _FakeFpReader(), _FakeFpWriter()

        monkeypatch.setattr(firepuncher.asyncio, "open_connection", fake_open)

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        client, _ = _client(g)
        out = _SendWs()
        client._fp = FpAgentSide(
            send=out.send, rules=[ForwardRule(host="ha")], enabled=True, target_guard=g
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        await client._handle_message(
            json.dumps(
                {"type": "fp_open", "stream_id": "s1", "target_host": "ha", "target_port": 22}
            )
        )
        elapsed = loop.time() - started
        assert elapsed < delay / 2, elapsed

        await asyncio.sleep(delay * 4)
        assert any(json.loads(frame)["type"] == "fp_ready" for frame in out.sent)


class TestFirepuncherOrdering:
    """A stream's data and close wait behind its own (possibly slow) open."""

    @staticmethod
    def _agent(monkeypatch, out):
        from hle_client import firepuncher
        from hle_client.firepuncher import FpAgentSide
        from hle_common.fp_protocol import ForwardRule

        order: list = []

        class Reader:
            async def read(self, _n: int) -> bytes:
                # Block so the stream stays open while the test writes to it.
                await asyncio.Event().wait()
                return b""  # pragma: no cover

        class Writer:
            def write(self, data) -> None:
                order.append(("write", bytes(data)))

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def fake_open(host, _port):
            order.append(("open", host))
            return Reader(), Writer()

        monkeypatch.setattr(firepuncher.asyncio, "open_connection", fake_open)

        async def slow(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                return [KUBE_API]
            await asyncio.sleep(0.05)
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        client, _ = _client(g)
        client._fp = FpAgentSide(
            send=out.send, rules=[ForwardRule(host="ha")], enabled=True, target_guard=g
        )
        return client, order

    async def test_data_after_open_waits_for_the_open(self, monkeypatch):
        from hle_common.fp_protocol import FpData

        out = _SendWs()
        client, order = self._agent(monkeypatch, out)

        await client._handle_message(
            json.dumps(
                {"type": "fp_open", "stream_id": "s1", "target_host": "ha", "target_port": 22}
            )
        )
        await client._handle_message(FpData.of("s1", b"PING").model_dump_json())

        assert order[0][0] == "open"
        assert order[1] == ("write", b"PING")
        assert any(json.loads(frame)["type"] == "fp_ready" for frame in out.sent)
        await client._fp.close_all()

    async def test_close_cancels_a_pending_open(self, monkeypatch):
        from hle_common.fp_protocol import FpClose

        async def slow(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                return [KUBE_API]
            await asyncio.sleep(30)
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        client, _ = _client(g)
        out = _SendWs()
        from hle_client.firepuncher import FpAgentSide
        from hle_common.fp_protocol import ForwardRule

        client._fp = FpAgentSide(
            send=out.send, rules=[ForwardRule(host="ha")], enabled=True, target_guard=g
        )

        await client._handle_message(
            json.dumps(
                {"type": "fp_open", "stream_id": "s1", "target_host": "ha", "target_port": 22}
            )
        )
        assert "s1" in client._fp_open_tasks

        await client._handle_message(FpClose(stream_id="s1").model_dump_json())

        assert "s1" not in client._fp_open_tasks
        assert not client._fp._pending_opens
        assert not any(json.loads(frame)["type"] == "fp_ready" for frame in out.sent)


class TestApiIpRefresh:
    """The API address set refreshes in the background and stamps only success."""

    async def test_a_failed_refresh_does_not_stamp_and_backs_off(self):
        async def failing(host: str) -> list[str]:
            raise OSError(f"no dns for {host}")

        g = k8s_targets.KubernetesTargetGuard(resolver=failing, pod_namespace=NAMESPACE)
        await g.wait_for_api_refresh()

        assert g._api_ips_refreshed_at is None
        assert g._api_refresh_failures == 1
        # A retry is armed for the first backoff interval, not the 5-minute one.
        assert g._api_refresh_handle is not None
        # The API is still refused by name while the address set is empty.
        decision = await g.check("https://kubernetes.default.svc")
        assert not decision.allowed
        assert "kubernetes API" in decision.reason

    async def test_a_full_success_stamps_the_time(self):
        g = guard()
        await g.wait_for_api_refresh()
        assert g._api_ips_refreshed_at is not None
        assert g._api_refresh_failures == 0

    async def test_a_partial_refresh_merges_the_answers_that_resolved(self):
        import ipaddress

        async def partial(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                return [KUBE_API]
            raise OSError("no dns")

        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="api.aks.example",
            resolver=partial,
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )
        await g.wait_for_api_refresh()

        # Only the name that resolved is added; the time is not stamped because
        # the sibling name failed, and a retry is armed.
        assert ipaddress.ip_address(KUBE_API) in g._api_ips
        assert g._api_ips_refreshed_at is None
        assert g._api_refresh_failures == 1
        assert g._api_refresh_handle is not None
        decision = await g.check("https://10.96.0.1:443")
        assert not decision.allowed, decision.reason
        assert "kubernetes API" in decision.reason

    async def test_a_bounded_wait_does_not_cancel_the_refresh(self):
        release = asyncio.Event()

        async def slow(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                await release.wait()
                return [KUBE_API]
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        task = g._api_refresh_task
        assert task is not None

        loop = asyncio.get_running_loop()
        started = loop.time()
        await g.wait_for_api_refresh(timeout=0.05)
        assert loop.time() - started < 0.2
        # The wait timed out but the refresh itself was not cancelled.
        assert not task.cancelled()

        release.set()
        await g.wait_for_api_refresh()
        assert not task.cancelled()
        assert g._api_ips_refreshed_at is not None

    async def test_failed_refreshes_back_off_two_five_fifteen_then_sixty(self):
        async def failing(host: str) -> list[str]:
            raise OSError(f"no dns for {host}")

        g = k8s_targets.KubernetesTargetGuard(resolver=failing, pod_namespace=NAMESPACE)
        # Stop the task bootstrap scheduled at construction so the calls below
        # are the only refreshes and the sequence is exact.
        if g._api_refresh_task is not None:
            g._api_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await g._api_refresh_task

        delays: list[float] = []
        g._schedule_api_refresh = delays.append  # type: ignore[method-assign]

        for _ in range(5):
            await g._refresh_api_ips()

        assert delays == [2.0, 5.0, 15.0, 60.0, 60.0]
        assert g._api_ips_refreshed_at is None

    async def test_a_check_does_not_wait_for_a_slow_refresh(self):
        release = asyncio.Event()

        async def slow(host: str) -> list[str]:
            if host.rstrip(".").startswith("kubernetes.default.svc."):
                await release.wait()
                return [KUBE_API]
            return [INTERNAL]

        g = k8s_targets.KubernetesTargetGuard(resolver=slow, pod_namespace=NAMESPACE)
        loop = asyncio.get_running_loop()
        started = loop.time()
        decision = await g.check("http://ha.default.svc.cluster.local")
        elapsed = loop.time() - started

        assert decision.allowed, decision.reason
        assert elapsed < 0.2, elapsed
        release.set()
        await g.wait_for_api_refresh()

    async def test_the_hostname_service_host_is_refused_in_any_spelling(self):
        g = k8s_targets.KubernetesTargetGuard(
            kube_service_host="API.AKS.Example.",
            resolver=resolver({}, default=None),
            pod_namespace=NAMESPACE,
            allow_raw_urls=True,
        )
        for url in (
            "https://api.aks.example",
            "https://API.AKS.EXAMPLE.",
            "https://api.aks.example:443",
        ):
            decision = await g.check(url)
            assert not decision.allowed, url
            assert "kubernetes API" in decision.reason
