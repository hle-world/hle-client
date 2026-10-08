"""ops.agents — the account's agents, and this machine's enrollment."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hle_client.errors import AuthError, HleError
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
