from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Iterable, Protocol

from agent_core.control_store import (
    ControlStateSnapshot,
    TaskRunSnapshot,
    TurnRegistration,
    UpsertIntent,
    UpsertTurn,
)
from agent_core.domain import (
    EXECUTION_TERMINAL_STATUSES,
    DomainError,
    DomainTransitionError,
    Execution,
    ExecutionEvidencePort,
    ExecutionStatus,
    Fact,
    FactStatus,
    FailureAction,
    FailureObservation,
    FailureSummary,
    FailureType,
    Intent,
    IntentStatus,
    Plan,
    PlanValidationEvidence,
    PlanValidationEvidencePort,
    PlanValidationStatus,
    PlanStatus,
    Turn,
    TurnStatus,
    classify_failure,
)
from agent_core.modeling import IntentCandidate
from agent_core.runtime import TaskRun, TaskRunStatus


_TURN_TERMINAL_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.INCONCLUSIVE,
        TurnStatus.FAILED,
    }
)
_TURN_REGISTRATION_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "qinyuan-agent/turn-registration/v1",
)


class TurnTaskRunPort(Protocol):
    """State needed by the domain; persistent working copies have no lifecycle I/O."""

    @property
    def task_run(self) -> TaskRun | TaskRunSnapshot: ...

    def status(self) -> TaskRunStatus: ...

    def model_failed(self, reason: str) -> TaskRunStatus: ...

    def conclude(self, status: TaskRunStatus) -> TaskRunStatus: ...


class _SnapshotTaskRun:
    def __init__(self, task_run: TaskRunSnapshot) -> None:
        self.task_run = task_run

    def status(self) -> TaskRunStatus:
        return self.task_run.status

    def model_failed(self, reason: str) -> TaskRunStatus:
        self.task_run = replace(
            self.task_run, status=TaskRunStatus.FAILED, failure_reason=reason
        )
        return self.task_run.status

    def conclude(self, status: TaskRunStatus) -> TaskRunStatus:
        self.task_run = replace(self.task_run, status=status)
        return status


class _CommittedPlanEvidence:
    def __init__(self, state: ControlStateSnapshot) -> None:
        self._records = {
            record.plan_validation_id: record for record in state.plan_validation_records
        }

    def lookup(self, plan_validation_id: str) -> PlanValidationEvidence | None:
        return self._records.get(plan_validation_id)


