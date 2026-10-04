"""Typed, bounded input to one incremental T6 model decision."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from agent_core.command_contracts import T6CommandRecorderPort
from agent_core.contracts import Target
from agent_core.control_state_repository import ModelContextSnapshot
from agent_core.control_store import ControlStateSnapshot
from agent_core.domain import (
    ExecutionStatus, FactStatus, FailureSummary, FailureType, IntentStatus,
    PlanStatus, TurnStatus,
)
from agent_core.intent_postconditions import (
    OutputContains, Postcondition, PostconditionStorePort,
)
from agent_core.modeling import FactSnapshot
from agent_core.pending_candidates import (
    PendingCandidateContext,
    PendingCandidateStorePort,
    recent_pending_candidate_context,
)
from agent_core.runtime import TaskRunStatus
from agent_core.actions import ProbeAction
from agent_core.observation_context import T6Observation, recent_t6_observations


@dataclass(frozen=True, slots=True)
class RetryProbeContext:
    plan_id: str
    timeout_evidence_ref: str
    dispatched: bool
    conditions: tuple[Postcondition, ...]

    def payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "timeout_evidence_ref": self.timeout_evidence_ref,
            "dispatched": self.dispatched,
            "conditions": [
                {
                    "kind": "output_contains" if isinstance(item, OutputContains) else "file_sha256",
                    **asdict(item),
                }
                for item in self.conditions
            ],
        }


@dataclass(frozen=True, slots=True)
class T6ActionModelRequest:
    model_request_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    goal: str
    target: Target
    intent_description: str
    verified_facts: tuple[FactSnapshot, ...]
    failure_summaries: tuple[FailureSummary, ...]
    observations: tuple[T6Observation, ...]
    pending_candidates: PendingCandidateContext
    retry_probe: RetryProbeContext | None = None
    correction_of_model_request_id: str | None = None
    short_reason: str | None = None
    intent_verified_fact_ids: tuple[str, ...] = ()
    latest_verifiable_plan_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "model_request_id", "task_run_id", "turn_id", "intent_id",
            "goal", "intent_description",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid_{name}")
        if not isinstance(self.target, Target):
            raise TypeError("target_required")
        if any(not isinstance(item, FactSnapshot) for item in self.verified_facts):
            raise TypeError("invalid_verified_facts")
        if any(
            not isinstance(item, FailureSummary)
            or item.failure_type is not FailureType.TARGET_MISMATCH
            for item in self.failure_summaries
        ):
            raise TypeError("invalid_failure_summaries")
        if len(self.observations) > 20 or any(
            not isinstance(item, T6Observation) for item in self.observations
        ):
            raise ValueError("invalid_observations")
        if not isinstance(self.pending_candidates, PendingCandidateContext):
            raise TypeError("invalid_pending_candidates")
        if self.retry_probe is not None and not isinstance(self.retry_probe, RetryProbeContext):
            raise TypeError("invalid_retry_probe")
        if (self.correction_of_model_request_id is None) != (self.short_reason is None):
            raise ValueError("incomplete_action_correction")
        if self.short_reason is not None and not self.short_reason.strip():
            raise ValueError("empty_action_correction")
        visible_fact_ids = {item.fact_id for item in self.verified_facts}
        if (
            not isinstance(self.intent_verified_fact_ids, tuple)
            or not isinstance(self.latest_verifiable_plan_fact_ids, tuple)
            or any(not isinstance(item, str) for item in self.intent_verified_fact_ids)
            or any(not isinstance(item, str) for item in self.latest_verifiable_plan_fact_ids)
            or
            len(self.intent_verified_fact_ids) != len(set(self.intent_verified_fact_ids))
            or any(item not in visible_fact_ids for item in self.intent_verified_fact_ids)
            or len(self.latest_verifiable_plan_fact_ids)
            != len(set(self.latest_verifiable_plan_fact_ids))
            or not set(self.latest_verifiable_plan_fact_ids).issubset(
                self.intent_verified_fact_ids
            )
        ):
            raise ValueError("invalid_completion_fact_ids")

    def context_payload(self) -> dict[str, object]:
        return {
            "model_request_id": self.model_request_id,
            "task_run_id": self.task_run_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "goal": self.goal,
            "target": self.target.to_dict(),
            "intent_description": self.intent_description,
            "verified_facts": [asdict(item) for item in self.verified_facts],
            "intent_verified_fact_ids": list(self.intent_verified_fact_ids),
            "latest_verifiable_plan_fact_ids": list(
                self.latest_verifiable_plan_fact_ids
            ),
            "failure_summaries": [
                {
                    "failure_type": item.failure_type.value,
                    "short_reason": item.short_reason,
                    "evidence_ref": item.evidence_ref,
                }
                for item in self.failure_summaries
            ],
            "observations": [asdict(item) for item in self.observations],
            "pending_candidates": asdict(self.pending_candidates),
            "retry_probe": self.retry_probe.payload() if self.retry_probe else None,
            "correction_of_model_request_id": self.correction_of_model_request_id,
            "short_reason": self.short_reason,
        }


def _completion_fact_ids(
    state: ControlStateSnapshot,
    context: ModelContextSnapshot,
    intent_id: str,
    turn_id: str,
    plans,
    postconditions: PostconditionStorePort | None,
    *,
    retrying: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Expose only visible, committed T6 Facts owned by this Intent."""

    visible_ids = {fact.fact_id for fact in context.facts}
    plan_ids = {plan.plan_id for plan in plans}
    execution_plan_ids = {
        execution.execution_id: execution.plan_id
        for execution in state.executions
        if execution.plan_id in plan_ids
    }
    scoped = tuple(
        (fact.fact_id, execution_plan_ids[fact.source_ref])
        for fact in state.facts
        if fact.fact_id in visible_ids
        and fact.status is FactStatus.VERIFIED
        and fact.turn_id == turn_id
        and fact.verification_ref is not None
        and fact.verification_ref.startswith("t6:")
        and fact.source_ref in execution_plan_ids
    )
    intent_ids = tuple(fact_id for fact_id, _ in scoped)
    if retrying or postconditions is None:
        return intent_ids, ()

    latest_plan_id = None
    for plan in reversed(plans):
        binding = postconditions.read(plan.plan_id, intent_id)
        if binding is None:
            continue
        if (
            binding.task_run_id != state.task_run.task_run_id
            or binding.intent_id != intent_id
            or binding.plan_id != plan.plan_id
        ):
            raise ValueError("t6_action_context_not_ready")
        latest_plan_id = plan.plan_id
        break
    latest_ids = tuple(
        fact_id for fact_id, plan_id in scoped if plan_id == latest_plan_id
    )
    return intent_ids, latest_ids


