"""Tests for ``hle status`` rendering."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from hle_client.cli import main
from hle_client.ops.models import Agent


def _state(agents: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What ``collect`` hands the renderer, built from the relay's raw payload."""
    return {
        "version": "0.0.0",
        "credentials": {
            "api_key": True,
            "api_key_source": "HLE_API_KEY",
            "api_key_prefix": "hle_abcd…",
            "agent_token": False,
        },
        "daemons": [],
        "tunnels": [],
        "agents": None if agents is None else [Agent.from_api(a) for a in agents],
    }


class TestAgentRendering:
    """The relay sends ``online``; the renderer used to read ``is_online``, so
    every agent showed as offline."""

    def test_online_agent_is_shown_online(self) -> None:
        state = _state([{"name": "homelab", "online": True}])
        with patch("hle_client.status_cmd.collect", return_value=state):
            result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        line = next(ln for ln in result.output.splitlines() if "homelab" in ln)
        assert "online" in line
        assert "offline" not in line

    def test_offline_agent_is_shown_offline(self) -> None:
        state = _state([{"name": "homelab", "online": False}])
        with patch("hle_client.status_cmd.collect", return_value=state):
            result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        line = next(ln for ln in result.output.splitlines() if "homelab" in ln)
        assert "offline" in line

    def test_json_passes_state_through(self) -> None:
        state = _state([{"name": "homelab", "online": True}])
        with patch("hle_client.status_cmd.collect", return_value=state):
            result = CliRunner().invoke(main, ["-o", "json", "status"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["agents"][0]["online"] is True
