"""Read-only v3 TaskRun result projection over committed D7 state."""

from __future__ import annotations

from pathlib import Path

from agent_adapters.run_files import immutable_read
from agent_core.goal_feedback import project_candidate_feedback
from agent_core.run_outcome import D7RunOutcomeState, map_run_outcome
from agent_core.runtime import TERMINAL_STATUSES
from agent_core.sqlite_control_store import SqliteControlStore


def _cleanup(directory: Path, task_run) -> tuple[dict[str, object], str]:
    path = directory / "cleanup.json"
    if not path.exists():
        return {"status": "not_confirmed"}, "unconfirmed"
    value = immutable_read(path)
    if not isinstance(value, dict) or value.get("status") != "succeeded":
        return {"status": "not_confirmed"}, "unconfirmed"
    if value.get("sandbox_id") != task_run.sandbox_id:
        return {"status": "not_confirmed"}, "unconfirmed"
    if task_run.target_network is not None:
        receipt_path = directory / "target-network-cleanup.json"
        if not receipt_path.is_file() or receipt_path.is_symlink():
            return {"status": "not_confirmed"}, "unconfirmed"
        from agent_adapters.target_network_cleanup import target_network_cleanup_receipt

        if immutable_read(receipt_path) != target_network_cleanup_receipt(
            task_run.task_run_id, task_run.target_network
        ):
            return {"status": "not_confirmed"}, "unconfirmed"
    return {"status": "succeeded"}, "confirmed"


def _candidate_projection(
    store, task_run_id: str, *, through_event_sequence: int | None = None,
) -> tuple[list[dict[str, object]], str, bool]:
    candidates = store.list_d7_answer_candidates(task_run_id)
    projected: list[dict[str, object]] = []
    evaluations: list[str] = []
    recovery_required = False
    for candidate in candidates:
        feedback = store.list_d7_validation_feedback(
            task_run_id, candidate.candidate_id, candidate.evidence_version,
            through_event_sequence=through_event_sequence,
        )
        state = project_candidate_feedback(
            task_run_id,
            candidate.candidate_id,
            candidate.evidence_version,
            feedback,
        )
        recovery_required = recovery_required or state.state in {"unknown", "conflict"}
        evaluations.append(state.state)
        feedback_projection = [
            {
                "operation_id": item.operation_id,
                "verdict": item.verdict,
                "feedback_kind": item.feedback_kind,
                "resolves_operation_id": getattr(item, "resolves_operation_id", None),
            }
            for item in feedback
        ]
        projected.append(
            {
                "candidate_id": candidate.candidate_id,
                "turn_id": candidate.turn_id,
                "intent_id": candidate.intent_id,
                "plan_id": candidate.plan_id,
                "execution_id": candidate.execution_id,
                "evidence_version": candidate.evidence_version,
                "validation_state": state.state,
                "feedback": feedback_projection,
            }
        )

    if "conflict" in evaluations or "unknown" in evaluations:
        goal_evaluation = "unknown"
    elif "accepted" in evaluations:
        goal_evaluation = "accepted"
    elif evaluations and all(item == "rejected" for item in evaluations):
        goal_evaluation = "rejected"
    else:
        goal_evaluation = "not_evaluated"
    return projected, goal_evaluation, recovery_required


def _dispatch_status(task_status: str) -> str:
    if task_status in {"running", "awaiting_confirmation", "awaiting_external_feedback"}:
        return task_status
    return "stopped"


def _empty_result(task_run_id: str, mode: str, *, error: str, recovery: bool) -> dict:
    return {
        "protocol_version": 3,
        "task_run_id": task_run_id,
        "mode": mode,
        "task_run": None,
        "checkpoint": None,
        "dispatch": {"status": "unknown"},
        "turns": [],
        "plans": [],
        "executions": [],
        "verified_facts": [],
        "budget": {"status": "unknown"},
        "retrospective": [],
        "process_facts": [],
        "imports": [],
        "answer_candidates": [],
        "pending_candidates": [],
        "external_validation_feedback": [],
        "external_validation_checks": [],
        "validation_accounting": {"status": "unknown"},
        "goal_evaluation": "unknown" if recovery else "not_evaluated",
        "overall_goal": {"outcome": "unknown" if recovery else "not_evaluated"},
        "overall_goal_summary": {
            "schema_version": 1,
            "outcome": "unknown" if recovery else "not_evaluated",
            "candidate_count": 0,
        },
        "cleanup": {"status": "not_confirmed"},
        "cleanup_status": "unconfirmed",
        "recovery": {"required": recovery, "reason": error if recovery else None},
        "recovery_status": "required" if recovery else "not_required",
        "errors": [{"code": error}] if error else [],
    }


