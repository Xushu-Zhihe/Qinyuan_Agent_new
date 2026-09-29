"""Read-only v2 TaskResult from committed state and immutable evidence."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from agent_adapters.action_evidence_store import T6ActionEvidenceStore
from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.observation_proof_store import T6ObservationProofStore
from agent_adapters.pending_candidate_store import PendingCandidateStore
from agent_adapters.postcondition_store import T6PostconditionStore
from agent_adapters.run_files import immutable_read
from agent_adapters.target_network_cleanup import target_network_cleanup_receipt
from agent_core.action_audit import T6ActionJournal
from agent_core.control_state_repository import ControlStateRepository
from agent_core.observation_proof import T6ObservationProofVerifier
from agent_core.pending_candidates import grouped_pending_candidates
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore


def _unverified_import(directory: Path, declaration: dict) -> bool:
    if declaration["mode"] != "real":
        return False
    for name, ids in (
        ("input-imports.sqlite", declaration["ids"]["imports"]),
        ("work-copies.sqlite", declaration["ids"]["copies"]),
    ):
        path = directory / name
        if not path.exists():
            continue
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for import_id in ids:
                row = db.execute(
                    "SELECT status FROM imports WHERE import_id=?", (import_id,),
                ).fetchone()
                if row is not None and row[0] != "verified":
                    return True
    return False


def _snapshot_task_result(directory: Path, declaration: dict, *, progress: bool = False) -> dict:
    task_id = declaration["task_run_id"]
    result = {
        "protocol_version": 2,
        "task_run_id": task_id,
        "mode": declaration["mode"],
        "task_run": None,
        "turns": [],
        "verified_facts": [],
        "pending_candidates": [],
        "overall_goal": {"outcome": "not_evaluated"},
        "stop": None,
        "recovery": {"required": False, "reason": None},
        "cleanup": {"status": "not_confirmed"},
        "errors": [],
        "exit_code": 4,
    }
    cleanup = directory / "cleanup.json"
    if cleanup.exists():
        result["cleanup"] = immutable_read(cleanup)
    database = directory / "control.sqlite"
    if not database.exists():
        failure = directory / "start-failure.json"
        if failure.exists():
            error = immutable_read(failure)
            result["errors"].append(error)
            if error.get("code") == "target_network_error" and error.get("outcome") != "creation_failed":
                result["recovery"] = {"required": True, "reason": "target_network_outcome_unknown"}
                result["exit_code"] = 5
            else:
                result["cleanup"] = {"status": "not_required"}
                result["exit_code"] = 3
        else:
            result["recovery"] = {"required": True, "reason": "start_outcome_unknown"}
            result["exit_code"] = 5
        return result
    with SqliteControlStore.open_readonly(database) as store:
        commands = T6CommandRecorder(directory / "commands", create=False)
        postconditions = T6PostconditionStore(directory / "conditions", create=False)
        journal = T6ActionJournal(
            store, T6ActionEvidenceStore(directory / "actions", create=False),
        )
        proofs = T6ObservationProofVerifier(
            store, postconditions, commands,
            T6ObservationProofStore(directory / "proofs", create=False),
            time.time, journal,
        )
        state = ControlStateRepository(
            store, commands, t6_observation_proofs=proofs,
        ).load_control_state(task_id)
        if state is None:
            failure = directory / "start-failure.json"
            if failure.exists():
                error = immutable_read(failure)
                result["errors"].append(error)
                if error.get("code") == "target_network_error" and error.get("outcome") != "creation_failed":
                    result["recovery"] = {"required": True, "reason": "target_network_outcome_unknown"}
                    result["exit_code"] = 5
                else:
                    result["cleanup"] = {"status": "not_required"}
                    result["exit_code"] = 3
                return result
            result["recovery"] = {"required": True, "reason": "start_outcome_unknown"}
            result["exit_code"] = 5
            return result
        task = state.task_run

        if result["cleanup"]["status"] == "succeeded":
            if result["cleanup"].get("sandbox_id") != task.sandbox_id:
                result["cleanup"] = {"status": "not_confirmed"}
            elif task.target_network is not None:
                target_receipt = directory / "target-network-cleanup.json"
                if (
                    not target_receipt.exists()
                    or immutable_read(target_receipt)
                    != target_network_cleanup_receipt(task_id, task.target_network)
                ):
                    result["cleanup"] = {"status": "not_confirmed"}
                else:
                    result["cleanup"]["target_network_confirmed"] = True

        result["task_run"] = {
            "status": task.status.value,
            "reason": task.failure_reason,
            "sandbox_id": task.sandbox_id,
            "deadline_at": task.deadline_at,
            "finished_at": task.finished_at,
        }
        result["turns"] = [
            {"turn_id": turn.turn_id, "sequence": turn.sequence,
             "status": turn.status.value,
             "intents": [
                 {"intent_id": item.intent_id, "priority": item.priority,
                  "status": item.status.value, "description": item.description}
                 for item in state.intents if item.turn_id == turn.turn_id
             ]}
            for turn in state.turns
        ]
        result["verified_facts"] = [
            asdict(item) for item in state.facts if item.status.value == "verified"
        ]
        grouped = grouped_pending_candidates(
            PendingCandidateStore(directory / "candidates", create=False).list_for_task(task_id)
        )
        result["pending_candidates"] = [
            {"value": value, "source_execution_ids": list(sources), "status": "pending"}
            for value, sources in grouped
        ]
        if progress:
            from agent_adapters.task_progress import control_progress

            result.update(control_progress(state, commands, postconditions, journal))
        unknown_execution = any(
            item.status.value in {"pending", "running"} for item in state.executions
        )
        unknown_action = False
        for intent in state.intents:
            for record in store.list_t6_actions(task_id, intent.intent_id):
                if record.completion is None:
                    unknown_action = True
                else:
                    journal.read_result(record)
        unknown_receipt = any(
            (receipt := commands.read_completed(path.name.removeprefix("claim-"))) is None
            or receipt.record.status == "outcome_unknown"
            for path in commands.directory.glob("claim-*")
        )
        unknown_import = _unverified_import(directory, declaration)
        if unknown_execution or unknown_action or unknown_receipt or unknown_import:
            result["recovery"] = {
                "required": True,
                "reason": (
                    "execution_outcome_unknown" if unknown_execution or unknown_receipt
                    else "import_outcome_unknown" if unknown_import
                    else "model_response_unknown"
                ),
            }
        stop_files = sorted(directory.glob("dispatch-stop-*.json"))
        if stop_files:
            stops = [immutable_read(path) for path in stop_files]
            result["stop"] = max(stops, key=lambda item: item["at"])
            if result["stop"].get("stop") == "recovery_required":
                result["recovery"] = {
                    "required": True,
                    "reason": result["stop"].get("reason") or "recovery_required",
                }
        if task.status in TERMINAL_STATUSES:
            if result["cleanup"]["status"] != "succeeded" or result["recovery"]["required"]:
                result["exit_code"] = 5
            elif task.status is TaskRunStatus.CANCELLED:
                result["exit_code"] = 130
            elif task.status is TaskRunStatus.DEADLINE_EXCEEDED:
                result["exit_code"] = 3
            else:
                result["exit_code"] = 3
        elif result["recovery"]["required"]:
            result["exit_code"] = 5
        else:
            result["exit_code"] = 4
    return result


def snapshot_task_result(directory: Path, declaration: dict, *, _progress: bool = False) -> dict:
    try:
        return _snapshot_task_result(directory, declaration, progress=_progress)
    except Exception as exc:  # noqa: BLE001 -- read-only CLI result boundary
        return {
            "protocol_version": 2,
            "task_run_id": declaration["task_run_id"],
            "mode": declaration["mode"],
            "task_run": None,
            "turns": [],
            "verified_facts": [],
            "pending_candidates": [],
            "overall_goal": {"outcome": "not_evaluated"},
            "stop": None,
            "recovery": {"required": True, "reason": "state_or_evidence_unreadable"},
            "cleanup": {"status": "not_confirmed"},
            "errors": [{"code": "state_read_failed", "category": type(exc).__name__}],
            "exit_code": 5,
        }


def snapshot_task_progress(directory: Path, declaration: dict) -> dict:
    """Workbench-only projection; the v2 CLI result contract is unchanged."""
    return snapshot_task_result(directory, declaration, _progress=True)
