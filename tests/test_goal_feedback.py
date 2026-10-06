from dataclasses import replace
from uuid import uuid4

import pytest

from agent_core.goal_feedback import (
    MAX_EXTERNAL_VALIDATION_FEEDBACK_JSON_BYTES,
    MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES,
    ExternalValidationFeedback,
    ExternalValidationFeedbackConflict,
    ExternalValidationReconciliation,
    external_validation_feedback_from_json,
    project_candidate_feedback,
    require_exact_feedback_replay,
)
from agent_core.run_outcome import D7RunOutcomeState, map_run_outcome


def _feedback(**changes):
    values = {
        "task_run_id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "evidence_version": "a" * 64,
        "verdict": "rejected",
        "source_ref": "judge-result:case-17",
        "operation_id": str(uuid4()),
    }
    values.update(changes)
    return ExternalValidationFeedback(**values)


def _reconciliation(original, **changes):
    values = {
        "task_run_id": original.task_run_id,
        "candidate_id": original.candidate_id,
        "evidence_version": original.evidence_version,
        "verdict": "accepted",
        "source_ref": "judge-query:case-17",
        "operation_id": str(uuid4()),
        "resolves_operation_id": original.operation_id,
    }
    values.update(changes)
    return ExternalValidationReconciliation(**values)


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "unknown"])
def test_submission_json_round_trip_preserves_binding(verdict):
    feedback = _feedback(verdict=verdict)

    restored = external_validation_feedback_from_json(feedback.to_json())

    assert restored == feedback
    assert restored.to_dict()["schema_version"] == 1
    assert restored.to_dict()["feedback_kind"] == "submission"


def test_reconciliation_json_round_trip_binds_original_and_query_operation_ids():
    original = _feedback(verdict="unknown")
    reconciliation = _reconciliation(original)

    restored = external_validation_feedback_from_json(reconciliation.to_json())

    assert restored == reconciliation
    assert restored.feedback_kind == "reconciliation"
    assert restored.operation_id == reconciliation.operation_id
    assert restored.resolves_operation_id == original.operation_id


@pytest.mark.parametrize(
    "changes",
    [
        {"task_run_id": "not-a-uuid"},
        {"candidate_id": "not-a-uuid"},
        {"operation_id": "not-a-uuid"},
        {"evidence_version": "A" * 64},
        {"evidence_version": "a" * 63},
        {"verdict": "pending"},
        {"source_ref": " \t "},
    ],
)
def test_submission_rejects_malformed_identity_decision_and_reference(changes):
    with pytest.raises(ValueError):
        _feedback(**changes)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "extra": "not in schema"},
        lambda value: {key: item for key, item in value.items() if key != "source_ref"},
        lambda value: {**value, "schema_version": True},
        lambda value: {**value, "schema_version": 2},
        lambda value: {**value, "feedback_kind": "reconciliation"},
        lambda value: {**value, "resolves_operation_id": str(uuid4())},
    ],
)
def test_submission_json_rejects_unknown_missing_or_wrong_kind_fields(mutate):
    record = _feedback()

    with pytest.raises(ValueError):
        ExternalValidationFeedback.from_dict(mutate(record.to_dict()))


def test_json_rejects_duplicate_object_keys():
    record = _feedback()
    encoded = record.to_json()
    duplicate = encoded[:-1] + ',"verdict":"accepted"}'

    with pytest.raises(ValueError, match="duplicate fields"):
        external_validation_feedback_from_json(duplicate)


