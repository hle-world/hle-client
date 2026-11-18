"""Agent protocol 1.7: dashboard override on a declared ack.

Additive only: ``DeclaredAckEntry`` gains ``overridden`` and the agent advertises
the ``declared-override`` capability, so the relay can tell an agent that a
declared endpoint is currently controlled from the HLE dashboard and the
Kubernetes operator can surface the override on the resource. Everything here is
proved round-trippable and ignorable by a peer that predates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    DECLARED_OVERRIDE_CAPABILITY,
    AgentHello,
    DeclaredAck,
    DeclaredAckEntry,
)
from hle_common.wire_samples import SAMPLES

_BASELINE: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "wire_baseline.json").read_text()
)


class TestVersion:
    def test_agent_protocol_is_1_7(self):
        assert AGENT_PROTOCOL_VERSION == "1.7"


class TestCapabilityConstant:
    def test_value(self):
        assert DECLARED_OVERRIDE_CAPABILITY == "declared-override"

    def test_is_exported_and_survives_a_hello(self):
        hello = AgentHello(token="t", capabilities=[DECLARED_OVERRIDE_CAPABILITY])
        again = AgentHello.model_validate_json(hello.model_dump_json())
        assert DECLARED_OVERRIDE_CAPABILITY in again.capabilities


class TestDeclaredAckEntryOverride:
    def test_defaults_to_false(self):
        assert DeclaredAckEntry(label="x").overridden is False

    def test_a_1_6_entry_without_the_field_parses(self):
        # Exactly what a 1.6 peer sends: the key is absent from the frame.
        entry = DeclaredAckEntry.model_validate({"label": "x", "status": "accepted"})
        assert entry.overridden is False

    def test_round_trips_with_the_field(self):
        entry = DeclaredAckEntry(label="x", status="conflict", overridden=True)
        again = DeclaredAckEntry.model_validate_json(entry.model_dump_json())
        assert again == entry
        assert again.overridden is True

    def test_round_trips_without_the_field(self):
        entry = DeclaredAckEntry(label="x")
        again = DeclaredAckEntry.model_validate_json(entry.model_dump_json())
        assert again == entry
        assert again.overridden is False

    def test_status_values_are_unchanged(self):
        assert DeclaredAckEntry(label="x").status == "accepted"
        assert DeclaredAckEntry(label="x", status="conflict").status == "conflict"
        with pytest.raises(ValueError, match="status"):
            DeclaredAckEntry(label="x", status="rejected")


class TestDeclaredAckFrame:
    def test_frame_round_trips_with_the_field(self):
        ack = DeclaredAck(
            revision=7,
            endpoints=[
                DeclaredAckEntry(label="a", status="accepted"),
                DeclaredAckEntry(label="b", status="conflict", overridden=True),
            ],
        )
        again = DeclaredAck.model_validate_json(ack.model_dump_json())
        assert again == ack
        assert [e.overridden for e in again.endpoints] == [False, True]

    def test_a_1_6_frame_loads_with_false(self):
        frame = DeclaredAck(revision=3, endpoints=[DeclaredAckEntry(label="x")])
        raw = frame.model_dump()
        del raw["endpoints"][0]["overridden"]
        assert DeclaredAck.model_validate(raw).endpoints[0].overridden is False

    def test_the_wire_sample_pins_the_field(self):
        sample = SAMPLES["DeclaredAck"]
        assert [e.overridden for e in sample.endpoints] == [False, True]
        raw = json.loads(_BASELINE["DeclaredAck"])
        assert [e["overridden"] for e in raw["endpoints"]] == [False, True]


class TestUnknownFieldsAreIgnored:
    """The new field keeps the protocol's rule: keys from the future are dropped."""

    def test_declared_ack_entry_ignores_a_future_key(self):
        entry = DeclaredAckEntry(label="x", status="conflict", overridden=True)
        raw = json.loads(entry.model_dump_json())
        raw["a_1_8_field"] = {"nested": True}
        assert DeclaredAckEntry.model_validate(raw).model_dump_json() == entry.model_dump_json()

    def test_the_declared_ack_sample_ignores_a_future_key(self):
        raw = json.loads(_BASELINE["DeclaredAck"])
        raw["a_1_8_field"] = {"nested": True}
        assert DeclaredAck.model_validate(raw).model_dump_json() == _BASELINE["DeclaredAck"]
