"""Test-only trusted adapter for the D7 simulated acceptance harness."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from agent_core.controller import TaskRunController
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.goal_feedback import ExternalValidationFeedback

CandidateValueReader = Callable[[D7AnswerCandidate], str]
CandidateFactory = Callable[[int, ExternalValidationFeedback | None], D7AnswerCandidate]


@dataclass(frozen=True, slots=True)
class FakeJudge:
    """Deterministic test judge that reads exact candidate text."""

    accepted_value: str = "correct"
    source_ref: str = "fake-judge"

    def __post_init__(self) -> None:
        if not isinstance(self.accepted_value, str) or not self.accepted_value:
            raise ValueError("accepted_value must be non-empty text")
        if "\x00" in self.accepted_value:
            raise ValueError("accepted_value must not contain NUL")
        if not isinstance(self.source_ref, str) or not self.source_ref:
            raise ValueError("source_ref must be non-empty text")

    def evaluate(
        self,
        candidate: D7AnswerCandidate,
        read_value: CandidateValueReader,
    ) -> ExternalValidationFeedback:
        value = read_value(candidate)
        if not isinstance(value, str):
            raise TypeError("candidate reader must return text")
        verdict = "accepted" if value == self.accepted_value else "rejected"
        return ExternalValidationFeedback(
            task_run_id=candidate.task_run_id,
            candidate_id=candidate.candidate_id,
            evidence_version=candidate.evidence_version,
            verdict=verdict,
            source_ref=self.source_ref,
            operation_id=str(uuid4()),
        )


def run_fake_judge_loop(
    controller: TaskRunController,
    *,
    judge: FakeJudge,
    make_candidate: CandidateFactory,
    read_value: CandidateValueReader,
    max_candidates: int = 2,
    candidate_already_registered: bool = False,
) -> tuple[D7AnswerCandidate, tuple[ExternalValidationFeedback, ...]]:
    """Drive a fake rejection and retry through the production Controller."""
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool):
        raise TypeError("max_candidates must be an integer")
    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    feedbacks: list[ExternalValidationFeedback] = []
    previous: ExternalValidationFeedback | None = None
    for ordinal in range(max_candidates):
        candidate = make_candidate(ordinal, previous)
        if not isinstance(candidate, D7AnswerCandidate):
            raise TypeError("candidate factory must return D7AnswerCandidate")
        if not candidate_already_registered:
            controller.register_answer_candidate(candidate, str(uuid4()))
        feedback = judge.evaluate(candidate, read_value)
        controller.apply_external_validation_feedback(feedback)
        feedbacks.append(feedback)
        if feedback.verdict == "accepted":
            return candidate, tuple(feedbacks)
        previous = feedback
    raise RuntimeError("fake_judge_candidate_budget_exhausted")


__all__ = ["FakeJudge", "run_fake_judge_loop"]
