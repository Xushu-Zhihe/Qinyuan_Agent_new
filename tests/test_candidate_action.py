"""Strict parsing for the D3 candidate action boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_core.actions import CompleteIntentAction, ProbeAction
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.dual_llm_modeling import (
    MAX_D3_CANDIDATE_VALUE_BYTES,
    D3ActionProposal,
    D3AuditedModel,
    D3CandidateFlagAction,
    D3ProposalRejected,
    parse_d3_action,
)


def _candidate_action(
    *,
    value: object = "flag{synthetic}",
    execution_id: object | None = None,
    evidence_version: object = "a" * 64,
    source_ref: object = "artifact:v1",
) -> dict[str, object]:
    return {
        "action": {
            "kind": "candidate_flag",
            "value": value,
            "execution_id": (
                execution_id if execution_id is not None else str(uuid4())
            ),
            "evidence_version": evidence_version,
            "source_ref": source_ref,
        },
        "review_requested": False,
    }


def test_candidate_action_preserves_original_text_and_binding_fields():
    original = " \tflag{原文不规范化}\n"
    execution_id = str(uuid4())
    evidence_version = "b" * 64

    result = parse_d3_action(
        _candidate_action(
            value=original,
            execution_id=execution_id,
            evidence_version=evidence_version,
            source_ref=" artifact:v1 ",
        ),
        "inner_candidate",
    )

    assert isinstance(result, D3ActionProposal)
    assert isinstance(result.action, D3CandidateFlagAction)
    assert result.action.value == original
    assert result.action.execution_id == execution_id
    assert result.action.evidence_version == evidence_version
    assert result.action.source_ref == " artifact:v1 "
    assert result.review_requested is False


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("x" * MAX_D3_CANDIDATE_VALUE_BYTES, True),
        ("x" * (MAX_D3_CANDIDATE_VALUE_BYTES + 1), False),
        ("界" * (MAX_D3_CANDIDATE_VALUE_BYTES // 3), True),
        ("界" * (MAX_D3_CANDIDATE_VALUE_BYTES // 3) + "x", True),
        ("界" * (MAX_D3_CANDIDATE_VALUE_BYTES // 3 + 1), False),
        ("", False),
        ("\x00", False),
        ("\ud800", False),
        (" \t\n", True),
    ],
    ids=[
        "ascii_at_limit",
        "ascii_over_limit",
        "unicode_below_limit",
        "unicode_exact_limit",
        "unicode_over_limit",
        "empty",
        "nul",
        "invalid_utf8",
        "whitespace_preserved",
    ],
)
def test_candidate_value_uses_exact_utf8_bytes_without_normalizing(value, accepted):
    if accepted:
        proposal = parse_d3_action(_candidate_action(value=value), "inner_candidate")
        assert isinstance(proposal.action, D3CandidateFlagAction)
        assert proposal.action.value == value
    else:
        with pytest.raises(D3ProposalRejected):
            parse_d3_action(_candidate_action(value=value), "inner_candidate")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("execution_id", "not-a-uuid"),
        ("execution_id", ""),
        ("evidence_version", "A" * 64),
        ("evidence_version", "a" * 63),
        ("source_ref", " \t "),
        ("source_ref", "bad\x00ref"),
        ("source_ref", "\ud800"),
    ],
)
def test_candidate_action_rejects_invalid_binding_metadata(field, value):
    action = _candidate_action()
    payload = action["action"]
    assert isinstance(payload, dict)
    payload[field] = value

    with pytest.raises(D3ProposalRejected):
        parse_d3_action(action, "inner_candidate")


@pytest.mark.parametrize(
    "raw",
    [
        {"action": {"kind": "candidate_flag"}},
        {
            **_candidate_action(),
            "unexpected": True,
        },
        {
            **_candidate_action(),
            "review_requested": 1,
        },
        {
            "action": {
                **_candidate_action()["action"],
                "extra": True,
            },
            "review_requested": False,
        },
        {
            "action": {
                **_candidate_action()["action"],
                "kind": "probe",
            },
            "review_requested": False,
        },
    ],
)
def test_candidate_action_envelope_is_exact(raw):
    with pytest.raises(D3ProposalRejected):
        parse_d3_action(raw, "inner_candidate")


def test_candidate_parser_does_not_echo_model_supplied_body_in_errors():
    secret = "synthetic-candidate-body"

    with pytest.raises(D3ProposalRejected) as error:
        parse_d3_action(_candidate_action(value=secret + "\x00"), "inner_candidate")

    assert secret not in str(error.value)


def test_candidate_stage_waits_for_protected_persistence_before_model_dispatch():
    model = object.__new__(D3AuditedModel)

    with pytest.raises(
        D3RecoveryRequired, match="d3_candidate_protected_store_required"
    ):
        asyncio.run(
            model.propose(
                SimpleNamespace(stage="inner_candidate"),
                SimpleNamespace(is_cancelled=False),
            )
        )


@pytest.mark.parametrize("stage", ["inner_size", "inner_sha256", "inner_complete"])
def test_existing_d3_action_stages_remain_stage_restricted(stage):
    raw = {
        "action": {
            "kind": "candidate_flag",
            "value": "candidate",
            "execution_id": str(uuid4()),
            "evidence_version": "a" * 64,
            "source_ref": "artifact:v1",
        },
        "review_requested": False,
    }

    with pytest.raises(D3ProposalRejected):
        parse_d3_action(raw, stage)


@pytest.mark.parametrize("stage", ["inner_size", "inner_sha256"])
def test_existing_probe_stages_keep_their_action_contract(stage):
    raw = {
        "action": {
            "kind": "probe",
            "script": "printf ok",
            "working_directory": "/workspace/work",
            "timeout_seconds": 5,
            "conditions": [],
        },
        "review_requested": True,
    }

    result = parse_d3_action(raw, stage)

    assert isinstance(result.action, ProbeAction)
    assert result.action.script == "printf ok"
    assert result.review_requested is True


def test_existing_complete_stage_keeps_its_action_contract():
    result = parse_d3_action(
        {
            "action": {"kind": "complete_intent", "fact_ids": ["fact-1"]},
            "review_requested": False,
        },
        "inner_complete",
    )

    assert result.action == CompleteIntentAction(("fact-1",))
