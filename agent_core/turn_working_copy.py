"""Prepare one control-plane operation without storage or Sandbox side effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from agent_core.control_store import (
    AppendFailureSummary,
    AppendPlanValidationRecord,
    ControlStateSnapshot,
    PlanValidationRecordDraft,
    StateChange,
    TurnOperation,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
)
from agent_core.domain import (
    DomainError,
    DomainTransitionError,
    Execution,
    ExecutionEvidencePort,
    ExecutionStatus,
    Fact,
    FailureObservation,
    FailureType,
    Plan,
    PlanStatus,
    PlanValidationStatus,
)
from agent_core.runtime import TaskRunStatus
from agent_core.turn_manager import TurnManager


class TurnWorkingCopy:
    """Each method computes a fresh, disposable copy of the same committed base.

    Keep the returned TurnOperation for retries. After a successful commit,
    construct the next working copy from Controller.control_state.
    """

    def __init__(
        self, state: ControlStateSnapshot, evidence: ExecutionEvidencePort
    ) -> None:
        if not isinstance(state, ControlStateSnapshot):
            raise DomainError("state must be a committed ControlStateSnapshot")
        self._state = state
        self._evidence = evidence

    def select_next_intent(self, turn_id: str) -> TurnOperation:
        def select(manager: TurnManager) -> str | None:
            intent = manager.select_next_intent(turn_id)
            return intent.intent_id if intent is not None else None

        return self._prepare("select_next_intent", select)

    def register_plan(self, plan: Plan) -> TurnOperation:
        def register(manager: TurnManager) -> str:
            manager.register_plan(replace(plan))
            return plan.plan_id

        return self._prepare("register_plan", register)

    def start_plan(self, plan_id: str) -> TurnOperation:
        return self._prepare(
            "start_plan", lambda manager: manager.start_plan(plan_id).status.value
        )

    def complete_plan(self, plan_id: str) -> TurnOperation:
        return self._prepare(
            "complete_plan", lambda manager: manager.complete_plan(plan_id).value
        )

    def append_plan_validation_record(
        self, record: PlanValidationRecordDraft
    ) -> TurnOperation:
        """Evidence append is its own mutation, before any domain consumption."""

        if not isinstance(record, PlanValidationRecordDraft):
            raise DomainError("record must be a PlanValidationRecordDraft")
        if self._state.task_run.status is not TaskRunStatus.RUNNING:
            raise DomainTransitionError("TaskRun is not running")
        if record.task_run_id != self._state.task_run.task_run_id:
            raise DomainError("PlanValidationRecord does not belong to this TaskRun")
        plans = {item.plan_id: item for item in self._state.plans}
        plan = plans.get(record.plan_id)
        if plan is None or plan.status is not PlanStatus.RUNNING:
            raise DomainTransitionError("Plan validation requires a running Plan")
        if record.step_index is not None and not 0 <= record.step_index < len(
            plan.steps
        ):
            raise DomainError("PlanValidationRecord step_index is outside the Plan")
        if len(record.short_reason) > 256:
            raise DomainError(
                "PlanValidationRecord.short_reason must be at most 256 Unicode code points"
            )
        if record.failure_type is FailureType.TARGET_MISMATCH:
            raise DomainError("target_mismatch requires a completed Execution")
        if record.command_ref is not None and not self._evidence.command_exists(
            record.command_ref
        ):
            raise DomainError("command_ref does not reference an existing record")
        records = self._state.plan_validation_records
        if any(
            item.plan_validation_id == record.plan_validation_id for item in records
        ):
            raise DomainError("duplicate plan_validation_id")
        previous = [
            item
            for item in records
            if (item.plan_id, item.step_index) == (record.plan_id, record.step_index)
        ]
        if record.attempt != len(previous) + 1:
            raise DomainError(
                "PlanValidationRecord attempt is not the next attempt for its scope"
            )
        if previous:
            last = previous[-1]
            consumed_failures = {
                item.evidence_ref for item in self._state.failure_summaries
            }
            environment_retry = (
                last.plan_validation_id in consumed_failures
                and last.status is PlanValidationStatus.FAILED
                and last.failure_type in {
                    FailureType.ENVIRONMENT_FAULT,
                    FailureType.STEP_TIMEOUT,
                }
            )
            timeout_retry = any(
                item.plan_id == record.plan_id
                and item.step_index == record.step_index
                and item.command_ref == last.command_ref
                and item.status is ExecutionStatus.TIMED_OUT
                and any(
                    failure.evidence_ref == item.execution_id
                    and failure.failure_type is FailureType.STEP_TIMEOUT
                    for failure in self._state.failure_summaries
                )
                for item in self._state.executions
            )
            if not environment_retry and not timeout_retry:
                if last.status is PlanValidationStatus.FAILED and last.plan_validation_id not in consumed_failures:
                    raise DomainTransitionError(
                        "previous failed validation must be consumed before retry"
                    )
                raise DomainTransitionError(
                    "previous validation has no eligible command retry"
                )
            if record.attempt != 2:
                raise DomainTransitionError(
                    "command generation permits only one retry"
                )
        return self._operation(
            "append_plan_validation_record",
            (AppendPlanValidationRecord(record),),
            record.plan_validation_id,
        )

    def create_execution(
        self, execution: Execution, *, plan_validation_id: str
    ) -> TurnOperation:
        return self._prepare(
            "create_execution",
            lambda manager: (
                manager.create_execution(
                    replace(execution), plan_validation_id=plan_validation_id
                ).execution_id
            ),
        )

    def start_execution(self, execution_id: str) -> TurnOperation:
        return self._prepare(
            "start_execution",
            lambda manager: manager.start_execution(execution_id).value,
        )

    def complete_execution(
        self, *, execution_id: str, output_ref: str
    ) -> TurnOperation:
        # The frozen complete_execution event only represents success.
        # All other outcomes must include their failure classification atomically.
        return self._prepare(
            "complete_execution",
            lambda manager: (
                manager.complete_execution(
                    execution_id=execution_id,
                    status=ExecutionStatus.SUCCEEDED,
                    output_ref=output_ref,
                ).value
            ),
        )

    def record_command_generation_failure(
        self, *, plan_validation_id: str
    ) -> TurnOperation:
        return self._prepare(
            "record_command_generation_failure",
            lambda manager: (
                manager.record_command_generation_failure(
                    plan_validation_id=plan_validation_id
                ).value
            ),
        )

    def record_execution_failure(
        self,
        *,
        execution_id: str,
        status: ExecutionStatus,
        output_ref: str,
        observation: FailureObservation,
        short_reason: str,
        preserve_running_on_unknown: bool = False,
    ) -> TurnOperation:
        return self._prepare(
            "record_execution_failure",
            lambda manager: (
                manager.record_execution_failure(
                    execution_id=execution_id,
                    status=status,
                    output_ref=output_ref,
                    observation=observation,
                    short_reason=short_reason,
                    preserve_running_on_unknown=preserve_running_on_unknown,
                ).value
            ),
        )

    def record_step_timeout(self, *, execution_id: str, output_ref: str) -> TurnOperation:
        return self._prepare(
            "record_step_timeout",
            lambda manager: manager.record_step_timeout(
                execution_id=execution_id, output_ref=output_ref
            ).value,
        )

    def record_target_mismatch(
        self, *, plan_id: str, evidence_ref: str, short_reason: str
    ) -> TurnOperation:
        return self._prepare(
            "record_target_mismatch",
            lambda manager: (
                manager.record_target_mismatch(
                    plan_id=plan_id,
                    evidence_ref=evidence_ref,
                    short_reason=short_reason,
                ).value
            ),
        )

    def mark_intent_inconclusive(self, *, intent_id: str) -> TurnOperation:
        return self._prepare(
            "mark_intent_inconclusive",
            lambda manager: manager.mark_intent_inconclusive(intent_id=intent_id).value,
        )

    def record_model_error(
        self, *, intent_id: str, short_reason: str = "model_error"
    ) -> TurnOperation:
        return self._prepare(
            "record_model_error",
            lambda manager: manager.record_model_error(
                intent_id=intent_id, short_reason=short_reason
            ).value,
        )

    def create_fact(self, fact: Fact) -> TurnOperation:
        return self._prepare(
            "create_fact", lambda manager: manager.create_fact(replace(fact)).fact_id
        )

    def verify_fact(
        self,
        *,
        fact_id: str,
        turn_target_satisfied: bool,
        evidence_supports_content: bool,
    ) -> TurnOperation:
        return self._prepare(
            "verify_fact",
            lambda manager: (
                manager.verify_fact(
                    fact_id=fact_id,
                    turn_target_satisfied=turn_target_satisfied,
                    evidence_supports_content=evidence_supports_content,
                ).value
            ),
        )

    def succeed_intent_with_fact(
        self, *, intent_id: str, fact_id: str
    ) -> TurnOperation:
        return self._prepare(
            "succeed_intent_with_fact",
            lambda manager: (
                manager.succeed_intent_with_fact(
                    intent_id=intent_id, fact_id=fact_id
                ).value
            ),
        )

    def complete_turn(self, turn_id: str) -> TurnOperation:
        return self._prepare(
            "complete_turn", lambda manager: manager.complete_turn(turn_id).value
        )

    def conclude_task(self, status: TaskRunStatus) -> TurnOperation:
        return self._prepare(
            "finish_task_run", lambda manager: manager.conclude_task(status).value
        )

    def _prepare(
        self, operation: str, apply: Callable[[TurnManager], str | None]
    ) -> TurnOperation:
        if self._state.task_run.status is not TaskRunStatus.RUNNING:
            raise DomainTransitionError("TaskRun is not running")
        manager = TurnManager.from_snapshot(self._state, self._evidence)
        result = apply(manager)
        changes = self._changes(manager)
        if result == "task_failed" or operation == "finish_task_run":
            task = self._state.task_run
            terminal_status = (
                TaskRunStatus.FAILED
                if result == "task_failed"
                else TaskRunStatus.INCONCLUSIVE
                if result is None
                else TaskRunStatus(result)
            )
            changes += (
                UpsertTaskRun(
                    task_run_id=task.task_run_id,
                    request=task.request,
                    effective_model_backend=task.effective_model_backend,
                    started_at=task.started_at,
                    deadline_at=task.deadline_at,
                    status=terminal_status,
                    sandbox_id=task.sandbox_id,
                    confirmation_deadline=task.confirmation_deadline,
                    failure_reason="model_error"
                    if result == "task_failed"
                    else task.failure_reason,
                    source_root=task.source_root,
                    target_network=task.target_network,
                ),
            )
        return self._operation(operation, changes, result)

    def _operation(
        self, operation: str, changes: tuple[StateChange, ...], result: str | None
    ) -> TurnOperation:
        entity_ids: list[str] = []
        for change in changes:
            match change:
                case (
                    UpsertTaskRun(task_run_id=value)
                    | UpsertTurn(turn_id=value)
                    | UpsertIntent(intent_id=value)
                ):
                    entity_id = value
                case (
                    UpsertPlan(plan_id=value)
                    | UpsertExecution(execution_id=value)
                    | UpsertFact(fact_id=value)
                ):
                    entity_id = value
                case AppendFailureSummary(evidence_ref=value):
                    entity_id = value
                case AppendPlanValidationRecord(record=record):
                    entity_id = record.plan_validation_id
                case _:
                    raise DomainError("unsupported state change")
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
        return TurnOperation(
            task_run_id=self._state.task_run.task_run_id,
            base_checkpoint_sequence=self._state.latest_checkpoint.sequence,
            operation=operation,
            state_changes=changes,
            entity_ids=tuple(entity_ids),
            result=result,
        )

    def _changes(self, manager: TurnManager) -> tuple[StateChange, ...]:
        changes: list[StateChange] = []
        turns = {item.turn_id: item.to_domain() for item in self._state.turns}
        intents = {item.intent_id: item.to_domain() for item in self._state.intents}
        plans = {item.plan_id: item.to_domain() for item in self._state.plans}
        executions = {
            item.execution_id: item.to_domain() for item in self._state.executions
        }
        facts = {item.fact_id: item.to_domain() for item in self._state.facts}
        for turn in manager.turns.values():
            if turns.get(turn.turn_id) != turn:
                changes.append(
                    UpsertTurn(
                        turn.turn_id,
                        turn.task_run_id,
                        turn.sequence,
                        turn.turn_target,
                        turn.intent_ids,
                        turn.status,
                    )
                )
        for intent in manager.intents.values():
            if intents.get(intent.intent_id) != intent:
                changes.append(
                    UpsertIntent(
                        intent.intent_id,
                        intent.turn_id,
                        intent.description,
                        intent.priority,
                        intent.intent_type,
                        intent.status,
                    )
                )
        for plan in manager.plans.values():
            if plans.get(plan.plan_id) != plan:
                changes.append(
                    UpsertPlan(plan.plan_id, plan.intent_id, plan.steps, plan.status)
                )
        for execution in manager.executions.values():
            if executions.get(execution.execution_id) != execution:
                changes.append(
                    UpsertExecution(
                        execution.execution_id,
                        execution.plan_id,
                        execution.step_index,
                        execution.command_ref,
                        execution.status,
                        execution.output_ref,
                    )
                )
        for fact in manager.facts.values():
            if facts.get(fact.fact_id) != fact:
                changes.append(
                    UpsertFact(
                        fact.fact_id,
                        fact.turn_id,
                        fact.content,
                        fact.source_ref,
                        fact.status,
                        fact.verification_ref,
                    )
                )
        changes.extend(
            AppendFailureSummary(
                item.failure_type, item.short_reason, item.evidence_ref
            )
            for item in manager.failures[len(self._state.failure_summaries) :]
        )
        return tuple(changes)
