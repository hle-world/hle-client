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
