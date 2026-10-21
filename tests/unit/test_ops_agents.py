"""ops.agents — the account's agents, and this machine's enrollment."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from hle_client.errors import AuthError, ConflictError, HleError
from hle_client.ops import agents
from tests.unit.test_ops_tunnels import FakeApi, _http_error


def api(**overrides: Any) -> FakeApi:
    return FakeApi(**overrides)


class TestListAgents:
    async def test_maps_online_and_state(self):
        found = await agents.list_agents(
            api(
                list_agents=[
                    {"name": "rpi", "online": True},
                    {"name": "nas", "online": False, "is_active": False},
                ]
            )
        )
        assert [(a.name, a.online, a.state) for a in found] == [
            ("rpi", True, "online"),
            ("nas", False, "disabled"),
        ]

    async def test_a_404_means_the_feature_is_off(self):
        a = api()
        a.errors["list_agents"] = _http_error(404)
        with pytest.raises(HleError, match="not enabled"):
            await agents.list_agents(a)

    async def test_a_401_carries_the_relays_words(self):
        a = api()
        a.errors["list_agents"] = _http_error(401, json={"detail": "agent token, not an API key"})
        with pytest.raises(AuthError, match="agent token") as info:
            await agents.list_agents(a)
        assert info.value.exit_code == 3


class TestClassifyCredential:
    """The relay is the only thing that can tell the two hle_ shapes apart."""

    async def test_an_account_key(self):
        cred = await agents.classify_credential(api(get_credential={"kind": "api_key"}))
        assert cred is not None
        assert (cred.kind, cred.active) == ("api_key", True)

    async def test_an_agent_carries_its_name_and_state(self):
        cred = await agents.classify_credential(
            api(get_credential={"kind": "agent", "name": "laptop", "active": True})
        )
        assert cred is not None
        assert (cred.kind, cred.name, cred.active) == ("agent", "laptop", True)

    async def test_a_404_means_the_relay_is_too_old(self):
        a = api()
        a.errors["get_credential"] = _http_error(404)
        assert await agents.classify_credential(a) is None

    async def test_an_unreachable_relay_is_none(self):
        a = api()
        a.errors["get_credential"] = httpx.ConnectError("down")
        assert await agents.classify_credential(a) is None

    async def test_a_401_is_an_auth_error(self):
        a = api()
        a.errors["get_credential"] = _http_error(401)
        with pytest.raises(AuthError, match="does not recognise"):
            await agents.classify_credential(a)

    async def test_an_unknown_kind_is_none(self):
        assert await agents.classify_credential(api(get_credential={"kind": "?"})) is None


class TestLocal:
    def test_status_reflects_the_saved_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HLE_AGENT_TOKEN", raising=False)
        monkeypatch.setenv("HLE_AGENT_CONFIG", str(tmp_path / "agent.toml"))
        assert agents.local_agent_status().enrolled is False
        agents.enroll("hlea_" + "b" * 20)
        status = agents.local_agent_status()
        assert status.enrolled is True
        assert status.token_prefix == "hlea_bbbb…"
        assert status.source == "~/.config/hle/agent.toml"

    @pytest.mark.parametrize(
        "token",
        [
            # What the dashboard's New Agent dialog hands out since v2609.2.
            "hle_" + "a" * 32,
            # Copied with a trailing newline.
            "hle_" + "a" * 32 + "\n",
            # Legacy enrollment token; still accepted by the relay.
            "hlea_" + "b" * 40,
        ],
    )
    def test_enroll_accepts_both_agent_credential_shapes(self, token):
        with patch("hle_client.config.save_agent_token") as save:
            agents.enroll(token)
        save.assert_called_once_with(token.strip())

    @pytest.mark.parametrize("token", ["", "hle_short", "sk_live_" + "a" * 32, "hle_" + "A" * 32])
    def test_enroll_rejects_what_is_not_an_hle_credential(self, token):
        with (
            patch("hle_client.config.save_agent_token") as save,
            pytest.raises(HleError, match="hle_"),
        ):
            agents.enroll(token)
        save.assert_not_called()


class TestEnrollNamed:
    """`enroll_named`: the account key mints an agent, the token is saved once."""

    async def test_saves_the_returned_token_and_reports_the_agent(self):
        token = "hle_" + "c" * 32
        a = api(enroll_agent={"agent_id": 12, "name": "laptop", "token": token})
        with patch("hle_client.config.save_agent_token") as save:
            enrolled = await agents.enroll_named(a, "laptop")
        save.assert_called_once_with(token)
        assert (enrolled.agent_id, enrolled.name) == (12, "laptop")
        assert a.calls == [("enroll_agent", ("laptop",))]

    async def test_uses_the_name_the_server_trimmed(self):
        a = api(enroll_agent={"agent_id": 1, "name": "laptop", "token": "hle_" + "d" * 32})
        with patch("hle_client.config.save_agent_token"):
            enrolled = await agents.enroll_named(a, "  laptop  ")
        assert enrolled.name == "laptop"

    @pytest.mark.parametrize(
        ("code", "detail", "kind"),
        [
            (401, "Invalid or expired key", AuthError),
            (403, "This account is not active", AuthError),
            (403, "This key is scoped to tunnels only", AuthError),
            (409, "Agent 'laptop' already exists — pick another name", ConflictError),
        ],
    )
    async def test_server_detail_is_shown_for_rejections(self, code, detail, kind):
        a = api()
        a.errors["enroll_agent"] = _http_error(code, json={"detail": detail})
        with (
            patch("hle_client.config.save_agent_token") as save,
            pytest.raises(kind, match=detail),
        ):
            await agents.enroll_named(a, "laptop")
        save.assert_not_called()

    async def test_a_404_says_the_server_is_too_old(self):
        a = api()
        a.errors["enroll_agent"] = _http_error(404)
        with (
            patch("hle_client.config.save_agent_token") as save,
            pytest.raises(HleError, match="doesn't support --name"),
        ):
            await agents.enroll_named(a, "laptop")
        save.assert_not_called()

    async def test_a_422_shows_the_first_validation_message(self):
        a = api()
        detail = [{"msg": "String should have at least 1 character"}]
        a.errors["enroll_agent"] = _http_error(422, json={"detail": detail})
        with pytest.raises(HleError, match="Invalid agent name: String should have at least 1"):
            await agents.enroll_named(a, " ")

    async def test_a_429_adds_the_retry_after(self):
        request = httpx.Request("POST", "https://hle.world/api/agents/enroll")
        response = httpx.Response(
            429,
            json={"detail": "Too many agent enrollments — try again later"},
            headers={"Retry-After": "30"},
            request=request,
        )
        a = api()
        a.errors["enroll_agent"] = httpx.HTTPStatusError("429", request=request, response=response)
        with pytest.raises(HleError, match="retry in 30s"):
            await agents.enroll_named(a, "laptop")

    async def test_an_unreachable_relay_is_typed(self):
        a = api()
        a.errors["enroll_agent"] = httpx.ConnectError("down")
        with pytest.raises(HleError, match="relay"):
            await agents.enroll_named(a, "laptop")
