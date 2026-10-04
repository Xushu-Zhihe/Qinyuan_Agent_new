"""Pure projection and transition checks; no storage or evidence service I/O."""

from dataclasses import fields, replace
from typing import TypeVar

from agent_core.control_store import (
    AppendD2ReviewApplication,
    AppendFailureSummary,
    AppendPlanValidationRecord,
    ControlMutation,
    ControlStateRows,
    ControlStateSnapshot,
    ExecutionSnapshot,
    FactRecordSnapshot,
    FailureSummaryRecordSnapshot,
    IntentSnapshot,
    PlanSnapshot,
    StorageError,
    TaskRunSnapshot,
    TurnSnapshot,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertIntentGoal,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
)
from agent_core.domain import (
    ExecutionStatus,
    IntentStatus,
    PlanStatus,
    PlanValidationRecord,
    TurnStatus,
)
from agent_core.dual_llm_contracts import D3ReviewApplication
from agent_core.process_facts import D5ProcessFact
from agent_core.runtime import TaskRunStatus


def project_candidate(
    state: ControlStateSnapshot, mutation: ControlMutation
) -> ControlStateRows:
    """Apply the entire delta without modifying the committed snapshot.

    Parent references are checked on the resulting graph, so new parents and
    children may appear in either order. Global ID collisions are checked by Store.
    """
    if state.task_run.task_run_id != mutation.task_run_id:
        raise StorageError("candidate belongs to another TaskRun")
    task = state.task_run
    turns = {item.turn_id: item for item in state.turns}
    intents = {item.intent_id: item for item in state.intents}
    plans = {item.plan_id: item for item in state.plans}
    executions = {item.execution_id: item for item in state.executions}
    facts = {item.fact_id: item for item in state.facts}
    validations = {
        item.plan_validation_id: item for item in state.plan_validation_records
    }
    failures = list(state.failure_summaries)
    seen: set[tuple[type, str]] = set()
    for ordinal, change in enumerate(mutation.state_changes):
        if isinstance(change, (AppendD2ReviewApplication, D3ReviewApplication, D5ProcessFact)):
            # D2 review applications are append-only control metadata; they do
            # not alter the projected TaskRun graph.
            continue
        if isinstance(change, AppendFailureSummary):
            failures.append(
                FailureSummaryRecordSnapshot(
                    change.failure_type,
                    change.short_reason,
                    change.evidence_ref,
                    mutation.checkpoint.sequence,
                    ordinal,
                )
            )
            continue
        if isinstance(change, AppendPlanValidationRecord):
            record = change.record
            if record.plan_validation_id in validations:
                raise StorageError("PlanValidationRecord is append-only")
            validations[record.plan_validation_id] = PlanValidationRecord(
                record.plan_validation_id,
                record.plan_id,
                record.task_run_id,
                record.step_index,
                record.attempt,
                record.status,
                record.failure_type,
                record.short_reason,
                record.command_ref,
                mutation.event.timestamp,
            )
            continue
        if isinstance(change, UpsertTaskRun):
            entity_id = change.task_run_id
            if entity_id != task.task_run_id:
                raise StorageError("TaskRun owner cannot be modified")
            for name in (
                "request",
                "effective_model_backend",
                "started_at",
                "deadline_at",
                "sandbox_id",
                "source_root",
                "target_network",
            ):
                if getattr(change, name) != getattr(task, name):
                    raise StorageError("immutable TaskRun fields cannot be modified")
            task = TaskRunSnapshot(
                change.task_run_id,
                change.request,
                change.effective_model_backend,
                change.started_at,
                change.deadline_at,
                change.status,
                change.sandbox_id,
                change.confirmation_deadline,
                change.finished_at,
                change.failure_reason,
                change.source_root,
                change.target_network,
            )
        elif isinstance(change, UpsertTurn):
            entity_id = change.turn_id
            _put_snapshot(
                turns,
                entity_id,
                TurnSnapshot(
                    change.turn_id,
                    change.task_run_id,
                    change.sequence,
                    change.turn_target,
                    change.intent_ids,
                    change.status,
                ),
                ("task_run_id",),
            )
        elif isinstance(change, UpsertIntent):
            entity_id = change.intent_id
            _put_snapshot(
                intents,
                entity_id,
                IntentSnapshot(
                    change.intent_id,
                    change.turn_id,
                    change.description,
                    change.priority,
                    change.intent_type,
                    change.status,
                ),
                ("turn_id",),
            )
        elif isinstance(change, UpsertIntentGoal):
            entity_id = change.goal.intent_id
            intent = intents.get(entity_id)
            if (
                intent is None
                or intent.status is not IntentStatus.RUNNING
                or change.goal.task_run_id != task.task_run_id
                or any(plan.intent_id == entity_id for plan in plans.values())
            ):
                raise StorageError("Intent goal requires a running Intent before Plan")
        elif isinstance(change, UpsertPlan):
            entity_id = change.plan_id
            _put_snapshot(
                plans,
                entity_id,
                PlanSnapshot(
                    change.plan_id,
                    change.intent_id,
                    change.steps,
                    change.status,
                ),
                ("intent_id", "steps"),
            )
        elif isinstance(change, UpsertExecution):
            entity_id = change.execution_id
            _put_snapshot(
                executions,
                entity_id,
                ExecutionSnapshot(
                    change.execution_id,
                    change.plan_id,
                    change.step_index,
                    change.command_ref,
                    change.status,
                    change.output_ref,
                ),
                ("plan_id", "step_index", "command_ref"),
            )
        elif isinstance(change, UpsertFact):
            entity_id = change.fact_id
            old_fact = facts.get(entity_id)
            _put_snapshot(
                facts,
                entity_id,
                FactRecordSnapshot(
                    change.fact_id,
                    change.turn_id,
                    change.content,
                    change.source_ref,
                    change.status,
                    old_fact.created_event_sequence
                    if old_fact
                    else mutation.checkpoint.sequence,
                    old_fact.created_event_ordinal if old_fact else ordinal,
                    change.verification_ref,
                ),
                ("turn_id", "source_ref", "verification_ref"),
            )
        else:
            raise StorageError("unsupported candidate state change")
        key = (type(change), entity_id)
        if key in seen:
            raise StorageError("candidate contains duplicate writes to one object")
        seen.add(key)
    return ControlStateRows(
        task_run=task,
        latest_checkpoint=state.latest_checkpoint,
        turns=tuple(sorted(turns.values(), key=lambda item: item.sequence)),
        intents=tuple(
            sorted(intents.values(), key=lambda item: (item.turn_id, item.priority))
        ),
        plans=tuple(plans.values()),
        executions=tuple(executions.values()),
        facts=tuple(facts.values()),
        failure_summaries=tuple(failures),
        plan_validation_records=tuple(
            sorted(
                validations.values(),
                key=lambda item: (
                    item.plan_id,
                    -1 if item.step_index is None else item.step_index,
                    item.attempt,
                ),
            )
        ),
    )


