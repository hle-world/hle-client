"""Operator P3: logs responder, declared-endpoints hook, discovery 1.1 fields."""

from __future__ import annotations

import json
import logging

import pytest

from hle_client import agent_logs, agent_update
from hle_client.agent import AgentClient
from hle_client.discovery.kubernetes import KubernetesProvider
from hle_common.agent_protocol import (
    MAX_LOG_LINE_CHARS,
    MAX_LOG_LINES,
    DeclaredAck,
    DeclaredEndpoint,
    LogsResponse,
)
from tests.unit.test_agent import FakeTunnel, _FakeWs

_WELCOME = json.dumps({"type": "welcome", "agent_public_id": "pub-1", "base_domain": "hle.world"})


class _Ws:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.fixture
def ring():
    buf = agent_update.ensure_log_buffer()
    saved = list(buf.lines)
    buf.lines.clear()
    yield buf
    buf.lines.clear()
    buf.lines.extend(saved)


def _client(**kw) -> AgentClient:
    return AgentClient("hlea_test", tunnel_factory=FakeTunnel, **kw)


class TestRedact:
    @pytest.mark.parametrize(
        "line, secret",
        [
            ("enrolled with hlea_AbCdEf1234567890xyz", "AbCdEf1234567890xyz"),
            ("key hle_0123456789abcdef0123456789abcdef used", "0123456789abcdef0123"),
            ("sent Bearer eyJhbGciOi.payload.sig ok", "eyJhbGciOi"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Proxy-Authorization: Basic cHJveHk6cHc=", "cHJveHk6cHc"),
            ("Cookie: sid=abc123; theme=dark", "abc123"),
            ("Set-Cookie: session=s3cr3tval; HttpOnly", "s3cr3tval"),
            ("X-Api-Key: k3y-value-1", "k3y-value-1"),
            ("GET /x?password=hunter2&a=1", "hunter2"),
            ("GET /x?access_token=abc123&a=1", "abc123"),
            ("login password: hunter2 failed", "hunter2"),
            ("got token: abc123def", "abc123def"),
            ('body {"password": "hunter2", "u": 1}', "hunter2"),
            ('body {"api_key":"abc123"}', "abc123"),
            ("dialing https://admin:s3cret@host.example/path", "s3cret"),
        ],
    )
    def test_masks_secret(self, line, secret):
        out = agent_logs.redact(line)
        assert secret not in out
        assert "[redacted]" in out

    @pytest.mark.parametrize(
        "line",
        [
            "Agent registered: public_id=pub-1 endpoints=2",
            "hle_client.agent connected",
            "hle_common.protocol: frame ok",
            "hle_server hle_tui.plugin loaded",
            "token expired, reconnecting",
            "visit the password reset page",
            "GET https://host.example:8443/path",
            "cookie jar is empty",
        ],
    )
    def test_leaves_ordinary_lines_alone(self, line):
        assert agent_logs.redact(line) == line

    def test_userinfo_keeps_user_and_host(self):
        out = agent_logs.redact("https://admin:s3cret@host.example/p")
        assert out == "https://admin:[redacted]@host.example/p"

    def test_keeps_query_params_after_a_masked_value(self):
        assert agent_logs.redact("/x?token=abc&a=1") == "/x?token=[redacted]&a=1"


class TestLogsResponder:
    async def test_answers_with_the_tail_of_the_ring(self, ring):
        client = _client()
        for i in range(5):
            logging.getLogger("t").warning("line %d", i)
        ws = _Ws()
        await client._handle_message(
            json.dumps({"type": "logs_request", "request_id": "r1", "lines": 3}), ws
        )
        resp = LogsResponse.model_validate(ws.sent[0])
        assert resp.request_id == "r1"
        assert [x.split()[-1] for x in resp.lines] == ["2", "3", "4"]
        assert resp.truncated is True

    async def test_not_truncated_when_everything_fits(self, ring):
        ring.lines.append("only line")
        resp = agent_logs.build_response({"type": "logs_request", "request_id": "r"})
        assert resp.lines == ["only line"]
        assert resp.truncated is False

    async def test_redacts_and_clamps(self, ring):
        ring.lines.append("auth hlea_SuperSecretToken1")
        ring.lines.append("x" * (MAX_LOG_LINE_CHARS + 500))
        resp = agent_logs.build_response({"type": "logs_request", "request_id": "r"})
        assert "SuperSecret" not in resp.lines[0]
        assert len(resp.lines[1]) == MAX_LOG_LINE_CHARS

    async def test_clamps_the_requested_line_count(self, ring):
        ring.lines.extend(f"l{i}" for i in range(MAX_LOG_LINES + 100))
        resp = agent_logs.build_response(
            {"type": "logs_request", "request_id": "r", "lines": 100000}
        )
        assert len(resp.lines) == MAX_LOG_LINES
        assert resp.truncated is True

    async def test_malformed_request_is_ignored(self, ring):
        ws = _Ws()
        await _client()._handle_message(json.dumps({"type": "logs_request"}), ws)
        assert ws.sent == []

    def test_the_ring_holds_two_thousand_lines(self):
        assert agent_update.ensure_log_buffer().lines.maxlen == 2000


class TestCapabilities:
    async def _hello(self, monkeypatch, **kw) -> dict:
        import hle_client.agent as agent_mod

        ws = _FakeWs(_WELCOME)
        monkeypatch.setattr(agent_mod.websockets, "connect", lambda *a, **k: ws)
        monkeypatch.setattr(agent_mod, "active_providers", list)
        client = _client(**kw)

        async def no_discovery(_ws) -> None:
            return None

        monkeypatch.setattr(client, "_report_discovery", no_discovery)
        await client._connect_once()
        return json.loads(ws.sent[0])

    async def test_logs_always_declared_never_by_default(self, monkeypatch):
        caps = (await self._hello(monkeypatch))["capabilities"]
        assert "logs" in caps
        assert "k8s:declared" not in caps

    async def test_declared_only_when_opted_in(self, monkeypatch):
        caps = (await self._hello(monkeypatch, declares_endpoints=True))["capabilities"]
        assert "k8s:declared" in caps


def _decl(label: str = "web") -> DeclaredEndpoint:
    return DeclaredEndpoint(
        label=label, service_url="http://web.ns.svc:80", source_ref="hletunnel:ns/web"
    )


class TestDeclaredEndpoints:
    async def test_sends_when_connected_and_opts_in(self):
        client = _client()
        client._ws = ws = _Ws()
        assert await client.send_declared_endpoints([_decl()], 3) is True
        frame = ws.sent[0]
        assert frame["type"] == "declared_endpoints"
        assert frame["revision"] == 3
        assert frame["endpoints"][0]["label"] == "web"
        assert client._declares_endpoints is True

    async def test_queued_while_disconnected_and_resent_on_welcome(self):
        client = _client()
        assert await client.send_declared_endpoints([_decl()], 1) is False
        await client.send_declared_endpoints([_decl("a"), _decl("b")], 2)
        ws = _Ws()
        client._ws = ws
        await client._after_welcome(ws)
        declared = [m for m in ws.sent if m["type"] == "declared_endpoints"]
        assert len(declared) == 1
        assert declared[0]["revision"] == 2
        assert [e["label"] for e in declared[0]["endpoints"]] == ["a", "b"]

    async def test_nothing_sent_on_welcome_without_a_declaration(self):
        client = _client()
        ws = _Ws()
        client._ws = ws
        await client._after_welcome(ws)
        assert ws.sent == []

    async def test_a_dead_socket_does_not_raise(self):
        class Dead:
            async def send(self, raw: str) -> None:
                raise ConnectionError

        client = _client()
        client._ws = Dead()
        assert await client.send_declared_endpoints([_decl()], 1) is False

    async def test_ack_reaches_sync_and_async_callbacks(self):
        got: list[DeclaredAck] = []

        async def acb(ack: DeclaredAck) -> None:
            got.append(ack)

        msg = json.dumps(
            {
                "type": "declared_ack",
                "revision": 4,
                "endpoints": [{"label": "web", "status": "conflict"}],
            }
        )
        for cb in (got.append, acb):
            await _client(on_declared_ack=cb)._handle_message(msg, _Ws())
        assert [a.revision for a in got] == [4, 4]
        assert got[0].endpoints[0].status == "conflict"

    async def test_ack_without_a_callback_or_with_a_failing_one_is_harmless(self):
        msg = json.dumps({"type": "declared_ack", "revision": 1, "endpoints": []})
        await _client()._handle_message(msg, _Ws())

        def boom(_ack: DeclaredAck) -> None:
            raise RuntimeError

        await _client(on_declared_ack=boom)._handle_message(msg, _Ws())


class TestDiscovery11:
    def _svc(self, port: dict) -> dict:
        return {
            "metadata": {"name": "web", "namespace": "media"},
            "spec": {"clusterIP": "10.0.0.5", "ports": [{"protocol": "TCP", **port}]},
        }

    def test_port_name_and_app_protocol(self):
        item = self._svc({"port": 8080, "name": "Web", "appProtocol": "HTTPS"})
        svc = KubernetesProvider()._to_services(item)[0]
        assert svc.port_name == "Web"
        assert svc.app_protocol == "https"
        assert svc.exposed_by is None

    def test_absent_fields_stay_none(self):
        svc = KubernetesProvider()._to_services(self._svc({"port": 8080}))[0]
        assert svc.port_name is None
        assert svc.app_protocol is None
        assert svc.ready_endpoints is None  # endpoints unknown, not zero

    def test_ready_endpoints_counted_per_port_name(self):
        item = self._svc({"port": 80, "name": "http"})
        ready = {("media", "web"): [("http", 2), ("other", 5)]}
        assert KubernetesProvider()._to_services(item, ready)[0].ready_endpoints == 2

    def test_no_endpoints_object_means_zero(self):
        svc = KubernetesProvider()._to_services(self._svc({"port": 80}), {})[0]
        assert svc.ready_endpoints == 0

    async def test_scan_leaves_ready_unknown_when_endpoints_list_is_denied(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/services":
                return httpx.Response(200, json={"items": [self._svc({"port": 80})]})
            return httpx.Response(403, json={})

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        monkeypatch.setattr(
            "hle_client.discovery.kubernetes.httpx.AsyncClient",
            lambda **kw: real(transport=transport),
        )
        prov = KubernetesProvider()
        monkeypatch.setattr(prov, "_verify", lambda: "")
        monkeypatch.setattr(prov, "_token", lambda: "t")
        out = await prov.scan()
        assert out[0].ready_endpoints is None

    async def test_scan_fills_ready_from_endpoints(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/services":
                return httpx.Response(200, json={"items": [self._svc({"port": 80})]})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "web", "namespace": "media"},
                            "subsets": [
                                {"addresses": [{"ip": "1"}, {"ip": "2"}], "ports": [{"port": 80}]}
                            ],
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        monkeypatch.setattr(
            "hle_client.discovery.kubernetes.httpx.AsyncClient",
            lambda **kw: real(transport=transport),
        )
        prov = KubernetesProvider()
        monkeypatch.setattr(prov, "_verify", lambda: "")
        monkeypatch.setattr(prov, "_token", lambda: "t")
        assert (await prov.scan())[0].ready_endpoints == 2
