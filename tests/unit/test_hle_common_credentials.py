"""The agent-credential shape shared by the relay and the client."""

from __future__ import annotations

import pytest

from hle_client import config
from hle_client.ops.agents import enroll
from hle_common import credentials
from hle_common.credentials import (
    API_KEY_PATTERN,
    LEGACY_AGENT_TOKEN_PREFIX,
    is_agent_credential,
)

VALID_KEY = "hle_" + "0123456789abcdef" * 2


class TestIsAgentCredential:
    def test_accepts_a_current_hle_key(self):
        assert is_agent_credential(VALID_KEY)

    def test_accepts_a_legacy_hlea_token(self):
        assert is_agent_credential("hlea_whatever-the-relay-issued")

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "hle_" + "0123456789ABCDEF" * 2,  # uppercase hex
            "hle_" + "0" * 31,  # too short
            "hle_" + "0" * 33,  # too long
            "hle_" + "g" * 32,  # not hex
            "hle_",
            "sk_" + "0" * 32,
            "hlex_" + "0" * 32,
            "HLEA_abc",
            "hle" + "0" * 32,
            " " + VALID_KEY,
            VALID_KEY + " ",
            VALID_KEY + "\n",
            " hlea_abc",
        ],
    )
    def test_rejects_malformed(self, token):
        assert not is_agent_credential(token)


def test_config_reexports_the_shared_definitions():
    assert config.API_KEY_PATTERN is API_KEY_PATTERN
    assert config.AGENT_TOKEN_PREFIX == LEGACY_AGENT_TOKEN_PREFIX == "hlea_"


def test_module_exports():
    assert set(credentials.__all__) == {
        "API_KEY_PATTERN",
        "LEGACY_AGENT_TOKEN_PREFIX",
        "is_agent_credential",
    }


class TestEnrollUsesTheSharedCheck:
    def test_strips_before_validating(self, monkeypatch):
        saved = []
        monkeypatch.setattr(config, "save_agent_token", saved.append)
        enroll(f"  {VALID_KEY}\n")
        assert saved == [VALID_KEY]

    def test_rejects_other_prefixes(self, monkeypatch):
        monkeypatch.setattr(config, "save_agent_token", lambda t: None)
        from hle_client.errors import HleError

        with pytest.raises(HleError):
            enroll("sk_" + "0" * 32)