class TurnManager:
    """Domain state machine, used by legacy memory mode or disposable snapshots."""

    def __init__(
        self,
        controller: TurnTaskRunPort,
        evidence: ExecutionEvidencePort,
        plan_validation_evidence: PlanValidationEvidencePort,
    ) -> None:
        self._controller = controller
        self._evidence = evidence
        self._plan_validation_evidence = plan_validation_evidence
        self.turns: dict[str, Turn] = {}
        self.intents: dict[str, Intent] = {}
        self.plans: dict[str, Plan] = {}
        self.executions: dict[str, Execution] = {}
        self.facts: dict[str, Fact] = {}
        self.failures: list[FailureSummary] = []
        self._active_turn_id: str | None = None
        self._plan_validation_attempts: dict[tuple[str, int | None], int] = {}
        self._consumed_plan_validation_ids: set[str] = set()
        self._command_validation_ids: dict[tuple[str, int, str], str] = {}
        self._execution_retry_commands: dict[tuple[str, int], str] = {}
        self._execution_retry_counts: dict[tuple[str, int, str], int] = {}

    @classmethod
    def from_snapshot(
        cls, state: ControlStateSnapshot, evidence: ExecutionEvidencePort
    ) -> "TurnManager":
        """Restore a disposable domain copy using only committed control records."""

        manager = cls(_SnapshotTaskRun(state.task_run), evidence, _CommittedPlanEvidence(state))
        manager.turns = {item.turn_id: item.to_domain() for item in state.turns}
        manager.intents = {item.intent_id: item.to_domain() for item in state.intents}
        manager.plans = {item.plan_id: item.to_domain() for item in state.plans}
        manager.executions = {item.execution_id: item.to_domain() for item in state.executions}
        manager.facts = {item.fact_id: item.to_domain() for item in state.facts}
        manager.failures = [item.to_domain() for item in state.failure_summaries]
        running_turns = [item.turn_id for item in state.turns if item.status is TurnStatus.RUNNING]
        if len(running_turns) > 1:
            raise DomainError("snapshot contains multiple running Turns")
        manager._active_turn_id = next(iter(running_turns), None)

        # Appended evidence is not consumed until its separate domain mutation commits.
        failure_refs = {item.evidence_ref for item in state.failure_summaries}
        execution_commands = {
            (item.plan_id, item.step_index, item.command_ref) for item in state.executions
        }
        for record in state.plan_validation_records:
            command_key = (record.plan_id, record.step_index, record.command_ref)
            consumed = record.plan_validation_id in failure_refs
            if (
                record.status is PlanValidationStatus.SUCCEEDED
                and record.step_index is not None
                and record.command_ref is not None
                and command_key in execution_commands
            ):
                consumed = True
                manager._command_validation_ids[
                    (record.plan_id, record.step_index, record.command_ref)
                ] = record.plan_validation_id
            if consumed:
                validation_key = (record.plan_id, record.step_index)
                expected_attempt = manager._plan_validation_attempts.get(validation_key, 0) + 1
                if record.attempt != expected_attempt:
                    raise DomainError("consumed PlanValidationRecord attempts are not contiguous")
                manager._plan_validation_attempts[validation_key] = record.attempt
                manager._consumed_plan_validation_ids.add(record.plan_validation_id)

        for failure in state.failure_summaries:
            execution = manager.executions.get(failure.evidence_ref)
            if execution is not None and failure.failure_type is FailureType.ENVIRONMENT_FAULT:
                retry_key = (execution.plan_id, execution.step_index, execution.command_ref)
                manager._execution_retry_counts[retry_key] = manager._execution_retry_counts.get(retry_key, 0) + 1
                manager._execution_retry_commands[retry_key[:2]] = execution.command_ref
        return manager

    @property
    def task_run_id(self) -> str:
        return self._controller.task_run.task_run_id

    @property
    def strategy_failure_summaries(self) -> tuple[FailureSummary, ...]:
        """Only target mismatches may later constrain a new LLM prompt."""

        return tuple(
            item
            for item in self.failures
            if item.failure_type is FailureType.TARGET_MISMATCH
        )

    @staticmethod
    def prepare_turn_registration(
        *,
        state: ControlStateSnapshot,
        mutation_id: str,
        turn_target: str,
        candidates: tuple[IntentCandidate, ...],
    ) -> TurnRegistration:
        """Build, but do not submit, the first mutation for a new Turn."""

        if not isinstance(state, ControlStateSnapshot):
            raise DomainError("state must be a ControlStateSnapshot")
        if not isinstance(mutation_id, str):
            raise DomainError("mutation_id must be a UUID4 string")
        try:
            parsed_mutation_id = uuid.UUID(mutation_id)
        except (ValueError, AttributeError) as exc:
            raise DomainError("mutation_id must be a UUID4 string") from exc
        if (
            parsed_mutation_id.version != 4
            or str(parsed_mutation_id) != mutation_id.lower()
        ):
            raise DomainError("mutation_id must be a canonical UUID4 string")
        if not isinstance(turn_target, str) or not turn_target.strip():
            raise DomainError("turn_target must be a non-empty string")
        if not isinstance(candidates, tuple) or not candidates:
            raise DomainError("Intent candidates must be a non-empty tuple")
        if any(not isinstance(item, IntentCandidate) for item in candidates):
            raise DomainError("Intent candidates must contain IntentCandidate values")
        if state.task_run.status is not TaskRunStatus.RUNNING:
            raise DomainTransitionError(
                "Turn registration requires a running TaskRun"
            )
        if any(item.status not in _TURN_TERMINAL_STATUSES for item in state.turns):
            raise DomainTransitionError(
                "a previous Turn must be terminal before registering another Turn"
            )

        task_run_id = state.task_run.task_run_id
        turn_id = TurnManager.registration_turn_id(task_run_id, mutation_id)
        intent_ids = tuple(
            str(
                uuid.uuid5(
                    _TURN_REGISTRATION_NAMESPACE,
                    f"{task_run_id}:{mutation_id}:intent:{index}",
                )
            )
            for index, _candidate in enumerate(candidates, start=1)
        )
        if turn_id in {item.turn_id for item in state.turns} or any(
            intent_id in {item.intent_id for item in state.intents}
            for intent_id in intent_ids
        ):
            raise DomainError("deterministic Turn or Intent ID already exists")

        sequence = max((item.sequence for item in state.turns), default=0) + 1
        changes: tuple[UpsertTurn | UpsertIntent, ...] = (
            UpsertTurn(
                turn_id=turn_id,
                task_run_id=task_run_id,
                sequence=sequence,
                turn_target=turn_target,
                intent_ids=intent_ids,
                status=TurnStatus.PENDING,
            ),
            *(
                UpsertIntent(
                    intent_id=intent_id,
                    turn_id=turn_id,
                    description=candidate.description,
                    priority=index,
                    intent_type=candidate.intent_type,
                    status=IntentStatus.PENDING,
                )
                for index, (intent_id, candidate) in enumerate(
                    zip(intent_ids, candidates, strict=True),
                    start=1,
                )
            ),
        )
        return TurnRegistration(
            task_run_id=task_run_id,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            turn_id=turn_id,
            state_changes=changes,
        )

    @staticmethod
    def registration_turn_id(task_run_id: str, mutation_id: str) -> str:
        """Return the stable Turn ID used by a registered model generation."""
        return str(uuid.uuid5(
            _TURN_REGISTRATION_NAMESPACE,
            f"{task_run_id}:{mutation_id}:turn:0",
        ))

    def register_turn(self, turn: Turn, intents: Iterable[Intent]) -> None:
        self._require_running_task()
        if turn.task_run_id != self.task_run_id:
            raise DomainError("Turn does not belong to this TaskRun")
        if turn.turn_id in self.turns:
            raise DomainError("duplicate turn_id")
        if any(item.sequence == turn.sequence for item in self.turns.values()):
            raise DomainError("Turn sequence must be unique within a TaskRun")
        if any(
            item.status not in _TURN_TERMINAL_STATUSES
            for item in self.turns.values()
        ):
            raise DomainTransitionError(
                "a previous Turn must be terminal before registering another Turn"
            )

        intent_values = tuple(intents)
        if not intent_values:
            raise DomainError("Turn must contain a non-empty Intent list")
        if tuple(item.intent_id for item in intent_values) != turn.intent_ids:
            raise DomainError("Turn intent_ids must match the supplied Intent list")
        if len({item.priority for item in intent_values}) != len(intent_values):
            raise DomainError("Intent priority must be unique within a Turn")
        for intent in intent_values:
            if intent.turn_id != turn.turn_id:
                raise DomainError("Intent does not belong to this Turn")
            if intent.intent_id in self.intents:
                raise DomainError("duplicate intent_id")

        self.turns[turn.turn_id] = turn
        self.intents.update({item.intent_id: item for item in intent_values})

    def register_plan(self, plan: Plan) -> None:
        self._require_running_task()
        if plan.intent_id not in self.intents:
            raise DomainError("Plan intent_id does not reference an existing Intent")
        if plan.plan_id in self.plans:
            raise DomainError("duplicate plan_id")
        prior = [item for item in self.plans.values() if item.intent_id == plan.intent_id]
        if prior and prior[-1].status is not PlanStatus.COMPLETED:
            raise DomainTransitionError("previous Plan must be completed before another Plan")
        if plan.status is not PlanStatus.PENDING:
            raise DomainTransitionError("Plan must start as pending")
        self.plans[plan.plan_id] = plan

    def select_next_intent(self, turn_id: str) -> Intent | None:
        self._require_running_task()
        turn = self._turn(turn_id)
        if self._active_turn_id not in {None, turn_id}:
            raise DomainTransitionError("another Turn is already running")
        if turn.status is TurnStatus.PENDING:
            turn.status = TurnStatus.RUNNING
            self._active_turn_id = turn_id
        if turn.status is not TurnStatus.RUNNING:
            raise DomainTransitionError("only a running Turn can select an Intent")
        if any(
            self.intents[intent_id].status is IntentStatus.RUNNING
            for intent_id in turn.intent_ids
        ):
            raise DomainTransitionError("the current Intent must become terminal first")

        for intent in sorted(
            (self.intents[intent_id] for intent_id in turn.intent_ids),
            key=lambda item: item.priority,
        ):
            if intent.status is IntentStatus.PENDING:
                intent.status = IntentStatus.RUNNING
                return intent

        if any(
            self.intents[intent_id].status is IntentStatus.SUCCEEDED
            for intent_id in turn.intent_ids
        ):
            raise DomainTransitionError("a successful Turn must be completed explicitly")
        turn.status = TurnStatus.INCONCLUSIVE
        self._active_turn_id = None
        return None

    def start_plan(self, plan_id: str) -> Plan:
        self._require_running_task()
        plan = self._plan(plan_id)
        intent = self._intent(plan.intent_id)
        if intent.status is not IntentStatus.RUNNING:
            raise DomainTransitionError("Plan requires a running Intent")
        if self._active_turn_id != intent.turn_id:
            raise DomainTransitionError("Plan Intent is not in the active Turn")
        if plan.status is not PlanStatus.PENDING:
            raise DomainTransitionError("Plan can only start from pending")
        plan.status = PlanStatus.RUNNING
        return plan

    def complete_plan(self, plan_id: str) -> PlanStatus:
        self._require_running_task()
        plan = self._plan(plan_id)
        if plan.status is not PlanStatus.RUNNING:
            raise DomainTransitionError("only a running Plan can be completed")
        if any(
            item.plan_id == plan.plan_id and item.status is ExecutionStatus.RUNNING
            for item in self.executions.values()
        ):
            raise DomainTransitionError(
                "Plan cannot complete while an Execution is running"
            )
        plan.status = PlanStatus.COMPLETED
        return plan.status

    def record_command_generation_failure(
        self,
        *,
        plan_validation_id: str,
    ) -> FailureAction:
        """Handle a failure before an actual command exists or an Execution starts."""

        self._require_running_task()
        plan, validation = self._consume_plan_validation(
            plan_validation_id,
            expected_status=PlanValidationStatus.FAILED,
            allowed_failure_types=frozenset(FailureType)
            - {FailureType.TARGET_MISMATCH},
        )

        failure_type = validation.failure_type
        assert failure_type is not None
        self._add_failure(failure_type, validation.short_reason, plan_validation_id)

        if failure_type is FailureType.ENVIRONMENT_FAULT:
            if validation.attempt == 1:
                return FailureAction.RETRY_COMMAND_GENERATION
            return self._make_plan_inconclusive(plan)
        if failure_type is FailureType.STEP_TIMEOUT:
            if validation.attempt == 1:
                return FailureAction.RETRY_STEP_COMMAND
            return self._make_plan_failed(plan)
        if failure_type in {FailureType.POLICY_REJECTED, FailureType.PARAMETER_ERROR}:
            return self._make_plan_failed(plan)
        if failure_type is FailureType.MODEL_ERROR:
            return self._fail_task_for_model_error(plan)
        return self._make_plan_inconclusive(plan)

    def create_execution(
        self,
        execution: Execution,
        *,
        plan_validation_id: str,
    ) -> Execution:
        self._require_running_task()
        plan = self._running_plan(execution.plan_id)
        self._validate_step_index(plan, execution.step_index)
        if execution.execution_id in self.executions:
            raise DomainError("duplicate execution_id")
        if execution.status is not ExecutionStatus.PENDING or execution.output_ref is not None:
            raise DomainTransitionError(
                "Execution must start as pending without output_ref"
            )
        if not self._evidence.command_exists(execution.command_ref):
            raise DomainError("command_ref does not reference an existing record")
        retry_key = (execution.plan_id, execution.step_index)
        retry_command = self._execution_retry_commands.get(retry_key)
        if retry_command is not None:
            if execution.command_ref != retry_command:
                raise DomainError("environment retry must reuse the first command_ref")
            validation_key = (*retry_key, execution.command_ref)
            expected_validation_id = self._command_validation_ids.get(validation_key)
            if plan_validation_id != expected_validation_id:
                raise DomainError(
                    "environment retry must reuse the first PlanValidationRecord"
                )
            previous = [
                item for item in self.executions.values()
                if (item.plan_id, item.step_index) == retry_key
            ]
            if len(previous) != 1 or previous[0].status not in EXECUTION_TERMINAL_STATUSES:
                raise DomainTransitionError("only one environment retry Execution may be created")
        else:
            _, validation = self._consume_plan_validation(
                plan_validation_id,
                expected_status=PlanValidationStatus.SUCCEEDED,
                expected_plan_id=plan.plan_id,
                expected_step_index=execution.step_index,
            )
            if validation.command_ref != execution.command_ref:
                raise DomainError(
                    "Execution command_ref must match its PlanValidationRecord"
                )
            self._command_validation_ids[
                (execution.plan_id, execution.step_index, execution.command_ref)
            ] = validation.plan_validation_id

        self.executions[execution.execution_id] = execution
        return execution

    def start_execution(self, execution_id: str) -> ExecutionStatus:
        self._require_running_task()
        execution = self._execution(execution_id)
        self._running_plan(execution.plan_id)
        if execution.status is not ExecutionStatus.PENDING:
            raise DomainTransitionError("Execution can only start from pending")
        if any(item.status is ExecutionStatus.RUNNING for item in self.executions.values()):
            raise DomainTransitionError("another Execution is already running in the Sandbox")
        execution.status = ExecutionStatus.RUNNING
        return execution.status

    def complete_execution(
        self,
        *,
        execution_id: str,
        status: ExecutionStatus,
        output_ref: str,
    ) -> ExecutionStatus:
        self._require_running_task()
        if status not in EXECUTION_TERMINAL_STATUSES:
            raise DomainError("complete_execution requires a terminal status")
        execution = self._execution(execution_id)
        self._running_plan(execution.plan_id)
        if execution.status is not ExecutionStatus.RUNNING:
            raise DomainTransitionError("only a running Execution can be completed")
        if not isinstance(output_ref, str) or not output_ref.strip():
            raise DomainError("terminal Execution requires output_ref")
        if not self._evidence.output_exists(output_ref):
            raise DomainError("output_ref does not reference an existing record")
        execution.status = status
        execution.output_ref = output_ref
        return execution.status

    def record_execution_failure(
        self,
        *,
        execution_id: str,
        status: ExecutionStatus,
        output_ref: str,
        observation: FailureObservation,
        short_reason: str,
        preserve_running_on_unknown: bool = False,
    ) -> FailureAction:
        if status is ExecutionStatus.SUCCEEDED:
            raise DomainError("successful Execution cannot be recorded as a failure")
        failure_type = classify_failure(observation)
        if preserve_running_on_unknown and failure_type is not FailureType.OUTCOME_UNKNOWN:
            raise DomainError("only an unknown outcome may preserve running parents")
        self.complete_execution(
            execution_id=execution_id,
            status=status,
            output_ref=output_ref,
        )
        execution = self._execution(execution_id)
        plan = self._plan(execution.plan_id)
        self._add_failure(failure_type, short_reason, execution.execution_id)

        if preserve_running_on_unknown:
            # The Execution is terminal in the audit, but its output is not
            # trusted. Keep the Plan/Intent/Turn as the recovery anchor.
            return FailureAction.RECOVERY_REQUIRED

        if failure_type is FailureType.ENVIRONMENT_FAULT:
            retry_key = (plan.plan_id, execution.step_index)
            count_key = (*retry_key, execution.command_ref)
            attempts = self._execution_retry_counts.get(count_key, 0) + 1
            self._execution_retry_counts[count_key] = attempts
            if attempts == 1:
                self._execution_retry_commands[retry_key] = execution.command_ref
                return FailureAction.RETRY_EXECUTION
            return self._make_plan_inconclusive(plan)
        if failure_type in {FailureType.POLICY_REJECTED, FailureType.PARAMETER_ERROR}:
            return self._make_plan_failed(plan)
        if failure_type is FailureType.MODEL_ERROR:
            return self._fail_task_for_model_error(plan)
        return self._make_plan_inconclusive(plan)

    def record_step_timeout(
        self, *, execution_id: str, output_ref: str
    ) -> FailureAction:
        """A confirmed T6 timeout permits one changed command for the same step."""
        execution = self._execution(execution_id)
        earlier = 0
        for item in self.failures:
            if item.failure_type is not FailureType.STEP_TIMEOUT:
                continue
            previous_execution = self.executions.get(item.evidence_ref)
            if previous_execution is not None:
                same_step = (
                    previous_execution.plan_id == execution.plan_id
                    and previous_execution.step_index == execution.step_index
                )
            else:
                validation = self._plan_validation_evidence.lookup(item.evidence_ref)
                same_step = (
                    validation is not None
                    and validation.plan_id == execution.plan_id
                    and validation.step_index == execution.step_index
                )
            earlier += bool(same_step)
        if earlier > 1:
            raise DomainTransitionError("step timeout retry limit exceeded")
        self.complete_execution(
            execution_id=execution_id,
            status=ExecutionStatus.TIMED_OUT,
            output_ref=output_ref,
        )
        self._add_failure(FailureType.STEP_TIMEOUT, "step_timed_out", execution_id)
        if earlier == 0:
            return FailureAction.RETRY_STEP_COMMAND
        return self._make_plan_failed(self._plan(execution.plan_id))

    def record_target_mismatch(
        self,
        *,
        plan_id: str,
        evidence_ref: str,
        short_reason: str,
    ) -> FailureAction:
        """Record an explicit FactVerifier conclusion after a completed Plan."""

        self._require_running_task()
        plan = self._plan(plan_id)
        if plan.status is not PlanStatus.COMPLETED:
            raise DomainTransitionError("target mismatch requires a completed Plan")
        execution = self._execution(evidence_ref)
        if execution.plan_id != plan.plan_id:
            raise DomainError("target mismatch evidence must belong to the Plan")
        if execution.status is not ExecutionStatus.SUCCEEDED:
            raise DomainTransitionError(
                "target mismatch requires a successful Execution"
            )
        self._add_failure(FailureType.TARGET_MISMATCH, short_reason, evidence_ref)
        intent = self._intent(plan.intent_id)
        intent.status = IntentStatus.FAILED
        return self._after_terminal_intent(intent)

    def mark_intent_inconclusive(self, *, intent_id: str) -> FailureAction:
        """Stop dispatch after an outcome that cannot be safely classified."""

        self._require_running_task()
        intent = self._intent(intent_id)
        if intent.status is not IntentStatus.RUNNING:
            raise DomainTransitionError("only a running Intent can be inconclusive")
        intent.status = IntentStatus.INCONCLUSIVE
        turn = self._turn(intent.turn_id)
        turn.status = TurnStatus.INCONCLUSIVE
        self._active_turn_id = None
        return FailureAction.TURN_INCONCLUSIVE

    def record_model_error(
        self, *, intent_id: str, short_reason: str = "model_error"
    ) -> FailureAction:
        """Record a model failure without changing any lifecycle state.

        The model audit is the durable source for replaying the same error.
        This control operation adds a bounded classification for inspection,
        while the running Intent/Turn/TaskRun remain the recovery anchor.
        """

        self._require_running_task()
        if not isinstance(short_reason, str) or not short_reason.strip():
            raise DomainError("model error reason must be non-empty")
        intent = self._intent(intent_id)
        if intent.status is not IntentStatus.RUNNING:
            raise DomainTransitionError("only a running Intent can record model error")
        self._add_failure(FailureType.MODEL_ERROR, short_reason, intent_id)
        return FailureAction.MODEL_ERROR

    def create_fact(self, fact: Fact) -> Fact:
        self._require_running_task()
        if fact.status is not FactStatus.CANDIDATE:
            raise DomainTransitionError("Fact must start as candidate and be verified separately")
        if fact.fact_id in self.facts:
            raise DomainError("duplicate fact_id")
        turn = self._turn(fact.turn_id)
        execution = self._execution(fact.source_ref)
        plan = self._plan(execution.plan_id)
        intent = self._intent(plan.intent_id)
        if intent.turn_id != turn.turn_id:
            raise DomainError("Fact source Execution does not belong to the Fact Turn")
        if execution.status not in EXECUTION_TERMINAL_STATUSES:
            raise DomainTransitionError("Fact source Execution must be terminal")
        if execution.output_ref is None:
            raise DomainError("Fact source Execution has no output_ref")
        if not self._evidence.command_exists(execution.command_ref):
            raise DomainError("Fact source command_ref no longer exists")
        if not self._evidence.output_exists(execution.output_ref):
            raise DomainError("Fact source output_ref no longer exists")
        if self._evidence.lookup(execution.execution_id) is None:
            raise DomainError("Fact source Execution evidence does not exist")
        self.facts[fact.fact_id] = fact
        return fact

    def verify_fact(
        self,
        *,
        fact_id: str,
        turn_target_satisfied: bool,
        evidence_supports_content: bool,
    ) -> FactStatus:
        """Apply explicit FactVerifier judgments plus mechanical evidence checks."""

        self._require_running_task()
        if not isinstance(turn_target_satisfied, bool) or not isinstance(evidence_supports_content, bool):
            raise DomainError("FactVerifier judgments must be booleans")
        fact = self._fact(fact_id)
        if fact.status is not FactStatus.CANDIDATE:
            raise DomainTransitionError("only a candidate Fact can be verified")
        execution = self._execution(fact.source_ref)
        evidence = self._evidence.lookup(execution.execution_id)
        evidence_is_valid = (
            evidence is not None
            and evidence.task_run_id == self.task_run_id
            and evidence.target_ref in self._target_references()
            and self._controller.task_run.started_at
            <= evidence.observed_at
            <= self._controller.task_run.deadline_at
            and self._evidence.command_exists(execution.command_ref)
            and execution.output_ref is not None
            and self._evidence.output_exists(execution.output_ref)
        )
        if turn_target_satisfied and evidence_supports_content and evidence_is_valid:
            fact.status = FactStatus.VERIFIED
        else:
            fact.status = FactStatus.REJECTED
        return fact.status

    def succeed_intent_with_fact(self, *, intent_id: str, fact_id: str) -> IntentStatus:
        self._require_running_task()
        intent = self._intent(intent_id)
        fact = self._fact(fact_id)
        if intent.status is not IntentStatus.RUNNING:
            raise DomainTransitionError("only a running Intent can succeed")
        if fact.status is not FactStatus.VERIFIED or fact.turn_id != intent.turn_id:
            raise DomainError("Intent requires a verified Fact from its Turn")
        if fact.verification_ref is not None and fact.verification_ref.startswith("t6:"):
            raise DomainError("T6 observation requires trusted latest-Plan completion")
        source_execution = self._execution(fact.source_ref)
        source_plan = self._plan(source_execution.plan_id)
        if source_plan.intent_id != intent.intent_id:
            raise DomainError("Fact must originate from the successful Intent")
        intent.status = IntentStatus.SUCCEEDED
        return intent.status

    def complete_turn(self, turn_id: str) -> TurnStatus:
        self._require_running_task()
        turn = self._turn(turn_id)
        if turn.status is not TurnStatus.RUNNING:
            raise DomainTransitionError("only a running Turn can be completed")
        if not any(
            self.intents[intent_id].status is IntentStatus.SUCCEEDED
            for intent_id in turn.intent_ids
        ):
            raise DomainError("a completed Turn requires a successful Intent")
        turn.status = TurnStatus.COMPLETED
        self._active_turn_id = None
        return turn.status

    def conclude_task(self, status: TaskRunStatus) -> TaskRunStatus:
        """Apply the user-confirmed partial/inconclusive terminal rule."""

        self._require_running_task()
        if status not in {TaskRunStatus.PARTIAL, TaskRunStatus.INCONCLUSIVE}:
            raise DomainError("TaskRun conclusion must be partial or inconclusive")
        if any(
            turn.status in {TurnStatus.PENDING, TurnStatus.RUNNING}
            for turn in self.turns.values()
        ):
            raise DomainTransitionError("all Turns must be terminal before conclusion")
        has_verified_fact = any(
            fact.status is FactStatus.VERIFIED for fact in self.facts.values()
        )
        if status is TaskRunStatus.PARTIAL and not has_verified_fact:
            raise DomainError("partial TaskRun requires at least one verified Fact")
        if status is TaskRunStatus.INCONCLUSIVE and has_verified_fact:
            raise DomainError("verified Facts require a partial TaskRun conclusion")
        return self._controller.conclude(status)

    def _make_plan_failed(self, plan: Plan) -> FailureAction:
        plan.status = PlanStatus.FAILED
        intent = self._intent(plan.intent_id)
        intent.status = IntentStatus.FAILED
        return self._after_terminal_intent(intent)

    def _make_plan_inconclusive(self, plan: Plan) -> FailureAction:
        plan.status = PlanStatus.INCONCLUSIVE
        intent = self._intent(plan.intent_id)
        intent.status = IntentStatus.INCONCLUSIVE
        return self._after_terminal_intent(intent)

    def _fail_task_for_model_error(self, plan: Plan) -> FailureAction:
        # The caller has already recorded the classified model failure.
        # Keep the Plan and its parents running for explicit recovery.
        self._intent(plan.intent_id)
        return FailureAction.MODEL_ERROR

    def _after_terminal_intent(self, intent: Intent) -> FailureAction:
        turn = self._turn(intent.turn_id)
        if any(
            self.intents[intent_id].status is IntentStatus.PENDING
            for intent_id in turn.intent_ids
        ):
            return FailureAction.TRY_NEXT_INTENT
        target_mismatch_intents: set[str] = set()
        for failure in self.failures:
            if failure.failure_type is not FailureType.TARGET_MISMATCH:
                continue
            execution = self.executions.get(failure.evidence_ref)
            if execution is not None:
                plan = self.plans.get(execution.plan_id)
                if plan is not None:
                    target_mismatch_intents.add(plan.intent_id)
                continue
            validation = self._plan_validation_evidence.lookup(failure.evidence_ref)
            if validation is not None:
                plan = self.plans.get(validation.plan_id)
                if plan is not None:
                    target_mismatch_intents.add(plan.intent_id)
        if (
            all(
                self.intents[intent_id].status is IntentStatus.FAILED
                for intent_id in turn.intent_ids
            )
            and set(turn.intent_ids) <= target_mismatch_intents
        ):
            turn.status = TurnStatus.FAILED
            self._active_turn_id = None
            return FailureAction.TURN_FAILED
        turn.status = TurnStatus.INCONCLUSIVE
        self._active_turn_id = None
        return FailureAction.TURN_INCONCLUSIVE

    def _add_failure(
        self,
        failure_type: FailureType,
        short_reason: str,
        evidence_ref: str,
    ) -> None:
        if failure_type is FailureType.MODEL_ERROR:
            # Model failures are tied to the running Intent.  The durable
            # model-invocation ledger carries the full request/response audit;
            # the control summary keeps only its bounded category and reason.
            # Lower-level plan/execution failure routing may use its own
            # already-committed validation or Execution evidence.
            if (
                evidence_ref not in self.intents
                and evidence_ref not in self.executions
                and self._plan_validation_evidence.lookup(evidence_ref) is None
            ):
                raise DomainError("model failure evidence_ref must reference its Intent or evidence")
        elif (
            evidence_ref not in self.executions
            and self._plan_validation_evidence.lookup(evidence_ref) is None
        ):
            raise DomainError("failure evidence_ref does not reference an existing record")
        self.failures.append(
            FailureSummary(
                failure_type=failure_type,
                short_reason=short_reason,
                evidence_ref=evidence_ref,
            )
        )

    def _require_running_task(self) -> None:
        if (
            isinstance(self._controller.task_run, TaskRunSnapshot)
            and not isinstance(self._controller, _SnapshotTaskRun)
        ):
            raise DomainTransitionError(
                "persistent TaskRun operations require TurnWorkingCopy and Controller commit"
            )
        status = self._controller.status()
        if status is not TaskRunStatus.RUNNING:
            raise DomainTransitionError("TaskRun is not running")

    def _target_references(self) -> frozenset[str]:
        target = self._controller.task_run.request.target
        return frozenset(
            [item.url for item in target.endpoints]
            + [item.value for item in target.attachments]
        )

    def _validate_step_index(self, plan: Plan, step_index: int) -> None:
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            raise DomainError("step_index must be an integer")
        if not 0 <= step_index < len(plan.steps):
            raise DomainError("step_index is outside the Plan")

    def _consume_plan_validation(
        self,
        plan_validation_id: str,
        *,
        expected_status: PlanValidationStatus,
        expected_plan_id: str | None = None,
        expected_step_index: int | None = None,
        allowed_failure_types: frozenset[FailureType] | None = None,
    ) -> tuple[Plan, PlanValidationEvidence]:
        if not isinstance(plan_validation_id, str) or not plan_validation_id.strip():
            raise DomainError("plan_validation_id must be a non-empty string")
        if plan_validation_id in self._consumed_plan_validation_ids:
            raise DomainError("PlanValidationRecord has already been consumed")
        validation = self._plan_validation_evidence.lookup(plan_validation_id)
        if validation is None:
            raise DomainError("PlanValidationRecord does not exist")
        if validation.plan_validation_id != plan_validation_id:
            raise DomainError("PlanValidationEvidence does not match its lookup reference")
        if validation.status is not expected_status:
            raise DomainError(
                "PlanValidationRecord status does not match the requested operation"
            )
        if (
            allowed_failure_types is not None
            and validation.failure_type not in allowed_failure_types
        ):
            raise DomainError("target_mismatch requires a completed Execution")
        if validation.task_run_id != self.task_run_id:
            raise DomainError("PlanValidationRecord does not belong to this TaskRun")
        task_run = self._controller.task_run
        if not task_run.started_at <= validation.observed_at <= task_run.deadline_at:
            raise DomainError("PlanValidationRecord is outside the TaskRun time range")

        plan = self._running_plan(validation.plan_id)
        if expected_plan_id is not None and validation.plan_id != expected_plan_id:
            raise DomainError("PlanValidationRecord does not belong to the Execution Plan")
        if validation.step_index is not None:
            self._validate_step_index(plan, validation.step_index)
        if expected_step_index is not None and validation.step_index != expected_step_index:
            raise DomainError("PlanValidationRecord step_index must match Execution")
        attempt_key = (validation.plan_id, validation.step_index)
        expected_attempt = self._plan_validation_attempts.get(attempt_key, 0) + 1
        if validation.attempt != expected_attempt:
            raise DomainError("PlanValidationRecord attempt is not the next attempt for its scope")
        self._plan_validation_attempts[attempt_key] = validation.attempt
        self._consumed_plan_validation_ids.add(plan_validation_id)
        return plan, validation

    def _running_plan(self, plan_id: str) -> Plan:
        plan = self._plan(plan_id)
        if plan.status is not PlanStatus.RUNNING:
            raise DomainTransitionError("Plan is not running")
        return plan

    def _turn(self, turn_id: str) -> Turn:
        try:
            return self.turns[turn_id]
        except KeyError as exc:
            raise DomainError("Turn does not exist") from exc

    def _intent(self, intent_id: str) -> Intent:
        try:
            return self.intents[intent_id]
        except KeyError as exc:
            raise DomainError("Intent does not exist") from exc

    def _plan(self, plan_id: str) -> Plan:
        try:
            return self.plans[plan_id]
        except KeyError as exc:
            raise DomainError("Plan does not exist") from exc

    def _execution(self, execution_id: str) -> Execution:
        try:
            return self.executions[execution_id]
        except KeyError as exc:
            raise DomainError("Execution does not exist") from exc

    def _fact(self, fact_id: str) -> Fact:
        try:
            return self.facts[fact_id]
        except KeyError as exc:
            raise DomainError("Fact does not exist") from exc
