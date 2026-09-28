"""Read-only presentation of committed plans, actions and execution evidence."""

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder
from agent_core.domain import EXECUTION_TERMINAL_STATUSES
from agent_core.modeling import ModelError
from agent_core.actions import (
    AbandonIntentAction,
    CandidateFlagAction,
    CompleteIntentAction,
    ProbeAction,
)

ACTION_KINDS = {ProbeAction: "probe", CompleteIntentAction: "complete_intent",
                AbandonIntentAction: "abandon_intent", CandidateFlagAction: "candidate_flag"}


def control_progress(state, commands, postconditions, journal) -> dict:
    plans = []
    for plan in state.plans:
        binding = postconditions.read(plan.plan_id, plan.intent_id)
        plans.append({
            "plan_id": plan.plan_id, "intent_id": plan.intent_id,
            "status": plan.status.value, "steps": [asdict(step) for step in plan.steps],
            "conditions": [asdict(item) for item in binding.conditions] if binding else [],
        })
    executions = []
    for execution in state.executions:
        item = asdict(execution)
        command = commands.read_command(execution.command_ref)
        if command is None or command.task_run_id != state.task_run.task_run_id:
            raise ValueError("command_evidence_unavailable")
        item["detail_ready"] = True
        item["started_at"] = None
        item["finished_at"] = None
        if execution.status in EXECUTION_TERMINAL_STATUSES:
            completed = commands.read_completed(execution.execution_id)
            if completed is None or completed.record.plan_id != execution.plan_id:
                raise ValueError("execution_evidence_unavailable")
            record = completed.record
            item.update(started_at=record.started_at, finished_at=record.finished_at,
                        exit_code=record.exit_code, timed_out=record.timed_out,
                        dispatched=record.dispatched)
        executions.append(item)
    actions = []
    for intent in state.intents:
        for record in journal.list_for_intent(state.task_run.task_run_id, intent.intent_id):
            kind = "awaiting_response"
            if record.completion is not None:
                action = journal.read_result(record)
                kind = record.completion.result_kind if isinstance(action, ModelError) else ACTION_KINDS[type(action)]
            actions.append({
                "model_request_id": record.start.model_request_id,
                "intent_id": intent.intent_id, "kind": kind,
                "started_at": record.start.started_at,
                "finished_at": record.completion.finished_at if record.completion else None,
                "applied_at": record.applied_at,
            })
    return {"plans": plans, "executions": executions, "actions": actions,
            "checkpoint": state.latest_checkpoint.sequence,
            "failure_summaries": [asdict(item) for item in state.failure_summaries]}


def execution_detail(directory: Path, declaration: dict, execution_id: str) -> dict:
    # Reuse the same state/evidence verifier as CLI status, never attach a
    # Controller or instantiate a Sandbox on this read path.
    from agent_adapters.task_result import snapshot_task_progress

    progress = snapshot_task_progress(directory, declaration)
    item = next((item for item in progress.get("executions", [])
                 if item["execution_id"] == execution_id), None)
    if item is None:
        raise ValueError("execution_not_available")
    commands = T6CommandRecorder(directory / "commands", create=False)
    command = commands.read_command(item["command_ref"])
    if command is None or command.task_run_id != declaration["task_run_id"]:
        raise ValueError("command_evidence_unavailable")
    result = {**item, "script": command.argv[2],
              "working_directory": command.working_directory,
              "effective_timeout_seconds": command.effective_timeout_seconds,
              "output": None}
    if item["output_ref"] is not None:
        completed = commands.read_completed(execution_id)
        if completed is None:
            raise ValueError("execution_evidence_unavailable")
        record = completed.record
        result["output"] = {
            "stdout": record.stdout.decode("utf-8", errors="backslashreplace"),
            "stderr": record.stderr.decode("utf-8", errors="backslashreplace"),
            "stdout_truncated": record.stdout_truncated,
            "stderr_truncated": record.stderr_truncated,
            "stream_complete": record.stream_complete,
            "termination_confirmed": record.termination_confirmed,
            "dispatched": record.dispatched,
            "exit_code": record.exit_code,
        }
    return result