def test_source_reference_uses_utf8_byte_limit_without_truncation():
    valid = _feedback(source_ref="r" * MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES)
    assert (
        len(valid.source_ref.encode("utf-8"))
        == MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES
    )

    with pytest.raises(ValueError, match="4096 UTF-8 bytes"):
        _feedback(source_ref="r" * (MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES + 1))

    with pytest.raises(ValueError, match="4096 UTF-8 bytes"):
        _feedback(source_ref="é" * (MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES // 2 + 1))


def test_json_input_over_one_mib_is_rejected_before_parsing():
    record = _feedback()
    oversized = record.to_json() + " " * (
        MAX_EXTERNAL_VALIDATION_FEEDBACK_JSON_BYTES - len(record.to_json()) + 1
    )

    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        external_validation_feedback_from_json(oversized)


def test_exact_operation_replay_returns_original_record():
    original = _feedback()
    replay = ExternalValidationFeedback.from_json(original.to_json())

    assert require_exact_feedback_replay(original, replay) is original


@pytest.mark.parametrize(
    "change",
    [
        {"task_run_id": str(uuid4())},
        {"candidate_id": str(uuid4())},
        {"evidence_version": "b" * 64},
        {"verdict": "accepted"},
        {"source_ref": "judge-result:case-18"},
    ],
)
def test_reused_submission_operation_id_rejects_changed_body(change):
    original = _feedback()
    conflicting = replace(original, **change)

    with pytest.raises(ExternalValidationFeedbackConflict):
        require_exact_feedback_replay(original, conflicting)


def test_query_operation_replay_is_exact_and_changed_body_is_rejected():
    original = _feedback(verdict="unknown")
    query = _reconciliation(original)
    replay = ExternalValidationReconciliation.from_json(query.to_json())
    changed = replace(query, verdict="rejected")

    assert require_exact_feedback_replay(query, replay) is query
    with pytest.raises(ExternalValidationFeedbackConflict):
        require_exact_feedback_replay(query, changed)


def test_unknown_submission_stays_append_only_until_read_only_reconciliation():
    original = _feedback(verdict="unknown")
    resolution = _reconciliation(original, verdict="accepted")
    projection = project_candidate_feedback(
        original.task_run_id,
        original.candidate_id,
        original.evidence_version,
        (original, resolution),
    )

    assert original.verdict == "unknown"
    assert projection.state == "accepted"
    assert projection.accepted_operation_ids == (resolution.operation_id,)
    assert projection.unresolved_unknown_operation_ids == ()


def test_unresolved_unknown_is_not_interpreted_as_acceptance_or_rejection():
    original = _feedback(verdict="unknown")
    projection = project_candidate_feedback(
        original.task_run_id,
        original.candidate_id,
        original.evidence_version,
        (original,),
    )

    assert projection.state == "unknown"
    assert projection.accepted_operation_ids == ()
    assert projection.rejected_operation_ids == ()
    assert projection.unresolved_unknown_operation_ids == (original.operation_id,)


def test_opposite_definite_submission_decisions_project_as_conflict():
    accepted = _feedback(verdict="accepted")
    rejected = replace(
        accepted,
        verdict="rejected",
        operation_id=str(uuid4()),
        source_ref="judge-result:second-opinion",
    )
    projection = project_candidate_feedback(
        accepted.task_run_id,
        accepted.candidate_id,
        accepted.evidence_version,
        (accepted, rejected),
    )

    assert projection.state == "conflict"
    assert projection.conflict_operation_ids == tuple(
        sorted((accepted.operation_id, rejected.operation_id))
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_opposite_verdict_conflict_is_independent_of_append_order(reverse):
    accepted = _feedback(verdict="accepted")
    rejected = replace(
        accepted,
        verdict="rejected",
        operation_id=str(uuid4()),
        source_ref="judge-result:second-opinion",
    )
    records = (accepted, rejected)
    if reverse:
        records = tuple(reversed(records))

    projection = project_candidate_feedback(
        accepted.task_run_id,
        accepted.candidate_id,
        accepted.evidence_version,
        records,
    )

    assert projection.state == "conflict"
    assert len(projection.conflict_operation_ids) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_changed_body_for_existing_operation_id_never_overwrites(reverse):
    original = _feedback(verdict="unknown")
    conflicting = replace(original, verdict="accepted")
    records = (original, conflicting)
    if reverse:
        records = tuple(reversed(records))

    with pytest.raises(ExternalValidationFeedbackConflict):
        project_candidate_feedback(
            original.task_run_id,
            original.candidate_id,
            original.evidence_version,
            records,
        )


def test_same_verdict_from_distinct_operation_ids_remains_two_records():
    accepted = _feedback(verdict="accepted")
    second = replace(
        accepted,
        operation_id=str(uuid4()),
        source_ref="judge-result:separate-query",
    )
    projection = project_candidate_feedback(
        accepted.task_run_id,
        accepted.candidate_id,
        accepted.evidence_version,
        (accepted, second),
    )

    assert projection.state == "accepted"
    assert projection.accepted_operation_ids == tuple(
        sorted((accepted.operation_id, second.operation_id))
    )


def test_opposite_read_only_reconciliations_project_as_conflict():
    original = _feedback(verdict="unknown")
    accepted = _reconciliation(original, verdict="accepted")
    rejected = _reconciliation(original, verdict="rejected")
    projection = project_candidate_feedback(
        original.task_run_id,
        original.candidate_id,
        original.evidence_version,
        (original, accepted, rejected),
    )

    assert projection.state == "conflict"
    assert projection.conflict_operation_ids == tuple(
        sorted((accepted.operation_id, rejected.operation_id))
    )


def test_unresolved_unknown_prevents_projection_from_claiming_acceptance():
    unknown = _feedback(verdict="unknown")
    accepted = _feedback(
        task_run_id=unknown.task_run_id,
        candidate_id=unknown.candidate_id,
        evidence_version=unknown.evidence_version,
        verdict="accepted",
    )
    projection = project_candidate_feedback(
        unknown.task_run_id,
        unknown.candidate_id,
        unknown.evidence_version,
        (unknown, accepted),
    )

    assert projection.state == "unknown"
    assert projection.accepted_operation_ids == (accepted.operation_id,)
    assert projection.unresolved_unknown_operation_ids == (unknown.operation_id,)


@pytest.mark.parametrize(
    "task_run_status",
    ["achieved", "cancelled", "deadline_exceeded"],
)
def test_unknown_plus_accepted_feedback_blocks_success_cancel_and_deadline_codes(
    task_run_status,
):
    unknown = _feedback(verdict="unknown")
    accepted = _feedback(
        task_run_id=unknown.task_run_id,
        candidate_id=unknown.candidate_id,
        evidence_version=unknown.evidence_version,
        verdict="accepted",
    )
    projection = project_candidate_feedback(
        unknown.task_run_id,
        unknown.candidate_id,
        unknown.evidence_version,
        (unknown, accepted),
    )
    outcome = map_run_outcome(
        D7RunOutcomeState(
            task_run_status=task_run_status,
            goal_evaluation="accepted",
            cleanup_state="confirmed",
            unresolved_operation_unknown=projection.state == "unknown",
        )
    )

    assert projection.state == "unknown"
    assert outcome.run_exit_code == 5


def test_conflicting_feedback_requires_recovery_before_run_can_succeed():
    accepted = _feedback(verdict="accepted")
    rejected = replace(
        accepted,
        verdict="rejected",
        operation_id=str(uuid4()),
        source_ref="judge-result:second-opinion",
    )
    projection = project_candidate_feedback(
        accepted.task_run_id,
        accepted.candidate_id,
        accepted.evidence_version,
        (accepted, rejected),
    )
    outcome = map_run_outcome(
        D7RunOutcomeState(
            task_run_status="achieved",
            goal_evaluation="accepted",
            cleanup_state="confirmed",
            recovery_required=projection.state == "conflict",
        )
    )

    assert projection.state == "conflict"
    assert outcome.run_exit_code == 5


@pytest.mark.parametrize(
    "original",
    [_feedback(verdict="accepted"), _feedback(verdict="rejected")],
)
def test_reconciliation_must_resolve_a_recorded_unknown_submission(original):
    resolution = _reconciliation(original)

    with pytest.raises(ExternalValidationFeedbackConflict, match="unknown submission"):
        project_candidate_feedback(
            original.task_run_id,
            original.candidate_id,
            original.evidence_version,
            (original, resolution),
        )


def test_reconciliation_for_missing_submission_is_rejected():
    original = _feedback(verdict="unknown")
    resolution = _reconciliation(original)

    with pytest.raises(ExternalValidationFeedbackConflict, match="unknown submission"):
        project_candidate_feedback(
            original.task_run_id,
            original.candidate_id,
            original.evidence_version,
            (resolution,),
        )


def test_projection_rejects_records_outside_requested_candidate_version():
    record = _feedback()

    with pytest.raises(ValueError, match="outside the requested"):
        project_candidate_feedback(
            record.task_run_id,
            record.candidate_id,
            "b" * 64,
            (record,),
        )
