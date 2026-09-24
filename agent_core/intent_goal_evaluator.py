"""Read-only evaluation of a bound file-type Intent goal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import ControlStore, StorageError
from agent_core.domain import ExecutionStatus, FactStatus, FailureType
from agent_core.file_type_fact import fact_content
from agent_core.file_type_verifier import FileTypeProofEvidencePort


IntentGoalOutcome = Literal["satisfied", "not_satisfied", "inconclusive"]
IntentGoalReason = Literal[
    "verified_file_type_fact",
    "no_supporting_verified_fact",
    "execution_outcome_unknown",
]


class IntentGoalEvaluationError(RuntimeError):
    def __init__(self, code: Literal["state_conflict"]) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IntentGoalEvaluation:
    task_run_id: str
    intent_id: str
    outcome: IntentGoalOutcome
    verified_fact_ids: tuple[str, ...]
    short_reason: IntentGoalReason

    def __post_init__(self) -> None:
        expected = {
            "satisfied": "verified_file_type_fact",
            "not_satisfied": "no_supporting_verified_fact",
            "inconclusive": "execution_outcome_unknown",
        }
        if self.outcome not in expected or self.short_reason != expected[self.outcome]:
            raise ValueError("Intent goal outcome and reason do not match")
        if not isinstance(self.verified_fact_ids, tuple) or any(
            not isinstance(item, str) or not item for item in self.verified_fact_ids
        ):
            raise ValueError("verified_fact_ids must contain Fact IDs")
        if bool(self.verified_fact_ids) != (self.outcome == "satisfied"):
            raise ValueError("only a satisfied Intent may list supporting Facts")


class IntentGoalEvaluatorPort(Protocol):
    def evaluate(self, task_run_id: str, intent_id: str) -> IntentGoalEvaluation: ...


class FileTypeIntentGoalEvaluator:
    def __init__(
        self,
        *,
        store: ControlStore,
        repository: ControlStateRepository,
        proofs: FileTypeProofEvidencePort,
    ) -> None:
        self._store = store
        self._repository = repository
        self._proofs = proofs

    def evaluate(self, task_run_id: str, intent_id: str) -> IntentGoalEvaluation:
        state = self._repository.load_control_state(task_run_id)
        binding = self._store.load_intent_goal(intent_id)
        if (
            state is None
            or binding is None
            or binding.goal.task_run_id != task_run_id
            or not any(item.intent_id == intent_id for item in state.intents)
        ):
            raise IntentGoalEvaluationError("state_conflict")

        plan_ids = {
            plan.plan_id for plan in state.plans if plan.intent_id == intent_id
        }
        executions = {
            execution.execution_id: execution
            for execution in state.executions
            if execution.plan_id in plan_ids
        }
        supporting: list[str] = []
        for fact in state.facts:
            if (
                fact.status is not FactStatus.VERIFIED
                or fact.verification_ref is None
                or fact.source_ref not in executions
            ):
                continue
            try:
                verified = self._proofs.lookup_file_type_proof(
                    fact.source_ref, fact.verification_ref
                )
            except Exception as exc:
                raise StorageError("file-type Fact proof is unavailable") from exc
            if (
                verified.proof.task_run_id != task_run_id
                or verified.proof.intent_id != intent_id
                or verified.proof.path != binding.goal.path
                or fact.content != fact_content(verified.proof.observed_type)
                or verified.execution.task_run_id != task_run_id
                or verified.execution.target_ref != verified.proof.local_path
            ):
                raise StorageError("file-type Fact proof does not support its Intent goal")
            supporting.append(fact.fact_id)

        if supporting:
            return IntentGoalEvaluation(
                task_run_id, intent_id, "satisfied", tuple(supporting),
                "verified_file_type_fact",
            )

        unknown_ids = {
            summary.evidence_ref
            for summary in state.failure_summaries
            if summary.failure_type is FailureType.OUTCOME_UNKNOWN
        }
        if any(
            execution.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}
            or execution.execution_id in unknown_ids
            for execution in executions.values()
        ):
            return IntentGoalEvaluation(
                task_run_id, intent_id, "inconclusive", (),
                "execution_outcome_unknown",
            )
        return IntentGoalEvaluation(
            task_run_id, intent_id, "not_satisfied", (),
            "no_supporting_verified_fact",
        )