def build_action_model_request(
    state: ControlStateSnapshot,
    context: ModelContextSnapshot,
    intent_id: str,
    model_request_id: str,
    commands: T6CommandRecorderPort,
    candidates: PendingCandidateStorePort,
    postconditions: PostconditionStorePort | None = None,
    retry_action: ProbeAction | None = None,
    original_command_request_id: str | None = None,
) -> T6ActionModelRequest:
    """Build one proposal from committed state and independently checked evidence."""
    intent = next((item for item in state.intents if item.intent_id == intent_id), None)
    turn = next(
        (item for item in state.turns if intent is not None and item.turn_id == intent.turn_id),
        None,
    )
    plans = [item for item in state.plans if item.intent_id == intent_id]
    if (
        state.task_run.status is not TaskRunStatus.RUNNING
        or intent is None or intent.status is not IntentStatus.RUNNING
        or turn is None or turn.status is not TurnStatus.RUNNING
        or any(item.status is not PlanStatus.COMPLETED for item in plans[:-1])
    ):
        raise ValueError("t6_action_context_not_ready")
    retry: RetryProbeContext | None = None
    if plans and plans[-1].status is PlanStatus.RUNNING:
        if (
            postconditions is None or retry_action is None
            or original_command_request_id is None
        ):
            raise ValueError("retry_action_evidence_required")
        latest = plans[-1]
        executions = [item for item in state.executions if item.plan_id == latest.plan_id]
        record = commands.read_record(original_command_request_id)
        if (
            record is None or record.status != "step_timeout"
            or not record.termination_confirmed
            or record.request_id != original_command_request_id
            or record.task_run_id != state.task_run.task_run_id
            or record.sandbox_id != state.task_run.sandbox_id
            or record.plan_id != latest.plan_id or record.step_index != 0
        ):
            raise ValueError("t6_action_context_not_ready")
        if record.dispatched:
            if (
                len(executions) != 1
                or executions[0].execution_id != original_command_request_id
                or executions[0].status is not ExecutionStatus.TIMED_OUT
                or executions[0].output_ref != original_command_request_id
                or executions[0].command_ref != record.command_ref
            ):
                raise ValueError("t6_action_context_not_ready")
            evidence_ref = original_command_request_id
        else:
            validations = [
                item for item in state.plan_validation_records
                if item.plan_id == latest.plan_id and item.step_index == 0
            ]
            if (
                executions or len(validations) != 1
                or validations[0].failure_type is not FailureType.STEP_TIMEOUT
                or validations[0].command_ref is not None
                or record.command_ref is not None
            ):
                raise ValueError("t6_action_context_not_ready")
            evidence_ref = validations[0].plan_validation_id
        if sum(
            item.failure_type is FailureType.STEP_TIMEOUT
            and item.evidence_ref == evidence_ref
            for item in state.failure_summaries
        ) != 1:
            raise ValueError("t6_action_context_not_ready")
        binding = postconditions.read(latest.plan_id, intent_id)
        if (
            (binding is None) != (not retry_action.conditions)
            or binding is not None and (
                binding.task_run_id != state.task_run.task_run_id
                or binding.plan_id != latest.plan_id or binding.intent_id != intent_id
                or binding.conditions != retry_action.conditions
            )
        ):
            raise ValueError("retry_postconditions_mismatch")
        retry = RetryProbeContext(
            latest.plan_id, evidence_ref, record.dispatched,
            retry_action.conditions,
        )
    elif plans and plans[-1].status is not PlanStatus.COMPLETED:
        raise ValueError("t6_action_context_not_ready")
    intent_fact_ids, latest_fact_ids = _completion_fact_ids(
        state, context, intent_id, intent.turn_id, plans, postconditions,
        retrying=retry is not None,
    )
    return T6ActionModelRequest(
        model_request_id,
        state.task_run.task_run_id,
        turn.turn_id,
        intent_id,
        state.task_run.request.goal,
        state.task_run.request.target,
        intent.description,
        context.facts,
        context.failure_summaries,
        recent_t6_observations(state, intent_id, commands),
        recent_pending_candidate_context(state, intent_id, candidates, commands),
        retry,
        intent_verified_fact_ids=intent_fact_ids,
        latest_verifiable_plan_fact_ids=latest_fact_ids,
    )
