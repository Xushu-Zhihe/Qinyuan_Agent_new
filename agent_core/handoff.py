"""D8-2 safe history selection, joint summaries and frozen local reads."""

from __future__ import annotations

import json

from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from agent_core.capacity import check_capacity, estimate_frontier_tokens, format_capacity_error
from agent_core.conclusion_contracts import conclusion_digest, _fields
from agent_core.control_store import StorageError, UpsertIntent, UpsertTurn
from agent_core.domain import IntentStatus, TurnStatus
from agent_core.dual_llm_contracts import (
    D3CommitMetadata, D3IntentBinding, D3RecoveryRequired, D3TurnBinding,
)
from agent_core.dual_llm_modeling import D3TurnProposal, parse_d3_turn_proposal
from agent_core.handoff_contracts import (
    D8Capacity, D8CapacityVersion, D8HandoffRequest, D8HandoffVersion, D8HistoryItem,
    D8ReadStart, D8SummaryCandidate, D8TreeRecord, checked_refs, handoff_json,
)
from agent_core.intent_context import build_intent_frontier, format_frontier_for_context
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4BudgetExhausted, D4OperationCompletion, D4OperationRecord, D4OperationStart, _uuid
from agent_core.turn_modeling import stable_id, digest


EVALUATION_FALLBACK_LIMIT = 3


@dataclass(frozen=True)
class D8PlanningResult:
    proposal: D3TurnProposal | None
    dependencies: tuple[tuple[str, ...], ...]
    rejected_indices: tuple[int, ...]
    exhaustion_reason: str | None = None


class D8DependencyCandidatesRejected(ValueError):
    def __init__(self, indices):
        self.indices = tuple(indices)
        super().__init__("d8_all_dependency_candidates_rejected")


def _provider_billing_code(error):
    """Map a provider payment/auth status to an actionable, non-retryable code.

    ``deepseek_http_402`` means the account cannot pay; 401/403 indicate
    authentication or access rejection. Neither improves by automatic replanning.
    """
    reason = getattr(error, "short_reason", "") or ""
    if reason.endswith("http_402"):
        return "d8_provider_payment_required"
    if reason.endswith(("http_401", "http_403")):
        return "d8_provider_auth_failed"
    return None


def _trace(event, **fields):
    """Localize a stalled handoff call; silent unless QINYUAN_DEBUG_TRACE is set."""
    import os
    if not os.environ.get("QINYUAN_DEBUG_TRACE"):
        return
    import sys
    print("[trace]", event,
          " ".join(f"{k}={v}" for k, v in fields.items()), file=sys.stderr, flush=True)


