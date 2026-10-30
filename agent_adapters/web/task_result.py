"""Read-only v3 TaskRun result projection over committed D7 state."""

from __future__ import annotations

import time
import json
from pathlib import Path
from dataclasses import asdict

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
    from agent_adapters.competition_submission import feedback_progress
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
                **feedback_progress(store.path.parent,item),
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
                "source_kind": candidate.source_kind,
                "source_ref": candidate.source_ref,
                "evidence_version": candidate.evidence_version,
                "validation_state": state.state,
                "feedback": feedback_projection,
            }
        )
        config = store.load_d3_run_config(task_run_id)
        if config is not None and config.contract_version in {19, 20, 21, 22} or store.load_stage_run_config(task_run_id) is not None:
            projected[-1]['checkable_submission_ids'] = list(
                state.conflict_operation_ids if state.state == 'conflict'
                else state.unresolved_unknown_operation_ids if state.state == 'unknown' else ())

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


def _stage_activity(evidence, store, task_id, operations, requests, turns):
    """Bound public subjects and logical prefixes; ledger state is not worker liveness."""
    from agent_core.stage_contracts import (
        PlanningSession, StageSessionBranch, TurnIntent, StageFactBatch, StageConclusionChange,
        TurnCompletion, RoadResolution, StagePlanningDecision, PlanningExhausted,
        fact_candidates, reviewed_fact, fact_verification_ref,
    )
    from agent_core.stage_planning import stage_runtime, review_members
    from agent_core.stage_storage import branch_id
    from agent_core.stage_plans import bindings
    by_id = {op.start.operation_id: op for op in operations}
    owners = {turn.turn_id: turn.turn_id for turn in turns}
    owners.update({road.road_id: turn.turn_id for turn in turns for road in turn.roads})
    targets = {}
    for session, source in bindings(store, task_id):
        targets.setdefault(source, []).append(dict(turn_id=owners[session.owner_id],
            road_id=session.owner_id if session.scope == "road_execution" else None))
    activity = []
    attempts = {}
    for identity, request in requests.items():
        op = by_id[identity]
        application = op.application_ref or ""
        disposition = ("discarded" if application.startswith("stage-discarded/") else
            "reused" if application.startswith("stage-reused/") else "applied" if application else "unapplied")
        item = dict(operation_id=identity, phase=op.start.category, stage=request.stage,
            status=op.completion.result_kind if op.completion else "unsettled", applied=bool(application),
            turn_id=owners.get(request.context.session.owner_id, request.context.turn.turn_id if request.context.turn else None),
            road_id=request.context.session.owner_id if request.context.session.scope == "road_execution" else None,
            started_at=op.start.started_at, finished_at=op.completion.finished_at if op.completion else None,
            branch_id=stage_runtime(request).get("branch_id"), disposition=disposition,
            disposition_ref=application.split("/", 1)[1] if disposition in {"discarded", "reused"} else None)
        activity.append((op.start.role, item))
        if request.stage == "stage_review":
            evidence.review_sources(request)
            for member in review_members(request):
                attempts.setdefault(member, []).append(identity)
    formal = {row["verification_ref"]: row["fact_id"] for row in store._connection.execute(
        "SELECT f.fact_id,f.verification_ref FROM facts f JOIN stage_turns t ON t.turn_id=f.turn_id WHERE t.task_run_id=?", (task_id,))}
    queue = []
    for entry in store.list_stage_reviews(task_id):
        source = evidence.read_model_result(by_id[entry.proposal_operation_id])
        value = source.result
        kind = ("fact_candidates" if fact_candidates(value) else "turn_intent" if isinstance(value, TurnIntent)
            else "roads" if isinstance(value, tuple) else "conclusion_change" if isinstance(value, StageConclusionChange)
            else "turn_completion" if isinstance(value, TurnCompletion) else "road_resolution" if isinstance(value, RoadResolution)
            else value.kind if isinstance(value, StagePlanningDecision) else "exhausted" if isinstance(value, PlanningExhausted) else type(value).__name__)
        subject = targets.get(entry.proposal_operation_id) or [dict(turn_id=owners.get(source.session.owner_id),
            road_id=source.session.owner_id if source.session.scope == "road_execution" else None)]
        item = dict(**asdict(entry), kind=kind, author=asdict(source.session), targets=subject,
            attempt_ids=attempts.get(entry.proposal_operation_id, []), decision=None, reason=None, candidates=[])
        reviewed = review_request = None
        if entry.state == "settled":
            review_request = requests[entry.review_operation_id]
            reviewed = evidence.read_model_result(by_id[entry.review_operation_id]).result
            item.update(decision=reviewed.decision, reason=reviewed.reason)
            if isinstance(value, tuple):
                item["suggested_indices"] = list(reviewed.accepted_road_indices)
        slots = ({(identity, index): slot for slot, (identity, index, _) in enumerate(evidence.review_claims(review_request))}
            if reviewed is not None and fact_candidates(value) else {})
        for index, claim in fact_candidates(value):
            candidate = dict(index=index, content=claim.content, inferred=claim.inferred,
                support_refs=list(claim.support_refs), decision=None, reason=None, fact_id=None)
            if reviewed is not None:
                slot = slots[(entry.proposal_operation_id, index)] if isinstance(review_request.proposal, StageFactBatch) else None
                _, decision = reviewed_fact(review_request.proposal, reviewed, slot)
                candidate.update(decision=decision.decision, reason=decision.reason,
                    fact_id=formal.get(fact_verification_ref(entry.review_operation_id, slot)))
            item["candidates"].append(candidate)
        queue.append(item)
    branches = []
    if store._schema_version >= 37:
        rows = store._connection.execute("SELECT b.*,s.scope,s.owner_id FROM stage_branches b "
            "JOIN stage_sessions s ON s.session_id=b.session_id WHERE s.task_run_id=? ORDER BY b.rowid", (task_id,)).fetchall()
        heads = {row["session_id"]: row["branch_id"] for row in rows}
        for row in rows:
            source = requests[row["proposal_operation_id"]]
            session = PlanningSession(row["scope"], row["owner_id"])
            inherited = source.context.session != session
            value = StageSessionBranch(row["branch_id"], row["proposal_operation_id"], row["review_operation_id"])
            if source.task_run_id != task_id or branch_id(task_id, value, session if inherited else None) != value.branch_id:
                raise ValueError("stage.branch_binding_changed")
            if value.review_operation_id and value.proposal_operation_id not in evidence.review_rejections(value.review_operation_id):
                raise ValueError("stage.branch_correction_unbound")
            branches.append(dict(**asdict(value), session=asdict(session), author=asdict(source.context.session),
                parent_branch_id=stage_runtime(source).get("branch_id"), current=heads[row["session_id"]] == value.branch_id,
                kind="correction" if value.review_operation_id else "inheritance" if inherited else "continuation",
                turn_id=owners.get(session.owner_id), road_id=session.owner_id if session.scope == "road_execution" else None))
    return dict(review_queue=queue, branches=branches,
        inner_activity=[item for role, item in activity if role == "inner_action"],
        retrospective=[item for role, item in activity if role == "outer_planning"])


