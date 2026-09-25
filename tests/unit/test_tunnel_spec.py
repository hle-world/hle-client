"""TunnelSpec: one model for the tunnel option set, and everything derived from it.

Covers the model itself, the single ``to_tunnel_config`` mapping, the
``@tunnel_options`` decorator on the four commands that create a tunnel,
agent protocol 1.3 (EndpointSpec = TunnelSpec + id) against 1.2 peers, and
the agent starting a dashboard endpoint with every field.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any, cast

import click
import pytest

from hle_client.agent import AgentClient
from hle_client.cli import expose, main
from hle_client.errors import UsageError
from hle_client.tunnel import TunnelConfig, to_tunnel_config
from hle_client.tunnel_options import spec_from_params, tunnel_param_names
from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    AgentStateSync,
    AgentWelcome,
    EndpointSpec,
)
from hle_common.models import TunnelRegistration
from hle_common.tunnel_spec import TunnelSpec, cli_fields

_BASELINE: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "wire_baseline.json").read_text()
)

# EndpointSpec exactly as a 1.2 server sends it, and the field set a 1.2 agent knows.
_ENDPOINT_1_2 = (
    '{"id":1,"label":"ha","service_url":"http://localhost:8123","zone":"example.com",'
    '"auth_mode":"sso","webhook_path":"/hook","websocket_enabled":false}'
)
_ENDPOINT_1_2_FIELDS = {
    "id",
    "label",
    "service_url",
    "zone",
    "auth_mode",
    "webhook_path",
    "websocket_enabled",
}
_NEW_1_3_FIELDS = {
    "verify_ssl",
    "forward_host",
    "upstream_basic_auth",
    "apex",
    "options",
    "response_timeout",
    "managed_by",
}

_FULL = TunnelSpec(
    label="prox",
    service_url="https://192.168.1.10:8006",
    zone="t00t.us",
    auth_mode="none",
    webhook_path=None,
    websocket_enabled=False,
    verify_ssl=True,
    forward_host=True,
    upstream_basic_auth="admin:s3:cret",
    apex=False,
    options={"k": "v"},
    response_timeout=300,
    managed_by="hle-operator",
)


def _cmd(*path: str) -> click.Command:
    node: click.Command = main
    for name in path:
        node = cast("click.Group", node).commands[name]
    return node


# The four commands that take the whole tunnel option set.
_FOUR = {
    "expose": _cmd("expose"),
    "tunnel create": _cmd("tunnel", "create"),
    "daemon install tunnel": _cmd("daemon", "install", "tunnel"),
    "daemon install _flags": _cmd("daemon", "install", "_flags"),
}


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


class TestTunnelSpecModel:
    def test_defaults_match_the_1_2_endpoint_and_tunnel_config(self):
        spec = TunnelSpec()
        assert spec.label is None
        assert spec.service_url == ""
        assert spec.zone is None
        assert spec.auth_mode == "sso"
        assert spec.webhook_path is None
        assert spec.websocket_enabled is True
        # Everything added after the 1.2 fields is null = "tunnel default".
        assert all(getattr(spec, name) is None for name in _NEW_1_3_FIELDS)

    def test_json_round_trip(self):
        assert TunnelSpec.model_validate_json(_FULL.model_dump_json()) == _FULL

    def test_unknown_keys_are_ignored(self):
        raw = _FULL.model_dump()
        raw["a_1_4_field"] = 1
        assert TunnelSpec.model_validate(raw) == _FULL

    def test_repr_masks_the_secret(self):
        text = repr(_FULL)
        assert "s3:cret" not in text
        assert "upstream_basic_auth='***'" in text
        assert "service_url='https://192.168.1.10:8006'" in text

    def test_repr_of_endpoint_spec_masks_too(self):
        ep = EndpointSpec(id=1, label="a", service_url="http://x", upstream_basic_auth="u:pw")
        assert "u:pw" not in repr(ep)
        assert repr(ep).startswith("EndpointSpec(id=1, label='a'")

    def test_repr_shows_none_secret_as_none(self):
        assert "upstream_basic_auth=None" in repr(TunnelSpec())

    def test_every_flagged_field_has_help_and_a_dest(self):
        for name, meta in cli_fields():
            assert meta["help"], name
            assert meta["dest"], name
            assert meta["opts"], name


# --------------------------------------------------------------------------- #
# to_tunnel_config — the one mapping
# --------------------------------------------------------------------------- #


class TestToTunnelConfig:
    def test_every_field(self):
        cfg = to_tunnel_config(_FULL, api_key="hle_k", relay_host="relay.example", relay_port=8443)
        assert cfg.service_url == "https://192.168.1.10:8006"
        assert cfg.service_label == "prox"
        assert cfg.zone == "t00t.us"
        assert cfg.auth_mode == "none"
        assert cfg.webhook_path is None
        assert cfg.websocket_enabled is False
        assert cfg.verify_ssl is True
        assert cfg.forward_host is True
        assert cfg.upstream_basic_auth == ("admin", "s3:cret")
        assert cfg.apex is False
        assert cfg.options == {"k": "v"}
        assert cfg.response_timeout == 300
        assert cfg.managed_by == "hle-operator"
        assert cfg.api_key == "hle_k"
        assert cfg.relay_host == "relay.example"
        assert cfg.relay_port == 8443

    def test_every_spec_field_is_mapped(self):
        """A new TunnelSpec field must reach TunnelConfig (or be listed here)."""
        renamed = {"label": "service_label"}
        config_fields = {f.name for f in dataclasses.fields(TunnelConfig)}
        for f in dataclasses.fields(TunnelSpec):
            assert renamed.get(f.name, f.name) in config_fields, f.name

    def test_nulls_take_tunnel_config_defaults(self):
        cfg = to_tunnel_config(TunnelSpec(label="a", service_url="http://x"))
        default = TunnelConfig(service_url="http://x", service_label="a")
        assert cfg == default

    def test_webhook_path(self):
        cfg = to_tunnel_config(TunnelSpec(label="gh", service_url="http://x", webhook_path="/h"))
        assert cfg.webhook_path == "/h"

    def test_managed_by_override_wins(self):
        cfg = to_tunnel_config(_FULL, managed_by="hle-agent")
        assert cfg.managed_by == "hle-agent"

    def test_options_are_copied_not_shared(self):
        cfg = to_tunnel_config(_FULL)
        cfg.options["x"] = "y"
        assert _FULL.options == {"k": "v"}

    def test_missing_service_url(self):
        with pytest.raises(ValueError):
            to_tunnel_config(TunnelSpec(label="a"))

    def test_malformed_basic_auth(self):
        with pytest.raises(ValueError):
            to_tunnel_config(
                TunnelSpec(label="a", service_url="http://x", upstream_basic_auth="nocolon")
            )

    def test_response_timeout_reaches_the_registration(self):
        reg = TunnelRegistration(
            service_url="http://x", service_label="a", api_key="hle_x", response_timeout=60
        )
        assert reg.model_dump()["response_timeout"] == 60

    @pytest.mark.parametrize("bad", [0, -1, True, "30"])
    def test_registration_rejects_a_bad_timeout(self, bad: Any):
        with pytest.raises(ValueError):
            TunnelRegistration(
                service_url="http://x", service_label="a", api_key="hle_x", response_timeout=bad
            )


# --------------------------------------------------------------------------- #
# @tunnel_options — the same params on all four commands
# --------------------------------------------------------------------------- #


def _tunnel_params(cmd: click.Command) -> dict[str, tuple[Any, ...]]:
    names = set(tunnel_param_names())
    return {
        p.name: (p.name, tuple(p.opts), tuple(p.secondary_opts), p.envvar, p.default, p.type.name)
        for p in cmd.params
        if p.name in names
    }


class TestTunnelOptionsDecorator:
    def test_every_command_has_the_whole_set(self):
        for label, cmd in _FOUR.items():
            assert set(_tunnel_params(cmd)) == set(tunnel_param_names()), label

    @pytest.mark.parametrize("name", sorted(tunnel_param_names()))
    def test_identical_across_the_four_commands(self, name: str):
        seen = {label: _tunnel_params(cmd)[name] for label, cmd in _FOUR.items()}
        assert len(set(seen.values())) == 1, seen

    @pytest.mark.parametrize("name", sorted(tunnel_param_names()))
    def test_identical_help_across_the_four_commands(self, name: str):
        helps = {
            label: next(p for p in cmd.params if p.name == name).help  # type: ignore[attr-defined]
            for label, cmd in _FOUR.items()
        }
        assert len(set(helps.values())) == 1, helps

    def test_upstream_basic_auth_reads_its_envvar(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HLE_UPSTREAM_BASIC_AUTH", "u:p")
        ctx = expose.make_context("expose", ["--service", "http://x", "--label", "a"])
        assert ctx.params["upstream_basic_auth"] == "u:p"

    @pytest.mark.parametrize("value", ["0", "1201"])
    def test_response_timeout_range(self, value: str):
        with pytest.raises(click.BadParameter):
            expose.make_context(
                "expose", ["--service", "http://x", "--label", "a", "--response-timeout", value]
            )


class TestSpecFromParams:
    def test_maps_click_names_to_spec_fields(self):
        spec = spec_from_params(
            service_url="http://x",
            label="a",
            auth="none",
            websocket=False,
            zone="z.example",
            apex=False,
            verify_ssl=True,
            forward_host=True,
            upstream_basic_auth="u:p",
            options=("k=v", "e=a=b"),
            response_timeout=45,
            allow=("x@y.z",),  # not a spec field: ignored
            api_key="hle_x",  # not a spec field: ignored
        )
        assert spec == TunnelSpec(
            label="a",
            service_url="http://x",
            zone="z.example",
            auth_mode="none",
            websocket_enabled=False,
            verify_ssl=True,
            forward_host=True,
            upstream_basic_auth="u:p",
            apex=False,
            options={"k": "v", "e": "a=b"},
            response_timeout=45,
        )

    def test_bad_option_pair(self):
        with pytest.raises(UsageError):
            spec_from_params(service_url="http://x", label="a", options=("novalue",))

    def test_bad_basic_auth(self):
        with pytest.raises(UsageError, match="USER:PASS"):
            spec_from_params(service_url="http://x", label="a", upstream_basic_auth="nocolon")


# --------------------------------------------------------------------------- #
# Agent protocol 1.3
# --------------------------------------------------------------------------- #


class TestAgentProtocol13:
    def test_version(self):
        assert AGENT_PROTOCOL_VERSION == "1.3"

    def test_endpoint_spec_is_a_tunnel_spec(self):
        ep = EndpointSpec(id=1, label="a", service_url="http://x")
        assert isinstance(ep, TunnelSpec)

    def test_endpoint_spec_keeps_id_first_and_1_2_order(self):
        keys = list(json.loads(_BASELINE["EndpointSpec.v1_3"]))
        assert keys[:7] == [
            "id",
            "label",
            "service_url",
            "zone",
            "auth_mode",
            "webhook_path",
            "websocket_enabled",
        ]
        assert set(keys[7:]) == _NEW_1_3_FIELDS

    def test_label_and_service_url_still_required(self):
        with pytest.raises(ValueError):
            EndpointSpec.model_validate({"id": 1, "service_url": "http://x"})
        with pytest.raises(ValueError):
            EndpointSpec.model_validate({"id": 1, "label": "a"})

    def test_1_2_endpoint_parses_on_a_1_3_agent(self):
        ep = EndpointSpec.model_validate_json(_ENDPOINT_1_2)
        assert ep.label == "ha"
        assert ep.websocket_enabled is False
        assert all(getattr(ep, name) is None for name in _NEW_1_3_FIELDS)

    def test_1_3_default_endpoint_differs_from_1_2_only_by_null_keys(self):
        new = json.loads(_BASELINE["EndpointSpec"])
        old = json.loads(_ENDPOINT_1_2)
        assert {k: v for k, v in new.items() if k in old} == old
        assert set(new) - set(old) == _NEW_1_3_FIELDS
        assert all(new[k] is None for k in _NEW_1_3_FIELDS)

    @pytest.mark.parametrize("name", ["AgentWelcome", "AgentStateSync"])
    def test_nested_endpoints_only_gain_null_keys(self, name: str):
        for ep in json.loads(_BASELINE[name])["endpoints"]:
            assert set(ep) == _ENDPOINT_1_2_FIELDS | _NEW_1_3_FIELDS
            assert all(ep[k] is None for k in _NEW_1_3_FIELDS)

    def test_1_3_endpoint_projects_down_for_a_1_2_agent(self):
        """A 1.2 agent keeps the fields it declares and drops the rest."""
        full = json.loads(_BASELINE["EndpointSpec.v1_3"])
        projected = {k: v for k, v in full.items() if k in _ENDPOINT_1_2_FIELDS}
        ep = EndpointSpec.model_validate(projected)
        assert ep.label == "prox"
        assert ep.verify_ssl is None

    def test_state_sync_with_1_3_endpoints_round_trips(self):
        raw = json.dumps(
            {"type": "state_sync", "endpoints": [json.loads(_BASELINE["EndpointSpec.v1_3"])]}
        )
        sync = AgentStateSync.model_validate_json(raw)
        assert sync.endpoints[0].upstream_basic_auth == "admin:s3cret"
        assert sync.endpoints[0].options == {"k": "v"}
        welcome = AgentWelcome(agent_public_id="p", base_domain="d", endpoints=sync.endpoints)
        again = AgentWelcome.model_validate_json(welcome.model_dump_json())
        assert again.endpoints == sync.endpoints


class TestReconcileKey:
    _BASE = EndpointSpec(id=1, label="a", service_url="http://x")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("service_url", "http://y"),
            ("zone", "z.example"),
            ("auth_mode", "none"),
            ("webhook_path", "/h"),
            ("websocket_enabled", False),
            ("verify_ssl", True),
            ("forward_host", True),
            ("upstream_basic_auth", "u:p"),
            ("apex", True),
            ("options", {"k": "v"}),
            ("response_timeout", 60),
            ("managed_by", "x"),
        ],
    )
    def test_changes_on_every_field(self, field: str, value: Any):
        changed = dataclasses.replace(self._BASE, **{field: value})
        assert changed.reconcile_key() != self._BASE.reconcile_key()

    def test_every_tunnel_field_except_label_is_covered(self):
        # One entry per field, so this fails if a new field is left out.
        assert len(self._BASE.reconcile_key()) == len(dataclasses.fields(TunnelSpec)) - 1

    @pytest.mark.parametrize(
        ("field", "default"),
        [("verify_ssl", False), ("forward_host", False), ("apex", False), ("options", {})],
    )
    def test_null_equals_the_explicit_default(self, field: str, default: Any):
        """A server switching from nulls to explicit defaults restarts nothing."""
        explicit = dataclasses.replace(self._BASE, **{field: default})
        assert explicit.reconcile_key() == self._BASE.reconcile_key()

    def test_null_response_timeout_stays_distinct(self):
        # The relay owns that default (30s, 120s for webhooks); the agent can't
        # know which, so an explicit 30 is a change.
        explicit = dataclasses.replace(self._BASE, response_timeout=30)
        assert explicit.reconcile_key() != self._BASE.reconcile_key()

    def test_stable_across_option_order_and_id(self):
        a = dataclasses.replace(self._BASE, options={"a": "1", "b": "2"})
        b = dataclasses.replace(self._BASE, id=9, options={"b": "2", "a": "1"})
        assert a.reconcile_key() == b.reconcile_key()
        hash(a.reconcile_key())


# --------------------------------------------------------------------------- #
# The agent runs a dashboard endpoint with every field
# --------------------------------------------------------------------------- #


class _FakeTunnel:
    is_connected = False
    public_url = None

    def __init__(self, config: TunnelConfig) -> None:
        self.config = config

    async def connect(self) -> None:
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        return None


class TestAgentEndpoint:
    async def test_every_field_reaches_the_tunnel(self):
        created: list[_FakeTunnel] = []

        def factory(cfg: TunnelConfig) -> _FakeTunnel:
            created.append(_FakeTunnel(cfg))
            return created[-1]

        client = AgentClient(
            "hlea_t", relay_host="r.example", relay_port=8443, tunnel_factory=factory
        )
        ep = EndpointSpec(id=7, **{**_FULL.tunnel_fields(), "managed_by": "someone-else"})
        await client.reconcile([ep])
        cfg = created[0].config
        assert cfg.verify_ssl is True
        assert cfg.forward_host is True
        assert cfg.upstream_basic_auth == ("admin", "s3:cret")
        assert cfg.options == {"k": "v"}
        assert cfg.response_timeout == 300
        assert cfg.relay_host == "r.example"
        assert cfg.relay_port == 8443
        assert cfg.api_key == "hlea_t"
        assert cfg.managed_by == "hle-agent"  # the agent manages it, whatever the spec says
        await client._stop_all()

    async def test_dashboard_edit_of_a_new_field_restarts_the_endpoint(self):
        created: list[_FakeTunnel] = []

        def factory(cfg: TunnelConfig) -> _FakeTunnel:
            created.append(_FakeTunnel(cfg))
            return created[-1]

        client = AgentClient("hlea_t", tunnel_factory=factory)
        ep = EndpointSpec(id=1, label="a", service_url="http://x")
        await client.reconcile([ep])
        await client.reconcile([dataclasses.replace(ep, forward_host=True)])
        assert len(created) == 2
        assert created[1].config.forward_host is True
        await client._stop_all()

    async def test_a_bad_endpoint_does_not_stop_the_others(self):
        created: list[_FakeTunnel] = []

        def factory(cfg: TunnelConfig) -> _FakeTunnel:
            created.append(_FakeTunnel(cfg))
            return created[-1]

        client = AgentClient("hlea_t", tunnel_factory=factory)
        bad = EndpointSpec(id=1, label="bad", service_url="http://x", upstream_basic_auth="nocolon")
        good = EndpointSpec(id=2, label="good", service_url="http://y")
        await client.reconcile([bad, good])
        assert [t.config.service_label for t in created] == ["good"]
        await client._stop_all()

    async def test_a_bad_endpoint_is_reported_with_its_reason(self):
        created: list[_FakeTunnel] = []

        def factory(cfg: TunnelConfig) -> _FakeTunnel:
            created.append(_FakeTunnel(cfg))
            return created[-1]

        client = AgentClient("hlea_t", tunnel_factory=factory)
        bad = EndpointSpec(id=1, label="bad", service_url="http://x", upstream_basic_auth="nocolon")
        good = EndpointSpec(id=2, label="good", service_url="http://y")
        await client.reconcile([bad, good])
        status = {s.label: s for s in client._build_status()}
        assert set(status) == {"bad", "good"}
        assert status["bad"].connected is False
        assert status["bad"].error is not None
        assert "upstream_basic_auth" in status["bad"].error
        assert "nocolon" not in status["bad"].error  # the value is never echoed
        assert status["good"].error is None

        # A later state_sync that fixes the spec clears the error.
        await client.reconcile([dataclasses.replace(bad, upstream_basic_auth="u:p"), good])
        status = {s.label: s for s in client._build_status()}
        assert status["bad"].error is None
        assert [t.config.service_label for t in created] == ["good", "bad"]

        # One that drops the endpoint stops reporting it.
        await client.reconcile([bad, good])
        await client.reconcile([good])
        assert [s.label for s in client._build_status()] == ["good"]
        await client._stop_all()

    async def test_a_running_endpoint_edited_into_a_bad_one_reports_the_error(self):
        client = AgentClient("hlea_t", tunnel_factory=_FakeTunnel)
        ep = EndpointSpec(id=1, label="a", service_url="http://x")
        await client.reconcile([ep])
        await client.reconcile([dataclasses.replace(ep, upstream_basic_auth="bad")])
        [status] = client._build_status()
        assert status.label == "a"
        assert status.error is not None
        await client._stop_all()


# --------------------------------------------------------------------------- #
# `hle daemon install tunnel` end to end: CLI -> unit file on disk
# --------------------------------------------------------------------------- #


class TestDaemonInstallWritesTheSpec:
    def _install(self, tmp_path: Path, *extra: str) -> Path:
        from unittest.mock import patch

        from click.testing import CliRunner

        from hle_client import service_cmd

        with (
            patch.object(service_cmd, "_require_supported", return_value="linux"),
            patch.object(service_cmd, "current_platform", return_value="linux"),
            patch.object(service_cmd, "_unit_dir", return_value=tmp_path),
            patch.object(service_cmd, "_unit_path_in_other_scope", return_value=None),
            patch.object(service_cmd, "find_hle_path", return_value="/usr/bin/hle"),
            patch.object(service_cmd.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            argv = ["daemon", "install", "tunnel", "ha", "http://localhost:8123", "--user"]
            result = CliRunner().invoke(main, [*argv, "--no-start", *extra])
        assert result.exit_code == 0, result.output
        return tmp_path / "hle-ha.service"

    def test_secret_goes_to_an_owner_only_environment(self, tmp_path: Path):
        from hle_client.service_cmd import parse_service_spec, stamped_tunnel_spec

        unit = self._install(
            tmp_path, "--upstream-basic-auth", "admin:pw", "--response-timeout", "90"
        )
        text = unit.read_text()
        exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
        assert "admin:pw" not in exec_line
        assert "--response-timeout 90" in exec_line
        assert 'Environment="HLE_UPSTREAM_BASIC_AUTH=admin:pw"' in text
        assert unit.stat().st_mode & 0o777 == 0o600
        stamp = parse_service_spec(text)
        assert stamp is not None
        spec = stamped_tunnel_spec(stamp)
        assert spec is not None
        assert spec.upstream_basic_auth == "admin:pw"
        assert spec.response_timeout == 90

    def _refresh(self, tmp_path: Path, stamp: dict[str, Any]) -> Path:
        from unittest.mock import patch

        from hle_client import service_cmd

        with (
            patch.object(service_cmd, "service_spec", return_value=stamp),
            patch.object(service_cmd, "current_platform", return_value="linux"),
            patch.object(service_cmd, "_unit_dir", return_value=tmp_path),
            patch.object(service_cmd, "_unit_path_in_other_scope", return_value=None),
            patch.object(service_cmd, "find_hle_path", return_value="/usr/bin/hle"),
            patch.object(service_cmd.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            assert service_cmd.refresh_service("hle-ha.service", True) == "refreshed"
        return tmp_path / "hle-ha.service"

    def _old_stamp(self, *run_args: str) -> dict[str, Any]:
        return {
            "version": "2609.1",
            "label": "ha",
            "run_args": list(run_args),
            "name": None,
            "run_as": None,
            "description": "HLE tunnel: ha",
            "restart": "on-failure",
            "agent_config": None,
            "user_mode": True,
        }

    def test_refresh_moves_a_secret_out_of_an_old_argv(self, tmp_path: Path):
        """A pre-TunnelSpec unit with the secret in ExecStart gets migrated."""
        from hle_client.service_cmd import parse_service_spec

        stamp = self._old_stamp(
            "expose",
            "--service",
            "http://localhost:8123",
            "--label",
            "ha",
            "--upstream-basic-auth",
            "a:b",
            "--allow",
            "x@y.z",
            "--verify-ssl",
        )
        unit = self._refresh(tmp_path, stamp)
        text = unit.read_text()
        exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
        assert "a:b" not in exec_line
        assert "--upstream-basic-auth" not in exec_line
        assert "--verify-ssl" in exec_line
        assert "--allow x@y.z" in exec_line
        assert 'Environment="HLE_UPSTREAM_BASIC_AUTH=a:b"' in text
        assert unit.stat().st_mode & 0o777 == 0o600
        # Stamped with the spec now, so the next refresh takes the new path.
        restamped = parse_service_spec(text)
        assert restamped is not None
        assert restamped["tunnel"]["upstream_basic_auth"] == "a:b"
        assert restamped["allow"] == ["x@y.z"]

    def test_refresh_ignores_the_refreshing_users_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HLE_UPSTREAM_BASIC_AUTH", "mine:secret")
        stamp = self._old_stamp("expose", "--service", "http://x", "--label", "ha")
        text = self._refresh(tmp_path, stamp).read_text()
        assert "mine:secret" not in text

    def test_unparseable_old_argv_is_replayed_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        stamp = self._old_stamp("expose", "--service", "http://x", "--no-such-flag")
        with caplog.at_level("WARNING", logger="hle_client.service_cmd"):
            text = self._refresh(tmp_path, stamp).read_text()
        assert "--no-such-flag" in text
        assert any("ha" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    def test_without_a_secret_the_unit_stays_world_readable(self, tmp_path: Path):
        unit = self._install(tmp_path, "--verify-ssl")
        assert unit.stat().st_mode & 0o004  # world-readable, as before
        assert "HLE_UPSTREAM_BASIC_AUTH" not in unit.read_text()