_Snapshot = TypeVar(
    "_Snapshot",
    TurnSnapshot,
    IntentSnapshot,
    PlanSnapshot,
    ExecutionSnapshot,
    FactRecordSnapshot,
)


def _put_snapshot(
    table: dict[str, _Snapshot],
    entity_id: str,
    item: _Snapshot,
    immutable: tuple[str, ...],
) -> None:
    previous = table.get(entity_id)
    if previous is not None and any(
        getattr(previous, name) != getattr(item, name) for name in immutable
    ):
        raise StorageError(
            "existing object owner and immutable fields cannot be modified"
        )
    table[entity_id] = item


RECOVERY_OPERATION = "recover_running_execution_as_inconclusive"


def validate_control_ownership(rows: ControlStateRows) -> None:
    """Validate final links, including reverse Turn lists and both Fact parents."""
    turns = {item.turn_id: item for item in rows.turns}
    intents = {item.intent_id: item for item in rows.intents}
    plans = {item.plan_id: item for item in rows.plans}
    executions = {item.execution_id: item for item in rows.executions}
    for turn in rows.turns:
        if turn.task_run_id != rows.task_run.task_run_id:
            raise StorageError("Turn belongs to another TaskRun")
        children = tuple(
            item.intent_id
            for item in sorted(
                (item for item in rows.intents if item.turn_id == turn.turn_id),
                key=lambda item: item.priority,
            )
        )
        if children != turn.intent_ids:
            raise StorageError(
                "Turn.intent_ids must match the Intents owned by this Turn"
            )
    if any(item.turn_id not in turns for item in rows.intents):
        raise StorageError("Intent parent belongs to another TaskRun or does not exist")
    if any(item.intent_id not in intents for item in rows.plans):
        raise StorageError("Plan parent belongs to another TaskRun or does not exist")
    if any(item.plan_id not in plans for item in rows.executions):
        raise StorageError(
            "Execution parent belongs to another TaskRun or does not exist"
        )
    for fact in rows.facts:
        source = executions.get(fact.source_ref)
        if source is None or fact.turn_id not in turns:
            raise StorageError("Fact parents must belong to this TaskRun")
        if intents[plans[source.plan_id].intent_id].turn_id != fact.turn_id:
            raise StorageError("Fact source Execution must belong to its Turn")
    validations = {
        item.plan_validation_id: item for item in rows.plan_validation_records
    }
    for record in rows.plan_validation_records:
        if (
            record.plan_id not in plans
            or record.task_run_id != rows.task_run.task_run_id
        ):
            raise StorageError(
                "PlanValidationRecord parents must belong to this TaskRun"
            )
    if any(
        item.evidence_ref not in executions
        and item.evidence_ref not in validations
        and not (
            item.failure_type.value == "model_error"
            and item.evidence_ref in intents
        )
        for item in rows.failure_summaries
    ):
        raise StorageError("FailureSummary evidence must belong to this TaskRun")


