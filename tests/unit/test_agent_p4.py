"""Operator P4 client hooks: declared access, spec resolver, statuses, handover group."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest

from hle_client.agent import AgentClient, handover_group_from_env
from hle_common.agent_protocol import (
    AllowedUser,
    DeclaredAccess,
    DeclaredEndpoint,
    DeclaredEndpoints,
    EndpointSpec,
)
from tests.unit.test_agent import FakeTunnel, _FakeWs, _spec
from tests.unit.test_agent_p3 import _WELCOME


def _make() -> tuple[AgentClient, list[FakeTunnel]]:
    return _make_with()


def _make_with(**kw) -> tuple[AgentClient, list[FakeTunnel]]:
    created: list[FakeTunnel] = []

    def factory(cfg):
        t = FakeTunnel(cfg)
        created.append(t)
        return t

    return AgentClient("hlea_test", tunnel_factory=factory, **kw), created


def _decl(**kw) -> DeclaredEndpoint:
    return DeclaredEndpoint(
        label="web", service_url="http://web.ns.svc:80", source_ref="hletunnel:ns/web", **kw
    )


class TestDeclaredAccess:
    def test_defaults_to_none(self):
        assert _decl().access is None

    def test_round_trips(self):
        access = DeclaredAccess(
            allowed_users=[
                AllowedUser(email="a@example.com"),
                AllowedUser(email="b@x.io", provider="github"),
            ],
            pin="1234",
            basic_auth="user:pass",
        )
        wire = DeclaredEndpoints(endpoints=[_decl(access=access)], revision=1).model_dump_json()
        again = DeclaredEndpoints.model_validate_json(wire)
        got = again.endpoints[0].access
        assert got is not None
        assert [(u.email, u.provider) for u in got.allowed_users] == [
            ("a@example.com", "any"),
            ("b@x.io", "github"),
        ]
        assert (got.pin, got.basic_auth) == ("1234", "user:pass")

    def test_old_frame_without_access_parses(self):
        wire = json.loads(_decl().model_dump_json())
        del wire["access"]
        assert DeclaredEndpoint.model_validate(wire).access is None

    @pytest.mark.parametrize("bad", ["nocolon", ":pass", ""])
    def test_rejects_bad_basic_auth(self, bad):
        with pytest.raises(ValueError, match="basic_auth"):
            DeclaredAccess(basic_auth=bad)

    def test_rejects_empty_pin(self):
        with pytest.raises(ValueError, match="pin"):
            DeclaredAccess(pin="")

    @pytest.mark.parametrize("email", ["", "no-at-sign"])
    def test_rejects_bad_email(self, email):
        with pytest.raises(ValueError, match="email"):
            AllowedUser(email=email)

    def test_secrets_never_in_repr(self):
        access = DeclaredAccess(pin="9876", basic_auth="visitor:hunter2")
        for text in (repr(access), repr(_decl(access=access)), str(_decl(access=access))):
            assert "9876" not in text
            assert "hunter2" not in text
            assert "'***'" in text

    def test_none_secrets_not_masked(self):
        assert "pin=None" in repr(DeclaredAccess())


class TestSpecResolver:
    async def test_resolver_runs_before_the_diff_and_restarts_only_that_endpoint(self):
        secrets = {"a": "u:one", "b": "u:one"}

        def resolver(spec: EndpointSpec) -> EndpointSpec:
            return replace(spec, upstream_basic_auth=secrets[spec.label])

        client, created = _make_with(spec_resolver=resolver)
        await client.reconcile([_spec("a"), _spec("b")])
        await asyncio.sleep(0)
        assert len(created) == 2
        # Same incoming specs, one resolved value changed: only "a" restarts.
        secrets["a"] = "u:two"
        await client.reconcile([_spec("a"), _spec("b")])
        await asyncio.sleep(0)
        assert [t.config.service_label for t in created] == ["a", "b", "a"]
        assert created[0].disconnect_calls == 1
        assert created[1].disconnect_calls == 0
        await client._stop_all()

    async def test_unchanged_resolution_does_not_restart(self):
        client, created = _make_with(spec_resolver=lambda s: replace(s, upstream_basic_auth="u:p"))
        await client.reconcile([_spec("a")])
        await client.reconcile([_spec("a")])
        await asyncio.sleep(0)
        assert len(created) == 1
        await client._stop_all()

    async def test_resolver_error_marks_only_that_endpoint(self, caplog):
        def resolver(spec: EndpointSpec) -> EndpointSpec:
            if spec.label == "bad":
                raise ValueError("secret ns/db#key not found")
            return spec

        client, created = _make_with(spec_resolver=resolver)
        await client.reconcile([_spec("good"), _spec("bad")])
        await asyncio.sleep(0)
        assert [t.config.service_label for t in created] == ["good"]
        assert client._failed == {"bad": "unresolved: secret ns/db#key not found"}
        # The failure survives later reconciles while still desired, and reaches status.
        await client.reconcile([_spec("good"), _spec("bad")])
        statuses = {s.label: s for s in client._build_status()}
        assert statuses["bad"].error == "unresolved: secret ns/db#key not found"
        assert statuses["good"].error is None
        await client._stop_all()

    async def test_resolver_error_stops_a_running_endpoint_and_clears_when_dropped(self):
        broken = False

        def resolver(spec: EndpointSpec) -> EndpointSpec:
            if broken:
                raise ValueError("gone")
            return spec

        client, created = _make_with(spec_resolver=resolver)
        await client.reconcile([_spec("a")])
        await asyncio.sleep(0)
        broken = True
        await client.reconcile([_spec("a")])
        assert "a" not in client._endpoints
        assert created[0].disconnect_calls == 1
        assert "a" in client._failed
        await client.reconcile([])
        assert client._failed == {}

    async def test_recovers_once_resolvable(self):
        broken = True

        def resolver(spec: EndpointSpec) -> EndpointSpec:
            if broken:
                raise ValueError("not yet")
            return spec

        client, created = _make_with(spec_resolver=resolver)
        await client.reconcile([_spec("a")])
        broken = False
        await client.reconcile([_spec("a")])
        await asyncio.sleep(0)
        assert len(created) == 1
        assert client._failed == {}
        await client._stop_all()

    async def test_no_resolver_is_unchanged(self):
        client, created = _make()
        await client.reconcile([_spec("a")])
        await asyncio.sleep(0)
        assert len(created) == 1
        await client._stop_all()


class TestEndpointStatuses:
    async def _live(self) -> AgentClient:
        client, _ = _make()
        await client.reconcile([_spec("a")])
        await asyncio.sleep(0)
        client._ws = object()
        return client

    async def test_none_when_disconnected(self):
        client, _ = _make()
        await client.reconcile([_spec("a")])
        assert client.endpoint_statuses() is None
        await client._stop_all()

    async def test_statuses_while_holding_the_session(self):
        client = await self._live()
        statuses = client.endpoint_statuses()
        assert statuses is not None
        assert [(s.label, s.connected) for s in statuses] == [("a", True)]
        await client._stop_all()

    @pytest.mark.parametrize("flag", ["_canary_probation", "_control_taken", "_handover_done"])
    async def test_none_on_probation_or_after_handover(self, flag):
        client = await self._live()
        setattr(client, flag, True)
        assert client.endpoint_statuses() is None
        await client._stop_all()


class TestHandoverGroup:
    async def _hello(self, monkeypatch) -> dict:
        import hle_client.agent as agent_mod

        ws = _FakeWs(_WELCOME)
        monkeypatch.setattr(agent_mod.websockets, "connect", lambda *a, **k: ws)
        monkeypatch.setattr(agent_mod, "active_providers", list)
        client = AgentClient("hlea_test", tunnel_factory=FakeTunnel)

        async def no_discovery(_ws) -> None:
            return None

        monkeypatch.setattr(client, "_report_discovery", no_discovery)
        await client._connect_once()
        return json.loads(ws.sent[0])

    async def test_env_reaches_the_hello(self, monkeypatch):
        monkeypatch.setenv("HLE_HANDOVER_GROUP", "hle/hle-operator")
        assert (await self._hello(monkeypatch))["handover_group"] == "hle/hle-operator"

    async def test_unset_is_null(self, monkeypatch):
        monkeypatch.delenv("HLE_HANDOVER_GROUP", raising=False)
        assert (await self._hello(monkeypatch))["handover_group"] is None

    @pytest.mark.parametrize("value", ["Hle/Op", "-lead", "trail-", "has space", "x" * 129, "a/b!"])
    def test_invalid_is_unset_with_one_warning(self, value, caplog):
        with caplog.at_level(logging.WARNING, logger="hle_client.agent"):
            assert handover_group_from_env({"HLE_HANDOVER_GROUP": value}) is None
        assert len([r for r in caplog.records if "HLE_HANDOVER_GROUP" in r.getMessage()]) == 1

    @pytest.mark.parametrize("value", ["a", "ns/rel", "a.b_c-d/e", "x" * 128])
    def test_valid_values_pass(self, value):
        assert handover_group_from_env({"HLE_HANDOVER_GROUP": value}) == value

    async def test_invalid_env_warns_once_across_reconnects(self, monkeypatch, caplog):
        monkeypatch.setenv("HLE_HANDOVER_GROUP", "BAD")
        with caplog.at_level(logging.WARNING, logger="hle_client.agent"):
            client = AgentClient("hlea_test", tunnel_factory=FakeTunnel)
        assert client._handover_group is None
        assert len([r for r in caplog.records if "HLE_HANDOVER_GROUP" in r.getMessage()]) == 1
