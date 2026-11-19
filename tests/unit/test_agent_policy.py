"""The agent's local, operator-owned policy.

Outside Kubernetes the agent used to take the relay's word for everything:
which upstreams became endpoints, which URLs a preflight probed, and the whole
raw-TCP forward allowlist — replaced wholesale and live. A server-side
authorization bug, or a compromised relay, then had the agent's LAN and the
internet behind it. These tests pin the local ceiling that must not move.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from hle_client import config, k8s_targets
from hle_client.agent import AgentClient
from hle_client.firepuncher import FpAgentSide
from hle_common.agent_protocol import AgentStateSync, EndpointSpec
from hle_common.fp_protocol import LOCAL_NETWORKS, ForwardRule, default_rules

PUBLIC = "93.184.216.34"
METADATA = "169.254.169.254"
LOCAL = "192.168.1.50"


class FakeTunnel:
    def __init__(self, config) -> None:
        self.config = config
        self.connected = False
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connected = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connected = False
            raise

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def public_url(self) -> str | None:
        return None


def policy(**kw) -> config.AgentPolicy:
    base: dict = dict(
        allowed_targets=default_rules(),
        firepuncher=False,
        preflight=True,
        remote_update=True,
    )
    base.update(kw)
    return config.AgentPolicy(**base)


def make_client(policy_obj: config.AgentPolicy | None = None):
    created: list[FakeTunnel] = []

    def factory(cfg):
        tunnel = FakeTunnel(cfg)
        created.append(tunnel)
        return tunnel

    client = AgentClient("hlea_test", tunnel_factory=factory, policy=policy_obj or policy())
    return client, created


def spec(label: str, url: str) -> EndpointSpec:
    return EndpointSpec(id=1, label=label, service_url=url)


class _SendWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


class TestLoadPolicy:
    def test_defaults_to_the_protocol_default_and_off_firepuncher(self):
        loaded = config.load_agent_policy()
        assert loaded.allowed_targets == default_rules()
        assert loaded.firepuncher is None
        assert loaded.preflight is True
        assert loaded.remote_update is True

    def test_env_allowed_targets_override(self, monkeypatch):
        monkeypatch.setenv("HLE_ALLOWED_TARGETS", "@local, localhost")
        loaded = config.load_agent_policy()
        assert loaded.allowed_targets == [
            ForwardRule(host=LOCAL_NETWORKS),
            ForwardRule(host="localhost"),
        ]

    def test_agent_toml_allowed_targets(self):
        path = config.agent_config_path()
        path.write_text('allowed_targets = ["@local", "10.0.0.0/8:22"]\n')
        loaded = config.load_agent_policy()
        assert loaded.allowed_targets == [
            ForwardRule(host=LOCAL_NETWORKS),
            ForwardRule(host="10.0.0.0/8", port=22),
        ]

    @pytest.mark.parametrize(
        ("env_name", "key"),
        [("HLE_FIREPUNCHER", "firepuncher"), ("HLE_PREFLIGHT", "preflight")],
    )
    def test_switches_are_read_from_env(self, monkeypatch, env_name, key):
        monkeypatch.setenv(env_name, "off")
        assert getattr(config.load_agent_policy(), key) is False
        monkeypatch.setenv(env_name, "on")
        assert getattr(config.load_agent_policy(), key) is True

    def test_an_empty_allowed_targets_allows_nothing(self, monkeypatch):
        monkeypatch.setenv("HLE_ALLOWED_TARGETS", "")
        assert config.load_agent_policy().allowed_targets == []


class TestIntersectRules:
    def test_default_route_is_dropped(self):
        kept = k8s_targets.intersect_rules([ForwardRule(host="0.0.0.0/0")], default_rules())
        assert kept == []

    def test_a_rule_kept_only_where_the_policy_covers_it(self):
        kept = k8s_targets.intersect_rules(
            [ForwardRule(host="0.0.0.0/0"), ForwardRule(host=LOCAL_NETWORKS)],
            default_rules(),
        )
        assert kept == [ForwardRule(host=LOCAL_NETWORKS)]

    def test_an_explicitly_allowed_public_rule_survives(self):
        kept = k8s_targets.intersect_rules(
            [ForwardRule(host="203.0.113.7")],
            default_rules() + [ForwardRule(host="203.0.113.0/24")],
        )
        assert kept == [ForwardRule(host="203.0.113.7")]

    def test_a_narrower_policy_drops_a_broader_server_rule(self):
        kept = k8s_targets.intersect_rules(
            [ForwardRule(host="192.168.0.0/16")],
            [ForwardRule(host="192.168.1.0/24")],
        )
        assert kept == []

    def test_named_hosts_must_be_named_locally(self):
        kept = k8s_targets.intersect_rules([ForwardRule(host="nas.lan")], default_rules())
        assert kept == []
        kept = k8s_targets.intersect_rules(
            [ForwardRule(host="nas.lan")], [ForwardRule(host="nas.lan")]
        )
        assert kept == [ForwardRule(host="nas.lan")]

    def test_ports_narrow_correctly(self):
        assert k8s_targets.intersect_rules(
            [ForwardRule(host="10.0.0.5", port=22)],
            [ForwardRule(host="10.0.0.0/8")],
        ) == [ForwardRule(host="10.0.0.5", port=22)]
        assert (
            k8s_targets.intersect_rules(
                [ForwardRule(host="10.0.0.5")],
                [ForwardRule(host="10.0.0.0/8", port=22)],
            )
            == []
        )

    def test_an_empty_policy_allows_nothing(self):
        assert k8s_targets.intersect_rules(default_rules(), []) == []


class TestStateSyncKeepsThePolicy:
    async def test_default_route_rules_do_not_broaden_the_allowlist(self):
        client, _ = make_client()
        sync = AgentStateSync(endpoints=[], forward_rules=[ForwardRule(host="0.0.0.0/0")])
        await client._handle_message(sync.model_dump_json())
        assert client._forward_rules == []

    async def test_the_session_allowlist_is_intersected_not_replaced(self):
        client, _ = make_client()
        ws = _SendWs()
        client._fp = FpAgentSide(
            send=ws.send,
            rules=list(client._policy.allowed_targets),
            enabled=False,
            target_guard=client._policy_guard,
        )
        sync = AgentStateSync(
            endpoints=[],
            forward_rules=[ForwardRule(host="0.0.0.0/0"), ForwardRule(host=LOCAL_NETWORKS)],
        )
        await client._handle_message(sync.model_dump_json())
        assert client._forward_rules == [ForwardRule(host=LOCAL_NETWORKS)]
        assert client._fp.rules == [ForwardRule(host=LOCAL_NETWORKS)]


class TestEndpointPolicy:
    async def test_an_endpoint_to_the_metadata_service_is_refused(self):
        client, created = make_client()
        await client.reconcile([spec("meta", f"http://{METADATA}/latest/meta-data/")])
        await asyncio.sleep(0)
        assert created == []
        error = client._failed["meta"]
        assert error.startswith("refused:")
        assert "local policy" in error

    async def test_an_endpoint_to_a_public_ip_is_refused(self):
        client, created = make_client()
        await client.reconcile([spec("pub", f"http://{PUBLIC}:8080")])
        await asyncio.sleep(0)
        assert created == []
        assert "local policy" in client._failed["pub"]

    async def test_an_endpoint_on_the_local_network_is_allowed(self):
        client, created = make_client()
        await client.reconcile([spec("nas", f"http://{LOCAL}:8006")])
        await asyncio.sleep(0)
        assert len(created) == 1
        assert created[0].connected is True
        await client._stop_all()

    async def test_a_hostname_resolving_to_public_is_refused(self):
        client, _ = make_client()
        client._policy_guard = k8s_targets.LocalTargetGuard(
            allowed=default_rules(), resolver=_resolver({"evil.example": [PUBLIC]})
        )
        created: list[FakeTunnel] = []

        def factory(cfg):
            created.append(FakeTunnel(cfg))
            return created[-1]

        client._tunnel_factory = factory
        await client.reconcile([spec("evil", "http://evil.example:80")])
        await asyncio.sleep(0)
        assert created == []
        assert "local policy" in client._failed["evil"]


class TestPreflightPolicy:
    async def test_a_refused_preflight_does_no_network_io(self, monkeypatch):
        from hle_client import preflight as preflight_mod

        called: list[str] = []

        async def explode(*args, **kwargs):
            called.append("run")
            raise AssertionError("network I/O attempted")

        monkeypatch.setattr(preflight_mod, "run_preflight", explode)

        client, _ = make_client()
        ws = _SendWs()
        await client._run_preflight(
            {
                "type": "preflight_request",
                "request_id": "r1",
                "service_url": f"http://{METADATA}/latest/meta-data/",
            },
            ws,
        )
        assert called == []
        report = json.loads(ws.sent[0])
        assert report["request_id"] == "r1"
        assert "local policy" in report["error"]

    async def test_a_public_preflight_is_refused(self, monkeypatch):
        from hle_client import preflight as preflight_mod

        async def explode(*args, **kwargs):
            raise AssertionError("network I/O attempted")

        monkeypatch.setattr(preflight_mod, "run_preflight", explode)

        client, _ = make_client()
        ws = _SendWs()
        await client._run_preflight(
            {
                "type": "preflight_request",
                "request_id": "r2",
                "service_url": f"http://{PUBLIC}",
            },
            ws,
        )
        assert json.loads(ws.sent[0])["error"]

    async def test_preflight_off_refuses_before_any_probe(self, monkeypatch):
        from hle_client import preflight as preflight_mod

        async def explode(*args, **kwargs):
            raise AssertionError("network I/O attempted")

        monkeypatch.setattr(preflight_mod, "run_preflight", explode)

        client, _ = make_client(policy(preflight=False))
        ws = _SendWs()
        await client._run_preflight(
            {
                "type": "preflight_request",
                "request_id": "r3",
                "service_url": "http://localhost:8000",
            },
            ws,
        )
        report = json.loads(ws.sent[0])
        assert "disabled by the local policy" in report["error"]


class TestFirepuncherPolicy:
    def test_firepuncher_unset_keeps_the_existing_default_on(self, monkeypatch):
        # Unset keeps the behaviour agents shipped with, so an upgrade does not
        # cut existing forwards; allowed_targets still bounds every forward.
        monkeypatch.delenv("HLE_FIREPUNCHER", raising=False)
        client, _ = make_client(policy(firepuncher=None))
        assert client._firepuncher_enabled is True

    def test_firepuncher_on_when_the_operator_allows_it(self, monkeypatch):
        monkeypatch.setenv("HLE_FIREPUNCHER", "on")
        client, _ = make_client(policy(firepuncher=True))
        assert client._firepuncher_enabled is True

    async def test_firepuncher_off_refuses_fp_open(self):
        client, _ = make_client(policy(firepuncher=False))
        assert client._firepuncher_enabled is False

        out: list[str] = []

        async def send(raw: str) -> None:
            out.append(raw)

        client._fp = FpAgentSide(
            send=send,
            rules=list(client._policy.allowed_targets),
            enabled=client._firepuncher_enabled,
            target_guard=client._policy_guard,
        )
        await client._handle_message(
            json.dumps(
                {
                    "type": "fp_open",
                    "stream_id": "s1",
                    "target_host": "localhost",
                    "target_port": 22,
                }
            )
        )
        for _ in range(50):
            if out:
                break
            await asyncio.sleep(0.01)
        error = json.loads(out[0])
        assert error["type"] == "fp_error"
        assert error["code"] == "not_allowed"
        assert "disabled" in error["message"]


class TestLocalGuardResolveThenDial:
    async def test_a_name_resolving_to_local_is_allowed_and_the_ip_returned(self):
        guard = k8s_targets.LocalTargetGuard(
            allowed=default_rules(), resolver=_resolver({"nas.lan": [LOCAL]})
        )
        decision = await guard.check_host("nas.lan", 22)
        assert decision.allowed, decision.reason
        assert decision.address == LOCAL

    async def test_a_name_resolving_to_a_public_address_is_refused(self):
        guard = k8s_targets.LocalTargetGuard(
            allowed=default_rules(), resolver=_resolver({"evil.example": [PUBLIC]})
        )
        decision = await guard.check_host("evil.example", 22)
        assert not decision.allowed, decision.reason
        assert "local policy" in decision.reason

    async def test_a_name_resolving_to_metadata_is_refused(self):
        guard = k8s_targets.LocalTargetGuard(
            allowed=default_rules(), resolver=_resolver({"metadata.example": [METADATA]})
        )
        decision = await guard.check_host("metadata.example", 22)
        assert not decision.allowed, decision.reason

    async def test_an_unresolvable_name_is_refused_for_a_dial(self):
        guard = k8s_targets.LocalTargetGuard(allowed=default_rules(), resolver=_resolver({}))
        decision = await guard.check_host("nowhere.invalid", 22)
        assert not decision.allowed, decision.reason

    async def test_metadata_is_refused_even_under_the_local_sentinel(self):
        guard = k8s_targets.LocalTargetGuard(allowed=[ForwardRule(host=LOCAL_NETWORKS)])
        decision = await guard.check(f"http://{METADATA}")
        assert not decision.allowed, decision.reason
        assert "cloud metadata" in decision.reason


def _resolver(mapping: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        key = host.rstrip(".")
        if key in mapping:
            return mapping[key]
        raise OSError(f"name or service not known: {host}")

    return resolve
