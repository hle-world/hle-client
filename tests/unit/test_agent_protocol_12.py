"""Agent protocol 1.2: update_* messages, the richer hello, and HANDOVER."""

from __future__ import annotations

import pytest

from hle_common import close_codes
from hle_common.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    SELF_UPDATE_METHODS,
    AgentHello,
    AgentMsgType,
    UpdateAck,
    UpdateProgress,
    UpdateRequest,
    UpdateResult,
    update_capability,
)


class TestVersion:
    def test_is_1_2(self):
        assert AGENT_PROTOCOL_VERSION == "1.2"

    def test_update_types_are_registered(self):
        assert AgentMsgType.UPDATE_REQUEST == "update_request"
        assert AgentMsgType.UPDATE_ACK == "update_ack"
        assert AgentMsgType.UPDATE_PROGRESS == "update_progress"
        assert AgentMsgType.UPDATE_RESULT == "update_result"
        # Every model's default `type` is the registered string.
        assert UpdateRequest(request_id="r", target_version="v").type == "update_request"
        assert UpdateAck(request_id="r", accepted=True).type == "update_ack"
        assert UpdateProgress(request_id="r", phase="staging").type == "update_progress"
        assert (
            UpdateResult(request_id="r", ok=True, from_version="a", to_version="b").type
            == "update_result"
        )


class TestUpdateModels:
    def test_request_defaults(self):
        req = UpdateRequest.model_validate({"request_id": "r", "target_version": "2609.9"})
        assert req.deadline_s == 300
        assert req.drain_policy == "wait"

    def test_request_requires_a_target(self):
        with pytest.raises(ValueError):
            UpdateRequest.model_validate({"request_id": "r"})

    def test_ack_round_trips_reason(self):
        ack = UpdateAck.model_validate_json(
            UpdateAck(request_id="r", accepted=False, reason="busy:draining").model_dump_json()
        )
        assert ack.accepted is False
        assert ack.reason == "busy:draining"

    def test_progress_detail_is_optional(self):
        prog = UpdateProgress.model_validate({"request_id": "r", "phase": "swapping"})
        assert prog.detail is None

    def test_result_log_tail_defaults_empty_and_is_not_shared(self):
        a = UpdateResult(request_id="r", ok=True, from_version="1", to_version="2")
        b = UpdateResult(request_id="r", ok=True, from_version="1", to_version="2")
        a.log_tail.append("x")
        assert b.log_tail == []

    def test_result_parses_log_tail(self):
        res = UpdateResult.model_validate(
            {
                "request_id": "r",
                "ok": False,
                "from_version": "1",
                "to_version": "2",
                "log_tail": ["a", "b"],
            }
        )
        assert res.log_tail == ["a", "b"]


class TestUpdateCapability:
    @pytest.mark.parametrize("method", ["venv", "pipx", "uv"])
    def test_self_owned_venvs_can_update(self, method):
        assert update_capability(method) == f"update:{method}"

    @pytest.mark.parametrize(
        "method", ["brew", "docker", "pip", "externally-managed", "unknown", None, ""]
    )
    def test_everything_else_cannot(self, method):
        assert update_capability(method) is None

    def test_matches_the_cli_classifier_vocabulary(self):
        """The detector's identifiers and this mapping must not drift apart."""
        from hle_client import update_cmd

        assert {update_cmd.VENV, update_cmd.PIPX, update_cmd.UV} == SELF_UPDATE_METHODS
        assert update_capability(update_cmd.BREW) is None
        assert update_capability(update_cmd.PIP) is None
        assert update_capability(update_cmd.EXTERNALLY_MANAGED) is None


class TestHello12Fields:
    def test_all_new_fields_default_to_none(self):
        hello = AgentHello(token="t")
        for name in (
            "install_method",
            "platform",
            "python_version",
            "service_manager",
            "successor_of",
            "successor_nonce",
        ):
            assert getattr(hello, name) is None

    def test_successor_fields_travel_together(self):
        hello = AgentHello(token="t", successor_of="inst-0", successor_nonce="n")
        again = AgentHello.model_validate_json(hello.model_dump_json())
        assert (again.successor_of, again.successor_nonce) == ("inst-0", "n")


class TestHandoverCloseCode:
    def test_value(self):
        assert close_codes.HANDOVER == 4011

    def test_is_not_fatal(self):
        """Nothing went wrong: the caller must not report an error to the operator."""
        assert not close_codes.is_fatal(close_codes.HANDOVER)

    def test_but_must_not_reconnect(self):
        """Reconnecting would take the endpoints back off the successor."""
        assert not close_codes.should_reconnect(close_codes.HANDOVER)
        assert close_codes.retry_after_seconds(close_codes.HANDOVER) is None

    @pytest.mark.parametrize("code", sorted(close_codes.FATAL_CODES))
    def test_fatal_codes_do_not_reconnect_either(self, code):
        assert not close_codes.should_reconnect(code)

    @pytest.mark.parametrize("code", [1000, 1006, close_codes.TOO_MANY_REGISTRATIONS, None])
    def test_ordinary_closes_reconnect(self, code):
        assert close_codes.should_reconnect(code)

    def test_fatal_and_no_reconnect_sets_are_disjoint(self):
        """A caller needs to tell "stop, something is wrong" from "stop, all good"."""
        assert not (close_codes.FATAL_CODES & close_codes.NO_RECONNECT_CODES)

    def test_existing_codes_unchanged(self):
        assert close_codes.REPLACED == 4009
        assert close_codes.DUPLICATE_INSTANCE == 4010
        assert close_codes.TOO_MANY_REGISTRATIONS == 4029
        assert {4001, 4003, 4004, 4009, 4010} == close_codes.FATAL_CODES
