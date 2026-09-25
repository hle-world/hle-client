"""ops.auth — PIN, Basic Auth, share links."""

from __future__ import annotations

from typing import Any

import pytest

from hle_client.errors import HleError, NotFoundError, UsageError
from hle_client.ops import auth
from tests.unit.test_ops_tunnels import FakeApi, _http_error


def api(**overrides: Any) -> FakeApi:
    fixtures = {"get_me": {"user_code": "x7k"}}
    fixtures.update(overrides)
    return FakeApi(**fixtures)


class TestPin:
    async def test_status(self):
        s = await auth.pin_status(api(get_tunnel_pin_status={"has_pin": True}), "ha")
        assert s.enabled is True

    async def test_set_validates_before_calling(self):
        a = api()
        with pytest.raises(HleError, match="4-8 digits"):
            await auth.set_pin(a, "ha", "12")
        assert a.calls == []

    async def test_set_and_remove(self):
        a = api()
        assert await auth.set_pin(a, "ha", "1234") == "ha-x7k"
        assert await auth.remove_pin(a, "ha") == "ha-x7k"
        assert ("set_tunnel_pin", ("ha-x7k", "1234")) in a.calls
        assert ("remove_tunnel_pin", ("ha-x7k",)) in a.calls


class TestBasicAuth:
    async def test_status(self):
        s = await auth.basic_auth_status(
            api(get_tunnel_basic_auth_status={"enabled": True, "username": "u"}), "ha"
        )
        assert (s.enabled, s.username) == (True, "u")

    @pytest.mark.parametrize(
        ("username", "password", "why"),
        [("", "password1", "empty"), ("a:b", "password1", "':'"), ("u", "short", "8 characters")],
    )
    async def test_set_validates(self, username, password, why):
        with pytest.raises(HleError, match=why):
            await auth.set_basic_auth(api(), "ha", username, password)

    async def test_set_strips_the_username(self):
        a = api()
        await auth.set_basic_auth(a, "ha", " user ", "password1")
        assert ("set_tunnel_basic_auth", ("ha-x7k", "user", "password1")) in a.calls

    async def test_remove_404_names_the_tunnel(self):
        a = api()
        a.errors["remove_tunnel_basic_auth"] = _http_error(404)
        with pytest.raises(NotFoundError, match="ha-x7k"):
            await auth.remove_basic_auth(a, "ha")


class TestShare:
    async def test_create_returns_the_url_once(self):
        a = api(
            create_share_link={
                "share_url": "https://ha-x7k.hle.world/s/abc",
                "link": {"id": 1, "expires_at": "soon", "label": "bob", "max_uses": 3},
            }
        )
        link = await auth.create_share(a, "ha", duration="1h", label="bob", max_uses=3)
        assert link.share_url == "https://ha-x7k.hle.world/s/abc"
        assert (link.id, link.label, link.max_uses) == (1, "bob", 3)
        assert ("create_share_link", ("ha-x7k", "1h", "bob", 3)) in a.calls

    async def test_bad_duration_is_a_usage_error(self):
        with pytest.raises(UsageError):
            await auth.create_share(api(), "ha", duration="2h")

    async def test_list_and_revoke(self):
        a = api(list_share_links=[{"id": 1, "token_prefix": "abc", "is_active": True}])
        links = await auth.list_shares(a, "ha")
        assert [(link.id, link.active) for link in links] == [(1, True)]
        assert await auth.revoke_share(a, "ha", 1) == "ha-x7k"
        assert ("delete_share_link", ("ha-x7k", 1)) in a.calls
