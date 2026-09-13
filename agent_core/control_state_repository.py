from __future__ import annotations

from dataclasses import dataclass

from agent_core.control_store import (
    ControlMutation,
    UpsertFact,
    ControlCommitResult,
    ControlStateRows,
    ControlStateSnapshot,
    ControlStore,
    StorageError,
    TaskRunNotFoundError,
    TaskRunSnapshot,
)
from agent_core.control_candidate import project_candidate, validate_control_ownership, validate_running_transition
from agent_core.domain import (
    EXECUTION_TERMINAL_STATUSES,
    ExecutionEvidencePort,
    ExecutionStatus,
    FailureSummary,
    FailureType,
    FactStatus,
    PlanValidationStatus,
    TurnStatus,
)
from agent_core.modeling import FactSnapshot


MAX_CONTEXT_FACTS = 64
MAX_CONTEXT_FACT_CONTENT = 16_384
MAX_CONTEXT_FAILURES = 32


@dataclass(frozen=True, slots=True)
class ModelContextSnapshot:
    facts: tuple[FactSnapshot, ...]
    failure_summaries: tuple[FailureSummary, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: ControlStateSnapshot
    commit: ControlCommitResult | None


class ControlStateRepository:
    """Validate opaque execution references before exposing control snapshots."""

    def __init__(
        self,
        store: ControlStore,
        execution_evidence: ExecutionEvidencePort,
    ) -> None:
        self._store = store
        self._execution_evidence = execution_evidence

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        return self._store.load_task_run(task_run_id)

    def validate_candidate(self, state: ControlStateSnapshot, mutation: ControlMutation) -> None:
        """Recheck the resulting candidate, without writing or retaining evidence."""
        try:
            rows = project_candidate(state, mutation)
            self._validate_rows(rows)
            validate_running_transition(state, rows, mutation)
            executions = {item.execution_id: item for item in rows.executions}
            targets = {item.url for item in rows.task_run.request.target.endpoints} | {
                item.value for item in rows.task_run.request.target.attachments
            }
            # Fact creation/verification also consumes Execution evidence, not
            # just the opaque command/output references checked by _validate_rows.
            for change in mutation.state_changes:
                if not isinstance(change, UpsertFact):
                    continue
                execution = executions[change.source_ref]
                if execution.status not in EXECUTION_TERMINAL_STATUSES:
                    raise StorageError("Fact source Execution must be terminal")
                evidence = self._execution_evidence.lookup(change.source_ref)
                if evidence is None:
                    raise StorageError("Fact source Execution evidence is unavailable")
                if change.status is FactStatus.VERIFIED and (
                    evidence.task_run_id != rows.task_run.task_run_id
                    or evidence.target_ref not in targets
                    or not rows.task_run.started_at <= evidence.observed_at <= rows.task_run.deadline_at
                ):
                    raise StorageError("verified Fact evidence is outside its TaskRun scope or time range")
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"candidate validation failed before commit: {exc}") from exc

    def load_control_state(
        self,
        task_run_id: str,
    ) -> ControlStateSnapshot | None:
        rows = self._store.load_control_state_rows(task_run_id)
        if rows is None:
            return None
        try:
            self._validate_rows(rows)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"persisted control state is invalid: {exc}") from exc
        return ControlStateSnapshot(
            task_run=rows.task_run,
            latest_checkpoint=rows.latest_checkpoint,
            turns=rows.turns,
            intents=rows.intents,
            plans=rows.plans,
            executions=rows.executions,
            facts=rows.facts,
            failure_summaries=rows.failure_summaries,
            plan_validation_records=rows.plan_validation_records,
        )

    def query_model_context(self, task_run_id: str) -> ModelContextSnapshot:
        state = self.load_control_state(task_run_id)
        if state is None:
            raise TaskRunNotFoundError(f"TaskRun does not exist: {task_run_id}")

        selected_facts: list[FactSnapshot] = []
        content_length = 0
        for fact in reversed(state.facts):
            if fact.status is not FactStatus.VERIFIED:
                continue
            if len(selected_facts) >= MAX_CONTEXT_FACTS:
                break
            next_length = content_length + len(fact.content)
            if next_length > MAX_CONTEXT_FACT_CONTENT:
                break
            selected_facts.append(
                FactSnapshot(
                    fact_id=fact.fact_id,
                    content=fact.content,
                    source_ref=fact.source_ref,
                )
            )
            content_length = next_length
        selected_facts.reverse()

        selected_failures = [
            item
            for item in reversed(state.failure_summaries)
            if item.failure_type is FailureType.TARGET_MISMATCH
        ][:MAX_CONTEXT_FAILURES]
        selected_failures.reverse()
        return ModelContextSnapshot(
            facts=tuple(selected_facts),
            failure_summaries=tuple(item.to_domain() for item in selected_failures),
        )

    def _validate_rows(self, rows: ControlStateRows) -> None:
        validate_control_ownership(rows)
        task_run_id = rows.task_run.task_run_id
        turns = {item.turn_id: item for item in rows.turns}
        intents = {item.intent_id: item for item in rows.intents}
        plans = {item.plan_id: item for item in rows.plans}
        executions = {item.execution_id: item for item in rows.executions}
        validations = {
            item.plan_validation_id: item
            for item in rows.plan_validation_records
        }

        if len(turns) != len(rows.turns):
            raise StorageError("control state contains duplicate Turn IDs")
        if len(intents) != len(rows.intents):
            raise StorageError("control state contains duplicate Intent IDs")
        if len(plans) != len(rows.plans):
            raise StorageError("control state contains duplicate Plan IDs")
        if len(executions) != len(rows.executions):
            raise StorageError("control state contains duplicate Execution IDs")
        if len(validations) != len(rows.plan_validation_records):
            raise StorageError("control state contains duplicate PlanValidationRecord IDs")

        running_turns = [
            item.turn_id for item in rows.turns if item.status is TurnStatus.RUNNING
        ]
        if len(running_turns) > 1:
            raise StorageError("control state contains multiple running Turns")
        running_executions = [
            item.execution_id
            for item in rows.executions
            if item.status is ExecutionStatus.RUNNING
        ]
        if len(running_executions) > 1:
            raise StorageError("control state contains multiple running Executions")

        intents_by_turn: dict[str, list[str]] = {item.turn_id: [] for item in rows.turns}
        for intent in rows.intents:
            if intent.turn_id not in turns:
                raise StorageError("Intent references an unknown Turn")
            intents_by_turn[intent.turn_id].append(intent.intent_id)
            intent.to_domain()
        for registered_turn in rows.turns:
            if registered_turn.task_run_id != task_run_id:
                raise StorageError("Turn belongs to another TaskRun")
            registered_turn.to_domain()
            if tuple(intents_by_turn[registered_turn.turn_id]) != registered_turn.intent_ids:
                raise StorageError("Turn.intent_ids do not match its persisted Intents")

        plan_by_intent: dict[str, str] = {}
        for registered_plan in rows.plans:
            if registered_plan.intent_id not in intents:
                raise StorageError("Plan references an unknown Intent")
            if registered_plan.intent_id in plan_by_intent:
                raise StorageError("an Intent contains multiple Plans")
            plan_by_intent[registered_plan.intent_id] = registered_plan.plan_id
            registered_plan.to_domain()

        command_results: dict[str, bool] = {}
        output_results: dict[str, bool] = {}
        execution_commands: dict[tuple[str, int], str] = {}
        for stored_execution in rows.executions:
            plan = plans.get(stored_execution.plan_id)
            if plan is None:
                raise StorageError("Execution references an unknown Plan")
            if not 0 <= stored_execution.step_index < len(plan.steps):
                raise StorageError("Execution step_index is outside its Plan")
            previous_command = execution_commands.setdefault(
                (stored_execution.plan_id, stored_execution.step_index),
                stored_execution.command_ref,
            )
            if previous_command != stored_execution.command_ref:
                raise StorageError(
                    "Execution retry chain contains inconsistent command_ref values"
                )
            stored_execution.to_domain()
            self._require_reference(
                "command",
                stored_execution.command_ref,
                command_results,
            )
            if stored_execution.status in EXECUTION_TERMINAL_STATUSES:
                assert stored_execution.output_ref is not None
                self._require_reference(
                    "output",
                    stored_execution.output_ref,
                    output_results,
                )

        successful_validations: dict[tuple[str, int], str] = {}
        attempts: dict[tuple[str, int | None], list[int]] = {}
        for stored_validation in rows.plan_validation_records:
            plan = plans.get(stored_validation.plan_id)
            if plan is None or stored_validation.task_run_id != task_run_id:
                raise StorageError(
                    "PlanValidationRecord references a Plan outside its TaskRun"
                )
            if stored_validation.step_index is not None and not (
                0 <= stored_validation.step_index < len(plan.steps)
            ):
                raise StorageError(
                    "PlanValidationRecord step_index is outside its Plan"
                )
            attempt_key = (stored_validation.plan_id, stored_validation.step_index)
            attempts.setdefault(attempt_key, []).append(stored_validation.attempt)
            if (
                stored_validation.status is PlanValidationStatus.SUCCEEDED
                and stored_validation.step_index is not None
            ):
                success_key = (stored_validation.plan_id, stored_validation.step_index)
                if success_key in successful_validations:
                    raise StorageError(
                        "a Plan step contains multiple successful command records"
                    )
                assert stored_validation.command_ref is not None
                successful_validations[success_key] = stored_validation.command_ref
                self._require_reference(
                    "command",
                    stored_validation.command_ref,
                    command_results,
                )
        for attempt_values in attempts.values():
            if attempt_values != list(range(1, len(attempt_values) + 1)):
                raise StorageError(
                    "PlanValidationRecord attempts must be contiguous from one"
                )

        for execution_key, command_ref in execution_commands.items():
            if successful_validations.get(execution_key) != command_ref:
                raise StorageError(
                    "Execution does not match its successful command generation record"
                )

        for fact in rows.facts:
            source = executions.get(fact.source_ref)
            turn = turns.get(fact.turn_id)
            if source is None or turn is None:
                raise StorageError("Fact references missing control state")
            source_plan = plans[source.plan_id]
            source_intent = intents[source_plan.intent_id]
            if source_intent.turn_id != turn.turn_id:
                raise StorageError("Fact source Execution belongs to another Turn")
            fact.to_domain()

        for summary in rows.failure_summaries:
            execution = executions.get(summary.evidence_ref)
            validation = validations.get(summary.evidence_ref)
            if execution is None and validation is None:
                raise StorageError("FailureSummary references missing evidence")
            summary.to_domain()

    def _require_reference(
        self,
        kind: str,
        reference: str,
        cache: dict[str, bool],
    ) -> None:
        if reference in cache:
            exists = cache[reference]
        else:
            try:
                if kind == "command":
                    exists = self._execution_evidence.command_exists(reference)
                else:
                    exists = self._execution_evidence.output_exists(reference)
            except Exception as exc:
                raise StorageError(
                    f"{kind} evidence validation is unavailable: {exc}"
                ) from exc
            cache[reference] = exists
        if not exists:
            raise StorageError(
                f"persisted control state contains an invalid {kind}_ref: {reference}"
            )