def validate_running_transition(
    before: ControlStateRows | ControlStateSnapshot | None,
    after: ControlStateRows,
    mutation: ControlMutation,
) -> None:
    """Only the complete, named recovery transition may terminalize active parents."""
    running = [
        item for item in after.executions if item.status is ExecutionStatus.RUNNING
    ]
    if len(running) > 1:
        raise StorageError("control state contains multiple running Executions")
    plans = {item.plan_id: item for item in after.plans}
    intents = {item.intent_id: item for item in after.intents}
    turns = {item.turn_id: item for item in after.turns}
    recovery = mutation.operation == RECOVERY_OPERATION
    if recovery:
        if before is None or len(running) != 1:
            raise StorageError(
                "recovery requires exactly one existing running Execution"
            )
        execution = running[0]
        old_running = [
            item for item in before.executions if item.status is ExecutionStatus.RUNNING
        ]
        plan = plans[execution.plan_id]
        intent = intents[plan.intent_id]
        turn = turns[intent.turn_id]
        if old_running != [execution] or execution.output_ref is not None:
            raise StorageError(
                "recovery must preserve the unknown running Execution unchanged"
            )
        expected = replace(
            before,
            task_run=replace(
                before.task_run,
                status=TaskRunStatus.INCONCLUSIVE,
                finished_at=mutation.event.timestamp,
                failure_reason="unfinished_execution_detected_during_recovery",
            ),
            plans=tuple(
                replace(item, status=PlanStatus.INCONCLUSIVE)
                if item.plan_id == plan.plan_id
                else item
                for item in before.plans
            ),
            intents=tuple(
                replace(item, status=IntentStatus.INCONCLUSIVE)
                if item.intent_id == intent.intent_id
                else item
                for item in before.intents
            ),
            turns=tuple(
                replace(item, status=TurnStatus.INCONCLUSIVE)
                if item.turn_id == turn.turn_id
                else item
                for item in before.turns
            ),
        )
        old_plan = next(item for item in before.plans if item.plan_id == plan.plan_id)
        old_intent = next(
            item for item in before.intents if item.intent_id == intent.intent_id
        )
        old_turn = next(item for item in before.turns if item.turn_id == turn.turn_id)
        expected_ids = {
            after.task_run.task_run_id,
            execution.execution_id,
            plan.plan_id,
            intent.intent_id,
            turn.turn_id,
        }
        if (
            before.task_run.status
            not in {TaskRunStatus.RUNNING, TaskRunStatus.AWAITING_CONFIRMATION}
            or old_plan.status is not PlanStatus.RUNNING
            or old_intent.status is not IntentStatus.RUNNING
            or old_turn.status is not TurnStatus.RUNNING
            or len(mutation.state_changes) != 5
            or {type(item) for item in mutation.state_changes}
            != {UpsertTaskRun, UpsertExecution, UpsertPlan, UpsertIntent, UpsertTurn}
            or set(mutation.event.details["entity_ids"]) != expected_ids
            or mutation.event.status is not TaskRunStatus.INCONCLUSIVE
            or mutation.event.details.get("result") != "inconclusive"
            or any(
                getattr(after, field.name) != getattr(expected, field.name)
                for field in fields(after)
                if field.name != "latest_checkpoint"
            )
        ):
            raise StorageError(
                "recovery requires the complete agreed state combination"
            )
        return
    for execution in running:
        plan = plans[execution.plan_id]
        intent = intents[plan.intent_id]
        turn = turns[intent.turn_id]
        if (
            plan.status is not PlanStatus.RUNNING
            or intent.status is not IntentStatus.RUNNING
            or turn.status is not TurnStatus.RUNNING
        ):
            raise StorageError(
                "running Execution requires running parents outside the recovery operation"
            )