def _stage_task_result(directory, declaration, store, expected):
    """Project actual stage/Road identities without constructing a writer or legacy tree."""
    from agent_adapters.stage_evidence import StageEvidence
    from agent_core.stage_storage import active_turn, execution_policy
    from agent_core.turn_modeling import digest
    task_id = declaration["task_run_id"]
    config = store.load_stage_run_config(task_id)
    policy = execution_policy(expected)
    evidence = StageEvidence(Path(directory) / "stage-evidence", store, create=False)
    frozen_policy = evidence.read("policy", task_id)
    queue_policy = frozen_policy.get("review_queue") if isinstance(frozen_policy, dict) else None
    supported_queue = queue_policy is None or queue_policy in (
        {"binding": "immutable_proposal_v1", "dispatch": "synchronous_v1"},
        {"binding": "immutable_proposal_v1", "dispatch": "async_fact_v1"},
        {"binding": "immutable_proposal_v1", "dispatch": "async_planning_v1"})
    supported_confirmation = isinstance(frozen_policy, dict) and frozen_policy.get("human_confirmation") in {None, "assume_after_300s_v1"}
    if (config.outer_role != expected.outer_role or config.inner_role != expected.inner_role
            or config.goal_validation != declaration.get("goal_validation")
            or config.budget != expected.d4_budget or not isinstance(frozen_policy, dict) or not supported_queue or not supported_confirmation
            or config.execution_policy_ref != "sha256:" + digest(frozen_policy)
            or {k: v for k, v in frozen_policy.items() if k not in {"review_queue", "human_confirmation"}} != {k: v for k, v in policy.items() if k not in {"review_queue", "human_confirmation"}}):
        raise ValueError("stage.execution_configuration_changed")
    rows = store.load_control_state_rows(task_id)
    if rows is None:
        raise ValueError("stage.task_run_not_found")
    task, checkpoint = rows.task_run, rows.latest_checkpoint
    turns = store.list_stage_turns(task_id)
    current = active_turn(turns)
    result = _empty_result(task_id, declaration["mode"], error="", recovery=False)
    result.update(execution_profile=declaration.get("execution_profile", "d8_v22"),
        contract_version=declaration.get("contract_version", 22))
    result["task_run"] = dict(goal=task.request.goal, status=task.status.value, reason=task.failure_reason,
        sandbox_id=task.sandbox_id, deadline_at=task.deadline_at, finished_at=task.finished_at,
        confirmation_deadline=task.confirmation_deadline)
    result["checkpoint"] = dict(sequence=checkpoint.sequence, watermark_event_sequence=checkpoint.watermark_event_sequence)
    result["dispatch"] = dict(status=_dispatch_status(task.status.value), checkpoint_sequence=checkpoint.sequence,
        turn_id=current.turn_id if current else None)
    from agent_core.stage_plans import status as planning_review_status
    from agent_core.stage_contracts import PlanningSession
    result["turns"] = [dict(turn_id=t.turn_id, parent_turn_id=t.parent_turn_id,
        intent=asdict(t.intent), planning_state=t.planning_state.value,
        review_state=planning_review_status(store, task_id, PlanningSession("turn_planning", t.turn_id), evidence=evidence),
        achievement=asdict(t.achievement) if t.achievement else None, fact_refs=list(t.fact_refs),
        roads=[dict(road_id=r.road_id, description=r.candidate.description,
            support_refs=list(r.candidate.support_refs), status=r.status.value,
            review_state=planning_review_status(store, task_id, PlanningSession("road_execution", r.road_id), evidence=evidence))
            for r in t.roads]) for t in turns]
    history = evidence.history(task_id)
    result["verified_facts"] = [dict(fact_id=i.record_id, turn_id=i.turn_id, road_id=i.intent_id,
        content=i.content, ref=i.ref, source_refs=list(i.source_refs), source_status=i.source_status,
        current_status=i.current_status, effective_ref=i.effective_ref, conflict=i.conflict)
        for i in history if i.record_type == "verified_fact"]
    operations = store.list_d4_operations(task_id)
    queue = {entry.proposal_operation_id: entry for entry in store.list_stage_reviews(task_id)}
    requests = {op.start.operation_id: evidence.request(op.start.operation_id)
        for op in operations if op.start.category != "tool_dispatch"}
    corrected = {r.correction_of_model_request_id for r in requests.values() if r.correction_of_model_request_id}
    failures = []
    for op in operations:
        if op.start.operation_id not in requests or op.completion is None:
            continue
        saved = evidence.read("result", op.start.operation_id)
        if saved is None or digest(saved) != op.completion.evidence_sha256:
            raise ValueError("stage.model_result_changed")
        if saved["outcome"] != "valid" and op.start.operation_id not in corrected:
            request = requests[op.start.operation_id]
            failure = dict(operation_id=op.start.operation_id,
                phase=request.stage, code="stage.model_output_unusable",
                category="input" if saved["outcome"] == "invalid" else "dependency")
            if saved["outcome"] == "superseded":
                stop = evidence.branch_stop(request)
                if stop is None or digest(stop) != saved.get("branch_stop_sha256"):
                    raise ValueError("stage.branch_stop_binding_changed")
                failure.update(code="stage.generation_superseded", category="branch_correction")
            if saved["outcome"] == "invalid":
                from agent_core.output_contracts import D8ModelResponse
                from agent_core.stage_planning import stage_output_issues
                failure["issue_codes"] = [i.code for i in stage_output_issues(requests[op.start.operation_id],
                    D8ModelResponse(saved["content"], saved["finish_reason"], saved.get("tool_calls")))]
            provider = evidence.read("provider-result", op.start.operation_id) or {}
            if saved["outcome"] == "model_error" and evidence.transport_failure(request, saved) is not None:
                from agent_core.stage_planning import MAX_STAGE_CORRECTIONS
                failure.update(code="stage.model_transport_failed", model_error_code=provider["error"]["code"])
                if request.stage == "stage_supervision" and evidence.correction_depth(request) == MAX_STAGE_CORRECTIONS:
                    failure["code"] = "stage.supervision_unavailable"
                from agent_core.stage_contracts import StageFactProposal, StageFactBatch, TurnIntent
                from agent_core.stage_plans import admitted
                async_planning = (store._schema_version >= 38 and supported_queue
                    and queue_policy == {"binding": "immutable_proposal_v1", "dispatch": "async_planning_v1"}
                    and isinstance(request.proposal, (TurnIntent, tuple))
                    and admitted(store, request.proposal_operation_id))
                if (request.stage == "stage_review" and (async_planning or request.context.session.scope == "road_execution"
                        and isinstance(request.proposal, (StageFactProposal, StageFactBatch)))
                        and evidence.correction_depth(request) == MAX_STAGE_CORRECTIONS):
                    from agent_core.stage_planning import review_members, conversation_operation_ids
                    members = review_members(request)
                    if all(member in queue and (
                            queue[member].state == "blocked" and queue[member].review_operation_id == op.start.operation_id
                            or queue[member].state == "superseded" and set(evidence.review_interruptions(
                                queue[member].review_operation_id)).intersection(conversation_operation_ids(evidence.request(member))))
                            for member in members):
                        failure["code"] = "stage.review_unavailable"
            transport = provider.get("transport", {})
            if transport.get("stream_error"):
                failure.update(stream_error=transport["stream_error"], finish_seen=transport["finish_seen"],
                    done_seen=transport["done_seen"])
            failures.append(failure)
    result["model_failures"] = failures
    result["errors"] = [f for f in failures if f["code"] not in {
        "stage.supervision_unavailable", "stage.review_unavailable", "stage.generation_superseded"}]
    result["tool_operations"] = [dict(operation_id=op.start.operation_id,
        road_id=op.start.resource_ref.split("/")[1], phase="tool_dispatch",
        status=op.completion.result_kind if op.completion else "in_flight")
        for op in operations if op.start.category == "tool_dispatch"]
    result.update(_stage_activity(evidence, store, task_id, operations, requests, turns))
    usage = store.d4_budget_usage(task_id)
    result["budget"] = dict(status="available", policy_ref=usage.budget_policy_ref, total_used=usage.total_used,
        total_limit=None if expected.intent_execution_policy == "continuous_v1" else expected.d4_budget.total,
        category_counts=dict(usage.category_counts))
    candidates, goal, feedback_unknown = _candidate_projection(store, task_id,
        through_event_sequence=checkpoint.watermark_event_sequence)
    result["answer_candidates"] = [{("road_id" if k == "intent_id" else k): v for k, v in item.items()} for item in candidates]
    result["goal_validation"] = dict(mode=config.goal_validation, confirmation_deadline=task.confirmation_deadline,
        timeout_action="assume" if frozen_policy.get("human_confirmation") == "assume_after_300s_v1" else "end_unverified",
        candidate_id=candidates[-1]["candidate_id"] if candidates else None,
        actions=["accept", "continue", "end"] if task.status.value == "awaiting_confirmation" else [])
    result["goal_evaluation"] = goal
    result["overall_goal"] = dict(outcome=goal)
    result["overall_goal_summary"].update(outcome=goal, candidate_count=len(candidates))
    result["external_validation_feedback"] = [dict(candidate_id=item["candidate_id"], **feedback)
        for item in candidates for feedback in item["feedback"]]
    checks = store.list_d7_external_validation_checks(task_id)
    result["external_validation_checks"] = [dict(query_id=r.request.query_id,
        submission_operation_id=r.request.submission_operation_id, candidate_id=r.request.candidate_id,
        evidence_version=r.request.evidence_version, status="started" if r.result is None else "completed",
        verdict=r.result.verdict if r.result else None) for r in checks]
    result["validation_accounting"] = dict(status="available", logical_check_count=len(checks),
        completed_count=sum(r.result is not None for r in checks))
    result["imports"] = [dict(ref=i.ref, metadata=json.loads(i.content))
        for i in history if i.identity == "imported_attachment_not_fact"]
    unresolved = store.blocking_d4_operations(task_id)
    recovery = bool(unresolved) or feedback_unknown or any(r.result is None for r in checks) or bool(result["errors"])
    result["recovery"] = dict(required=recovery, reason=result["errors"][0]["code"] if result["errors"] else "operation_or_evidence_unknown" if recovery else None)
    result["recovery_status"] = "required" if recovery else "not_required"
    result["cleanup"], result["cleanup_status"] = _cleanup(Path(directory), task)
    from agent_core.operator_controls import current as operator_control
    control = operator_control(store, task_id)
    if control is not None:
        result["operator_control"] = control
    from agent_core.operator_controls import user_hints
    result["user_hints"] = [dict(**hint, ref=next(i.ref for i in history
        if i.record_id == "user/" + hint["operation_id"])) for hint in user_hints(store, task_id)]
    if task.status in TERMINAL_STATUSES:
        terminal = store.load_terminal_commit(task_id)
        if terminal is None:
            raise ValueError("terminal_result_unavailable")
        _, terminal_goal, terminal_unknown = _candidate_projection(store, task_id,
            through_event_sequence=terminal.checkpoint.watermark_event_sequence)
        outcome = map_run_outcome(D7RunOutcomeState(task_run_status=terminal.event.status.value,
            goal_evaluation=terminal_goal, cleanup_state=result["cleanup_status"],
            unresolved_operation_unknown=bool(unresolved) or terminal_unknown, recovery_required=bool(unresolved)))
        result["run_exit_code"] = outcome.run_exit_code
        result["run_outcome"] = dict(**outcome.to_dict(), recorded_checkpoint_sequence=terminal.checkpoint.sequence,
            recorded_event_sequence=terminal.event.sequence, run_status=terminal.event.status.value, goal_evaluation=terminal_goal)
    return result


