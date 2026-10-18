"""Confirmed T6 action shapes and conservative parsing."""

import base64
import hashlib

import pytest

from agent_core.actions import (
    AbandonIntentAction,
    CandidateFlagAction,
    CompleteIntentAction,
    ProbeAction,
    T6ActionError,
    parse_t6_action,
)


def test_probe_parses_exploration_and_fixed_conditions():
    action = parse_t6_action(
        {
            "kind": "probe",
            "script": "printf hello",
            "stdin_base64": base64.b64encode(b"input").decode(),
            "working_directory": "/workspace/work",
            "timeout_seconds": 60,
            "conditions": [
                {
                    "kind": "output_contains",
                    "step_index": 0,
                    "stream": "stdout",
                    "literal": "hello",
                },
                {
                    "kind": "file_sha256",
                    "step_index": 0,
                    "path": "/workspace/work/result",
                    "expected_sha256": hashlib.sha256(b"result").hexdigest(),
                },
            ],
        }
    )
    assert isinstance(action, ProbeAction)
    assert action.stdin == b"input"
    assert len(action.conditions) == 2
    exploration = parse_t6_action(
        {
            "kind": "probe",
            "script": "pwd",
            "working_directory": "/workspace/work",
            "timeout_seconds": 5,
            "conditions": [],
        }
    )
    assert isinstance(exploration, ProbeAction) and exploration.conditions == ()


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "ignored"},
        {"timeout_seconds": True},
        {"working_directory": "/workspace/input"},
        {"stdin_base64": "not base64"},
        {"script": "x" * (64 * 1024 + 1)},
        {
            "conditions": [
                {
                    "kind": "output_contains",
                    "step_index": 1,
                    "stream": "stdout",
                    "literal": "x",
                }
            ]
        },
    ],
)
def test_probe_rejects_invalid_fields(change):
    payload = {
        "kind": "probe",
        "script": "pwd",
        "working_directory": "/workspace/work",
        "timeout_seconds": 5,
        "conditions": [],
    }
    payload.update(change)
    with pytest.raises(T6ActionError):
        parse_t6_action(payload)


def test_terminal_actions_have_distinct_strict_shapes():
    assert isinstance(
        parse_t6_action({"kind": "complete_intent", "fact_ids": ["fact"]}),
        CompleteIntentAction,
    )
    assert isinstance(
        parse_t6_action(
            {
                "kind": "abandon_intent",
                "execution_id": "execution",
                "short_reason": "not applicable",
            }
        ),
        AbandonIntentAction,
    )
    assert isinstance(
        parse_t6_action(
            {
                "kind": "candidate_flag",
                "execution_id": "execution",
                "value": "flag{candidate}",
            }
        ),
        CandidateFlagAction,
    )
    for payload in (
        {"kind": "complete_intent", "fact_ids": []},
        {"kind": "complete_intent", "fact_ids": ["fact", "fact"]},
        {
            "kind": "abandon_intent",
            "execution_id": "execution",
            "short_reason": "x" * 257,
        },
        {"kind": "candidate_flag", "execution_id": "execution", "value": " "},
        {
            "kind": "candidate_flag",
            "execution_id": "execution",
            "value": "x",
            "validated": True,
        },
    ):
        with pytest.raises(T6ActionError):
            parse_t6_action(payload)
