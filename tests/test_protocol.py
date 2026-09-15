"""Unit tests for agents/protocol.py (STEPS.md Step 11) -- pure Pydantic
validation, no LLM/network involved."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sumo_agents.agents.protocol import (
    UNTRUSTED_DATA_SYSTEM_NOTICE,
    Message,
    Proposal,
    Verdict,
    wrap_untrusted_data,
)
from sumo_agents.safety.validator import NoAction, SetGreenBounds


def _valid_proposal_json(action: dict) -> str:
    return json.dumps(
        {
            "junction_id": "J07",
            "action": action,
            "urgency": "medium",
            "rationale": "Hàng đợi hướng Bắc đang tăng nhanh.",
        }
    )


def test_proposal_parses_set_green_bounds_into_the_correct_concrete_type() -> None:
    action = {
        "type": "set_green_bounds",
        "junction_id": "J07",
        "phase_id": "0",
        "min_green_s": 20.0,
        "max_green_s": 90.0,
    }
    proposal = Proposal.model_validate_json(_valid_proposal_json(action))

    assert isinstance(proposal.action, SetGreenBounds)
    assert proposal.action.max_green_s == 90.0
    assert proposal.urgency == "medium"


def test_proposal_parses_no_action() -> None:
    action = {"type": "no_action", "junction_id": "J07"}
    proposal = Proposal.model_validate_json(_valid_proposal_json(action))

    assert isinstance(proposal.action, NoAction)


def test_proposal_with_unknown_action_type_raises_clearly() -> None:
    # This is the DoD case: a model returning JSON that doesn't match the
    # schema (here, an action `type` outside the 3 known literals) must
    # raise, not silently pass through as some default action.
    action = {"type": "teleport_all_vehicles", "junction_id": "J07"}

    with pytest.raises(ValidationError) as exc_info:
        Proposal.model_validate_json(_valid_proposal_json(action))

    message = str(exc_info.value)
    assert "action" in message
    assert "teleport_all_vehicles" in message


def test_proposal_with_missing_required_field_raises() -> None:
    payload = {
        "junction_id": "J07",
        "action": {"type": "no_action", "junction_id": "J07"},
        "urgency": "low",
        # "rationale" missing entirely
    }
    with pytest.raises(ValidationError, match="rationale"):
        Proposal.model_validate_json(json.dumps(payload))


def test_proposal_with_invalid_urgency_literal_raises() -> None:
    payload = {
        "junction_id": "J07",
        "action": {"type": "no_action", "junction_id": "J07"},
        "urgency": "extremely_high",  # not one of low/medium/high
        "rationale": "x",
    }
    with pytest.raises(ValidationError, match="urgency"):
        Proposal.model_validate_json(json.dumps(payload))


def test_message_round_trips_and_rejects_bad_intent() -> None:
    valid = Message(
        sender="J07",
        recipients=["J08"],
        intent="request_help",
        payload={"queue_len": 12},
        rationale="Cần J08 xả bớt xe hướng vào J07.",
    )
    assert valid.intent == "request_help"

    with pytest.raises(ValidationError, match="intent"):
        Message.model_validate(
            {
                "sender": "J07",
                "recipients": ["J08"],
                "intent": "shout",  # not a valid intent
                "payload": {},
                "rationale": "x",
            }
        )


def test_verdict_approved_does_not_need_a_modified_action() -> None:
    verdict = Verdict(junction_id="J07", decision="approved", reason="Trong giới hạn an toàn.")
    assert verdict.modified_action is None


def test_verdict_modified_without_modified_action_raises() -> None:
    with pytest.raises(ValidationError, match="modified_action"):
        Verdict(junction_id="J07", decision="modified", reason="Cần giảm delta_s.")


def test_verdict_modified_with_action_is_valid() -> None:
    verdict = Verdict(
        junction_id="J07",
        decision="modified",
        modified_action=SetGreenBounds(junction_id="J07", phase_id="0", min_green_s=15.0, max_green_s=50.0),
        reason="Giảm max_green_s từ 90 xuống 50 để tránh dao động.",
    )
    assert isinstance(verdict.modified_action, SetGreenBounds)


def test_wrap_untrusted_data_has_unambiguous_delimiters() -> None:
    wrapped = wrap_untrusted_data("osm_road_names", "Đường Láng; Đường Giải Phóng")

    assert wrapped.startswith("<<<UNTRUSTED_DATA label='osm_road_names'>>>")
    assert wrapped.endswith("<<<END_UNTRUSTED_DATA>>>")
    assert "Đường Láng; Đường Giải Phóng" in wrapped


def test_untrusted_data_system_notice_mentions_the_delimiter_markers() -> None:
    assert "<<<UNTRUSTED_DATA" in UNTRUSTED_DATA_SYSTEM_NOTICE
    assert "<<<END_UNTRUSTED_DATA>>>" in UNTRUSTED_DATA_SYSTEM_NOTICE
