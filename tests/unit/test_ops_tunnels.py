"""ops.tunnels against a recording fake of ApiClient."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from hle_client.errors import AuthError, ConflictError, HleError, NotFoundError, UnreachableError
from hle_client.ops import tunnels
from hle_client.ops.models import Conflict, Deleted


class FakeApi:
    """Records every call; answers from fixtures; raises what it is told to."""

    def __init__(self, **fixtures: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fixtures = fixtures
        self.errors: dict[str, Exception] = {}

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            if name in self.errors:
                raise self.errors[name]
            if name in self.fixtures:
                return self.fixtures[name]
            return {}

        return call


def _http_error(code: int, body: bytes = b"", json: dict[str, Any] | None = None):
    request = httpx.Request("GET", "https://hle.world/x")
    if json is not None:
        response = httpx.Response(code, json=json, request=request)
    else:
        response = httpx.Response(code, content=body, request=request)
    return httpx.HTTPStatusError(str(code), request=request, response=response)


TUNNELS = [
    {"subdomain": "ha-x7k", "tunnel_id": "t1", "is_active": True},
    {"subdomain": "old-x7k", "tunnel_id": "t2", "is_active": False},
    {"subdomain": "noid-x7k", "is_active": False},
]


def api(**overrides: Any) -> FakeApi:
    fixtures = {"get_me": {"user_code": "x7k"}, "list_tunnels": TUNNELS}
    fixtures.update(overrides)
    return FakeApi(**fixtures)


class TestResolveSubdomain:
    async def test_a_label_gets_the_user_code(self):
        assert await tunnels.resolve_subdomain(api(), "ha") == "ha-x7k"

    async def test_a_hyphenated_label_is_still_a_label(self):
        assert await tunnels.resolve_subdomain(api(), "home-assistant") == "home-assistant-x7k"

    async def test_a_resolved_name_passes_through(self):
        assert await tunnels.resolve_subdomain(api(), "home-assistant-x7k") == "home-assistant-x7k"

    async def test_the_bare_suffix_is_not_resolved(self):
        assert await tunnels.resolve_subdomain(api(), "-x7k") == "-x7k-x7k"

    async def test_user_code_is_looked_up_once_per_client(self):
        a = api()
        await tunnels.resolve_subdomain(a, "ha")
        await tunnels.resolve_subdomain(a, "jf")
        assert [c for c, _ in a.calls].count("get_me") == 1

    async def test_no_user_code_is_an_error(self):
        with pytest.raises(HleError, match="user_code"):
            await tunnels.resolve_subdomain(api(get_me={}), "ha")

    async def test_an_unreachable_relay_is_typed(self):
        a = api()
        a.errors["get_me"] = httpx.ConnectError("down")
        with pytest.raises(UnreachableError):
            await tunnels.resolve_subdomain(a, "ha")


class TestListAndFind:
    async def test_list_maps_online(self):
        found = await tunnels.list_tunnels(api())
        assert [(t.subdomain, t.online) for t in found] == [
            ("ha-x7k", True),
            ("old-x7k", False),
            ("noid-x7k", False),
        ]

    async def test_find_by_label(self):
        assert (await tunnels.find_tunnel(api(), "ha")).tunnel_id == "t1"

    async def test_find_missing_is_not_found_with_a_hint(self):
        with pytest.raises(NotFoundError) as info:
            await tunnels.find_tunnel(api(), "nope")
        assert info.value.exit_code == 4
        assert "hle tunnel list" in (info.value.hint or "")

    async def test_a_401_is_an_auth_error(self):
        a = api()
        a.errors["list_tunnels"] = _http_error(401)
        with pytest.raises(AuthError):
            await tunnels.list_tunnels(a)


class TestGetTunnel:
    async def test_maps_the_status_payload(self):
        a = api(
            get_tunnel_status={
                "subdomain": "ha-x7k",
                "is_active": False,
                "auth_mode": "sso",
                "pin": {"has_pin": True},
                "basic_auth": {"enabled": False},
                "is_protected": True,
            }
        )
        d = await tunnels.get_tunnel(a, "ha")
        assert ("get_tunnel_status", ("ha-x7k",)) in a.calls
        assert d.pin.enabled and d.protected and not d.tunnel.online

    async def test_a_404_names_the_tunnel(self):
        a = api()
        a.errors["get_tunnel_status"] = _http_error(404)
        with pytest.raises(NotFoundError, match="ha-x7k"):
            await tunnels.get_tunnel(a, "ha")


class TestDeleteTunnel:
    async def test_an_idle_tunnel_is_deleted_by_id(self):
        a = api()
        result = await tunnels.delete_tunnel(a, "old")
        assert result == Deleted(subject="old-x7k")
        assert ("delete_tunnel_record", ("t2",)) in a.calls

    async def test_a_live_tunnel_is_a_conflict_not_a_call(self):
        a = api()
        result = await tunnels.delete_tunnel(a, "ha")
        assert isinstance(result, Conflict)
        assert "stop it" in result.reason
        assert "hle daemon uninstall" in (result.hint or "")
        assert not [c for c, _ in a.calls if c == "delete_tunnel_record"]

    async def test_force_sends_the_request_and_the_relay_decides(self):
        a = api()
        a.errors["delete_tunnel_record"] = _http_error(409, json={"detail": "still connected"})
        with pytest.raises(ConflictError, match="still connected"):
            await tunnels.delete_tunnel(a, "ha", force=True)
        assert ("delete_tunnel_record", ("t1",)) in a.calls

    async def test_a_tunnel_object_skips_the_lookup(self):
        a = api()
        found = await tunnels.find_tunnel(a, "old")
        before = len(a.calls)
        await tunnels.delete_tunnel(a, found)
        assert [c for c, _ in a.calls[before:]] == ["delete_tunnel_record"]

    async def test_no_id_is_an_error(self):
        with pytest.raises(HleError, match="no id"):
            await tunnels.delete_tunnel(api(), "noid")

    async def test_missing_is_not_found(self):
        with pytest.raises(NotFoundError):
            await tunnels.delete_tunnel(api(), "ghost")


class TestSetAuthMode:
    async def test_sets_and_returns_the_subdomain(self):
        a = api()
        assert await tunnels.set_auth_mode(a, "ha", "none") == "ha-x7k"
        assert ("set_tunnel_auth_mode", ("ha-x7k", "none")) in a.calls

    async def test_an_unregistered_tunnel_is_explained(self):
        a = api()
        a.errors["set_tunnel_auth_mode"] = _http_error(404)
        with pytest.raises(NotFoundError, match="never been registered"):
            await tunnels.set_auth_mode(a, "ha", "none")

    async def test_a_webhook_tunnel_is_a_conflict(self):
        a = api()
        a.errors["set_tunnel_auth_mode"] = _http_error(400, body=b"Webhook tunnels are public")
        with pytest.raises(ConflictError, match="Webhook"):
            await tunnels.set_auth_mode(a, "ha", "sso")
