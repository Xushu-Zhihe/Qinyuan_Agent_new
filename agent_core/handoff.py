"""D8-2 safe history selection, joint summaries and frozen local reads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from uuid import uuid4

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
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4BudgetExhausted, D4OperationCompletion, D4OperationStart, _uuid
from agent_core.turn_modeling import stable_id, digest


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

    def _pause(self, kind, required, current, needed, omitted=()):
        self.pause_report = {"kind": kind, "required_refs": list(required),
                             "current_capacity": current, "known_needed": needed,
                             "unknown_needed": needed is None, "unit": "unicode_codepoints",
                             "omitted_refs": list(omitted),
                             "choices": ["increase_capacity", "narrow_authorized_branch", "end_task"]}
        identity = self._id("pause/" + conclusion_digest(self.pause_report))
        self.evidence.save("pause", identity, self.pause_report)
        raise D3RecoveryRequired("d8_context_paused:" + kind)

    def history(self):
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
            add(execution.execution_id, "progress", intent.turn_id, intent.intent_id,
                binding.artifact_version_id if binding else None,
                handoff_json({"kind": "execution", "status": execution.status.value,
                              "step_index": execution.step_index}), (execution.execution_id,), asdict(execution))
        for fact in state.facts:
            execution = next(e for e in state.executions if e.execution_id == fact.source_ref)
            intent = intents[plans[execution.plan_id].intent_id]
            artifact, status, version = None, "available", asdict(fact)
            try:
                if not fact.verification_ref or not fact.verification_ref.startswith("d3:"):
                    raise D3RecoveryRequired("d8_history_rule_not_registered")
                observed = self.app.conclusions.observation(fact.verification_ref[3:], historical=True)
                if observed.fact_content() != fact.content:
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
            add("review/" + record.record_id, "progress", record.turn_id, record.intent_id, None,
                handoff_json({"kind": "conclusion_review", "outcome": record.outcome,
                              "fact_id": record.fact_id}), (record.review_request_id,), asdict(record))
        for record in self.store.list_d8_tree_records(task_id):
            if record.turn_id not in turns:
                continue
            owner_intent = record.intent_id if record.intent_id in intents and intents[record.intent_id].turn_id == record.turn_id else None
            add("tree/" + record.record_id, "progress", record.turn_id, owner_intent, None,
                handoff_json({"kind": record.kind, "support_fact_id": record.support_fact_id,
                              "local_goal_decision_is_not_a_fact": True}),
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
        return tuple(sorted(items, key=lambda i: (i.committed_sequence, i.record_type, i.record_id)))

    def _latest_summary(self):
        rows = self.store.list_d8_handoffs(self.state.task_run.task_run_id)
        if not rows:
            return None, None
        record = rows[-1]
        data = self.evidence.read("handoff", record.evidence_ref)
        if data is None or conclusion_digest(data) != record.evidence_sha256:
            raise D3RecoveryRequired("d8_summary_evidence_unavailable")
        return record, data

    def _dependencies(self, intent_id):
        for row in self.store.list_d8_tree_records(self.state.task_run.task_run_id):
            for owner, refs in row.dependencies:
                if owner == intent_id:
                    return refs
        return ()

    def select(self, phase, turn_id=None, intent_id=None, support_fact_id=None):
        items, required = self.history(), set()
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
                required.add(by_id[supports[0]].ref)
            current = turns.get(current.parent_turn_id) if current.parent_turn_id else None
        if intent_id:
            required.add(by_id[intent_id].ref)
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
        for ref in required:
            item = by_ref.get(ref)
            if item is None or item.source_status != "available" or item.conflict:
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

        chosen = [i for i in items if i.ref in required]
        optional = []
        for kind, target in (("verified_fact", 8), ("reviewed_inference", 4), ("progress", 6)):
            room = max(0, target - sum(i.record_type == kind for i in chosen))
            candidates = [i for i in items if i.record_type == kind and i.ref not in required
                          and i.source_status == "available" and not i.conflict and group(i) < 5]
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
            return {"phase": phase, "summary_identity": "model_organized_not_verified",
                    "global_body": previous["global_body"] if previous and summary_available else "",
                    "global_source_status": "available" if summary_available else "unavailable",
                    "global_scope": "historical_source_snapshot",
                    "items": [i.visible() for i in chosen], "required_refs": sorted(required),
                    "coverage": {"local_index_count": len(items), "included_count": len(chosen),
                                 "omitted_count": len(omitted), "complete_semantic_coverage": False},
                    "query_entry": {"task_run_id": self.state.task_run.task_run_id,
                                    "filters": ["turn_id", "intent_id", "artifact_version_id", "record_type"]}}
        limit = min(version.capacity.initial_history, version.capacity.visible_history)
        blocking_kind = "initial_history" if version.capacity.initial_history <= version.capacity.visible_history else "visible_history"
        while len(handoff_json(packet())) > limit and optional:
            chosen.remove(optional.pop())
        result = packet()
        if len(handoff_json(result)) > limit:
            self._pause(blocking_kind, required, limit, len(handoff_json(result)),
                        [i.ref for i in items if i not in chosen])
        return tuple(chosen), tuple(i for i in items if i not in chosen), result

    def _parse(self, request, raw):
        refs = {i.ref for i in request.history}
        if request.stage == "handoff_summary":
            return D8SummaryCandidate.parse(raw, refs, request.capacity)
        if request.stage == "intent_evaluation":
            value = _fields(raw, {"decision", "reason", "evidence_refs"})
            if (value["decision"] not in {"succeeded", "failed", "inconclusive"}
                    or not isinstance(value["reason"], str) or not value["reason"].strip()
                    or len(value["reason"]) > 512):
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
        if not isinstance(value["intents"], list) or not 1 <= len(value["intents"]) <= 2:
            raise ValueError("d8_planning_candidates_required_not_exhausted")
        kept, dependencies, rejected = [], [], []
        for index, candidate in enumerate(value["intents"]):
            item = _fields(candidate, {"description", "intent_type", "dependency_refs"})
            try:
                deps = checked_refs(item["dependency_refs"], refs)
                if any(i.source_status != "available" or i.conflict for i in request.history if i.ref in deps):
                    raise ValueError("d8_dependency_unavailable")
            except ValueError:
                rejected.append(index)
                continue
            kept.append({"description": item["description"], "intent_type": item["intent_type"]})
            dependencies.append(deps)
        if not kept:
            raise D8DependencyCandidatesRejected(rejected)
        parsed = parse_d3_turn_proposal({"turn_target": value["turn_target"],
                                        "completion_rule": value["completion_rule"], "intents": kept})
        if parsed.attachment_index not in self.app.config.authorization.attachment_indices:
            raise ValueError("d8_planning_attachment_unauthorized")
        return D8PlanningResult(parsed, tuple(dependencies), tuple(rejected))

    def _known_call(self, request):
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
            raise D3RecoveryRequired("d8_summary_or_planning_outcome_unknown")
        if (result.get("model_request_id") != request.model_request_id
                or result.get("request_sha256") != request.context_sha256
                or result.get("outcome") not in {"valid", "invalid", "model_error", "sensitive_rejected", "stale"}):
            raise D3RecoveryRequired("d8_saved_result_binding_changed")
        if result["outcome"] == "valid":
            self._parse(request, result["raw"])
            if self.app.conclusions._sensitive(result["raw"]):
                raise D3RecoveryRequired("d8_saved_summary_not_safe")
        elif result["raw"] is not None:
            raise D3RecoveryRequired("d8_invalid_result_contains_text")
        completion = D4OperationCompletion(request.model_request_id, result["finished_at"],
                                          "completed" if result["outcome"] == "valid" else "rejected",
                                          request.model_request_id, conclusion_digest(result))
        if call.completion is None:
            self.store.complete_d4_operation(completion)
        elif call.completion != completion:
            raise D3RecoveryRequired("d8_saved_completion_changed")
        return result

    def reconcile(self):
        for operation in self.store.list_d4_operations(self.state.task_run.task_run_id):
            request = self.evidence.read("request", operation.start.operation_id)
            if request is not None and self.evidence.read("result", operation.start.operation_id) is not None:
                self._known_call(D8HandoffRequest.from_dict(request))

    def _request(self, stage, turn_id, intent_id=None, support_fact_id=None):
        selected, omitted, packet = self.select(stage, turn_id, intent_id, support_fact_id)
        if stage == "tree_planning":
            packet["dependency_review"] = (
                "In this planning call, check dependency relevance and obvious omissions. "
                "Use only exact visible versions. This is model self-checking, not independent verification; "
                "undeclared implicit dependencies may still be missed.")
        version = self.capacity()
        latest, _ = self._latest_summary()
        global_hint = latest.record_id if latest is not None and latest.turn_id != turn_id else None
        name = f"{stage}/{turn_id}/{intent_id}/{conclusion_digest([asdict(i) for i in selected])}/{version.version}/{global_hint}"
        request_id = self._id(name)
        saved = self.evidence.read("request", request_id)
        if saved is not None:
            return D8HandoffRequest.from_dict(saved), omitted
        return D8HandoffRequest(request_id, self.state.task_run.task_run_id, turn_id, intent_id,
                                stage, self.app.config.outer_role.role_config_ref,
                                self.state.latest_checkpoint.sequence, version.version, version.capacity,
                                self.app.redactor.redact(self.state.task_run.request.goal), selected,
                                handoff_json(packet), support_fact_id), omitted

    async def call(self, request, token):
        current = request
        for attempt in range(2):
            if self.model.adapter.role_config != self.app.config.outer_role:
                raise D3RecoveryRequired("d8_handoff_role_changed")
            self.evidence.save("request", current.model_request_id, asdict(current))
            result = self._known_call(current)
            if result is None:
                self.app._check_dispatch()
                if self.state.latest_checkpoint.sequence != current.base_checkpoint_sequence:
                    raise D3RecoveryRequired("d8_handoff_request_stale")
                policy = self.app.config.handoff_config.outer_capacity
                if not self.capacity_port.fits(current, policy):
                    self._pause("complete_model_request", [i.ref for i in current.history],
                                policy.total_units, None)
                category = {"handoff_summary": "handoff_summary", "tree_planning": "planning", "intent_evaluation": "review"}[current.stage]
                self.store.begin_d4_operation(D4OperationStart(
                    current.model_request_id, current.task_run_id, category, self.app.clock(),
                    current.base_checkpoint_sequence, current.context_sha256,
                    current.turn_id or current.task_run_id, self.app.config.d4_budget.budget_policy_ref,
                    "outer_planning", current.role_config_ref))
                raw = await self.model._invoke(current, min(180., self.state.task_run.deadline_at - self.app.clock()), token)
                outcome, reason, rejected_indices = "valid", None, []
                if isinstance(raw, ModelError):
                    if raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                        raise D3RecoveryRequired("d8_model_outcome_unknown")
                    outcome, raw = "model_error", None
                elif self.app.conclusions._sensitive(raw):
                    outcome, raw = "sensitive_rejected", None
                else:
                    try:
                        parsed = self._parse(current, raw)
                        if isinstance(parsed, D8PlanningResult):
                            rejected_indices = list(parsed.rejected_indices)
                    except (ValueError, TypeError, KeyError) as exc:
                        outcome, reason, raw = "invalid", "d8_output_contract_rejected", None
                        if isinstance(exc, D8DependencyCandidatesRejected):
                            reason = "d8_all_dependency_candidates_rejected"
                            rejected_indices = list(exc.indices)
                        # Do not persist invalid raw responses or exceptions containing text.
                if (token.is_cancelled or self.app.clock() >= self.state.task_run.deadline_at
                        or self.state.latest_checkpoint.sequence != current.base_checkpoint_sequence):
                    outcome, raw = "stale", None
                result = {"model_request_id": current.model_request_id,
                          "request_sha256": current.context_sha256, "outcome": outcome,
                          "raw": raw, "reason": reason, "rejected_candidate_indices": rejected_indices,
                          "finished_at": self.app.clock()}
                self.evidence.save("result", current.model_request_id, result)
                result = self._known_call(current)
            if result["outcome"] == "valid":
                return current, self._parse(current, result["raw"])
            if result["outcome"] != "invalid" or attempt == 1:
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
        self.app._check_dispatch()
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
        self.evidence.save("handoff", identity, data)
        record = D8HandoffVersion(identity, request.task_run_id, turn_id,
                                  sum(v.turn_id == turn_id for v in versions) + 1, len(versions) + 1,
                                  request.base_checkpoint_sequence, request.capacity_version, identity,
                                  conclusion_digest(data), source_sha, request_id, origin,
                                  tuple((i.ref, i.record_id, i.record_type, i.turn_id, i.intent_id,
                                         i.artifact_version_id) for i in all_items))
        self._commit("append_d8_handoff", record)
        return record

    async def plan_expansion(self, parent_turn_id, trigger_id, receipts, token):
        support = None
        if parent_turn_id:
            await self.summarize(parent_turn_id, token)
            successes = [r for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                         if r.kind == "succeeded" and r.intent_id == trigger_id]
            if len(successes) != 1:
                raise D3RecoveryRequired("d8_child_requires_committed_success_support")
            support = successes[0].support_fact_id
        request, _ = self._request("tree_planning", parent_turn_id, trigger_id, support)
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
        self._commit("select_d8_tree_intent", record,
                     (UpsertTurn(turn.turn_id, turn.task_run_id, turn.sequence, turn.turn_target,
                                 turn.intent_ids, TurnStatus.RUNNING),
                      UpsertIntent(intent.intent_id, intent.turn_id, intent.description,
                                   intent.priority, intent.intent_type, IntentStatus.RUNNING)))

    async def evaluate_intent(self, intent_id, plan_id, fact_id, inner_request_id, token):
        intent = next(i for i in self.state.intents if i.intent_id == intent_id)
        request, _ = self._request("intent_evaluation", intent.turn_id, intent_id, fact_id)
        final, decision = await self.call(request, token)
        if decision is None:
            raise D3RecoveryRequired("d8_local_goal_evaluation_failed")
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
        if len(records) != 1 or self.app.config.contract_version != 3:
            raise D3RecoveryRequired("d8_handoff_mutation_not_enabled")
        record = records[0]
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
                if not isinstance(parsed, D8PlanningResult) or parsed.proposal is None:
                    raise D3RecoveryRequired("d8_registration_has_no_candidates")
                change = next(c for c in mutation.state_changes if isinstance(c, UpsertTurn))
                children = [c for c in mutation.state_changes if isinstance(c, UpsertIntent)]
                if (change.parent_turn_id, change.triggering_intent_id) != (request.turn_id, request.intent_id):
                    raise D3RecoveryRequired("d8_child_parent_binding_changed")
                if (tuple((c.description, c.intent_type) for c in children)
                        != tuple((c.description, c.intent_type) for c in parsed.proposal.intents)
                        or tuple(refs for _, refs in record.dependencies) != parsed.dependencies
                        or change.turn_target != state.task_run.request.goal):
                    raise D3RecoveryRequired("d8_candidate_or_dependencies_changed")
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
                if (parsed["decision"] != record.kind
                        or (request.turn_id, request.intent_id, request.support_fact_id)
                        != (record.turn_id, record.intent_id, record.support_fact_id)):
                    raise D3RecoveryRequired("d8_local_goal_decision_changed")
                inner = self.app.validator._known_model(record.task_run_id, record.inner_request_id, "inner_complete")
                if inner.action.fact_ids != (record.support_fact_id,):
                    raise D3RecoveryRequired("d8_inner_completion_support_changed")
                plans = [p for p in state.plans if p.intent_id == record.intent_id]
                binding = self.store.load_d3_plan_binding(plans[-1].plan_id)
                result = self.store.load_d3_plan_result(binding.plan_id)
                self.app.validator._support(state, binding, result.proof_id, record.support_fact_id)

    def _validate_frozen_read_sources(self, items):
        for item in items:
            if item.record_type == "verified_fact" and item.source_status == "available":
                proof = next((r[3:] for r in item.source_refs if r.startswith("d3:")), None)
                if proof is None:
                    raise D3RecoveryRequired("d8_read_source_version_missing")
                observation = self.app.conclusions.observation(proof, historical=True)
                if observation.fact_content() != item.content:
                    raise D3RecoveryRequired("d8_read_source_version_changed")
                fact = next((f for f in self.state.facts if f.fact_id == item.record_id), None)
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

    def read_history(self, *, query=None, refs=None, cursor=None, request_id=None):
        self.app._check_dispatch()
        if sum(x is not None for x in (query, refs, cursor)) != 1:
            raise ValueError("d8_read_requires_one_mode")
        request_id = request_id or str(uuid4())
        _uuid(request_id, "request_id")
        if self.store.load_d8_read(request_id) is not None:
            start, _ = self.store.load_d8_read(request_id)
            frozen = self.evidence.read("read-snapshot", start["snapshot_id"])
            if frozen is None or frozen["read_input"] != {"query": query, "refs": refs, "cursor": cursor}:
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
                        "actor_intent_id": next((i.intent_id for i in self.state.intents if i.status is IntentStatus.RUNNING), None)}
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
        if not self.capacity_port.fits(result, self.app.config.handoff_config.inner_capacity):
            self._pause("complete_model_request", (), self.app.config.handoff_config.inner_capacity.total_units, None)
        self.evidence.save("execution-context", request.model_request_id, saved)
        return result

    def freeze_execution_request(self, request):
        if not self.capacity_port.fits(request, self.app.config.handoff_config.inner_capacity):
            self._pause("complete_model_request", (), self.app.config.handoff_config.inner_capacity.total_units, None)
        data = {"task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
                "capacity_version": request.history_capacity_version, "history_context": request.history_context}
        self.evidence.save("execution-context", request.model_request_id, data)

    def check_role_request_capacity(self, request):
        role_ref = getattr(request, "role_config_ref", None)
        policy = (self.app.config.handoff_config.inner_capacity
                  if role_ref == self.app.config.inner_role.role_config_ref
                  else self.app.config.handoff_config.outer_capacity)
        if not self.capacity_port.fits(request, policy):
            self._pause("complete_model_request", (), policy.total_units, None)

    def validate_read_action(self, request, action):
        import json
        packet = json.loads(request.history_context)
        visible = {i["ref"] for i in packet["items"]}
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