class HandoffContext:
    def __init__(self, app, evidence, model, capacity_port):
        self.app, self.store, self.evidence = app, app.store, evidence
        self.model, self.capacity_port = model, capacity_port
        self.pause_report = None

    @property
    def state(self):
        return self.app.state

    def _id(self, name):
        return stable_id(self.state.task_run.task_run_id, "d8-2/" + name)

    def _commit(self, operation, record, changes=(), metadata=None):
        return self.app.controller.commit_d3_transition(
            operation, (*changes, record), (record.record_id,), record.record_id,
            record.record_id, metadata=metadata)

    def capacity(self):
        rows = self.store.list_d8_capacities(self.state.task_run.task_run_id)
        if not rows:
            row = D8CapacityVersion(self._id("capacity/1"), self.state.task_run.task_run_id,
                                    1, self.app.config.handoff_config.capacity)
            self._commit("append_d8_capacity", row)
            rows = (row,)
        return rows[-1]

    def increase_capacity(self, updates):
        self.app._check_dispatch()
        if self.store.list_d4_unresolved_operations(self.state.task_run.task_run_id):
            raise D3RecoveryRequired("d8_restore_original_request_before_capacity_change")
        if any(call.start.category in {"planning", "action", "review", "fact_review", "handoff_summary"}
               and call.completion is not None and call.completion.result_kind == "completed"
               and call.application_ref is None
               for call in self.store.list_d4_operations(self.state.task_run.task_run_id)):
            raise D3RecoveryRequired("d8_apply_known_model_result_before_capacity_change")
        old = self.capacity()
        new = D8CapacityVersion(self._id(f"capacity/{old.version + 1}"), old.task_run_id,
                                old.version + 1, old.capacity.increased(updates))
        self._commit("append_d8_capacity", new)
        return new

    def _pause(self, kind, required, current, needed, omitted=(), *, reason=None):
        self.pause_report = {"kind": kind, "required_refs": list(required),
                             "current_capacity": current, "known_needed": needed,
                             "unknown_needed": needed is None, "unit": "unicode_codepoints",
                             "omitted_refs": list(omitted),
                             "choices": ["increase_capacity", "narrow_authorized_branch", "end_task"]}
        if self.app.config.intent_execution_policy and kind == "complete_model_request":
            measured = getattr(self.capacity_port, "last_measurement", None)
            if measured is not None:
                self.pause_report["input_measurement"] = measured
                self.pause_report["unit"] = measured.get("estimate_unit", self.pause_report["unit"])
        identity = self._id("pause/" + conclusion_digest(self.pause_report))
        self.evidence.save("pause", identity, self.pause_report)
        raise D3RecoveryRequired(reason or "d8_context_paused:" + kind)

    def history(self, *, apply_revisions=True):
        state, items = self.state, []
        task_id = state.task_run.task_run_id
        sequences = self.store.d8_history_sequences(task_id)
        turns = {t.turn_id: t for t in state.turns}
        intents = {i.intent_id: i for i in state.intents}
        plans = {p.plan_id: p for p in state.plans}

        def add(record_id, kind, turn_id, intent_id, artifact, content, source, version,
                identity="trusted_state", status="available", conflict=False):
            text = self.app.redactor.redact(content)
            scope = "current" if (intent_id and intents[intent_id].status is IntentStatus.RUNNING
                                   or not intent_id and turn_id and turns[turn_id].status is TurnStatus.RUNNING) else "historical"
            items.append(D8HistoryItem(record_id, task_id, kind, turn_id, intent_id, artifact,
                                       text if status == "available" else "", tuple(source),
                                       conclusion_digest(version), sequences.get(record_id, 1),
                                       identity, scope, status, conflict))

        for turn in state.turns:
            add(turn.turn_id, "turn", turn.turn_id, None, None,
                handoff_json({"status": turn.status.value, "parent_turn_id": turn.parent_turn_id,
                              "triggering_intent_id": turn.triggering_intent_id}), (), asdict(turn))
        for intent in state.intents:
            add(intent.intent_id, "intent", intent.turn_id, intent.intent_id, None,
                handoff_json({"goal": intent.description, "status": intent.status.value}), (), asdict(intent))
        for plan in state.plans:
            intent = intents[plan.intent_id]
            binding = self.store.load_d3_plan_binding(plan.plan_id)
            add(plan.plan_id, "progress", intent.turn_id, intent.intent_id,
                binding.artifact_version_id if binding else None,
                handoff_json({"kind": "plan", "status": plan.status.value}), (), asdict(plan))
        for execution in state.executions:
            plan = plans[execution.plan_id]
            intent = intents[plan.intent_id]
            binding = self.store.load_d3_plan_binding(plan.plan_id)
            if self.app.config.contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}:
                from agent_core.completion_evaluation import execution_projection
                content, version = execution_projection(self.app, execution, binding)
                add(execution.execution_id, "progress", intent.turn_id, intent.intent_id,
                    binding.artifact_version_id if binding else None, handoff_json(content),
                    (execution.execution_id,), version, "saved_execution_observation_not_verified_fact")
                continue
            add(execution.execution_id, "progress", intent.turn_id, intent.intent_id,
                binding.artifact_version_id if binding else None,
                handoff_json({"kind": "execution", "status": execution.status.value,
                              "step_index": execution.step_index}), (execution.execution_id,), asdict(execution))
        for fact in state.facts:
            if (fact.verification_ref or "").startswith("d8e:"):
                record=next((r for r in self.store.list_d8_conclusions(task_id) if r.review_request_id==fact.verification_ref[4:]),None)
                if record is None:raise D3RecoveryRequired("d22_fact_approval_missing")
                status="available"
                try:self.app.evidence_conclusions.validate_fact(fact,state)
                except D3RecoveryRequired:status="unavailable"
                add(fact.fact_id,"verified_fact",fact.turn_id,record.intent_id,None,fact.content,
                    (fact.source_ref,fact.verification_ref),{"fact":asdict(fact),"source":record.source_version_sha256},"outer_reviewed_evidence_fact",status)
                items[-1]=replace(items[-1],scope="historical")
                continue
            execution = next(e for e in state.executions if e.execution_id == fact.source_ref)
            intent = intents[plans[execution.plan_id].intent_id]
            artifact, status, version = None, "available", asdict(fact)
            try:
                if not fact.verification_ref or not fact.verification_ref.startswith("d3:"):
                    raise D3RecoveryRequired("d8_history_rule_not_registered")
                observed = self.app.conclusions.observation(fact.verification_ref[3:], historical=True)
                if (observed.fact_content() != fact.content
                        and self.app.config.contract_version not in {21, 22}):
                    # Contracts 21/22 commit the content the outer review
                    # accepted (refined_content), so the observation template is
                    # no longer the expected content there. Treating that as an
                    # unavailable source made every refined Fact unreliable and
                    # blocked the next dispatch.
                    raise D3RecoveryRequired("d8_history_fact_binding_changed")
                artifact = observed.artifact_version_id
                version = {"fact": asdict(fact), "source": observed.source_version_sha256}
            except D3RecoveryRequired:
                status = "unavailable"
            add(fact.fact_id, "verified_fact", fact.turn_id, intent.intent_id, artifact,
                fact.content, (fact.source_ref, fact.verification_ref or ""), version,
                "verified_observation_only", status)
            items[-1] = replace(items[-1], scope="historical")
        for view in self.app.conclusions.read_inferences(task_id):
            record = view.record
            add(record.record_id, "reviewed_inference", record.turn_id, record.intent_id,
                view.observation.artifact_version_id if view.observation else None,
                view.candidate.content if view.candidate else "", (record.proposal_request_id, record.review_request_id),
                {"record": asdict(record)}, "reviewed_inference_not_verified", view.source_status)
            if view.scope_status == "ended":
                items[-1] = replace(items[-1], scope="historical")
        for record in self.store.list_d8_conclusions(task_id):
            if record.plan_id is None and record.outcome=="reviewed_inference":
                request=self.app.conclusions.evidence.read_request(record.review_request_id)
                status="available"
                try:self.app.evidence_conclusions.validate_record(record,state)
                except D3RecoveryRequired:status="unavailable"
                add(record.record_id,"reviewed_inference",record.turn_id,record.intent_id,None,
                    self.app.conclusions.accepted_content(record),(record.proposal_request_id,record.review_request_id),
                    asdict(record),"reviewed_inference_not_verified",status)
            add("review/" + record.record_id, "progress", record.turn_id, record.intent_id, None,
                handoff_json({"kind": "conclusion_review", "outcome": record.outcome,
                              "fact_id": record.fact_id}), (record.review_request_id,), asdict(record))
        if getattr(self.app, "supervision", None) is not None and self.app.supervision.enabled:
            for identity, turn_id, note in self.app.supervision.interruptions():
                add("interruption/" + identity, "progress", turn_id, note["intent_id"], None,
                    handoff_json(note), (identity, *([note["review_id"]] if note.get("review_id") else [])),
                    note, "strategy_interruption_not_execution_or_fact")
        if self.app.config.intent_execution_policy and self.app.sessions is not None:
            for record in self.app.sessions.records():
                data = self.app.sessions.payload(record)
                add("decision/" + record.record_id, "progress", record.turn_id, record.intent_id, None,
                    handoff_json({"decision": data["response"], "observation": data["observation"],
                                  "identity": "unreviewed_working_record" if data["response"].get("kind") == "working_record" else "historical_decision_not_verified"}),
                    (record.model_request_id,), asdict(record), "unreviewed_working_record" if data["response"].get("kind") == "working_record" else "historical_decision_not_verified")
        if self.app.mcp is not None:
            for operation in self.app.mcp.records():
                claim = self.app.output_boundary.evidence.read('mcp-call',operation.start.operation_id)
                if not claim or 'experiment_request' not in claim or operation.completion is None:
                    continue
                owner = intents[claim['intent_id']]
                result = self.app.mcp.feedback(operation.start.operation_id)
                add(operation.start.operation_id,'progress',owner.turn_id,owner.intent_id,None,
                    handoff_json({'observation':result,'identity':'recorded_http_experiment_observation_not_fact'}),
                    (claim['model_request_id'],),{'claim':claim,'result':result},'execution_observation')
        for record in self.store.list_d8_tree_records(task_id):
            if record.turn_id not in turns:
                continue
            owner_intent = record.intent_id if record.intent_id in intents and intents[record.intent_id].turn_id == record.turn_id else None
            if record.kind in {"suspended", "strategy_yielded"} and self.app.sessions is not None:
                note = self.evidence.read("pause" if record.kind == "suspended" else "strategy-yield", record.record_id)
                if note is not None and "progress" in note:
                    self.app.sessions.validate_handoff_progress(owner_intent, note["progress"])
                    add("handoff/" + record.record_id, "progress", record.turn_id, owner_intent, None,
                        handoff_json(note), tuple(note["progress"]["decision_refs"]),
                        {"record": asdict(record), "note": note}, "unreviewed_intent_handoff_not_fact")
            content = {"kind": record.kind, "support_fact_id": record.support_fact_id,
                       "local_goal_decision_is_not_a_fact": True}
            if record.kind == "selection":
                selection = self.evidence.read("decision", record.record_id)
                if selection is not None:
                    from agent_core.intent_planning import selection_context
                    turn = turns[record.turn_id]
                    if selection != selection_context(self.app, turn, intents[record.intent_id]):
                        raise D3RecoveryRequired("d22_intent_selection_context_changed")
                    content["selection"] = selection
            if self.app.config.intent_execution_policy and record.inner_request_id:
                from agent_core.intent_execution import result_summary
                summary = result_summary(self.app, record)
                if summary is not None:
                    content.update(summary)
            add("tree/" + record.record_id, "progress", record.turn_id, owner_intent, None,
                handoff_json(content),
                (record.model_request_id,) if record.model_request_id else (), asdict(record),
                "model_local_goal_decision" if record.kind in {"succeeded", "failed", "inconclusive"} else "trusted_state")
        if self.app.refinement is not None:
            for record in self.store.list_process_facts(task_id):
                projections = [self.app.refinement.sources.project(task_id, source) for source in record.sources]
                candidate = self.app.refinement.evidence.read_candidate(record.model_request_id)
                status = "available" if (candidate is not None and digest(asdict(candidate)) == record.evidence_sha256
                                          and all(p["status"] == "available" for p in projections)) else "unavailable"
                if candidate is not None and self.app.conclusions._sensitive(asdict(candidate)):
                    status = "withheld"
                origin = next((p.get("origin") for p in projections if p.get("origin")), {})
                turn_id, intent_id = origin.get("turn_id"), origin.get("intent_id")
                if turn_id not in turns:
                    turn_id = None
                if intent_id not in intents:
                    intent_id = None
                artifact = next((s.artifact_version_id for s in record.sources if s.artifact_version_id), None)
                add(record.process_fact_id, "progress", turn_id, intent_id, artifact,
                    handoff_json(asdict(candidate)) if candidate is not None else "",
                    (s.object_id for s in record.sources), asdict(record),
                    "process_fact_model_summary_not_verified", status, bool(record.conflicts))
                if any(p.get("applicability") == "historical" for p in projections):
                    items[-1] = replace(items[-1], scope="historical")
        if self.app.config.contract_version in {19, 20, 21, 22}:
            for candidate, feedback, projection in self.app.answers.feedback_history():
                add(feedback.operation_id, 'progress', candidate.turn_id, candidate.intent_id,
                    candidate.source_ref, handoff_json({'kind': 'answer_feedback',
                        'candidate_id': candidate.candidate_id, 'verdict': feedback.verdict,
                        'operation_id': feedback.operation_id, 'source_ref': feedback.source_ref,
                        'evidence_version': candidate.evidence_version,
                        **(self.app.competition_feedback_port(feedback) if getattr(self.app,'competition_feedback_port',None) else {})}),
                    (candidate.source_ref,) if candidate.execution_id is None else (candidate.execution_id,), asdict(feedback),
                    'trusted_final_answer_feedback_not_local_goal_verdict', conflict=projection == 'conflict')
        if apply_revisions and self.app.revisions is not None:
            items = self.app.revisions.project(items)
        return tuple(sorted(items, key=lambda i: (i.committed_sequence, i.record_type, i.record_id)))

    def active_intent_frontier(self):
        """
        Build explicit active Intent frontier for tree planning context.

        Returns structured frontier with all pending/running Intents, their depth,
        activity status, and progress metrics. Injected into tree planning context
        so model can see Intent tree structure when proposing new candidates.

        Design: Lenient toward input (shows full frontier), strict about boundaries
        (only validated Intents from state).
        """
        frontier = build_intent_frontier(
            intents=list(self.state.intents),
            turns=list(self.state.turns),
            plans=list(self.state.plans),
            facts=list(self.state.facts),
            task_run_id=self.state.task_run.task_run_id,
        )
        return frontier

    def format_frontier_json(self) -> str:
        """Format active Intent frontier as JSON for context injection."""
        frontier = self.active_intent_frontier()
        return format_frontier_for_context(frontier)

    def check_frontier_capacity(self, available_capacity: int) -> tuple[bool, str | None]:
        """
        Check if active Intent frontier fits in available context capacity.

        Strict boundary check: either ALL active Intents fit, or we pause with
        explicit error listing which Intents were excluded.

        Args:
            available_capacity: Available tokens in model context

        Returns:
            (fits, error_message) tuple:
                - fits=True, error=None if frontier fits
                - fits=False, error=message if frontier doesn't fit

        Raises:
            D3RecoveryRequired: if capacity check fails
        """
        frontier = self.active_intent_frontier()
        result = check_capacity(frontier, available_capacity)

        if not result.fits:
            error_msg = format_capacity_error(result)
            return (False, error_msg)

        return (True, None)


    def _latest_summary(self):
        rows = self.store.list_d8_handoffs(self.state.task_run.task_run_id)
        if not rows:
            return None, None
        record = rows[-1]
        data = self.evidence.read("handoff", record.evidence_ref)
        if data is None or conclusion_digest(data) != record.evidence_sha256:
            raise D3RecoveryRequired("d8_summary_evidence_unavailable")
        return record, data

    def _dependencies(self, intent_id, *, apply_revisions=True):
        for row in self.store.list_d8_tree_records(self.state.task_run.task_run_id):
            for owner, refs in row.dependencies:
                if owner == intent_id:
                    if apply_revisions and self.app.revisions is not None:
                        resolved = tuple(self.app.revisions.relation_ref("intent_dependency", owner, ref) for ref in refs)
                        for original, current in zip(refs, resolved, strict=True):
                            self.app.revisions.check_relation_binding("intent_dependency", owner, original, current)
                        return resolved
                    return refs
        return ()

    def select(self, phase, turn_id=None, intent_id=None, support_fact_id=None):
        from qinyuan_core.logging import timed_phase
        with timed_phase("model", "history_selection", run=self.state.task_run.task_run_id,
                         turn=turn_id, intent=intent_id, stage=phase):
            return self._select(phase, turn_id, intent_id, support_fact_id)

    def _select(self, phase, turn_id=None, intent_id=None, support_fact_id=None):
        items, required = self.history(), set()
        active = []
        if self.app.config.contract_version == 22 and phase == 'tree_planning':
            active = [i for i in self.state.intents if i.status.value in {'pending', 'running'}]
            active_ids = {i.intent_id for i in active}
            required.update(i.ref for i in items if i.record_type == 'intent' and i.record_id in active_ids)
        if self.app.config.contract_version in {19, 20, 21, 22} and phase == 'tree_planning' and intent_id:
            required.update(i.ref for i in items if i.intent_id == intent_id
                and i.identity == 'trusted_final_answer_feedback_not_local_goal_verdict')
        if self.app.config.intent_execution_policy and intent_id:
            required.update(i.ref for i in items if i.intent_id == intent_id and i.record_type == "verified_fact" and i.source_status == "available" and not i.conflict and
                (self.app.revisions is None or self.app.revisions.reliable(i)))
            required.update(i.ref for i in items if i.intent_id == intent_id
                            and i.identity == "strategy_interruption_not_execution_or_fact")
        if phase == "execution" and intent_id and getattr(self.app, "supervision", None):
            stop = self.app.supervision.stop(intent_id)
            if stop and stop.get("review_latest_evidence"):
                review_refs = set((*stop["evidence_refs"], *stop["new_evidence_refs"]))
                required.update(i.ref for i in items if i.ref in review_refs
                    and not (i.record_type == "intent" and i.turn_id == turn_id and i.record_id != intent_id))
        if phase == "execution" and intent_id and self.app.config.intent_execution_policy == "continuous_v1":
            # Durable model-authored reading indexes remain unreviewed, but must
            # not disappear solely because newer dialog displaced their window.
            required.update(i.ref for i in items if i.intent_id == intent_id and i.identity == "unreviewed_working_record")
            from agent_core.intent_execution import last_continuation
            previous_review = last_continuation(self.app, intent_id)
            if previous_review is not None:
                required.update(i.ref for i in items if i.record_id == "tree/" + previous_review.record_id)
        if phase == "execution" and intent_id:
            handoffs = [i for i in items if i.identity == "unreviewed_intent_handoff_not_fact"
                        and i.turn_id == turn_id]
            own = [i for i in handoffs if i.intent_id == intent_id]
            required.update(i.ref for i in [*handoffs[-1:], *own[-1:]])
        evaluation = None
        by_ref = {i.ref: i for i in items}
        by_id = {i.record_id: i for i in items}
        turns = {t.turn_id: t for t in self.state.turns}
        ancestors = set()
        current = turns.get(turn_id)
        while current is not None:
            ancestors.add(current.turn_id)
            required.add(by_id[current.turn_id].ref)
            if current.triggering_intent_id:
                trigger = current.triggering_intent_id
                required.add(by_id[trigger].ref)
                supports = [r.support_fact_id for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                            if r.intent_id == trigger and r.kind == "succeeded"]
                if len(supports) != 1 or supports[0] not in by_id:
                    raise D3RecoveryRequired("d8_parent_support_relation_missing")
                support_id = supports[0]
                if self.app.revisions is not None and phase not in {"handoff_summary", "conclusion_repair"}:
                    support_id = self.app.revisions.parent_fact(trigger, support_id)
                required.add(by_id[support_id].ref)
                if self.app.config.intent_execution_policy:
                    parent = next(r for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                                  if r.intent_id == trigger and r.kind == "succeeded")
                    original = self.evidence.read("request", parent.model_request_id)
                    policy = json.loads(original["prior_summary"])["s4_evaluation_policy"]
                    from agent_core.intent_execution import selected_fact_refs
                    decision = self._known_call(D8HandoffRequest.from_dict(original))["raw"]
                    required.update(ref for ref in selected_fact_refs(policy, decision)
                                    if by_ref[ref].record_id != parent.support_fact_id)
            current = turns.get(current.parent_turn_id) if current.parent_turn_id else None
        if intent_id:
            required.add(by_id[intent_id].ref)
            if self.app.revisions is not None and phase in {"handoff_summary", "conclusion_repair"}:
                required.update(self._dependencies(intent_id, apply_revisions=False))
            else:
                required.update(self._dependencies(intent_id))
            plans = [p for p in self.state.plans if p.intent_id == intent_id]
            if plans:
                result = self.store.load_d3_plan_result(plans[-1].plan_id)
                if result is not None and result.supporting_fact_id:
                    support = by_id.get(result.supporting_fact_id)
                    if support is None:
                        raise D3RecoveryRequired("d8_current_plan_support_missing")
                    required.add(support.ref)
        if support_fact_id:
            if support_fact_id not in by_id:
                raise D3RecoveryRequired("d8_support_fact_missing")
            required.add(by_id[support_fact_id].ref)
        if self.app.config.contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22} and phase == "intent_evaluation":
            # These observations are evidence, never a program-generated semantic decision.
            from agent_core.completion_evaluation import freeze_evaluation_policy
            policy = freeze_evaluation_policy(self.app, intent_id, support_fact_id, items)
            required.update(policy["required_observation_refs"])
            evaluation = policy
        maintenance_summary = self.app.revisions is not None and phase in {"handoff_summary", "conclusion_repair"}
        if maintenance_summary:
            required = {i.ref for i in items if i.conflict or getattr(i, "current_status", "effective") != "effective"}
            required.update(i.last_version_ref for i in items if getattr(i, "last_version_ref", None)
                            and i.last_version_ref in by_ref and i.ref in required)
        for ref in required:
            item = by_ref.get(ref)
            if (not maintenance_summary and (item is None or item.source_status != "available" or item.conflict
                    or self.app.revisions is not None and not self.app.revisions.reliable(item))):
                raise D3RecoveryRequired("d8_required_source_unavailable_or_changed")
        _, previous = self._latest_summary()
        ranks = {r: n for n, r in enumerate(previous["ordered_refs"])} if previous else {}
        recent_turns = {t.turn_id for t in sorted(self.state.turns, key=lambda t: t.sequence)[-2:]}
        shared = {s.claim.artifact_version_id for s in self.app.workspace_evidence.list_shares(self.state.task_run.task_run_id)
                  if s.claim.target_intent_id == intent_id}

        def group(item):
            if intent_id and item.intent_id == intent_id:
                return 0
            if item.turn_id == turn_id:
                return 1
            if item.turn_id in ancestors:
                return 2
            if item.artifact_version_id and item.artifact_version_id in shared:
                return 3
            if item.record_type == "progress" and item.turn_id in recent_turns:
                return 4
            return 5

        if self.app.config.intent_execution_policy and phase == "tree_planning":
            prior_results = [i for i in items if i.identity == "model_local_goal_decision"
                             and i.source_status == "available" and not i.conflict]
            required.update(i.ref for i in prior_results if i.turn_id in ancestors)
        chosen = [i for i in items if i.ref in required]
        optional = []
        for kind, target in (("verified_fact", 8), ("reviewed_inference", 4), ("progress", 6)):
            room = max(0, target - sum(i.record_type == kind for i in chosen))
            candidates = [i for i in items if i.record_type == kind and i.ref not in required
                          and i.source_status == "available" and not i.conflict and group(i) < 5
                          and (i.identity != "unreviewed_working_record" or i.intent_id == intent_id)
                          and (self.app.revisions is None or self.app.revisions.reliable(i))]
            candidates.sort(key=lambda i: (group(i), ranks.get(i.ref, len(ranks)), -i.committed_sequence, i.ref))
            optional.extend(candidates[:room])
        chosen.extend(optional)
        version = self.capacity()

        def packet():
            omitted = [i.ref for i in items if i not in chosen]
            summary_available = True
            if previous:
                originals = [D8HistoryItem.from_dict(i) for i in previous["all_items"]
                             if f"{i['record_type']}:{i['record_id']}:{i['source_version']}" in previous["actual_read_refs"]]
                for original in originals:
                    current_source = by_id.get(original.record_id)
                    if original.record_type in {"verified_fact", "reviewed_inference"} and (
                            current_source is None or current_source.source_status != "available"
                            or current_source.source_version != original.source_version):
                        summary_available = False
            if self.app.revisions is not None and self.app.revisions.summary_stale(previous):
                summary_available = False
            result = {"phase": phase, "summary_identity": "model_organized_not_verified",
                    "global_body": previous["global_body"] if previous and summary_available else "",
                    "global_source_status": "available" if summary_available else "unavailable",
                    "global_scope": "historical_source_snapshot",
                    "items": [i.visible() for i in chosen], "required_refs": sorted(required),
                    "coverage": {"local_index_count": len(items), "included_count": len(chosen),
                                 "omitted_count": len(omitted), "complete_semantic_coverage": False},
                    "query_entry": {"task_run_id": self.state.task_run.task_run_id,
                                    "filters": ["turn_id", "intent_id", "artifact_version_id", "record_type"]}}
            if evaluation is not None:
                result['s4_evaluation_policy'] = {**evaluation,
                    'history_sha256': digest([asdict(i) for i in chosen])}
            if self.app.config.intent_execution_policy == "continuous_v1" and intent_id and phase == "execution":
                result["progress_rule"] = ("Repeated end applications and conclusion proposals are internal work, not "
                    "automatic progress. Compare actual evidence/deliverable gain and whether the next action can "
                    "distinguish the hypothesis under recorded observation limits. Consult preserved reading indexes "
                    "and original refs before calling a source unread; an unreviewed index is not evidence. "
                    "A useful sibling remains optional; absence of new requests or Facts alone never proves failure.")
            if self.app.config.intent_execution_policy:
                result["unresolved_conclusions"] = [i.visible() for i in items if i.conflict]
                result["intent_execution_policy"] = "rolling_v1"
            if intent_id and phase == "execution" and getattr(self.app, "supervision", None):
                advice = self.app.supervision.advice(intent_id)
                if advice:
                    result["strategy_advice"] = advice
            if self.app.config.contract_version == 22:
                result['intent_planning'] = {'version': 1, 'directions_are_solution_chains': True}
                if phase in {'tree_planning', 'execution'}:
                    result['intent_planning']['order'] = 'execution_priority'
                    result['intent_planning']['criteria'] = 'scoped_outcomes'
                if phase == 'tree_planning':
                    result['active_intent_frontier'] = [
                        {'intent_id': i.intent_id, 'turn_id': i.turn_id, 'status': i.status.value,
                         'ref': by_id[i.intent_id].ref} for i in active]
            return result
        limit = min(version.capacity.initial_history, version.capacity.visible_history)
        blocking_kind = "initial_history" if version.capacity.initial_history <= version.capacity.visible_history else "visible_history"
        while len(handoff_json(packet())) > limit and optional:
            chosen.remove(optional.pop())
        result = packet()
        if len(handoff_json(result)) > limit:
            self._pause(blocking_kind, required, limit, len(handoff_json(result)),
                        [i.ref for i in items if i not in chosen])
        return tuple(chosen), tuple(i for i in items if i not in chosen), result

    def _new_root_directions(self, request, result):
        policy = json.loads(request.prior_summary).get("continuous_root_replan")
        if policy is not None and result.proposal is not None:
            prior = {" ".join(value.split()).casefold() for value in policy["previous_directions"]}
            if any(" ".join(item.description.split()).casefold() in prior for item in result.proposal.intents):
                raise ValueError("d22_root_direction_already_attempted")
        return result

    def _parse(self, request, raw):
        if request.stage == "strategy_supervision":
            from agent_core.strategy_supervision import parse
            return parse(request, raw)
        if request.stage == "tree_planning":
            from agent_core.intent_planning import without_order_reason
            raw = without_order_reason(request, raw)
        if self.app.web is not None and request.stage == 'tree_planning':
            from agent_core.http_policy import planning
            return self._new_root_directions(request, planning(request, raw, self.app.revisions))
        refs = {i.ref for i in request.history}
        if request.stage == "handoff_summary":
            candidate = D8SummaryCandidate.parse(raw, refs, request.capacity)
            if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
                self.app.selection_bindings.verify(request, raw)
            return candidate
        if request.stage == "intent_evaluation":
            from agent_core.intent_execution import parse_evaluation
            policy = json.loads(request.prior_summary).get("s4_evaluation_policy", {})
            if policy.get("version") in {2, 3}:
                return parse_evaluation(request, raw, policy)
            value = _fields(raw, {"decision", "reason", "evidence_refs"})
            if (value["decision"] not in {"succeeded", "failed", "inconclusive"}
                    or not isinstance(value["reason"], str) or not value["reason"].strip()
                    or "\x00" in value["reason"]):
                raise ValueError("d8_evaluation_invalid")
            cited = checked_refs(value["evidence_refs"], refs)
            support = next((i.ref for i in request.history if i.record_id == request.support_fact_id), None)
            if not cited or value["decision"] == "succeeded" and support not in cited:
                raise ValueError("d8_evaluation_support_reference_required")
            return value
        if not isinstance(raw, dict):
            raise ValueError("d8_planning_object_required")
        if raw.get("kind") == "exhausted":
            value = _fields(raw, {"kind", "reason", "evidence_refs"})
            if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 512:
                raise ValueError("d8_exhaustion_reason_invalid")
            checked_refs(value["evidence_refs"], refs)
            return D8PlanningResult(None, (), (), value["reason"])
        value = _fields(raw, {"kind", "turn_target", "completion_rule", "intents"})
        if value["kind"] != "candidates" or value["turn_target"] != request.goal:
            raise ValueError("d8_planning_target_or_kind_invalid")
        if (not isinstance(value["intents"], list) or not value["intents"]
                or self.app.config.contract_version != 22 and len(value["intents"]) > 2):
            raise ValueError("d8_planning_candidates_required_not_exhausted")
        kept, dependencies, rejected = [], [], []
        for index, candidate in enumerate(value["intents"]):
            item = _fields(candidate, {"description", "intent_type", "dependency_refs"})
            try:
                deps = checked_refs(item["dependency_refs"], refs)
                if any(i.source_status != "available" or i.conflict
                       or self.app.revisions is not None and not self.app.revisions.reliable(i)
                       for i in request.history if i.ref in deps):
                    raise ValueError("d8_dependency_unavailable")
            except ValueError:
                rejected.append(index)
                continue
            kept.append({"description": item["description"], "intent_type": item["intent_type"]})
            dependencies.append(deps)
        if not kept:
            raise D8DependencyCandidatesRejected(rejected)
        parsed = parse_d3_turn_proposal({"turn_target": value["turn_target"],
                                        "completion_rule": value["completion_rule"], "intents": kept},
                                       maximum=None if self.app.config.contract_version == 22 else 2)
        if parsed.attachment_index not in self.app.config.authorization.attachment_indices:
            raise ValueError("d8_planning_attachment_unauthorized")
        return self._new_root_directions(request, D8PlanningResult(parsed, tuple(dependencies), tuple(rejected)))

    def _known_call(self, request):
        if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
            self.app.selection_bindings.repair_request(request)
        call = self.store.load_d4_operation(request.model_request_id)
        result = self.evidence.read("result", request.model_request_id)
        if call is None:
            if result is not None:
                raise D3RecoveryRequired("d8_result_without_admission")
            return None
        if (call.start.task_run_id != request.task_run_id
                or call.start.context_sha256 != request.context_sha256
                or call.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                or call.start.role_config_ref != request.role_config_ref):
            raise D3RecoveryRequired("d8_call_binding_changed")
        if result is None:
            bindings = getattr(self.app, "planning_assertion_bindings", None)
            if bindings is not None and request.stage == "tree_planning":
                known = bindings.known_result(request)
                if known is not None:
                    planned = self._parse(request, known["raw"])
                    if known["rejected_candidate_indices"] != list(planned.rejected_indices):
                        raise D3RecoveryRequired("s2_planning_rejection_indices_changed")
                    if self.app.conclusions._sensitive(known["raw"]):
                        raise D3RecoveryRequired("s2_saved_planning_not_safe")
                    self.evidence.save("result", request.model_request_id, known)
                    result = known
        if result is None:
            evaluations = getattr(self.app, "evaluation_bindings", None)
            if evaluations is not None and request.stage == "intent_evaluation":
                known = evaluations.known_result(request)
                if known is not None:
                    self.evidence.save("result", request.model_request_id, known)
                    result = known
        if result is None:
            if self.app.business_diagnostics is not None and request.stage == "tree_planning":
                # Reject ambiguous/corrupt diagnostic evidence before repairing
                # any result file, including the older format-diagnostic path.
                self.app.business_diagnostics.read(request.model_request_id, check_operation=True)
            failure = self.app.output_boundary.failure(request) if self.app.output_boundary is not None else None
            if failure is not None:
                result = {"model_request_id": request.model_request_id,
                    "request_sha256": request.context_sha256,
                    "outcome": "model_error" if failure["terminal"] else "invalid", "raw": None,
                    "reason": "d8_output_contract_rejected", "rejected_candidate_indices": [],
                    "finished_at": failure["finished_at"]}
                self.evidence.save("result", request.model_request_id, result)
            elif self.app.business_diagnostics is not None and request.stage == "tree_planning":
                diagnostic = self.app.business_diagnostics.read(request.model_request_id, check_operation=True)
                if diagnostic is not None:
                    result = self.app.business_diagnostics.rejected_result(request, diagnostic)
                    self.evidence.save("result", request.model_request_id, result)
        if result is None:
            raise D3RecoveryRequired("d8_summary_or_planning_outcome_unknown")
        if (result.get("model_request_id") != request.model_request_id
                or result.get("request_sha256") != request.context_sha256
                or result.get("outcome") not in {"valid", "invalid", "model_error", "sensitive_rejected", "stale"}):
            raise D3RecoveryRequired("d8_saved_result_binding_changed")
        if result["outcome"] == "valid":
            parsed = self._parse(request, result["raw"])
            if self.app.config.contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and request.stage == "tree_planning" and (
                    result.get("rejected_candidate_indices") != list(parsed.rejected_indices)):
                raise D3RecoveryRequired("s2_planning_rejection_indices_changed")
            if self.app.conclusions._sensitive(result["raw"]):
                raise D3RecoveryRequired("d8_saved_summary_not_safe")
        elif result["raw"] is not None:
            raise D3RecoveryRequired("d8_invalid_result_contains_text")
        evaluations = getattr(self.app, "evaluation_bindings", None)
        if evaluations is not None and request.stage == "intent_evaluation":
            evaluations.verify_result(request, result)
        if self.app.business_diagnostics is not None and request.stage == "tree_planning":
            self.app.business_diagnostics.verify_result(request, result)
        bindings = getattr(self.app, "planning_assertion_bindings", None)
        if bindings is not None and request.stage == "tree_planning":
            bindings.verify_result(request, result)
        completion = D4OperationCompletion(request.model_request_id, result["finished_at"],
                                          "completed" if result["outcome"] == "valid" else "rejected",
                                          request.model_request_id, conclusion_digest(result))
        if call.completion is None:
            self.store.complete_d4_operation(completion)
            _trace("call.completed", stage=request.stage, outcome=result["outcome"],
                   request=request.model_request_id)
        elif call.completion != completion:
            raise D3RecoveryRequired("d8_saved_completion_changed")
        return result

    def reconcile(self):
        for operation in self.store.list_d4_operations(self.state.task_run.task_run_id):
            if getattr(self.app, "supervision", None) and self.app.supervision.live(operation.start.operation_id):
                continue
            request = self.evidence.read("request", operation.start.operation_id)
            known_format = (self.app.output_boundary is not None
                            and self.app.output_boundary.diagnostic(operation.start.operation_id) is not None)
            known_business = (request is not None and request.get("stage") == "tree_planning"
                              and self.app.business_diagnostics is not None
                              and self.app.business_diagnostics.read(operation.start.operation_id, check_operation=True) is not None)
            bindings = getattr(self.app, "planning_assertion_bindings", None)
            known_assembly = (request is not None and request.get("stage") == "tree_planning" and bindings is not None
                              and bindings.known_result(D8HandoffRequest.from_dict(request)) is not None)
            evaluations = getattr(self.app, 'evaluation_bindings', None)
            known_evaluation = (request is not None and request.get('stage') == 'intent_evaluation'
                                and evaluations is not None
                                and evaluations.known_result(D8HandoffRequest.from_dict(request)) is not None)
            known_selection = (self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}
                and self.app.output_boundary.evidence.read('selection-binding', operation.start.operation_id) is not None)
            if request is not None and (self.evidence.read("result", operation.start.operation_id) is not None or known_format or known_business or known_assembly or known_evaluation or known_selection):
                self._known_call(D8HandoffRequest.from_dict(request))

    def reconcile_review_operation(self, operation: D4OperationRecord) -> bool:
        """Recognize only a bound local-goal review owned by this evidence port."""
        raw = self.evidence.read("request", operation.start.operation_id)
        if raw is None:
            return False
        request = D8HandoffRequest.from_dict(raw)
        if request.stage == "strategy_supervision":
            self.app.supervision.reconcile(request)
            return True
        start = operation.start
        if (request.stage != "intent_evaluation" or start.category != "review"
                or request.model_request_id != start.operation_id
                or request.task_run_id != self.state.task_run.task_run_id
                or request.role_config_ref != self.app.config.outer_role.role_config_ref
                or start.role != "outer_planning"
                or start.resource_ref != (request.turn_id or request.task_run_id)
                or start.budget_policy_ref != self.app.config.d4_budget.budget_policy_ref):
            raise D3RecoveryRequired("d8_review_operation_binding_changed")
        # Validates request digest/checkpoint, saved result and completion;
        # an unknown result pauses rather than dispatching another model call.
        self._known_call(request)
        return True

    def _request(self, stage, turn_id, intent_id=None, support_fact_id=None):
        suggestions = None
        if stage == "tree_planning" and intent_id and self.app.sessions is not None:
            suggestions = self.app.sessions.next_suggestions(intent_id)
        selected, omitted, packet = self.select(stage, turn_id, intent_id, support_fact_id)
        if stage == "tree_planning" and getattr(self.app, "_root_replanning", False):
            packet["continuous_root_replan"] = {
                "previous_directions": [i.description for i in self.state.intents],
                "rule": "This is the sole continuation root. Propose materially different authorized directions using preserved evidence, or explicitly return exhausted. Do not repeat an old direction with only different tools or parameters."}
        if self.app.config.contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22} and stage == "intent_evaluation":
            from agent_core.completion_evaluation import freeze_evaluation_policy
            packet["s4_evaluation_policy"] = freeze_evaluation_policy(
                self.app, intent_id, support_fact_id, selected)
            current_capacity = self.capacity().capacity
            limit = min(current_capacity.initial_history, current_capacity.visible_history)
            if len(handoff_json(packet)) > limit:
                self._pause("evaluation_required_context", [i.ref for i in selected], limit,
                            len(handoff_json(packet)))
        if stage == "tree_planning":
            if self.app.web is not None:
                packet['d20_http_policy'] = self.app.web.policy()
            elif self.app.config.contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                from agent_core.planning_assertion import freeze_planning_policy
                packet["s2_planning_policy"] = freeze_planning_policy(self.app)
            packet["dependency_review"] = (
                "In this planning call, check dependency relevance and obvious omissions. "
                "Use only exact visible versions. This is model self-checking, not independent verification; "
                "undeclared implicit dependencies may still be missed.")
        version = self.capacity()
        latest, _ = self._latest_summary()
        global_hint = latest.record_id if latest is not None and latest.turn_id != turn_id else None
        name = f"{stage}/{turn_id}/{intent_id}/{conclusion_digest([asdict(i) for i in selected])}/{version.version}/{global_hint}"
        if stage == "tree_planning" and getattr(self.app, "_root_replanning", False):name += "/root-replan-once"
        request_id = self._id(name)
        saved = self.evidence.read("request", request_id)
        if saved is not None:
            return D8HandoffRequest.from_dict(saved), omitted
        request = D8HandoffRequest(request_id, self.state.task_run.task_run_id, turn_id, intent_id,
                                stage, self.app.config.outer_role.role_config_ref,
                                self.state.latest_checkpoint.sequence, version.version, version.capacity,
                                self.app.redactor.redact(self.state.task_run.request.goal), selected,
                                handoff_json(packet), support_fact_id)
        if suggestions is not None:
            from dataclasses import fields
            from agent_core.session_contracts import D8SuggestedPlanningRequest
            request = D8SuggestedPlanningRequest(**{f.name: getattr(request, f.name) for f in fields(request)},
                                                  suggestion_request_id=suggestions[0], inner_suggestions=suggestions[1])
        return request, omitted

    async def call(self, request, token):
        current = request
        for attempt in range(2):
            _trace("call.enter", stage=current.stage, attempt=attempt,
                   request=current.model_request_id)
            if self.model.adapter.role_config != self.app.config.outer_role:
                raise D3RecoveryRequired("d8_handoff_role_changed")
            self.evidence.save("request", current.model_request_id, asdict(current))
            result = self._known_call(current)
            if result is None:
                self.app._check_dispatch(allow_conclusion_repair=current.stage == "handoff_summary")
                if self.state.latest_checkpoint.sequence != current.base_checkpoint_sequence:
                    raise D3RecoveryRequired("d8_handoff_request_stale")
                policy = self.app.config.handoff_config.outer_capacity
                if not self._fits_complete_request(current, policy):
                    self._pause("complete_model_request", [i.ref for i in current.history],
                                policy.total_units, None)
                if self.app.output_boundary is not None:
                    self.app.output_boundary.prepare(current)
                category = {"handoff_summary": "handoff_summary", "tree_planning": "planning", "intent_evaluation": "review"}[current.stage]
                self.store.begin_d4_operation(D4OperationStart(
                    current.model_request_id, current.task_run_id, category, self.app.clock(),
                    current.base_checkpoint_sequence, current.context_sha256,
                    current.turn_id or current.task_run_id, self.app.config.d4_budget.budget_policy_ref,
                    "outer_planning", current.role_config_ref))
                raw = await self.model._invoke(current, min(180., self.state.task_run.deadline_at - self.app.clock()), token)
                wire = raw
                bindings = getattr(self.app, "planning_assertion_bindings", None)
                if (bindings is not None and current.stage == "tree_planning"
                        and raw is not None and not isinstance(raw, ModelError)):
                    from agent_core.planning_assertion import materialize
                    raw = materialize(current, raw)
                outcome, reason, rejected_indices = "valid", None, []
                business_issues = None
                evaluation_issue = None
                if isinstance(raw, ModelError):
                    from agent_core.structured_output import ended_transport_failure
                    if (raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}
                            and not ended_transport_failure((self.app.output_boundary.evidence.read("provider-result", current.model_request_id) or {}).get("transport", {}))):
                        raise D3RecoveryRequired("d8_model_outcome_unknown")
                    reason = _provider_billing_code(raw)
                    outcome, raw = "model_error", None
                elif self.app.conclusions._sensitive(raw):
                    outcome, raw = "sensitive_rejected", None
                else:
                    try:
                        parsed = self._parse(current, raw)
                        if isinstance(parsed, D8PlanningResult):
                            rejected_indices = list(parsed.rejected_indices)
                    except (ValueError, TypeError, KeyError) as exc:
                        from agent_core.intent_execution import EVALUATION_ISSUES
                        evaluation_issue = str(exc) if str(exc) in EVALUATION_ISSUES else None
                        outcome, reason = "invalid", "d8_output_contract_rejected"
                        if isinstance(exc, D8DependencyCandidatesRejected):
                            reason = "d8_all_dependency_candidates_rejected"
                            rejected_indices = list(exc.indices)
                        if (self.app.business_diagnostics is not None and current.stage == "tree_planning"
                                and self.app.output_boundary.diagnostic(current.model_request_id) is None):
                            from agent_core.business_diagnostics import planning_issues
                            business_issues = planning_issues(current, raw, self.app.config.authorization,
                                lambda item: self.app.revisions is None or self.app.revisions.reliable(item),
                                maximum=None if self.app.config.contract_version == 22 else 2)
                        raw = None
                        # Do not persist invalid raw responses or exceptions containing text.
                if (token.is_cancelled or self.app.clock() >= self.state.task_run.deadline_at
                        or self.state.latest_checkpoint.sequence != current.base_checkpoint_sequence):
                    outcome, raw = "stale", None
                finished_at = self.app.clock()
                result = {"model_request_id": current.model_request_id,
                          "request_sha256": current.context_sha256, "outcome": outcome,
                          "raw": raw, "reason": reason, "rejected_candidate_indices": rejected_indices,
                          "finished_at": finished_at}
                if business_issues is not None and outcome == "invalid":
                    business = self.app.business_diagnostics.save(
                        current, business_issues, reason, rejected_indices, finished_at)
                    result = self.app.business_diagnostics.rejected_result(current, business)
                if bindings is not None and current.stage == "tree_planning" and outcome == "valid":
                    result = bindings.save(current, wire, result)
                evaluations = getattr(self.app, "evaluation_bindings", None)
                if evaluations is not None and current.stage == "intent_evaluation":
                    if outcome == "valid":
                        result = evaluations.save(current, result)
                    elif outcome == "invalid" and self.app.output_boundary.diagnostic(current.model_request_id) is None:
                        result = evaluations.save_diagnostic(current, result, evaluation_issue)
                self.evidence.save("result", current.model_request_id, result)
                result = self._known_call(current)
            if self.app.output_boundary is not None and result["outcome"] != "stale":
                self.app.output_boundary.terminal_check(current)
            _trace("call.result", stage=current.stage, attempt=attempt,
                   outcome=result["outcome"], reason=result.get("reason"))
            if result["outcome"] == "valid":
                return current, self._parse(current, result["raw"])
            if result["outcome"] == "model_error" and result.get("reason") in {
                    "d8_provider_payment_required", "d8_provider_auth_failed"}:
                # The known response and completion are durable before stopping.
                raise D3RecoveryRequired(result["reason"])
            technical_retry = (result["outcome"] == "model_error" and self.app.output_boundary is not None
                               and self.app.output_boundary.retryable_technical_failure(current))
            if (result["outcome"] != "invalid" and not technical_retry) or attempt == 1:
                _trace("call.return_none", stage=current.stage, attempt=attempt, outcome=result["outcome"])
                return current, None
            correction_id = self._id(f"correction/{request.model_request_id}")
            saved = self.evidence.read("request", correction_id)
            current = D8HandoffRequest.from_dict(saved) if saved else replace(
                request, model_request_id=correction_id,
                correction_of_model_request_id=request.model_request_id,
                rejection_code="d8_output_contract_rejected")
        raise AssertionError("bounded handoff call did not return")

    async def summarize(self, turn_id, token):
        request, omitted = self._request("handoff_summary", turn_id)
        source_sha = conclusion_digest([asdict(i) for i in request.history])
        versions = self.store.list_d8_handoffs(request.task_run_id)
        prior = next((v for v in reversed(versions) if v.turn_id == turn_id), None)
        if (prior is not None and prior.source_snapshot_sha256 == source_sha
                and versions[-1].turn_id == turn_id):
            data = self.evidence.read("handoff", prior.evidence_ref)
            if data is None or conclusion_digest(data) != prior.evidence_sha256:
                raise D3RecoveryRequired("d8_reused_summary_evidence_unavailable")
            return prior
        origin = "model_organized"
        try:
            final, candidate = await self.call(request, token)
            request_id = final.model_request_id
        except D4BudgetExhausted:
            final, candidate, request_id = request, None, None
        if candidate is None:
            # Only known failures and pre-admission budget exhaustion can reach this branch.
            if request_id and self._known_call(final)["outcome"] == "stale":
                raise D3RecoveryRequired("d8_stale_summary_cannot_fallback")
            origin = "trusted_code_fallback"
            candidate = D8SummaryCandidate(
                f"Stage snapshot for Turn {turn_id}; no model interpretation. See labelled source entries.",
                f"Local history index contains {len(request.history) + len(omitted)} entries; no semantic summary.",
                (), ())
        self.app._check_dispatch(allow_conclusion_repair=True)
        if self.state.latest_checkpoint.sequence != request.base_checkpoint_sequence:
            raise D3RecoveryRequired("d8_summary_commit_stale")
        all_items = self.history()
        current = {i.ref: i for i in all_items}
        if any(current.get(i.ref) != i for i in request.history):
            raise D3RecoveryRequired("d8_summary_source_changed")
        identity = self._id("handoff/" + request.model_request_id)
        data = {"task_run_id": request.task_run_id, "turn_id": turn_id,
                "base_checkpoint_sequence": request.base_checkpoint_sequence,
                "capacity_version": request.capacity_version, "origin": origin,
                "identity": "handoff_information_not_verified", "turn_body": candidate.turn_body,
                "global_body": candidate.global_body, "ordered_refs": list(candidate.ordered_refs),
                "open_questions": [{"text": t, "refs": list(refs)} for t, refs in candidate.open_questions],
                "actual_read_refs": [i.ref for i in request.history],
                "included_refs": [i.ref for i in request.history],
                "omitted_refs": [i.ref for i in omitted], "pending_refs": [i.ref for i in omitted],
                "unordered_refs": [i.ref for i in all_items if i.ref not in candidate.ordered_refs],
                "all_items": [asdict(i) for i in all_items]}
        if self.app.revisions is not None:
            data["revision_sequence"] = max((r.sequence for r in self.app.revisions.records()
                                             if r.decision == "accept"), default=0)
        self.evidence.save("handoff", identity, data)
        record = D8HandoffVersion(identity, request.task_run_id, turn_id,
                                  sum(v.turn_id == turn_id for v in versions) + 1, len(versions) + 1,
                                  request.base_checkpoint_sequence, request.capacity_version, identity,
                                  conclusion_digest(data), source_sha, request_id, origin,
                                  tuple((i.ref, i.record_id, i.record_type, i.turn_id, i.intent_id,
                                         i.artifact_version_id) for i in all_items))
        self._commit("append_d8_handoff", record)
        return record

    async def plan_expansion(self, parent_turn_id, trigger_id, receipts, token, *, frozen_request=None):
        support = None
        if parent_turn_id:
            await self.summarize(parent_turn_id, token)
            successes = [r for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                         if r.kind == "succeeded" and r.intent_id == trigger_id]
            if len(successes) != 1:
                raise D3RecoveryRequired("d8_child_requires_committed_success_support")
            support = successes[0].support_fact_id
            if self.app.revisions is not None:
                support = self.app.revisions.parent_fact(trigger_id, support)
        request = frozen_request
        if request is None:request, _ = self._request("tree_planning", parent_turn_id, trigger_id, support)
        final, planned = await self.call(request, token)
        if not isinstance(planned, D8PlanningResult):
            raise D3RecoveryRequired("d8_planning_failed_not_exhausted")
        identity = self._id("tree/" + final.model_request_id)
        if planned.proposal is None:
            record = D8TreeRecord(identity, request.task_run_id, parent_turn_id, trigger_id,
                                  "expansion_exhausted", final.model_request_id)
            self._commit("exhaust_d8_expansion", record)
            return None
        from agent_core.turn_manager import TurnManager
        turn_id = TurnManager.registration_turn_id(request.task_run_id, identity)
        intent_ids = tuple(self._id(f"intent/{turn_id}/{n}") for n in range(len(planned.proposal.intents)))
        if self.app.web is not None:
            binding = self.app.web.turn_binding(turn_id, final.model_request_id)
        else:
            receipt = receipts[planned.proposal.attachment_index]
            binding = D3TurnBinding(self._id("binding/" + turn_id), request.task_run_id, turn_id,
                                    planned.proposal.attachment_index, receipt.import_id, receipt.byte_size,
                                    receipt.sha256, final.model_request_id)
        metadata = D3CommitMetadata(turn_binding=binding, intent_bindings=tuple(
            D3IntentBinding(request.task_run_id, turn_id, i, f"/workspace/work/intents/{i}") for i in intent_ids))
        turn = UpsertTurn(turn_id, request.task_run_id, len(self.state.turns) + 1,
                          self.state.task_run.request.goal, intent_ids, TurnStatus.PENDING,
                          parent_turn_id, trigger_id)
        intents = tuple(UpsertIntent(i, turn_id, c.description, n + 1, c.intent_type, IntentStatus.PENDING)
                        for n, (i, c) in enumerate(zip(intent_ids, planned.proposal.intents)))
        record = D8TreeRecord(identity, request.task_run_id, turn_id, trigger_id, "registration",
                              final.model_request_id, dependencies=tuple(zip(intent_ids, planned.dependencies)))
        self._commit("register_d8_tree_turn", record, (turn, *intents), metadata)
        return turn_id

    def select_intent(self, turn, intent):
        self.select("execution", turn.turn_id, intent.intent_id)
        record = D8TreeRecord(self._id("selection/" + intent.intent_id), self.state.task_run.task_run_id,
                              turn.turn_id, intent.intent_id, "selection", None)
        from agent_core.intent_planning import selection_context
        selection = selection_context(self.app, turn, intent)
        self.evidence.save("decision", record.record_id, selection)
        self._commit("select_d8_tree_intent", record,
                     (UpsertTurn(turn.turn_id, turn.task_run_id, turn.sequence, turn.turn_target,
                                 turn.intent_ids, TurnStatus.RUNNING),
                      UpsertIntent(intent.intent_id, intent.turn_id, intent.description,
                                   intent.priority, intent.intent_type, IntentStatus.RUNNING)))
        from qinyuan_core import get_logger
        get_logger("ctrl").info("intent.selected", run=record.task_run_id, turn=turn.turn_id, intent=intent.intent_id,
            request=selection["model_request_id"], priority=intent.priority,
            code="model_priority_order" if selection["reason_status"] == "provided" else "planning_order_reason_not_provided")

    def note_evaluation_fallback(self, code):
        """Count consecutive unreadable evaluation verdicts.

        Returns True while the tree may degrade this Intent to ``inconclusive``.
        Returns False once the limit is reached, so a *systemic* evaluator failure
        (for example a schema and parser that disagree) still stops the run instead
        of silently turning every Intent inconclusive.
        """
        self._evaluation_fallbacks = getattr(self, "_evaluation_fallbacks", 0) + 1
        _trace("evaluation.fallback", code=code, count=self._evaluation_fallbacks)
        return self._evaluation_fallbacks < EVALUATION_FALLBACK_LIMIT

    def note_evaluation_success(self):
        self._evaluation_fallbacks = 0

    async def evaluate_intent(self, intent_id, plan_id, fact_id, inner_request_id, token):
        intent = next(i for i in self.state.intents if i.intent_id == intent_id)
        request, _ = self._request("intent_evaluation", intent.turn_id, intent_id, fact_id)
        final, decision = await self.call(request, token)
        if decision is None:
            # An unreadable verdict leaves this Intent unjudged; that is a local
            # unknown, not an unknown run state, so degrade to inconclusive.
            if not self.note_evaluation_fallback("d8_local_goal_evaluation_invalid"):
                raise D3RecoveryRequired("d8_local_goal_evaluation_failed")
            record = D8TreeRecord(self._id("evaluation/" + final.model_request_id), request.task_run_id,
                                  intent.turn_id, intent_id, "inconclusive", final.model_request_id,
                                  fact_id, inner_request_id)
            self._commit("evaluate_d8_tree_intent", record,
                         (UpsertIntent(intent.intent_id, intent.turn_id, intent.description, intent.priority,
                                       intent.intent_type, IntentStatus.INCONCLUSIVE),))
            return "inconclusive"
        self.note_evaluation_success()
        record = D8TreeRecord(self._id("evaluation/" + final.model_request_id), request.task_run_id,
                              intent.turn_id, intent_id, decision["decision"], final.model_request_id,
                              fact_id, inner_request_id)
        self._commit("evaluate_d8_tree_intent", record,
                     (UpsertIntent(intent.intent_id, intent.turn_id, intent.description, intent.priority,
                                   intent.intent_type, IntentStatus(decision["decision"])),))
        return decision["decision"]

    def exhaust_turn(self, turn):
        record = D8TreeRecord(self._id("exhaust/" + turn.turn_id), self.state.task_run.task_run_id,
                              turn.turn_id, None, "turn_exhausted", None)
        self._commit("exhaust_d8_tree_turn", record,
                     (UpsertTurn(turn.turn_id, turn.task_run_id, turn.sequence, turn.turn_target,
                                 turn.intent_ids, TurnStatus.COMPLETED),))

    def validate_mutation(self, mutation, state):
        from agent_core.handoff_storage import D8_CHANGES
        records = [c for c in mutation.state_changes if isinstance(c, D8_CHANGES)]
        if len(records) != 1 or self.app.config.contract_version not in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            raise D3RecoveryRequired("d8_handoff_mutation_not_enabled")
        record = records[0]
        from agent_core.session_contracts import D8DecisionRecord
        if isinstance(record, D8DecisionRecord):
            if self.app.sessions is None:
                raise D3RecoveryRequired("d8_session_not_enabled")
            self.app.sessions.validate_record(record, state)
            return
        from agent_core.conclusion_revision_contracts import D8RevisionRecord
        if isinstance(record, D8RevisionRecord):
            if self.app.revisions is None:
                raise D3RecoveryRequired("d8_revision_not_enabled")
            self.app.revisions.validate_record(record, state)
            return
        if isinstance(record, D8CapacityVersion):
            return  # Store independently validates the explicit whitelist and append relation.
        if isinstance(record, D8HandoffVersion):
            data = self.evidence.read("handoff", record.evidence_ref)
            if (data is None or conclusion_digest(data) != record.evidence_sha256
                    or (data["task_run_id"], data["turn_id"], data["base_checkpoint_sequence"], data["capacity_version"], data["origin"])
                    != (record.task_run_id, record.turn_id, record.base_checkpoint_sequence, record.capacity_version, record.origin)):
                raise D3RecoveryRequired("d8_joint_summary_binding_changed")
            frozen = tuple(D8HistoryItem.from_dict(i) for i in data["all_items"])
            expected_index = tuple((i.ref, i.record_id, i.record_type, i.turn_id, i.intent_id,
                                    i.artifact_version_id) for i in frozen)
            if record.index_entries != expected_index:
                raise D3RecoveryRequired("d8_local_index_binding_changed")
            refs = {i.ref for i in frozen}
            read_refs = checked_refs(data["actual_read_refs"], refs)
            if (data["identity"] != "handoff_information_not_verified"
                    or data["included_refs"] != list(read_refs)
                    or data["omitted_refs"] != [i.ref for i in frozen if i.ref not in read_refs]
                    or data["pending_refs"] != data["omitted_refs"]
                    or data["unordered_refs"] != [i.ref for i in frozen if i.ref not in data["ordered_refs"]]
                    or record.source_snapshot_sha256 != conclusion_digest(
                        [asdict(next(i for i in frozen if i.ref == ref)) for ref in read_refs])):
                raise D3RecoveryRequired("d8_summary_coverage_binding_changed")
            capacities = self.store.list_d8_capacities(record.task_run_id)
            capacity = next(v.capacity for v in capacities if v.version == record.capacity_version)
            D8SummaryCandidate.parse({k: data[k] for k in (
                "turn_body", "global_body", "ordered_refs", "open_questions")}, set(read_refs), capacity)
            if record.model_request_id:
                request = D8HandoffRequest.from_dict(self.evidence.read("request", record.model_request_id))
                result = self._known_call(request)
                if (request.base_checkpoint_sequence != state.latest_checkpoint.sequence or result is None
                        or request.stage != "handoff_summary" or request.turn_id != record.turn_id
                        or request.capacity_version != record.capacity_version
                        or list(read_refs) != [i.ref for i in request.history]):
                    raise D3RecoveryRequired("d8_summary_scope_stale")
                if record.origin == "model_organized":
                    candidate = self._parse(request, result["raw"])
                    if (candidate.turn_body, candidate.global_body, list(candidate.ordered_refs),
                        [{"text": text, "refs": list(refs)} for text, refs in candidate.open_questions]) != (
                            data["turn_body"], data["global_body"], data["ordered_refs"], data["open_questions"]):
                        raise D3RecoveryRequired("d8_summary_candidate_changed")
                elif result["outcome"] not in {"invalid", "model_error", "sensitive_rejected"}:
                    raise D3RecoveryRequired("d8_fallback_is_not_known_failure")
            elif record.origin != "trusted_code_fallback":
                raise D3RecoveryRequired("d8_model_summary_has_no_call")
            else:
                usage = self.store.d4_budget_usage(record.task_run_id)
                if (dict(usage.category_counts).get("handoff_summary", 0) < self.app.config.d4_budget.handoff_summary
                        and usage.total_used < self.app.config.d4_budget.total):
                    raise D3RecoveryRequired("d8_fallback_has_no_failure_or_exhausted_budget")
            if frozen != self.history():
                raise D3RecoveryRequired("d8_summary_source_snapshot_changed")
            return
        if record.kind in {"strategy_yielded", "strategy_resumed"}:
            from agent_core.strategy_supervision import validate_transition, validate_yield_note
            validate_transition(self.store, mutation, state)
            if record.kind == "strategy_yielded":
                validate_yield_note(self.app, record, mutation)
            return
        if record.kind in {"suspended", "resumed"}:
            from agent_core.intent_execution import validate_local_transition
            validate_local_transition(self.store, mutation, state)
            if record.kind == "suspended":
                note = self.evidence.read("pause", record.record_id)
                if (note is None or note.get("intent_id") != record.intent_id
                        or note.get("failed_request_id") != record.model_request_id
                        or note.get("identity") != "technical_suspension_not_business_verdict"):
                    raise D3RecoveryRequired("d22_suspension_evidence_changed")
            return
        if record.model_request_id:
            raw_request = self.evidence.read("request", record.model_request_id)
            if raw_request is None:
                raise D3RecoveryRequired("d8_tree_request_missing")
            request = D8HandoffRequest.from_dict(raw_request)
            result = self._known_call(request)
            if result is None or result["outcome"] != "valid" or request.base_checkpoint_sequence != state.latest_checkpoint.sequence:
                raise D3RecoveryRequired("d8_tree_call_stale_or_unavailable")
            parsed = self._parse(request, result["raw"])
            current = {i.ref: i for i in self.history()}
            if any(current.get(i.ref) != i for i in request.history):
                raise D3RecoveryRequired("d8_tree_source_changed")
            if record.kind == "registration":
                if state.turns and request.turn_id is None:
                    marker = self.evidence.read("pause", self.app._id("continuous/root-replan"))
                    contract = self.app.output_boundary.evidence.read("contract", record.model_request_id)
                    if (self.app.config.intent_execution_policy != "continuous_v1" or marker is None
                            or contract is None or contract["root_request_id"] != marker["model_request_id"]):
                        raise D3RecoveryRequired("d22_root_replan_binding_changed")
                if not isinstance(parsed, D8PlanningResult) or parsed.proposal is None:
                    raise D3RecoveryRequired("d8_registration_has_no_candidates")
                change = next(c for c in mutation.state_changes if isinstance(c, UpsertTurn))
                children = [c for c in mutation.state_changes if isinstance(c, UpsertIntent)]
                if (change.parent_turn_id, change.triggering_intent_id) != (request.turn_id, request.intent_id):
                    raise D3RecoveryRequired("d8_child_parent_binding_changed")
                if (tuple((c.description, c.intent_type) for c in children)
                        != tuple((c.description, c.intent_type) for c in parsed.proposal.intents)
                        or tuple(c.priority for c in children) != tuple(range(1, len(children) + 1))
                        or tuple(refs for _, refs in record.dependencies) != parsed.dependencies
                        or change.turn_target != state.task_run.request.goal):
                    raise D3RecoveryRequired("d8_candidate_or_dependencies_changed")
                if self.app.web is not None:
                    if mutation.d3.turn_binding != self.app.web.turn_binding(change.turn_id, record.model_request_id):
                        raise D3RecoveryRequired('d20_http_turn_source_changed')
                else:
                    receipt = self.app.imports.read_receipt(mutation.d3.turn_binding.import_id)
                    if receipt is None or (receipt.byte_size, receipt.sha256) != (
                            mutation.d3.turn_binding.expected_byte_size, mutation.d3.turn_binding.expected_sha256):
                        raise D3RecoveryRequired("d8_turn_import_binding_changed")
            elif record.kind == "expansion_exhausted":
                if not isinstance(parsed, D8PlanningResult) or parsed.proposal is not None:
                    raise D3RecoveryRequired("d8_exhaustion_is_not_explicit")
                if (request.turn_id, request.intent_id) != (record.turn_id, record.intent_id):
                    raise D3RecoveryRequired("d8_exhaustion_scope_changed")
            else:
                from agent_core.intent_execution import support_anchor
                policy = json.loads(request.prior_summary).get("s4_evaluation_policy", {})
                anchor = support_anchor(request, policy, parsed) if policy.get("version") in {2, 3} else request.support_fact_id
                if (("evaluation_continued" if parsed["decision"] == "continue" else parsed["decision"]) != record.kind
                        or (request.turn_id, request.intent_id, anchor)
                        != (record.turn_id, record.intent_id, record.support_fact_id)):
                    raise D3RecoveryRequired("d8_local_goal_decision_changed")
                if json.loads(request.prior_summary).get("s4_evaluation_policy", {}).get("version") in {2, 3}:
                    from agent_core.intent_execution import validate_record
                    validate_record(self.app, request, record)
                    return
                inner = self.app.validator._known_model(record.task_run_id, record.inner_request_id, "inner_complete")
                if inner.action.fact_ids != (record.support_fact_id,):
                    raise D3RecoveryRequired("d8_inner_completion_support_changed")
                plans = [p for p in state.plans if p.intent_id == record.intent_id]
                binding = self.store.load_d3_plan_binding(plans[-1].plan_id)
                result = self.store.load_d3_plan_result(binding.plan_id)
                self.app.validator.completion_support(state, binding, result, record.support_fact_id)

    def _validate_frozen_read_sources(self, items):
        for item in items:
            if item.record_type == "verified_fact" and item.source_status == "available":
                proof = next((r[3:] for r in item.source_refs if r.startswith("d3:")), None)
                if proof is None:
                    raise D3RecoveryRequired("d8_read_source_version_missing")
                observation = self.app.conclusions.observation(proof, historical=True)
                fact = next((f for f in self.state.facts if f.fact_id == item.record_id), None)
                expected = fact.content if self.app.config.contract_version in {21, 22} and fact is not None else observation.fact_content()
                if expected != item.content:
                    raise D3RecoveryRequired("d8_read_source_version_changed")
                if fact is None or item.source_version != conclusion_digest(
                        {"fact": asdict(fact), "source": observation.source_version_sha256}):
                    raise D3RecoveryRequired("d8_read_source_version_changed")

    def _page(self, snapshot, request_id):
        items = tuple(D8HistoryItem.from_dict(i) for i in snapshot["items"])
        capacity = D8Capacity(**snapshot["capacity"])
        response = {"request_id": request_id, "snapshot_id": snapshot["snapshot_id"],
                    "capacity_version": snapshot["capacity_version"], "checkpoint_sequence": snapshot["checkpoint_sequence"],
                    "historical_snapshot": True, "items": [], "has_more": True,
                    "remaining_count": len(items), "next_cursor": "0" * 36}
        positions = snapshot["positions"]
        if snapshot["mode"] == "index":
            start = positions[0]
            end = start
            for item in items[start:start + 8]:
                visible = item.visible(include_content=False)
                response["items"].append(visible)
                if len(handoff_json(response)) > capacity.read_return:
                    response["items"].pop()
                    break
                end += 1
            if end == start and start < len(items):
                self._pause("read_return_identity", [items[start].ref], capacity.read_return,
                            len(handoff_json({**response, "items": [items[start].visible(include_content=False)]})))
            remaining = len(items) - end
            next_positions = [end]
        else:
            for item, position in zip(items, positions):
                visible = item.visible(include_content=False)
                visible.update(content="", start=position, end=position, total=len(item.content))
                response["items"].append(visible)
            if len(handoff_json(response)) > capacity.read_return:
                self._pause("read_return_identity", [i.ref for i in items], capacity.read_return, len(handoff_json(response)))
            next_positions = list(positions)
            for index, (item, position) in enumerate(zip(items, positions)):
                low, high = position, len(item.content)
                while low < high:
                    mid = (low + high + 1) // 2
                    response["items"][index].update(content=item.content[position:mid], end=mid)
                    if len(handoff_json(response)) <= capacity.read_return:
                        low = mid
                    else:
                        high = mid - 1
                response["items"][index].update(content=item.content[position:low], end=low)
                next_positions[index] = low
            remaining = sum(p < len(i.content) for i, p in zip(items, next_positions))
            if remaining and next_positions == positions:
                self._pause("read_return_body", [i.ref for i in items], capacity.read_return, None)
        response.update(has_more=bool(remaining), remaining_count=remaining)
        cursor = None
        if remaining:
            cursor = self._id(f"cursor/{snapshot['snapshot_id']}/{next_positions}")
        response["next_cursor"] = cursor
        import json
        return json.loads(handoff_json(response)), next_positions

    def read_history(self, *, query=None, refs=None, cursor=None, request_id=None, actor_intent_id=None):
        self.app._check_dispatch(allow_conclusion_repair=True)
        if actor_intent_id is not None:
            if self.app.revisions is None:
                raise ValueError("d8_explicit_read_actor_requires_revision_contract")
            self.app.revisions._actor(actor_intent_id)
        if sum(x is not None for x in (query, refs, cursor)) != 1:
            raise ValueError("d8_read_requires_one_mode")
        request_id = request_id or str(uuid4())
        _uuid(request_id, "request_id")
        if self.store.load_d8_read(request_id) is not None:
            start, _ = self.store.load_d8_read(request_id)
            frozen = self.evidence.read("read-snapshot", start["snapshot_id"])
            if (frozen is None or frozen["read_input"] != {"query": query, "refs": refs, "cursor": cursor}
                    or actor_intent_id is not None and frozen["actor_intent_id"] != actor_intent_id):
                raise D3RecoveryRequired("d8_read_parameters_changed")
            if self.evidence.read("read-result", request_id) is None:
                raise D3RecoveryRequired("d8_read_requires_explicit_original_id_recovery")
            return self.resume_read(request_id)
        version = self.capacity()
        if cursor is not None:
            _uuid(cursor, "cursor")
            snapshot = self.evidence.read("read-snapshot", cursor)
            if snapshot is None or snapshot["task_run_id"] != self.state.task_run.task_run_id:
                raise D3RecoveryRequired("d8_cursor_missing_or_foreign")
            snapshot = {**snapshot, "capacity_version": version.version, "capacity": asdict(version.capacity)}
        else:
            all_items = self.history()
            if query is not None:
                allowed = {"turn_id", "intent_id", "artifact_version_id", "record_type"}
                if not isinstance(query, dict) or set(query) - allowed:
                    raise ValueError("d8_query_fields_invalid")
                for key, value in query.items():
                    if key != "record_type":
                        _uuid(value, key)
                    elif value not in {"verified_fact", "reviewed_inference", "progress", "intent", "turn"}:
                        raise ValueError("d8_query_type_invalid")
                items = [i for i in all_items if all(getattr(i, k) == v for k, v in query.items())]
                mode, positions = "index", [0]
            else:
                requested = checked_refs(refs, {i.ref for i in all_items})
                if not 1 <= len(requested) <= 2:
                    raise ValueError("d8_detail_reference_count_invalid")
                items = [next(i for i in all_items if i.ref == ref) for ref in requested]
                mode, positions = "details", [0] * len(items)
            snapshot = {"snapshot_id": self._id("read-snapshot/" + request_id),
                        "task_run_id": self.state.task_run.task_run_id, "mode": mode, "query": query,
                        "items": [asdict(i) for i in items], "positions": positions,
                        "capacity_version": version.version, "capacity": asdict(version.capacity),
                        "checkpoint_sequence": self.state.latest_checkpoint.sequence,
                        "actor_intent_id": actor_intent_id or next((i.intent_id for i in self.state.intents if i.status is IntentStatus.RUNNING), None)}
        # Frozen selection is prepared before admission; actual response materialization is after the durable start.
        snapshot_ref = self._id("read-page/" + request_id)
        snapshot = {**snapshot, "read_input": {"query": query, "refs": refs, "cursor": cursor}}
        self.evidence.save("read-snapshot", snapshot_ref, snapshot)
        start = D8ReadStart(request_id, self.state.task_run.task_run_id, snapshot_ref,
                            conclusion_digest(snapshot), self.state.latest_checkpoint.sequence,
                            version.version, self.app.clock())
        self.store.begin_d8_read(start)
        return self.resume_read(request_id)

    def resume_read(self, request_id):
        known = self.store.load_d8_read(request_id)
        if known is None or known[0]["task_run_id"] != self.state.task_run.task_run_id:
            raise D3RecoveryRequired("d8_read_request_missing_or_foreign")
        start, completion = known
        snapshot = self.evidence.read("read-snapshot", start["snapshot_id"])
        if snapshot is None or conclusion_digest(snapshot) != start["snapshot_sha256"]:
            raise D3RecoveryRequired("d8_read_snapshot_unavailable")
        if snapshot["task_run_id"] != start["task_run_id"] or snapshot["capacity_version"] != start["capacity_version"]:
            raise D3RecoveryRequired("d8_read_snapshot_owner_or_capacity_changed")
        saved = self.evidence.read("read-result", request_id)
        if saved is not None:
            if completion is not None and completion != {"sha256": conclusion_digest(saved)}:
                raise D3RecoveryRequired("d8_read_result_changed")
            self.store.complete_d8_read(request_id, {"sha256": conclusion_digest(saved)})
            return saved
        self._validate_frozen_read_sources(tuple(D8HistoryItem.from_dict(i) for i in snapshot["items"]))
        try:
            response, positions = self._page(snapshot, request_id)
        except D3RecoveryRequired as exc:
            if not str(exc).startswith("d8_context_paused:") or self.pause_report is None:
                raise
            # A known capacity failure finishes this read; it is not an unknown local outcome.
            response = {"request_id": request_id, "snapshot_id": snapshot["snapshot_id"],
                        "capacity_version": snapshot["capacity_version"],
                        "checkpoint_sequence": snapshot["checkpoint_sequence"],
                        "historical_snapshot": True, "items": [], "has_more": False,
                        "remaining_count": len(snapshot["items"]), "next_cursor": None,
                        "error": "capacity_paused", "pause_report": self.pause_report}
            positions = snapshot["positions"]
        if response["next_cursor"]:
            continuation = {**snapshot, "positions": positions}
            self.evidence.save("read-snapshot", response["next_cursor"], continuation)
        self.evidence.save("read-result", request_id, response)
        self.store.complete_d8_read(request_id, {"sha256": conclusion_digest(response)})
        return response

    def retrieved_for(self, intent_id):
        results = []
        current = {i.ref: i for i in self.history()}
        for request_id, start, completion in self.store.list_d8_reads(self.state.task_run.task_run_id):
            if completion is None:
                continue
            snapshot = self.evidence.read("read-snapshot", start["snapshot_id"])
            if snapshot is None or conclusion_digest(snapshot) != start["snapshot_sha256"]:
                raise D3RecoveryRequired("d8_retained_read_snapshot_unavailable")
            if snapshot["actor_intent_id"] != intent_id:
                continue
            response = self.evidence.read("read-result", request_id)
            if response is None or completion != {"sha256": conclusion_digest(response)}:
                raise D3RecoveryRequired("d8_retained_read_result_unavailable")
            views = []
            for item in response["items"]:
                now = current.get(item["ref"])
                views.append({"historical_item": item,
                              "current_source_status": now.source_status if now else "unavailable_or_changed",
                              "current_scope": now.scope if now else "historical",
                              "current_conflict": now.conflict if now else None,
                              "current_reference_matches": now is not None})
                if self.app.revisions is not None:
                    views[-1]["current_effectiveness"] = getattr(now, "current_status", "unavailable")
                    views[-1]["current_revision"] = getattr(now, "revision", None)
            results.append({"read": response, "current_applicability": views,
                            "historical_read_is_not_current_verification": True})
        return results

    def resume_pending_read_once(self):
        pending = [(identity, start) for identity, start, completion in
                   self.store.list_d8_reads(self.state.task_run.task_run_id) if completion is None]
        if len(pending) > 1:
            raise D3RecoveryRequired("d8_multiple_unfinished_local_reads")
        if pending:
            self.resume_read(pending[0][0])
            self._apply_read_parent(pending[0][0])

    def _apply_read_parent(self, request_id):
        for call in self.store.list_d3_operations(self.state.task_run.task_run_id):
            if self._id("model-read/" + call.start.operation_id) == request_id:
                self.store.mark_d3_operation_applied(call.start.operation_id, request_id)

    def reconcile_reads(self):
        for identity, _, completion in self.store.list_d8_reads(self.state.task_run.task_run_id):
            if completion is None:
                if self.evidence.read("read-result", identity) is None:
                    raise D3RecoveryRequired("d8_read_requires_explicit_original_id_recovery")
                self.resume_read(identity)
                self._apply_read_parent(identity)

    def read_count(self, intent_id):
        return len(self.retrieved_for(intent_id))

    def wrap_execution_request(self, request):
        from dataclasses import fields
        from agent_core.dual_llm_modeling import D8ExecutionModelRequest
        intent_id = getattr(request.context, "intent_id", None)
        if intent_id is None:
            intent_id = request.context.plan_binding.intent_id
        turn_id = next(i.turn_id for i in self.state.intents if i.intent_id == intent_id)
        saved = self.evidence.read("execution-context", request.model_request_id)
        if saved is None:
            selected, _, packet = self.select("execution", turn_id, intent_id)
            packet["process_facts"] = list(request.process_facts)
            packet["process_facts_identity"] = "model_summary_not_verified"
            packet["retrieved"] = self.retrieved_for(intent_id)
            if self.app.config.contract_version == 22 and request.stage == "inner_plan":
                plans = [p for p in self.state.plans if p.intent_id == intent_id]
                result = self.store.load_d3_plan_result(plans[-1].plan_id) if plans else None
                if result is not None and result.outcome == "no_new_information":
                    source = self.app.conclusions.observation(result.proof_id, historical=True)
                    packet["completion_facts"] = [{k: i[k] for k in ("fact_id", "content", "ref")}
                        for i in self.app.conclusions.reusable_facts(source)]
            if self.app.config.intent_execution_policy and request.stage == "inner_plan":
                from agent_core.intent_execution import execution_context
                packet["intent_execution"] = execution_context(self.app, intent_id)
            if self.app.evidence_conclusions is not None and request.stage=="inner_plan":
                packet["evidence_sources"]=self.app.evidence_conclusions.catalog(intent_id)
                if packet["evidence_sources"] and self.app.sessions is not None:
                    marker = self.app.sessions.consolidation_context(intent_id)
                    if marker is not None and not packet.get("intent_execution", {}).get("stop_requested"):
                        packet["knowledge_checkpoint"] = marker
            if self.app.mcp is not None and request.stage == "inner_plan":
                from agent_adapters.mcp_plan_executor import compact_tools
                packet["mcp_tools"] = compact_tools(self.app.mcp.tools)
            if self.app.web is not None:
                packet['d20_http_policy'] = self.app.web.policy()
            if self.app.config.contract_version in {17, 18, 19, 20, 21, 22} and request.stage == 'inner_plan':
                from agent_core.execution_feasibility import KEY, freeze
                packet[KEY] = freeze(self.app, turn_id, intent_id)
            if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
                from agent_core.governance_policy import freeze_policy
                packet['s5_policy'] = freeze_policy(self.app, packet)
            version = self.capacity()
            for retrieval in packet["retrieved"]:
                old_result = retrieval["read"]
                if old_result.get("error") == "capacity_paused":
                    old_start, _ = self.store.load_d8_read(old_result["request_id"])
                    old_snapshot = self.evidence.read("read-snapshot", old_start["snapshot_id"])
                    needed = old_result["pause_report"]["known_needed"]
                    if (version.capacity.read_return <= old_snapshot["capacity"]["read_return"]
                            or needed is not None and version.capacity.read_return < needed):
                        raise D3RecoveryRequired("d8_read_capacity_paused_requires_operator_update")
            while len(handoff_json({k: v for k, v in packet.items() if k != "retrieved"})) > version.capacity.initial_history and packet["process_facts"]:
                packet["process_facts"].pop()
            initial_length = len(handoff_json({k: v for k, v in packet.items() if k != "retrieved"}))
            if initial_length > version.capacity.initial_history:
                self._pause("initial_history", packet["required_refs"],
                            version.capacity.initial_history, initial_length)
            visible_length = len(handoff_json(packet))
            if visible_length > version.capacity.visible_history:
                self._pause("visible_history", [i.ref for i in selected],
                            version.capacity.visible_history, visible_length)
            saved = {"task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
                     "capacity_version": version.version, "history_context": handoff_json(packet)}
        elif (saved["task_run_id"], saved["model_request_id"]) != (request.task_run_id, request.model_request_id):
            raise D3RecoveryRequired("d8_execution_context_owner_changed")
        values = {f.name: getattr(request, f.name) for f in fields(request)}
        values["process_facts"] = ()
        result = D8ExecutionModelRequest(**values,
                                         history_context=saved["history_context"],
                                         history_capacity_version=saved["capacity_version"])
        if not self._fits_complete_request(result, self.app.config.handoff_config.inner_capacity):
            self._pause("complete_model_request", (), self.app.config.handoff_config.inner_capacity.total_units, None)
        self.evidence.save("execution-context", request.model_request_id, saved)
        return result

    def _fits_complete_request(self, request, policy):
        # New v22 uses the same frozen presentation for preliminary checks and
        # actual dispatch. Otherwise duplicate input could fail before prepare().
        if self.app.config.contract_version == 22 and self.app.output_boundary is not None:
            request = self.app.output_boundary._contract(request)[0]
        return self.capacity_port.fits(request, policy)

    def freeze_execution_request(self, request):
        if self.app.output_boundary is not None:
            self.app.output_boundary.prepare(request)
        if not self._fits_complete_request(request, self.app.config.handoff_config.inner_capacity):
            self._pause("complete_model_request", (), self.app.config.handoff_config.inner_capacity.total_units, None)
        data = {"task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
                "capacity_version": request.history_capacity_version, "history_context": request.history_context}
        self.evidence.save("execution-context", request.model_request_id, data)

    def check_role_request_capacity(self, request):
        if self.app.output_boundary is not None:
            self.app.output_boundary.prepare(request)
        role_ref = getattr(request, "role_config_ref", None)
        policy = (self.app.config.handoff_config.inner_capacity
                  if role_ref == self.app.config.inner_role.role_config_ref
                  else self.app.config.handoff_config.outer_capacity)
        if not self._fits_complete_request(request, policy):
            self._pause("complete_model_request", (), policy.total_units, None)

    def validate_read_action(self, request, action):
        import json
        packet = json.loads(request.history_context)
        if (packet.get("intent_execution", {}).get("stop_requested") or {}).get("history_reads_allowed") is False:
            from agent_core.dual_llm_modeling import D3ProposalRejected
            raise D3ProposalRejected("d22_closure_history_not_allowed")
        visible = {i["ref"] for i in packet["items"]}
        visible.update(json.loads(getattr(request, "session_context", "{}") or "{}").get("compaction", {}).get("omitted_decision_refs", []))
        for old in packet["retrieved"]:
            visible.update(i["ref"] for i in old["read"]["items"])
        if action.kind == "history_details":
            checked_refs(action.value, visible)
        elif action.kind == "history_next_page":
            _uuid(action.value, "cursor")
            allowed = {old["read"]["next_cursor"] for old in packet["retrieved"]}
            if action.value not in allowed:
                raise ValueError("d8_cursor_not_in_visible_results")
        else:
            for key, value in action.value.items():
                if key != "record_type":
                    _uuid(value, key)
                elif value not in {"verified_fact", "reviewed_inference", "progress", "intent", "turn"}:
                    raise ValueError("d8_history_query_record_type_invalid")

    def apply_read_action(self, model_request_id, action):
        request_id = self._id("model-read/" + model_request_id)
        mode = {"history_query": "query", "history_details": "refs", "history_next_page": "cursor"}[action.kind]
        response = self.read_history(**{mode: action.value}, request_id=request_id)
        self.store.mark_d3_operation_applied(model_request_id, request_id)
        if response.get("error") == "capacity_paused":
            raise D3RecoveryRequired("d8_read_capacity_paused_requires_operator_update")
        return response
