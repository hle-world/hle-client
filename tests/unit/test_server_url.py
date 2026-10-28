"""``HLE_SERVER_URL`` — point the client at a non-default HLE server.

One setting selects the server for the REST API, the relay and every dashboard
link. The default is ``https://hle.world``, so an unset variable changes
nothing; these pin the resolution order, the validation and the places that
consume it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import click
import httpx
import pytest

from hle_client import config
from hle_client.api import ApiClient, ApiClientConfig
from hle_client.cli import main
from hle_client.server import server_host, server_port, server_url
from hle_client.tunnel import TunnelConfig


def _write_config(path, server_url_value=None):
    if server_url_value is not None:
        path.write_text(f'server_url = "{server_url_value}"\n')


class TestResolution:
    def test_default_is_hle_world(self):
        assert server_url() == "https://hle.world"
        assert server_host() == "hle.world"
        assert server_port() == 443

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:8443/")
        assert server_url() == "https://stg.example.test:8443"
        assert server_host() == "stg.example.test"
        assert server_port() == 8443

    def test_config_file_overrides_default(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, "https://cfg.example.test")
        monkeypatch.setattr(config, "CONFIG_FILE", cfg)
        assert server_url() == "https://cfg.example.test"
        assert server_host() == "cfg.example.test"

    def test_env_overrides_config_file(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, "https://cfg.example.test")
        monkeypatch.setattr(config, "CONFIG_FILE", cfg)
        monkeypatch.setenv("HLE_SERVER_URL", "https://env.example.test")
        assert server_url() == "https://env.example.test"


class TestValidation:
    def test_https_port_and_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:8443/")
        assert server_url() == "https://stg.example.test:8443"
        assert (server_host(), server_port()) == ("stg.example.test", 8443)

    def test_https_explicit_default_port(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:443")
        assert server_url() == "https://stg.example.test:443"
        assert (server_host(), server_port()) == ("stg.example.test", 443)

    def test_http_localhost_is_accepted(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "http://localhost:8000")
        assert server_url() == "http://localhost:8000"
        assert (server_host(), server_port()) == ("localhost", 8000)

    def test_http_without_port_defaults_to_80(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "http://127.0.0.1")
        assert (server_host(), server_port()) == ("127.0.0.1", 80)

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com",
            "ftp://x",
            "https://x/path",
            "https://x?q=1",
            "https://x#frag",
            "hle.world",
        ],
    )
    def test_invalid_values_are_rejected_naming_the_variable(self, monkeypatch, value):
        monkeypatch.setenv("HLE_SERVER_URL", value)
        with pytest.raises(click.UsageError) as excinfo:
            server_url()
        assert "HLE_SERVER_URL" in str(excinfo.value)


class TestApiClientUsesServerUrl:
    async def test_requests_are_built_against_the_configured_url(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:8443")
        client = ApiClient(ApiClientConfig(api_key="hle_test"))
        assert client._base_url == "https://stg.example.test:8443"

        response = httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://stg.example.test:8443/api/tunnels"),
        )
        get = AsyncMock(return_value=response)
        with patch("httpx.AsyncClient.get", get):
            await client.list_tunnels()
        assert get.await_args.args[0] == "https://stg.example.test:8443/api/tunnels"
        await client.aclose()


class TestRelayDefaults:
    def test_tunnel_config_default_follows_server_url(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:8443")
        cfg = TunnelConfig(service_url="http://localhost:3000")
        assert (cfg.relay_host, cfg.relay_port) == ("stg.example.test", 8443)
        # Host and port are joined once, never doubled (wss://host:8443:443).
        assert f"{cfg.relay_host}:{cfg.relay_port}" == "stg.example.test:8443"

    def test_tunnel_config_explicit_relay_host_wins(self, monkeypatch):
        monkeypatch.setenv("HLE_SERVER_URL", "https://stg.example.test:8443")
        cfg = TunnelConfig(service_url="http://localhost:3000", relay_host="explicit.example")
        assert cfg.relay_host == "explicit.example"


class _RecordingAgent:
    """Stands in for AgentClient, recording how the CLI built it."""

    last_kwargs = {}

    def __init__(self, token, **kwargs):
        _RecordingAgent.last_kwargs = {"token": token, **kwargs}
        self.fatal_error = None
        self.exit_code = 0
        self.control_uri = "wss://relay.example/_hle/agent"

    async def run(self):
        return None


class TestAgentCliRelayHost:
    def _invoke(self, monkeypatch, extra, url):
        from click.testing import CliRunner

        monkeypatch.setenv("HLE_SERVER_URL", url)
        _RecordingAgent.last_kwargs = {}
        with (
            patch("hle_client.cli.AgentClient", _RecordingAgent),
            patch("hle_client.cli.shutdown.run", lambda coro: coro.close()),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "run", "--token", "hle_" + "a" * 32, *extra],
            )
        assert result.exit_code == 0, result.output
        return _RecordingAgent.last_kwargs

    def test_default_follows_server_url(self, monkeypatch):
        kwargs = self._invoke(monkeypatch, [], "https://stg.example.test:8443")
        assert kwargs["relay_host"] == "stg.example.test"
        assert kwargs["relay_port"] == 8443

    def test_explicit_relay_host_wins(self, monkeypatch):
        kwargs = self._invoke(
            monkeypatch, ["--relay-host", "explicit.example"], "https://stg.example.test:8443"
        )
        assert kwargs["relay_host"] == "explicit.example"
