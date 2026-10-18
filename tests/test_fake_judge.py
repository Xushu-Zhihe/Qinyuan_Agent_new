"""Coverage for the test-only D7 FakeJudge harness."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from fake_judge_fixture import FakeJudge, run_fake_judge_loop

from agent_core.goal_candidates import D7AnswerCandidate


def _candidate(task_run_id: str, ordinal: int) -> D7AnswerCandidate:
    return D7AnswerCandidate(
        task_run_id=task_run_id,
        candidate_id=str(uuid4()),
        turn_id=str(uuid4()),
        intent_id=str(uuid4()),
        plan_id=str(uuid4()),
        execution_id=str(uuid4()),
        evidence_version=(f"{ordinal:x}" * 64)[:64],
        source_ref=f"evidence:{ordinal}",
    )


class Controller:
    def __init__(self):
        self.candidates = []
        self.feedback = []

    def register_answer_candidate(self, candidate, mutation_id):
        self.candidates.append((candidate, mutation_id))

    def apply_external_validation_feedback(self, feedback):
        self.feedback.append(feedback)


def test_fake_judge_rejects_fake_then_accepts_correct_value():
    task_run_id = str(uuid4())
    controller = Controller()
    values = {0: "fake", 1: "correct"}

    def make_candidate(ordinal, previous):
        assert (ordinal == 0) is (previous is None)
        return _candidate(task_run_id, ordinal)

    chosen, feedback = run_fake_judge_loop(
        controller,
        judge=FakeJudge(),
        make_candidate=make_candidate,
        read_value=lambda candidate: values[int(candidate.source_ref.rsplit(":", 1)[1])],
    )

    assert chosen is controller.candidates[1][0]
    assert [item.verdict for item in feedback] == ["rejected", "accepted"]
    assert [item.verdict for item in controller.feedback] == ["rejected", "accepted"]
    assert len({item.operation_id for item in feedback}) == 2


def test_fake_judge_budget_exhaustion_does_not_claim_success():
    task_run_id = str(uuid4())
    controller = Controller()
    with pytest.raises(RuntimeError, match="fake_judge_candidate_budget_exhausted"):
        run_fake_judge_loop(
            controller,
            judge=FakeJudge(accepted_value="correct"),
            make_candidate=lambda ordinal, previous: _candidate(task_run_id, ordinal),
            read_value=lambda candidate: "fake",
            max_candidates=2,
        )
    assert [item.verdict for item in controller.feedback] == ["rejected", "rejected"]


def test_fake_judge_does_not_accept_source_ref_as_candidate_value():
    candidate = _candidate(str(uuid4()), 0)
    judge = FakeJudge()
    candidate = replace(candidate, source_ref="correct")
    # The reader is the sole source of candidate content; source_ref remains
    # provenance and cannot turn a fake value into an accepted result.
    feedback = judge.evaluate(candidate, lambda _candidate: "fake")
    assert feedback.verdict == "rejected"
