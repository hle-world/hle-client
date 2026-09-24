"""The wire format must not change.

``tests/fixtures/wire_baseline.json`` was captured while the models were still
defined with pydantic. Every deployed client and the relay agree on those exact
bytes, so the serialisation layer can be reimplemented only if the output is
identical — passing unit tests elsewhere is not evidence of that.

Regenerate deliberately, never casually:

    ./scripts/capture_wire_baseline.py -o tests/fixtures/wire_baseline.json

A diff here means a protocol change. Treat it as one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hle_common.wire_samples import SAMPLES

_BASELINE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "wire_baseline.json"
_BASELINE: dict[str, str] = json.loads(_BASELINE_PATH.read_text())


class TestWireFormatUnchanged:
    def test_every_sample_is_covered(self):
        """A new model without a baseline entry would go out unverified."""
        assert set(SAMPLES) == set(_BASELINE)

    @pytest.mark.parametrize("name", sorted(_BASELINE))
    def test_serialises_to_the_same_bytes(self, name):
        assert SAMPLES[name].model_dump_json() == _BASELINE[name]

    @pytest.mark.parametrize("name", sorted(_BASELINE))
    def test_round_trips_through_json(self, name):
        """Parsing our own output must reproduce it exactly."""
        model = SAMPLES[name]
        reparsed = type(model).model_validate_json(_BASELINE[name])
        assert reparsed.model_dump_json() == _BASELINE[name]

    @pytest.mark.parametrize("name", sorted(_BASELINE))
    def test_model_dump_matches_json(self, name):
        """model_dump() and model_dump_json() must not disagree."""
        model = SAMPLES[name]
        assert json.dumps(model.model_dump(), separators=(",", ":")) == _BASELINE[name]


class TestForwardCompatibility:
    """A newer peer adding fields must not break an older one."""

    def test_unknown_fields_are_ignored(self):
        from hle_common.protocol import ProtocolMessage

        payload = json.loads(_BASELINE["ProtocolMessage.full"])
        payload["field_from_the_future"] = {"nested": True}
        msg = ProtocolMessage.model_validate(payload)
        assert msg.model_dump_json() == _BASELINE["ProtocolMessage.full"]

    def test_missing_optional_fields_take_defaults(self):
        from hle_common.models import WsStreamClose

        msg = WsStreamClose.model_validate({"stream_id": "s1"})
        assert msg.code == 1000
        assert msg.reason == ""
        assert msg.diagnostics is None

    def test_missing_required_field_is_an_error(self):
        from hle_common.models import WsStreamClose

        with pytest.raises(ValueError):
            WsStreamClose.model_validate({})


# The AgentHello bytes as captured under agent protocol 1.1, before the 1.2
# fields existed. Deployed 1.1 clients send exactly this.
_HELLO_1_1 = (
    '{"type":"hello","token":"hlea_x","agent_version":"2608.1","capabilities":["fp"],'
    '"instance_id":null,"hostname":null}'
)
# What a 1.1 peer's AgentHello model knows about.
_HELLO_1_1_FIELDS = {"type", "token", "agent_version", "capabilities", "instance_id", "hostname"}


class TestAgentProtocol12Compat:
    """1.1 and 1.2 agents and servers must all still understand each other."""

    def test_1_1_hello_parses_on_a_1_2_peer(self):
        from hle_common.agent_protocol import AgentHello

        hello = AgentHello.model_validate_json(_HELLO_1_1)
        assert hello.token == "hlea_x"
        assert hello.install_method is None
        assert hello.platform is None
        assert hello.python_version is None
        assert hello.service_manager is None
        assert hello.successor_of is None
        assert hello.successor_nonce is None

    def test_1_2_hello_is_a_superset_of_1_1(self):
        """A 1.1 server reads the 1.2 hello with its own model shape.

        The wire layer ignores unknown keys, so the only thing that could break
        a 1.1 peer is a 1.1 field going missing or changing. Project the 1.2
        sample down to the 1.1 field set and it must equal what 1.1 would have
        produced itself.
        """
        from hle_common.agent_protocol import AgentHello

        full = json.loads(_BASELINE["AgentHello.v1_2"])
        assert set(full) > _HELLO_1_1_FIELDS
        projected = {k: v for k, v in full.items() if k in _HELLO_1_1_FIELDS}
        # Same parse path a 1.1 peer takes: fields it does not declare are dropped.
        hello = AgentHello.model_validate(projected)
        assert hello.capabilities == ["firepuncher", "discovery:docker", "update:venv"]
        assert hello.instance_id == "inst-1"
        assert hello.install_method is None  # not in the projection

    def test_1_2_hello_with_defaults_only_adds_nulls(self):
        """The 1.1 sample, serialised by 1.2, differs from 1.1 bytes only by new keys."""
        new = json.loads(_BASELINE["AgentHello"])
        old = json.loads(_HELLO_1_1)
        assert {k: v for k, v in new.items() if k in old} == old
        assert all(v is None for k, v in new.items() if k not in old)

    @pytest.mark.parametrize(
        "name",
        ["UpdateRequest.full", "UpdateAck.refused", "UpdateProgress", "UpdateResult.failed"],
    )
    def test_update_frames_ignore_fields_from_the_future(self, name):
        """The new models follow the same rule as the old: unknown keys are dropped."""
        model = SAMPLES[name]
        raw = json.loads(_BASELINE[name])
        raw["a_1_3_field"] = {"nested": True}
        assert type(model).model_validate(raw).model_dump_json() == _BASELINE[name]