def _snapshot_v3_task_result(directory: Path, declaration: dict, *, read_store=None) -> dict:
    task_id = declaration.get("task_run_id")
    mode = declaration.get("mode")
    if not isinstance(task_id, str) or declaration.get("protocol_version") != 3:
        raise ValueError("invalid_v3_run_declaration")
    result = _empty_result(task_id, mode if isinstance(mode, str) else "unknown", error="", recovery=False)

    # Try to find the database in agent-state directory first (correct location)
    agent_dirs = list((directory / "agent-state").glob("*/control.sqlite"))
    if agent_dirs:
        database = agent_dirs[0]
    else:
        # Fallback to old location
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

    from contextlib import nullcontext
    with (nullcontext(read_store) if read_store is not None else SqliteControlStore.open_readonly(database)) as store:
        rows = store.load_control_state_rows(task_id)
        if rows is None or rows.task_run.task_run_id != task_id:
            return _empty_result(task_id, result["mode"], error="task_run_state_unavailable", recovery=True)
        from agent_core.operator_controls import current
        operator=current(store,task_id)
        if operator is not None:result["operator_control"]=operator
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
        local_states = {r.intent_id: r.kind for r in store.list_d8_tree_records(task_id)
                        if r.kind in {"suspended", "resumed", "strategy_yielded", "strategy_resumed"}}
        for intent in rows.intents:
            if intent.turn_id not in intent_by_turn:
                return _empty_result(task_id, result["mode"], error="intent_ownership_invalid", recovery=True)
            intent_by_turn[intent.turn_id].append(
                {
                    "intent_id": intent.intent_id,
                    "description": intent.description,
                    "priority": intent.priority,
                    "status": intent.status.value,
                    **({"suspension_kind": "strategy"} if intent.status.value == "suspended"
                       and local_states.get(intent.intent_id) == "strategy_yielded" else {}),
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
                "content": fact.content,
                **({"source_execution_id":fact.source_ref} if any(e.execution_id==fact.source_ref for e in rows.executions)
                   else {"source_execution_id":None,"source_operation_id":fact.source_ref}),
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
                "total_limit": None if config.intent_execution_policy == "continuous_v1" else config.d4_budget.total,
                "total_used": usage.total_used,
                "category_limits": {
                    name: None if config.intent_execution_policy == "continuous_v1" else getattr(config.d4_budget, name)
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
        if time.time() >= task.deadline_at and task.status not in TERMINAL_STATUSES:
            pending = [op.start.operation_id for op in store.list_d4_operations(task_id)
                       if op.completion is None]
            result['dispatch'].update(status='stopped', reason='task_run_deadline_exceeded',
                                      pending_operation_ids=pending)
            if pending:
                result['errors'].append(dict(code='deadline_reached_with_pending_operations',
                    phase='operation_settlement', category='technical_wait'))
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


def _transient_storage_read(error) -> bool:
    import sqlite3
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 6:
        seen.add(id(error))
        if (isinstance(error, sqlite3.OperationalError)
                and getattr(error, "sqlite_errorcode", 0) & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}):
            return True
        if isinstance(error, OSError) and getattr(error, "winerror", None) in {32, 33}:
            return True
        error = error.__cause__ or error.__context__
    return False


def _snapshot_once(directory: Path, declaration: dict) -> dict:
    """Return a safe result; unreadable state is represented as recovery required."""
    phase = "declaration_validation"
    try:
        if ("execution_profile" in declaration.get("request", {})
                and "execution_profile" not in declaration):
            raise ValueError("d8_frozen_configuration_mismatch")
        d8_unresolved = False
        business_rows = []
        http_diagnostics = []
        model_format_diagnostics = []
        review_call_diagnostics = []
        known_model_failure = None
        adjudication = None
        result = None
        if "execution_profile" in declaration:
            from agent_adapters.simulation import parse_request
            from agent_core.http_contracts import is_http
            phase = "configuration_validation"
            _, expected = parse_request(declaration["request"])
            if (expected is None or declaration["execution_profile"] != declaration["request"]["execution_profile"]
                    or declaration.get("contract_version") != expected.contract_version
                    or declaration.get("mode") != declaration["request"]["mode"]
                    or declaration.get("d4_budget_policy") != asdict(expected.d4_budget)):
                raise ValueError("d8_frozen_configuration_mismatch")
            if declaration.get("mode") == "real":
                from agent_adapters.simulation import validate_real_declaration
                phase = "runtime_declaration"
                validate_real_declaration(Path(directory), declaration)
            path = Path(directory) / "control.sqlite"
            if path.exists():
                phase = "database_open"
                with SqliteControlStore.open_readonly(path) as store:
                    phase = "configuration_read"
                    if store.load_stage_run_config(declaration["task_run_id"]) is not None:
                        return _stage_task_result(directory, declaration, store, expected)
                    config = store.load_d3_run_config(declaration["task_run_id"])
                    if config != expected:
                        raise ValueError("d8_frozen_configuration_mismatch")
                    phase = "operation_read"
                    d8_unresolved = bool(store.list_d4_unresolved_operations(declaration["task_run_id"]))
                    if config.contract_version >= 6:
                        from types import SimpleNamespace
                        from agent_core.structured_output import StructuredOutput, format_stop_reason
                        from agent_adapters.output_evidence import FileD8OutputEvidence
                        output = FileD8OutputEvidence(Path(directory) / "d8-output", create=False)
                        boundary = StructuredOutput(SimpleNamespace(config=config), output)
                        operations = store.list_d4_operations(declaration["task_run_id"])
                        phase = "output_diagnostics"
                        review_call_diagnostics = output.review_failures(operations)
                        for operation in operations:
                            diagnostic = boundary.diagnostic(operation.start.operation_id)
                            if diagnostic is not None:
                                if diagnostic["task_run_id"] != declaration["task_run_id"]:
                                    raise ValueError("d8_output_diagnostic_binding_changed")
                                model_format_diagnostics.append(diagnostic)
                        # The ledger is in insertion order; clock timestamps can tie.
                        latest = operations[-1] if operations else None
                        from agent_core.intent_execution import suspended_calls
                        handled_format = suspended_calls(store, declaration["task_run_id"]) if config.intent_execution_policy == "continuous_v1" else set()
                        from agent_adapters.handoff_evidence import FileD8HandoffEvidence
                        from agent_core.conclusion_contracts import conclusion_digest
                        from agent_core.turn_modeling import stable_id
                        sessions = FileD8HandoffEvidence(Path(directory) / "d8-sessions", create=False)
                        for record in store.list_d8_decisions(declaration["task_run_id"]):
                            if record.disposition != "technical_failure":
                                continue
                            data = sessions.read("decision", record.record_id)
                            if data is None or conclusion_digest(data) != record.evidence_sha256:
                                raise ValueError("d22_optional_failure_binding_changed")
                            notice = data["observation"].get("optional_conclusion_failure")
                            if (notice is None or notice.get("proposal_request_id") != record.model_request_id
                                    or notice != sessions.read("pause", stable_id(record.task_run_id,
                                        "d8/optional-format/" + record.model_request_id))):
                                raise ValueError("d22_optional_failure_binding_changed")
                            handled_format.add(notice["failed_request_id"])
                        if latest is not None and latest.start.operation_id not in handled_format and latest.application_ref is None and latest.completion is not None and latest.completion.result_kind in {"rejected", "failed"}:
                            diagnostic = next((row for row in model_format_diagnostics
                                               if row["model_request_id"] == latest.start.operation_id), None)
                            if diagnostic is not None:
                                known_model_failure = format_stop_reason(diagnostic) or "d8_model_format_rejected"
                                if (diagnostic["stage"] == "intent_evaluation" and any(
                                        issue["code"] == "string_too_long" and issue["path"] == "$/reason"
                                        for issue in diagnostic["issues"])):
                                    known_model_failure = "d8_intent_evaluation_reason_too_long"
                            else:
                                from agent_adapters.handoff_evidence import FileD8HandoffEvidence
                                from agent_core.turn_modeling import digest
                                saved_failure = FileD8HandoffEvidence(Path(directory) / "d8-handoff", create=False).read(
                                    "result", latest.start.operation_id)
                                if (saved_failure is not None and saved_failure.get("outcome") == "model_error"
                                        and saved_failure.get("reason") in {"d8_provider_payment_required", "d8_provider_auth_failed"}
                                        and saved_failure.get("model_request_id") == latest.start.operation_id
                                        and saved_failure.get("request_sha256") == latest.start.context_sha256
                                        and latest.completion.evidence_ref == latest.start.operation_id
                                        and latest.completion.evidence_sha256 == digest(saved_failure)):
                                    known_model_failure = saved_failure["reason"]
                                elif latest.start.category == "review" and latest.start.resource_ref == latest.start.operation_id:
                                    known_model_failure = "d8_review_call_failed"
                    if is_http(config):
                        from agent_adapters.http_capability import check_http_task
                        from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
                        try:
                            phase = "http_evidence"
                            d8_unresolved |= check_http_task(Path(directory), store, config,
                                                            diagnostics=http_diagnostics)
                        except ExecutionRecoveryRequired as exc:
                            # A single broken HTTP operation must not erase the
                            # whole projection; keep what was read and report
                            # recovery with the precise reason instead.
                            d8_unresolved = True
                            http_diagnostics.append(
                                {"code": str(exc), "phase": "projection"})
                    if config.contract_version in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                        from agent_core.business_diagnostics import BusinessDiagnostics
                        from agent_adapters.output_evidence import FileD8OutputEvidence
                        from agent_adapters.handoff_evidence import FileD8HandoffEvidence
                        diagnostics = BusinessDiagnostics(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD8HandoffEvidence(Path(directory) / "d8-handoff", create=False), store)
                        phase = "business_diagnostics"
                        business_rows = diagnostics.projection(store.list_d4_operations(declaration["task_run_id"]))
                    if config.contract_version in {8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                        from agent_core.fact_diagnostics import FactBusinessDiagnostics
                        from agent_adapters.conclusion_evidence import FileD8Evidence
                        facts = FactBusinessDiagnostics(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD8Evidence(Path(directory) / "d8-conclusions", create=False), store)
                        phase = "fact_diagnostics"
                        business_rows += facts.projection(store.list_d4_operations(declaration["task_run_id"]))
                    if config.contract_version in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                        from agent_core.review_diagnostics import ReviewDiagnostics
                        from agent_adapters.review_ledger import FileD2ReviewLedger
                        reviews = ReviewDiagnostics(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD2ReviewLedger(Path(directory) / "d3-reviews"), store, lambda: 0)
                        phase = "review_diagnostics"
                        business_rows += reviews.projection(store.list_d4_operations(declaration["task_run_id"]))
                        d8_unresolved |= reviews.needs_repair
                    if config.contract_version in {11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                        from types import SimpleNamespace
                        from agent_core.fixed_probe import FixedProbeBindings
                        from agent_adapters.model_evidence import FileD3ModelEvidence
                        fixed = FixedProbeBindings(SimpleNamespace(config=config, store=store,
                            output_boundary=SimpleNamespace(evidence=FileD8OutputEvidence(
                                Path(directory) / "d8-output", create=False)),
                            sessions=SimpleNamespace(evidence=FileD8HandoffEvidence(
                                Path(directory) / "d8-sessions", create=False)),
                            model_evidence=FileD3ModelEvidence(Path(directory) / "d3-models", create=False)))
                        phase = "fixed_probe_evidence"
                        d8_unresolved |= fixed.check_task(declaration["task_run_id"])
                    if config.contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and not is_http(config) and config.tcp_authorization is None:
                        from agent_core.planning_assertion import PlanningAssertionBindings
                        from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
                        assemblies = PlanningAssertionBindings(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD8HandoffEvidence(Path(directory) / "d8-handoff", create=False),
                            FileD8Evidence(Path(directory) / "d8-conclusions", create=False), store,
                            FileAttachmentImportRecorder(Path(directory) / "imports", read_only=True))
                        phase = "planning_evidence"
                        d8_unresolved |= assemblies.check_task(declaration["task_run_id"])
                    if config.contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}:
                        from agent_core.completion_evaluation import EvaluationBindings
                        evaluations = EvaluationBindings(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD8HandoffEvidence(Path(directory) / "d8-handoff", create=False), store)
                        d8_unresolved |= evaluations.check_task(declaration["task_run_id"])
                        phase = "evaluation_evidence"
                        from agent_core.intent_execution import pending_adjudication
                        adjudication = pending_adjudication(store, evaluations.handoff, evaluations.output, declaration["task_run_id"])
                        from agent_core.intent_execution import budget_stop_reason
                        budget_pause = budget_stop_reason(store, evaluations.handoff, declaration["task_run_id"])
                        if budget_pause is not None:
                            known_model_failure = budget_pause
                        business_rows.extend(evaluations.projection(store.list_d4_operations(declaration["task_run_id"])))
                        from agent_core.completion_evaluation import check_candidate_task
                        d8_unresolved |= check_candidate_task(config,
                            FileD8OutputEvidence(Path(directory) / "d8-output", create=False),
                            FileD8HandoffEvidence(Path(directory) / "d8-sessions", create=False),
                            FileD3ModelEvidence(Path(directory) / "d3-models", create=False),
                            store, declaration["task_run_id"])
                    if config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
                        from agent_core.selection_bindings import SelectionBindings
                        from agent_adapters.process_fact_evidence import FileD5Evidence
                        selections = SelectionBindings(SimpleNamespace(config=config, store=store,
                            model_evidence=FileD3ModelEvidence(Path(directory) / 'd3-models', create=False),
                            sessions=SimpleNamespace(evidence=FileD8HandoffEvidence(Path(directory) / 'd8-sessions', create=False)),
                            handoff=SimpleNamespace(evidence=FileD8HandoffEvidence(Path(directory) / 'd8-handoff', create=False)),
                            revisions=SimpleNamespace(evidence=FileD8HandoffEvidence(Path(directory) / 'd8-revisions', create=False)),
                            refinement=SimpleNamespace(evidence=FileD5Evidence(Path(directory) / 'd5-process', create=False))),
                            FileD8OutputEvidence(Path(directory) / 'd8-output', create=False))
                        phase = "selection_evidence"
                        d8_unresolved |= selections.check_task(declaration['task_run_id'])
                    if config.contract_version==22:
                        from agent_adapters.conclusion_evidence import check_evidence_conclusions
                        phase="conclusion_evidence"
                        check_evidence_conclusions(Path(directory),store,config,declaration['task_run_id'])
                    phase="result_projection"
                    result=_snapshot_v3_task_result(Path(directory),declaration,read_store=store)
        if result is None:
            phase = "result_projection"
            result = _snapshot_v3_task_result(Path(directory), declaration)
        phase = "result_assembly"
        if declaration.get("execution_profile") in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
            result.update(execution_profile=declaration["execution_profile"], contract_version=expected.contract_version)
            if expected.contract_version in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                result["business_diagnostics"] = business_rows
            if declaration.get("mode") == "real":
                result.update(sandbox_backend=declaration["sandbox_backend"], model_backend="deepseek",
                              capacity_measurement="estimated_token_units")
            if is_http(expected):
                result['http_diagnostics'] = http_diagnostics
            if expected.contract_version >= 6:
                result["model_format_diagnostics"] = model_format_diagnostics
                result["review_call_diagnostics"] = review_call_diagnostics
            if d8_unresolved:
                # Surface the precise failing check; the generic code is a last
                # resort so an operator never has to re-derive it by hand.
                specific = next(
                    (item.get("code") for item in reversed(http_diagnostics)
                     if isinstance(item, dict) and isinstance(item.get("code"), str)
                     and item.get("active",True)
                     and item["code"].startswith(("d20_", "d22_", "d8_", "d3_", "mcp."))),
                    None,
                )
                result["recovery"] = {"required": True,
                                      "reason": specific or "operation_or_evidence_unknown"}
                result["recovery_status"] = "required"
            elif adjudication is not None and (result.get("task_run") or {}).get("status") == "running":
                result["recovery"] = {"required": True, "reason": adjudication["reason"], "cause": adjudication["cause"]}
                result["recovery_status"] = "required"
            elif known_model_failure is not None and (result.get("task_run") or {}).get("status") == "running":
                result["recovery"] = {"required": True, "reason": known_model_failure}
                result["recovery_status"] = "required"
            if (expected.intent_execution_policy == "continuous_v1" and not d8_unresolved
                    and (result.get("task_run") or {}).get("status") == "running"):
                from agent_adapters.handoff_evidence import FileD8HandoffEvidence
                block = FileD8HandoffEvidence(Path(directory) / "d8-handoff", create=False).capacity_block(
                    declaration["task_run_id"], result["checkpoint"]["sequence"])
                if block is not None:
                    result["capacity_block"] = block
                    if not result.get("recovery", {}).get("required"):
                        result["recovery"] = {"required": True, "reason": "d8_context_capacity_blocked"}
                        result["recovery_status"] = "required"
            # An inconclusive local Intent is not a recovery condition: it only
            # means that direction's evidence could not decide it. The run keeps
            # exploring other directions, and only a found flag needs a human.
        return result
    except Exception as exc:  # noqa: BLE001 -- read-only result boundary
        from agent_adapters.output_evidence import safe_failure_details
        task_id = declaration.get("task_run_id") if isinstance(declaration, dict) else None
        mode = declaration.get("mode") if isinstance(declaration, dict) else None
        result = _empty_result(
            task_id if isinstance(task_id, str) else "unknown",
            mode if isinstance(mode, str) else "unknown",
            error="state_read_busy" if _transient_storage_read(exc) else "state_read_failed",
            recovery=True,
        )
        result["errors"][0].update(phase="state_read", read_stage=phase,
                                   **safe_failure_details(exc, include_chain=True))
        return result


def _state_read_busy(result) -> bool:
    errors = result.get("errors") if isinstance(result, dict) else None
    return (bool(errors) and isinstance(errors[0], dict)
            and errors[0].get("code") == "state_read_busy")


def snapshot_v3_task_result(directory: Path, declaration: dict) -> dict:
    """Project the committed result, retrying a transient storage read.

    The projection runs while the run is still settling, so an evidence file or
    the SQLite WAL can be briefly unavailable. Retrying those keeps a healthy run
    from being reported as state_read_busy with an empty body; a persistent
    failure still returns the safe empty result. Invalid evidence is not retried.
    """
    result = _snapshot_once(directory, declaration)
    for attempt in range(2):
        if not _state_read_busy(result):
            break
        time.sleep(0.15 * (attempt + 1))
        result = _snapshot_once(directory, declaration)
    path=Path(directory).parent.parent/"competition-binding.json"
    if path.is_file():
        from agent_adapters.competition_submission import frozen_binding
        try:
            binding=frozen_binding(path,declaration)
            result["competition"]={"adapter":binding["profile"]["adapter"],"remote_ref":binding["question"]["remote_ref"]}
        except (OSError,ValueError):
            result.setdefault("errors",[]).append({"code":"competition_run_binding_invalid"})
            result["recovery"]={"required":True,"reason":"competition_run_binding_invalid"}
    return result


__all__ = ["snapshot_v3_task_result"]
