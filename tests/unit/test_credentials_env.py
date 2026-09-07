"""An agent token left in HLE_API_KEY still has to work.

Observed on a live host: /etc/hle/otsiasi.env sets HLE_API_KEY to an `hlea_`
agent token, because that is what enrolment produced at the time. The relay
accepts it for registering tunnels, so the service runs — but nothing that
reads HLE_AGENT_TOKEN can see the machine has a credential at all.
"""

from __future__ import annotations

from hle_client.credentials import normalize_credential_env


class TestNormalizeCredentialEnv:
    def test_a_legacy_token_becomes_readable_as_an_agent_token(self):
        env = {"HLE_API_KEY": "hlea_" + "a" * 40}
        notice = normalize_credential_env(env)
        assert env["HLE_AGENT_TOKEN"] == env["HLE_API_KEY"]
        assert notice is not None

    def test_the_api_key_is_left_alone(self):
        """The relay accepts it there; clearing it breaks tunnel registration."""
        env = {"HLE_API_KEY": "hlea_" + "a" * 40}
        normalize_credential_env(env)
        assert env["HLE_API_KEY"] == "hlea_" + "a" * 40

    def test_an_explicit_agent_token_wins(self):
        env = {"HLE_API_KEY": "hlea_" + "a" * 40, "HLE_AGENT_TOKEN": "hlea_" + "b" * 40}
        assert normalize_credential_env(env) is None
        assert env["HLE_AGENT_TOKEN"] == "hlea_" + "b" * 40

    def test_an_empty_agent_token_is_not_a_choice(self):
        env = {"HLE_API_KEY": "hlea_" + "a" * 40, "HLE_AGENT_TOKEN": "  "}
        assert normalize_credential_env(env) is not None
        assert env["HLE_AGENT_TOKEN"] == env["HLE_API_KEY"]

    def test_a_real_api_key_is_untouched(self):
        env = {"HLE_API_KEY": "hle_" + "a" * 32}
        assert normalize_credential_env(env) is None
        assert "HLE_AGENT_TOKEN" not in env

    def test_no_credentials_at_all_is_silent(self):
        env: dict[str, str] = {}
        assert normalize_credential_env(env) is None
        assert env == {}