def _snapshot_v3_task_result(directory: Path, declaration: dict) -> dict:
    task_id = declaration.get("task_run_id")
    mode = declaration.get("mode")
    if not isinstance(task_id, str) or declaration.get("protocol_version") != 3:
        raise ValueError("invalid_v3_run_declaration")
    result = _empty_result(task_id, mode if isinstance(mode, str) else "unknown", error="", recovery=False)
    database = directory / "control.sqlite"
    if not database.is_file() or database.is_symlink():
        failure_path = directory / "start-failure.json"
        if failure_path.is_file() and not failure_path.is_symlink():
            failure = immutable_read(failure_path)
            result["errors"] = [
                {"code": failure.get("code", "start_failed")}
                if isinstance(failure, dict)
                else {"code": "start_failed"}
            ]
            result["cleanup"] = {"status": "not_required"}
            result["cleanup_status"] = "not_required"
            result["recovery"] = {"required": False, "reason": None}
            result["recovery_status"] = "not_required"
            return result
        return _empty_result(task_id, result["mode"], error="start_outcome_unknown", recovery=True)

    with SqliteControlStore.open_readonly(database) as store:
        rows = store.load_control_state_rows(task_id)
        if rows is None or rows.task_run.task_run_id != task_id:
            return _empty_result(task_id, result["mode"], error="task_run_state_unavailable", recovery=True)
        task = rows.task_run
        task_status = task.status.value
        result["task_run"] = {
            "status": task_status,
            "reason": task.failure_reason,
            "sandbox_id": task.sandbox_id,
            "deadline_at": task.deadline_at,
            "finished_at": task.finished_at,
        }
        checkpoint = rows.latest_checkpoint
        if checkpoint.task_run_id != task_id:
            return _empty_result(task_id, result["mode"], error="checkpoint_ownership_invalid", recovery=True)
        result["checkpoint"] = {
            "sequence": checkpoint.sequence,
            "watermark_event_sequence": checkpoint.watermark_event_sequence,
        }
        result["dispatch"] = {
            "status": _dispatch_status(task_status),
            "checkpoint_sequence": checkpoint.sequence,
        }
        intent_by_turn: dict[str, list[dict[str, object]]] = {
            item.turn_id: [] for item in rows.turns
        }
        for intent in rows.intents:
            if intent.turn_id not in intent_by_turn:
                return _empty_result(task_id, result["mode"], error="intent_ownership_invalid", recovery=True)
            intent_by_turn[intent.turn_id].append(
                {
                    "intent_id": intent.intent_id,
                    "description": intent.description,
                    "priority": intent.priority,
                    "status": intent.status.value,
                }
            )
        result["turns"] = [
            {
                "turn_id": turn.turn_id,
                "sequence": turn.sequence,
                "status": turn.status.value,
                "intents": intent_by_turn[turn.turn_id],
            }
            for turn in rows.turns
        ]
        result["plans"] = [
            {
                "plan_id": plan.plan_id,
                "intent_id": plan.intent_id,
                "status": plan.status.value,
                "step_count": len(plan.steps),
            }
            for plan in rows.plans
        ]
        result["executions"] = [
            {
                "execution_id": execution.execution_id,
                "plan_id": execution.plan_id,
                "step_index": execution.step_index,
                "status": execution.status.value,
            }
            for execution in rows.executions
        ]
        result["verified_facts"] = [
            {
                "fact_id": fact.fact_id,
                "turn_id": fact.turn_id,
                "source_execution_id": fact.source_ref,
                "status": fact.status.value,
                "verification_ref": fact.verification_ref,
            }
            for fact in rows.facts
            if fact.status.value == "verified"
        ]

        config = store.load_d3_run_config(task_id)
        d3_operations = store.list_d3_operations(task_id)
        recovery_required = any(
            operation.completion is None for operation in d3_operations
        )
        operation_recovery = recovery_required
        result["retrospective"] = [
            {
                "review_id": review.review_id,
                "scope": review.review_scope,
                "decision": review.decision,
                "paused": review.paused_gate,
                "pause_reason": review.pause_reason,
                "output_sha256": review.output_sha256,
            }
            for review in store.list_d3_review_applications(task_id)
        ]
        process_facts = store.list_process_facts(task_id)
        result["process_facts"] = [
            {
                "process_fact_id": fact.process_fact_id,
                "version": fact.version,
                "source_count": len(fact.sources),
                "evidence_sha256": fact.evidence_sha256,
                "conflict_count": len(fact.conflicts),
            }
            for fact in process_facts
        ]
        result["imports"] = [
            {
                "index": item.get("index"),
                "name": item.get("name"),
                "byte_size": item.get("byte_size"),
                "sha256": item.get("sha256"),
                "status": "staged",
            }
            for item in declaration.get("attachments", [])
            if isinstance(item, dict)
        ]
        if config is None or config.d4_budget is None:
            result["budget"] = {"status": "not_configured"}
            recovery_required = True
        else:
            usage = store.d4_budget_usage(task_id)
            result["budget"] = {
                "status": "available",
                "policy_ref": usage.budget_policy_ref,
                "total_limit": config.d4_budget.total,
                "total_used": usage.total_used,
                "category_limits": {
                    name: getattr(config.d4_budget, name)
                    for name, _ in usage.category_counts
                },
                "category_counts": dict(usage.category_counts),
                "audit_only_used": usage.audit_only_used,
            }
        candidates, goal_evaluation, feedback_recovery = _candidate_projection(store, task_id)
        result["answer_candidates"] = candidates
        result["pending_candidates"] = [
            item for item in candidates if item["validation_state"] == "pending"
        ]
        result["external_validation_feedback"] = [
            {
                "candidate_id": item["candidate_id"],
                "evidence_version": item["evidence_version"],
                **feedback,
            }
            for item in candidates
            for feedback in item["feedback"]
        ]
        checks = store.list_d7_external_validation_checks(task_id)
        result["external_validation_checks"] = [
            {
                "query_id": record.request.query_id,
                "submission_operation_id": record.request.submission_operation_id,
                "candidate_id": record.request.candidate_id,
                "evidence_version": record.request.evidence_version,
                "checkpoint_sequence": record.request.checkpoint_sequence,
                "status": "started" if record.result is None else "completed",
                "verdict": None if record.result is None else record.result.verdict,
                "started_event_sequence": record.started_event_sequence,
                "result_event_sequence": record.result_event_sequence,
            }
            for record in checks
        ]
        result["validation_accounting"] = {
            "status": "available",
            "logical_check_count": len(checks),
            "completed_count": sum(record.result is not None for record in checks),
            "pending_count": sum(record.result is None for record in checks),
            "unknown_result_count": sum(
                record.result is not None and record.result.verdict == "unknown"
                for record in checks
            ),
        }
        result["goal_evaluation"] = goal_evaluation
        recovery_required = recovery_required or feedback_recovery
        pending_checks = any(record.result is None for record in checks)
        recovery_required = recovery_required or pending_checks
        pending_count = sum(
            item["validation_state"] == "pending" for item in candidates
        )
        outcome = (
            "pending"
            if goal_evaluation == "not_evaluated" and pending_count
            else goal_evaluation
        )
        result["overall_goal"] = {"outcome": outcome}
        result["overall_goal_summary"] = {
            "schema_version": 1,
            "outcome": outcome,
            "candidate_count": len(candidates),
            "accepted_count": sum(item["validation_state"] == "accepted" for item in candidates),
            "rejected_count": sum(item["validation_state"] == "rejected" for item in candidates),
            "pending_count": pending_count,
        }
        cleanup, cleanup_state = _cleanup(directory, task)
        result["cleanup"] = cleanup
        result["cleanup_status"] = cleanup_state
        stop_files = sorted(directory.glob("dispatch-stop-*.json"))
        stop_recovery = False
        if stop_files:
            stop = immutable_read(stop_files[-1])
            stop_recovery = isinstance(stop, dict) and stop.get("stop") == "recovery_required"
        recovery_required = recovery_required or stop_recovery
        operation_recovery = operation_recovery or stop_recovery
        feedback_conflict = any(
            candidate["validation_state"] == "conflict" for candidate in candidates
        )
        unresolved_feedback_unknown = any(
            candidate["validation_state"] == "unknown" for candidate in candidates
        )
        result["recovery"] = {
            "required": recovery_required,
            "reason": (
                "external_feedback_reconciliation_required"
                if feedback_conflict
                else "external_validation_check_outcome_unknown"
                if pending_checks
                else "external_validation_feedback_unknown"
                if unresolved_feedback_unknown
                else "operation_or_evidence_unknown"
                if recovery_required
                else None
            ),
        }
        result["recovery_status"] = "required" if recovery_required else "not_required"
        if task.status in TERMINAL_STATUSES:
            terminal = store.load_terminal_commit(task_id)
            if terminal is None or terminal.event.status not in TERMINAL_STATUSES:
                raise ValueError("terminal_result_unavailable")
            # Late feedback changes the current assessment, while the first
            # committed terminal checkpoint remains the business outcome.
            _, terminal_goal, terminal_feedback_recovery = _candidate_projection(
                store, task_id,
                through_event_sequence=terminal.checkpoint.watermark_event_sequence,
            )
            outcome_result = map_run_outcome(
                D7RunOutcomeState(
                    task_run_status=terminal.event.status.value,
                    goal_evaluation=terminal_goal,
                    cleanup_state=cleanup_state,
                    unresolved_operation_unknown=(
                        operation_recovery or terminal_feedback_recovery
                    ),
                    recovery_required=operation_recovery,
                )
            )
            result["run_exit_code"] = outcome_result.run_exit_code
            result["run_outcome"] = {
                **outcome_result.to_dict(),
                "recorded_checkpoint_sequence": terminal.checkpoint.sequence,
                "recorded_event_sequence": terminal.event.sequence,
                "run_status": terminal.event.status.value,
                "goal_evaluation": terminal_goal,
            }
        return result


def snapshot_v3_task_result(directory: Path, declaration: dict) -> dict:
    """Return a safe result; unreadable state is represented as recovery required."""
    try:
        return _snapshot_v3_task_result(Path(directory), declaration)
    except Exception:  # noqa: BLE001 -- read-only result boundary
        task_id = declaration.get("task_run_id") if isinstance(declaration, dict) else None
        mode = declaration.get("mode") if isinstance(declaration, dict) else None
        return _empty_result(
            task_id if isinstance(task_id, str) else "unknown",
            mode if isinstance(mode, str) else "unknown",
            error="state_read_failed",
            recovery=True,
        )


__all__ = ["snapshot_v3_task_result"]
