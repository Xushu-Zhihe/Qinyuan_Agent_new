"""Focused contract tests for the D2 action envelope."""

from __future__ import annotations

import pytest

from agent_core.actions import (
    CompleteIntentAction,
    D2ActionEnvelope,
    ProbeAction,
    T6ActionError,
    T6ActionValidationCode,
    parse_d2_action_envelope,
    parse_t6_action,
)


def _probe() -> dict[str, object]:
    return {
        "kind": "probe",
        "script": "printf ok",
        "working_directory": "/workspace/work",
        "timeout_seconds": 30,
        "conditions": [],
    }


def test_d2_envelope_parses_probe_and_review_request() -> None:
    result = parse_d2_action_envelope({"action": _probe(), "review_requested": True})

    assert isinstance(result, D2ActionEnvelope)
    assert isinstance(result.action, ProbeAction)
    assert result.action.script == "printf ok"
    assert result.review_requested is True


def test_d2_envelope_parses_complete_intent() -> None:
    result = parse_d2_action_envelope(
        {
            "action": {"kind": "complete_intent", "fact_ids": ["fact-1"]},
            "review_requested": False,
        }
    )

    assert isinstance(result.action, CompleteIntentAction)
    assert result.action.fact_ids == ("fact-1",)
    assert result.review_requested is False


def test_d1_parser_keeps_accepting_unwrapped_actions() -> None:
    action = parse_t6_action(_probe())

    assert isinstance(action, ProbeAction)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, T6ActionValidationCode.ACTION_ENVELOPE_INVALID),
        ({"action": _probe()}, T6ActionValidationCode.ACTION_FIELD_MISSING),
        (
            {"action": _probe(), "review_requested": False, "extra": 1},
            T6ActionValidationCode.ACTION_FIELD_UNEXPECTED,
        ),
        (
            {"action": _probe(), "review_requested": 1},
            T6ActionValidationCode.ACTION_VALUE_INVALID,
        ),
    ],
)
def test_d2_envelope_rejects_invalid_outer_shape(
    raw: object, code: T6ActionValidationCode
) -> None:
    with pytest.raises(T6ActionError) as error:
        parse_d2_action_envelope(raw)

    assert error.value.validation_code is code


def test_d2_envelope_rejects_d1_only_action_kinds() -> None:
    with pytest.raises(T6ActionError) as error:
        parse_d2_action_envelope(
            {
                "action": {
                    "kind": "abandon_intent",
                    "execution_id": "execution-1",
                    "short_reason": "stop",
                },
                "review_requested": False,
            }
        )

    assert error.value.validation_code is T6ActionValidationCode.ACTION_KIND_INVALID


def test_d2_envelope_reuses_d1_nested_validation() -> None:
    with pytest.raises(T6ActionError) as error:
        parse_d2_action_envelope(
            {
                "action": {"kind": "probe", "unexpected": True},
                "review_requested": False,
            }
        )

    assert error.value.validation_code is T6ActionValidationCode.ACTION_FIELD_UNEXPECTED
