"""D8-3 review, immutable changes, current projections and dependency gates."""

from dataclasses import asdict, replace
import json

from agent_core.conclusion_contracts import conclusion_digest
from agent_core.conclusion_revision_contracts import (
    D8ChangeDecision, D8ChangeRequest, D8ConclusionChange,
    D8PriorChange, D8RevisionHistoryItem, D8RevisionRecord,
)
from agent_core.domain import FactStatus, IntentStatus
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.handoff_contracts import D8HistoryItem, handoff_json
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4OperationCompletion, D4OperationStart, _uuid
from agent_core.turn_modeling import stable_id


class ConclusionRevisions:
    def __init__(self, app, evidence):
        self.app, self.store, self.evidence = app, app.store, evidence

    @property
    def state(self):
        return self.app.state

    def _id(self, name):
        return stable_id(self.state.task_run.task_run_id, "d8-3/" + name)

    def records(self):
        return self.store.list_d8_revisions(self.state.task_run.task_run_id)

    def prior_changes(self, roots=None):
        result = []
        roots = None if roots is None else set(roots)
        for record in self.records():
            _, action, decision = self._payload(record)
            owners = tuple(t.root_id for t in action.targets)
            if roots is None or set(owners) & roots:
                result.append(D8PriorChange(record.record_id, owners, action.operation, decision.decision,
                                            action.reason, decision.reason))
        return tuple(result)

    def _payload(self, record):
        data = self.evidence.read("handoff", record.evidence_ref)
        if (data is None or conclusion_digest(data) != record.evidence_sha256
                or data.get("record_id") != record.record_id or data.get("request_id") != record.review_request_id):
            raise ExecutionRecoveryRequired("d8_revision_evidence_unavailable")
        action = D8ConclusionChange.from_dict(data["candidate"])
        decision = D8ChangeDecision.from_dict(data["decision"])
        if (action.operation, decision.decision) != (record.operation, record.decision):
            raise ExecutionRecoveryRequired("d8_revision_metadata_changed")
        return data, action, decision

    def _projection(self, base):
        items = list(base)
        heads = {i.record_id: {"version": 1, "ref": i.ref, "last_ref": i.ref,
                               "kind": i.record_type, "version_id": i.record_id,
                               "adoption_id": i.record_id, "validated_by": []}
                 for i in items if i.record_type in {"verified_fact", "reviewed_inference"}}
        conflicts, relations = {}, {}
        for record in self.records():
            data, action, decision = self._payload(record)
            actor = next(i for i in self.state.intents if i.intent_id == record.actor_intent_id)
            items.append(D8HistoryItem("revision/" + record.record_id, record.task_run_id, "progress",
                                       actor.turn_id, actor.intent_id, None,
                                       handoff_json({"kind": "conclusion_change", "operation": action.operation,
                                                     "decision": decision.decision, "reason": decision.reason,
                                                     "record_id": record.record_id}),
                                       (record.review_request_id,), record.evidence_sha256,
                                       data["commit_sequence"], "review_metadata_not_verified", "historical", "available"))
            if decision.decision != "accept":
                continue
            for target in action.targets:
                head = heads.get(target.root_id)
                if (head is None or head["version"] != target.version
                        or head["last_ref"] != target.ref):
                    raise ExecutionRecoveryRequired("d8_revision_history_chain_changed")
            operation, roots = action.operation, tuple(t.root_id for t in action.targets)
            if operation in {"correct", "restore"}:
                head = heads[roots[0]]
                if action.content is not None:
                    sources = data["sources"]
                    status = "available"
                    for source in sources:
                        try:
                            current = self._source_snapshot(source)
                            if current != source:
                                status = "unavailable"
                        except ExecutionRecoveryRequired:
                            status = "unavailable"
                    actor = next(i for i in self.state.intents if i.intent_id == record.actor_intent_id)
                    entry = D8HistoryItem(record.record_id, record.task_run_id, "reviewed_inference",
                                          actor.turn_id, actor.intent_id,
                                          sources[0].get("artifact_version_id") if sources else None,
                                          action.content if status == "available" else "",
                                          tuple(action.evidence_refs), record.evidence_sha256,
                                          data["commit_sequence"], "reviewed_inference_not_verified",
                                          "current" if actor.status is IntentStatus.RUNNING else "historical", status)
                    items.append(entry)
                    head["ref"] = head["last_ref"] = entry.ref
                else:
                    head["ref"] = head["last_ref"] = action.replacement_ref
            elif operation == "withdraw":
                heads[roots[0]]["ref"] = None
            elif operation == "report_conflict":
                conflicts[tuple(sorted(roots))] = tuple(t.ref for t in action.targets)
            elif operation == "resolve_conflict":
                pair = tuple(sorted(roots))
                if pair not in conflicts:
                    raise ExecutionRecoveryRequired("d8_revision_conflict_history_changed")
                if action.resolution != "unresolved":
                    del conflicts[pair]
                if action.resolution in {"keep_first", "withdraw_both"}:
                    heads[roots[1]]["ref"] = None
                if action.resolution in {"keep_second", "withdraw_both"}:
                    heads[roots[0]]["ref"] = None
            elif operation == "replace_support":
                key = (action.relation_kind, action.relation_owner_id, data["relation_old_ref"])
                relations[key] = action.replacement_ref
            elif operation == "link_verified":
                heads[roots[0]]["validated_by"].append(action.replacement_ref)
            if operation != "replace_support":
                for root in roots:
                    heads[root]["version"] += 1
                    heads[root]["version_id"] = record.record_id
                    if operation in {"correct", "restore"}:
                        heads[root]["adoption_id"] = record.record_id
        return items, heads, conflicts, relations

    def project(self, base):
        items, heads, conflicts, _ = self._projection(base)
        roots_for_ref = {}
        for record in self.records():
            _, action, decision = self._payload(record)
            if decision.decision == "accept" and action.content is not None:
                item = next(i for i in items if i.record_id == record.record_id)
                roots_for_ref[item.ref] = action.targets[0].root_id
        conflicted_roots = {root for pair in conflicts for root in pair}
        conflict_refs = {ref for refs in conflicts.values() for ref in refs}
        narrow_conflicts = self._registered_conflicts(items)
        conflict_refs.update(ref for pair in narrow_conflicts for ref in pair)
        canonical_roots = {i.ref: i.record_id for i in items if i.record_id in heads}
        canonical_roots.update(roots_for_ref)
        by_ref = {i.ref: i for i in items}
        def available(ref):
            item, owner = by_ref.get(ref), canonical_roots.get(ref)
            return (item is not None and item.source_status == "available" and owner in heads
                    and heads[owner]["ref"] == ref and owner not in conflicted_roots
                    and ref not in conflict_refs)
        result = []
        for item in items:
            root = item.record_id if item.record_id in heads else roots_for_ref.get(item.ref)
            if root is None:
                result.append(item)
                continue
            head = heads[root]
            status = ("withdrawn" if head["ref"] is None else
                      "superseded" if head["ref"] != item.ref else "effective")
            conflict = root in conflicted_roots or item.ref in conflict_refs
            if conflict and status == "effective":
                status = "conflicted"
            usable = head["ref"] is not None and not conflict and available(head["ref"])
            if not usable and status == "effective":
                status = "unavailable"
            result.append(self._label(item, root, head, status, conflict or item.conflict, usable))
        return tuple(result)

    @staticmethod
    def _label(item, root, head, status, conflict, usable):
        return D8RevisionHistoryItem(**{**asdict(item), "conflict": conflict}, logical_root_id=root,
                                     revision=head["version"], current_status=status,
                                     effective_ref=head["ref"] if usable else None,
                                     effective_version_id=head["adoption_id"] if usable else None,
                                     committed_version_id=head["version_id"], last_version_ref=head["last_ref"],
                                     validated_by_refs=tuple(head["validated_by"]))

    def _registered_conflicts(self, items):
        claims = []
        for item in items:
            if item.record_type != "verified_fact" or item.source_status != "available":
                continue
            fact = next((f for f in self.state.facts if f.fact_id == item.record_id), None)
            if fact is None or not (fact.verification_ref or "").startswith("d3:"):
                continue
            observed = self.app.conclusions.observation(fact.verification_ref[3:], historical=True)
            claims.append((item.ref, observed.artifact_version_id, observed.observation_scope, observed.observed_sha256))
        return tuple((a[0], b[0]) for n, a in enumerate(claims) for b in claims[n + 1:]
                     if a[1:3] == b[1:3] and a[3] != b[3])

    @staticmethod
    def reliable(item):
        return (item is not None and item.source_status == "available" and not item.conflict
                and getattr(item, "current_status", "effective") == "effective")

    def _base(self):
        return self.app.handoff.history(apply_revisions=False)

    def _history(self):
        return self.project(self._base())

    def _actor(self, actor_id):
        self.app._check_role()
        if (self.app.conclusions.inner_model.role_config != self.app.config.inner_role
                or self.app.conclusions.outer_model.role_config != self.app.config.outer_role):
            raise ExecutionRecoveryRequired("d8_revision_frozen_role_changed")
        if not self.app._redaction_configured:
            raise ExecutionRecoveryRequired("d5_redaction_configuration_unavailable")
        self.app._check_dispatch(allow_conclusion_repair=True)
        if self.store.load_d3_run_config(self.state.task_run.task_run_id) != self.app.config:
            raise ExecutionRecoveryRequired("d8_revision_frozen_configuration_changed")
        actor = next((i for i in self.state.intents if i.intent_id == actor_id), None)
        if actor is None or actor.status not in {IntentStatus.RUNNING, IntentStatus.PENDING}:
            raise ExecutionRecoveryRequired("d8_revision_actor_scope_ended")
        return actor

    def validate_action(self, action, visible):
        if self.app.conclusions._sensitive(asdict(action)):
            raise ExecutionRecoveryRequired("d8_revision_sensitive_rejected")
        current_items, heads, conflicts, _ = self._projection(self._base())
        current = {i.ref: i for i in self.project(self._base())}
        visible_refs = {i.ref for i in visible}
        for target in action.targets:
            head = heads.get(target.root_id)
            if (head is None or head["version"] != target.version or head["last_ref"] != target.ref
                    or target.ref not in visible_refs):
                raise ExecutionRecoveryRequired("d8_revision_head_or_visibility_changed")
            frozen = next((i for i in visible if i.ref == target.ref), None)
            now = current.get(target.ref)
            if frozen is None or now is None or frozen != now:
                raise ExecutionRecoveryRequired("d8_revision_frozen_target_changed")
        for ref in action.evidence_refs:
            if ref not in visible_refs or not self.reliable(current.get(ref)):
                raise ExecutionRecoveryRequired("d8_revision_evidence_unavailable")
        if action.replacement_ref is not None:
            item = current.get(action.replacement_ref)
            restoring_original = (action.operation == "restore" and action.replacement_ref == action.targets[0].ref
                                  and item is not None and item.source_status == "available" and not item.conflict)
            if action.replacement_ref not in visible_refs or not (self.reliable(item) or restoring_original):
                raise ExecutionRecoveryRequired("d8_revision_replacement_unavailable")
            kind = heads[action.targets[0].root_id]["kind"]
            if action.operation == "link_verified":
                if kind != "reviewed_inference" or item.record_type != "verified_fact":
                    raise ExecutionRecoveryRequired("d8_validation_link_identity_invalid")
            elif action.operation in {"correct", "restore"} and item.record_type != kind:
                raise ExecutionRecoveryRequired("d8_revision_identity_change_forbidden")
            if action.operation == "replace_support" and item.record_type not in {"verified_fact", "reviewed_inference"}:
                raise ExecutionRecoveryRequired("d8_dependency_requires_conclusion")
            if action.operation == "replace_support" and action.relation_kind == "parent_support" and kind != "verified_fact":
                raise ExecutionRecoveryRequired("d8_parent_support_requires_verified_fact")
            if action.replacement_ref == action.targets[0].ref and action.operation not in {"restore", "replace_support"}:
                raise ExecutionRecoveryRequired("d8_revision_replacement_is_unchanged")
        root = action.targets[0].root_id
        if action.operation == "restore":
            if heads[root]["ref"] is not None:
                raise ExecutionRecoveryRequired("d8_revision_restore_requires_withdrawal")
        elif action.operation in {"correct", "withdraw"} and heads[root]["ref"] is None:
            raise ExecutionRecoveryRequired("d8_revision_requires_adopted_version")
        if action.content is not None:
            if heads[root]["kind"] != "reviewed_inference":
                raise ExecutionRecoveryRequired("d8_revision_text_cannot_create_fact")
            if any(current[ref].record_type not in {"verified_fact", "reviewed_inference"}
                   for ref in action.evidence_refs):
                raise ExecutionRecoveryRequired("d8_revision_inference_source_kind_invalid")
        if action.operation == "report_conflict":
            if any(heads[t.root_id]["ref"] is None for t in action.targets):
                raise ExecutionRecoveryRequired("d8_conflict_requires_adopted_versions")
            if len({t.ref for t in action.targets}) != 2:
                raise ExecutionRecoveryRequired("d8_conflict_requires_distinct_versions")
        if action.operation == "resolve_conflict":
            if tuple(sorted(t.root_id for t in action.targets)) not in conflicts:
                raise ExecutionRecoveryRequired("d8_conflict_pair_not_open")
        if action.operation == "replace_support":
            self._relation(action, current_items)
        return current, heads

    def _relation(self, action, items):
        return self._relation_details(action, items)[0]

    def _relation_details(self, action, items):
        owner = next((i for i in self.state.intents if i.intent_id == action.relation_owner_id), None)
        if owner is None:
            raise ExecutionRecoveryRequired("d8_support_owner_missing")
        by_ref = {i.ref: i for i in self._history()}
        root = action.targets[0].root_id
        if action.relation_kind == "parent_support":
            success = [r for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                       if r.kind == "succeeded" and r.intent_id == owner.intent_id]
            if len(success) != 1:
                raise ExecutionRecoveryRequired("d8_parent_success_relation_missing")
            original = next((i.ref for i in items if i.record_id == success[0].support_fact_id), None)
            existing = self.relation_ref("parent_support", owner.intent_id, original)
            if existing not in by_ref or getattr(by_ref[existing], "logical_root_id", None) != root:
                raise ExecutionRecoveryRequired("d8_support_relation_version_changed")
        else:
            original = self.app.handoff._dependencies(owner.intent_id, apply_revisions=False)
            effective = tuple(self.relation_ref("intent_dependency", owner.intent_id, ref) for ref in original)
            candidates = [ref for ref in effective if ref in by_ref and getattr(by_ref[ref], "logical_root_id", None) == root]
            if len(candidates) != 1:
                raise ExecutionRecoveryRequired("d8_dependency_relation_version_changed")
            existing = candidates[0]
        return self.app.redactor.redact(owner.description), existing

    def relation_ref(self, kind, owner_id, original):
        if original is None:
            return None
        _, _, _, relations = self._projection(self._base())
        ref, seen = original, set()
        while (kind, owner_id, ref) in relations:
            if ref in seen:
                raise ExecutionRecoveryRequired("d8_support_relation_cycle")
            seen.add(ref)
            replacement = relations[(kind, owner_id, ref)]
            if replacement == ref:
                return ref
            ref = replacement
        return ref

    def check_relation_binding(self, kind, owner_id, original, current_ref):
        frozen = None
        for record in reversed(self.records()):
            _, action, decision = self._payload(record)
            if (decision.decision == "accept" and action.operation == "replace_support"
                    and (action.relation_kind, action.relation_owner_id) == (kind, owner_id)
                    and action.replacement_ref == current_ref):
                request = D8ChangeRequest.from_dict(self.evidence.read("request", record.review_request_id))
                frozen = next((i for i in request.history if i.ref == current_ref), None)
                break
        if frozen is None:
            for row in self.store.list_d8_tree_records(self.state.task_run.task_run_id):
                if ((kind == "parent_support" and row.kind == "succeeded" and row.intent_id == owner_id)
                        or (kind == "intent_dependency" and any(owner == owner_id for owner, _ in row.dependencies))):
                    raw = self.app.handoff.evidence.read("request", row.model_request_id)
                    if raw is None:
                        raise ExecutionRecoveryRequired("d8_support_original_request_missing")
                    frozen = next((D8HistoryItem.from_dict(i) for i in raw["history"] if
                                   f"{i['record_type']}:{i['record_id']}:{i['source_version']}" == original), None)
                    if frozen is not None:
                        break
        current = next((i for i in self._history() if i.ref == current_ref), None)
        if frozen is None or current is None or not self.reliable(current):
            raise ExecutionRecoveryRequired("d8_support_binding_not_effective")
        binding = lambda item: (item.record_id, item.source_version,
                                getattr(item, "logical_root_id", None), getattr(item, "revision", None),
                                getattr(item, "effective_version_id", None), getattr(item, "committed_version_id", None))
        if binding(frozen) != binding(current):
            raise ExecutionRecoveryRequired("d8_support_requires_explicit_reapproval")

    def parent_fact(self, intent_id, original_fact_id):
        items = {i.record_id: i for i in self._history()}
        old = items.get(original_fact_id)
        if old is None:
            raise ExecutionRecoveryRequired("d8_parent_support_missing")
        ref = self.relation_ref("parent_support", intent_id, old.ref)
        self.check_relation_binding("parent_support", intent_id, old.ref, ref)
        item = next((i for i in items.values() if i.ref == ref), None)
        if not self.reliable(item) or item.record_type != "verified_fact":
            raise ExecutionRecoveryRequired("d8_parent_support_not_effective")
        return item.record_id

    def check_required(self):
        if self._dirty_turns():
            raise ExecutionRecoveryRequired("d8_revision_summaries_pending")
        history = {i.ref: i for i in self._history()}
        by_id = {i.record_id: i for i in history.values()}
        turns = {t.turn_id: t for t in self.state.turns}
        for intent in self.state.intents:
            if intent.status is not IntentStatus.RUNNING:
                continue
            required = list(self.app.handoff._dependencies(intent.intent_id))
            plans = [p for p in self.state.plans if p.intent_id == intent.intent_id]
            if plans:
                result = self.store.load_d3_plan_result(plans[-1].plan_id)
                if result is not None and result.supporting_fact_id:
                    item = by_id.get(result.supporting_fact_id)
                    if item is not None:
                        required.append(item.ref)
                    elif not any(
                        f.fact_id == result.supporting_fact_id
                        and f.status is FactStatus.VERIFIED
                        for f in self.store.load_control_state_rows(
                            self.state.task_run.task_run_id).facts
                    ):
                        # A fact committed in this same step is not in the
                        # history index yet; only a fact that is neither indexed
                        # nor verified leaves the requirement unmet. The
                        # committed state is authoritative here because the
                        # in-memory snapshot predates the commit.
                        required.append("missing")
            turn = turns[intent.turn_id]
            while turn.parent_turn_id:
                trigger = turn.triggering_intent_id
                successes = [r for r in self.store.list_d8_tree_records(self.state.task_run.task_run_id)
                             if r.kind == "succeeded" and r.intent_id == trigger]
                if len(successes) != 1:
                    raise ExecutionRecoveryRequired("d8_parent_support_relation_missing")
                self.parent_fact(trigger, successes[0].support_fact_id)
                turn = turns[turn.parent_turn_id]
            if any(not self.reliable(history.get(ref)) for ref in required):
                raise ExecutionRecoveryRequired("d8_required_conclusion_not_effective")

    def validate_frozen_execution(self, request_id):
        import json
        saved = self.app.handoff.evidence.read("execution-context", request_id)
        if saved is None:
            raise ExecutionRecoveryRequired("d8_frozen_execution_context_missing")
        packet = json.loads(saved["history_context"])
        frozen = {i["ref"]: i for i in packet["items"]}
        current = {i.ref: i.visible() for i in self._history()}
        for ref in packet["required_refs"]:
            if ref not in current or conclusion_digest(frozen.get(ref)) != conclusion_digest(current[ref]):
                raise ExecutionRecoveryRequired("d8_frozen_execution_premise_changed")

    @staticmethod
    def _parse(request, raw):
        if request.stage == "fact_review":
            return D8ChangeDecision.from_dict(raw)
        from agent_core.dual_llm_modeling import D8HistoryReadAction
        read = D8HistoryReadAction.parse(raw)
        if read is not None:
            return read
        action = D8ConclusionChange.parse_action(raw)
        if action is None:
            raise ValueError("d8_repair_only_accepts_conclusion_change")
        return action

    def _known(self, request, *, repair=True):
        if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22} and repair:
            self.app.selection_bindings.repair_request(request)
        call = self.store.load_d4_operation(request.model_request_id)
        data = self.evidence.read("result", request.model_request_id)
        if call is None:
            if data is not None:
                raise ExecutionRecoveryRequired("d8_revision_result_without_start")
            return None
        inner = request.stage == "fact_proposal"
        role = self.app.config.inner_role if inner else self.app.config.outer_role
        if request.role_config_ref != role.role_config_ref or request.task_run_id != self.state.task_run.task_run_id:
            raise ExecutionRecoveryRequired("d8_revision_frozen_role_changed")
        expected = D4OperationStart(request.model_request_id, request.task_run_id,
                                   "action" if inner else "fact_review", call.start.started_at,
                                   request.base_checkpoint_sequence, request.context_sha256,
                                   request.actor_intent_id, self.app.config.d4_budget.budget_policy_ref,
                                   "inner_action" if inner else "outer_planning", request.role_config_ref)
        if call.start != expected:
            raise ExecutionRecoveryRequired("d8_revision_start_binding_changed")
        if data is None:
            boundary = self.app.output_boundary
            failure = boundary.failure(request) if boundary is not None else None
            if failure is not None:
                data = {"model_request_id": request.model_request_id, "request_sha256": request.context_sha256,
                        "outcome": "model_error" if failure["terminal"] else "invalid", "raw": None,
                        "finished_at": failure["finished_at"]}
                self.evidence.save("result", request.model_request_id, data)
        if data is None:
            raise ExecutionRecoveryRequired("d8_revision_model_outcome_unknown")
        if (set(data) != {"model_request_id", "request_sha256", "outcome", "raw", "finished_at"}
                or data["model_request_id"] != request.model_request_id
                or data["request_sha256"] != request.context_sha256
                or data["outcome"] not in {"valid", "invalid", "sensitive_rejected", "model_error", "stale"}):
            raise ExecutionRecoveryRequired("d8_revision_result_binding_changed")
        if data["outcome"] == "valid":
            if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
                self.app.selection_bindings.verify(request, data['raw'])
            self._parse(request, data["raw"])
            if self.app.conclusions._sensitive(data["raw"]):
                raise ExecutionRecoveryRequired("d8_revision_saved_sensitive_text")
        elif data["raw"] is not None:
            raise ExecutionRecoveryRequired("d8_revision_failed_result_contains_text")
        completion = D4OperationCompletion(request.model_request_id, data["finished_at"],
                                           "completed" if data["outcome"] == "valid" else "rejected",
                                           request.model_request_id, conclusion_digest(data))
        if call.completion is None:
            if not repair:
                raise ExecutionRecoveryRequired("d8_revision_completion_missing")
            self.store.complete_d4_operation(completion)
        elif call.completion != completion:
            raise ExecutionRecoveryRequired("d8_revision_completion_changed")
        return data

    async def _call(self, request, token):
        if self.app.sessions is not None:
            from agent_core.session_contracts import D8SessionChangeRequest
            if isinstance(request, D8SessionChangeRequest):
                self.app.sessions.freeze(request)
        saved = self.evidence.read("request", request.model_request_id)
        if saved is None:
            self.evidence.save("request", request.model_request_id, asdict(request))
        elif D8ChangeRequest.from_dict(saved) != request:
            raise ExecutionRecoveryRequired("d8_revision_request_changed")
        result = self._known(request)
        if result is None:
            self._actor(request.actor_intent_id)
            if self.state.latest_checkpoint.sequence != request.base_checkpoint_sequence:
                raise ExecutionRecoveryRequired("d8_revision_request_stale")
            if token.is_cancelled:
                raise ExecutionRecoveryRequired("d8_revision_cancelled")
            self.app.handoff.check_role_request_capacity(request)
            inner = request.stage == "fact_proposal"
            self.store.begin_d4_operation(D4OperationStart(
                request.model_request_id, request.task_run_id, "action" if inner else "fact_review",
                self.app.clock(), request.base_checkpoint_sequence, request.context_sha256,
                request.actor_intent_id, self.app.config.d4_budget.budget_policy_ref,
                "inner_action" if inner else "outer_planning", request.role_config_ref))
            raw = await self.app.conclusions._invoke(request, token)
            outcome = "valid"
            if isinstance(raw, ModelError):
                if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22} and raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                    raise ExecutionRecoveryRequired('d8_revision_model_outcome_unknown')
                outcome, raw = "model_error", None
            elif self.app.conclusions._sensitive(raw):
                outcome, raw = "sensitive_rejected", None
            else:
                try:
                    self._parse(request, raw)
                except (TypeError, ValueError, KeyError):
                    outcome, raw = "invalid", None
            if token.is_cancelled or self.app._dispatch_stopped() or self.state.latest_checkpoint.sequence != request.base_checkpoint_sequence:
                outcome, raw = "stale", None
            result = {"model_request_id": request.model_request_id, "request_sha256": request.context_sha256,
                      "outcome": outcome, "raw": raw, "finished_at": self.app.clock()}
            self.evidence.save("result", request.model_request_id, result)
            result = self._known(request)
        if self.app.output_boundary is not None and result["outcome"] != "stale":
            self.app.output_boundary.terminal_check(request)
        if result["outcome"] == "invalid" and request.correction_of_model_request_id is None:
            self._actor(request.actor_intent_id)
            identity = self._id("correction/" + request.model_request_id)
            saved = self.evidence.read("request", identity)
            correction = D8ChangeRequest.from_dict(saved) if saved else replace(
                request, model_request_id=identity, correction_of_model_request_id=request.model_request_id,
                rejection_code="d8_revision_response_contract_invalid")
            return await self._call(correction, token)
        if result["outcome"] != "valid":
            raise ExecutionRecoveryRequired("d8_revision_model_" + result["outcome"])
        if token.is_cancelled:
            raise ExecutionRecoveryRequired("d8_revision_cancelled")
        self._actor(request.actor_intent_id)
        return request, self._parse(request, result["raw"])

    def _source_snapshot(self, source):
        if "sources" in source:
            from agent_core.evidence_conclusions import EvidenceObservation
            observed=EvidenceObservation(**{**source,"sources":tuple(source["sources"])})
            self.app.evidence_conclusions.validate_sources(observed)
            return json.loads(handoff_json(asdict(observed)))
        return asdict(self.app.conclusions.observation(source["proof_id"],historical=True))

    def _sources(self, action, history):
        by_ref = {i.ref:i for i in history}
        result={}
        def include(observed):
            source=json.loads(handoff_json(asdict(observed)))
            self._source_snapshot(source)
            result[conclusion_digest(source)]=source
        def include_fact(fact):
            if (fact.verification_ref or "").startswith("d8e:"):
                _,observed=self.app.evidence_conclusions.validate_fact(fact,self.state)
            else:
                observed=self.app.conclusions.observation(fact.verification_ref[3:],historical=True)
            include(observed)
        for ref in action.evidence_refs:
            item=by_ref[ref]
            if item.record_type=="verified_fact":
                include_fact(next(f for f in self.state.facts if f.fact_id==item.record_id))
            else:
                for source in item.source_refs:
                    fact=next((f for f in self.state.facts if f.verification_ref==source),None)
                    if fact is not None:include_fact(fact)
                record=next((r for r in self.store.list_d8_conclusions(self.state.task_run.task_run_id) if r.record_id==item.record_id),None)
                if record is not None:
                    request=self.app.conclusions.evidence.read_request(record.review_request_id)
                    if record.plan_id is None:include(request.observation)
                    else:include(self.app.conclusions.observation(request.observation.proof_id,historical=True))
                revision=next((r for r in self.records() if r.record_id==item.record_id),None)
                if revision is not None:
                    for source in self._payload(revision)[0]["sources"]:
                        if self._source_snapshot(source)!=source:
                            raise ExecutionRecoveryRequired("d8_revision_inference_source_changed")
                        result[conclusion_digest(source)]=source
        if action.content is not None and not result:
            raise ExecutionRecoveryRequired("d8_revision_inference_has_no_safe_observation")
        return list(result.values())

    async def review(self, action, actor_id, proposal_id, visible, token):
        actor = self._actor(actor_id)
        existing = next((r for r in self.records() if r.proposal_request_id == proposal_id), None)
        if existing is not None:
            _, recorded_action, _ = self._payload(existing)
            if recorded_action != action or existing.actor_intent_id != actor_id:
                raise ExecutionRecoveryRequired("d8_revision_replay_changed")
            await self.refresh_summaries(token)
            return existing
        if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
            self.app.selection_bindings.check_relation(proposal_id, action)
        self.validate_action(action, visible)
        identity = self._id("review/" + proposal_id)
        saved = self.evidence.read("request", identity)
        original_goal, relation_old_ref = (self._relation_details(action, self._base())
                                           if action.operation == "replace_support" else (None, None))
        request = D8ChangeRequest.from_dict(saved) if saved else D8ChangeRequest(
            identity, self.state.task_run.task_run_id, actor_id, "fact_review",
            self.app.config.outer_role.role_config_ref, self.state.latest_checkpoint.sequence,
            tuple(visible), action, original_goal,
            prior_changes=self.prior_changes(t.root_id for t in action.targets), relation_old_ref=relation_old_ref)
        if request.candidate != action:
            raise ExecutionRecoveryRequired("d8_revision_candidate_changed")
        final, decision = await self._call(request, token)
        if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
            self.app.selection_bindings.check_relation(proposal_id, action)
        self.validate_action(action, final.history)
        if final.base_checkpoint_sequence != self.state.latest_checkpoint.sequence:
            raise ExecutionRecoveryRequired("d8_revision_approval_stale")
        if decision.decision == "accept":
            self._check_decision(action, decision)
        record_id = self._id("record/" + proposal_id)
        data = {"record_id": record_id, "candidate": asdict(action), "decision": asdict(decision),
                "request_id": final.model_request_id, "commit_sequence": self.state.latest_checkpoint.sequence + 1,
                "sources": self._sources(action, final.history), "relation_old_ref": final.relation_old_ref}
        self.evidence.save("handoff", record_id, data)
        record = D8RevisionRecord(record_id, self.state.task_run.task_run_id, actor.intent_id,
                                   len(self.records()) + 1, action.operation, decision.decision,
                                   proposal_id, final.model_request_id, record_id, conclusion_digest(data))
        self.app.controller.commit_d3_transition("append_d8_revision", (record,), (record_id,),
                                                record_id, record_id)
        await self.refresh_summaries(token)
        return record

    def _check_decision(self, action, decision):
        if action.operation == "replace_support" and action.relation_kind == "parent_support" and decision.goal_supported is not True:
            raise ExecutionRecoveryRequired("d8_replacement_does_not_support_original_goal")
        if action.operation == "resolve_conflict" and action.resolution == "different_scope" and not decision.scope_explanation:
            raise ExecutionRecoveryRequired("d8_scope_resolution_requires_explanation")
        if (action.operation == "resolve_conflict" and action.resolution == "different_scope"
                and any(set(pair) == {t.ref for t in action.targets} for pair in self._registered_conflicts(self._base()))):
            raise ExecutionRecoveryRequired("d8_registered_attribute_conflict_cannot_be_dismissed")

    def validate_record(self, record, state):
        data, action, decision = self._payload(record)
        request = D8ChangeRequest.from_dict(self.evidence.read("request", record.review_request_id))
        result = self._known(request)
        if (result is None or result["outcome"] != "valid" or request.candidate != action
                or self._parse(request, result["raw"]) != decision
                or request.actor_intent_id != record.actor_intent_id
                or request.task_run_id != record.task_run_id
                or request.base_checkpoint_sequence != state.latest_checkpoint.sequence
                or data["commit_sequence"] != state.latest_checkpoint.sequence + 1
                or data["sources"] != self._sources(action, request.history)):
            raise ExecutionRecoveryRequired("d8_revision_approval_binding_changed")
        self._actor(record.actor_intent_id)
        self.validate_action(action, request.history)
        if decision.decision == "accept":
            self._check_decision(action, decision)
        if action.operation == "replace_support":
            goal, old = self._relation_details(action, self._base())
            if (request.original_goal, request.relation_old_ref, data["relation_old_ref"]) != (goal, old, old):
                raise ExecutionRecoveryRequired("d8_support_approval_relation_changed")
        if self.store.load_d3_operation(record.proposal_request_id) is not None:
            evidence = self.app.model_evidence.read(record.proposal_request_id)
            candidate = D8ConclusionChange.parse_action(evidence.raw) if evidence is not None else None
            if (candidate != action or evidence.outcome != "accepted"
                    or evidence.task_run_id != record.task_run_id):
                raise ExecutionRecoveryRequired("d8_revision_inner_proposal_changed")
            if evidence.stage == "inner_plan":
                parts = evidence.resource_ref.split(":")
                actor_id = parts[1] if len(parts) == 3 and parts[0] == "plan" else None
            else:
                binding = self.store.load_d3_plan_binding(evidence.resource_ref.rsplit(":", 1)[0])
                actor_id = binding.intent_id if binding is not None else None
            if actor_id != record.actor_intent_id:
                raise ExecutionRecoveryRequired("d8_revision_inner_actor_changed")
        else:
            proposal = D8ChangeRequest.from_dict(self.evidence.read("request", record.proposal_request_id))
            known = self._known(proposal)
            if (known is None or known["outcome"] != "valid" or proposal.stage != "fact_proposal"
                    or proposal.actor_intent_id != record.actor_intent_id
                    or self._parse(proposal, known["raw"]) != action):
                raise ExecutionRecoveryRequired("d8_revision_inner_proposal_changed")

    def _dirty_turns(self):
        records = [r for r in self.records() if r.decision == "accept"]
        if not records:
            return ()
        watermarks = {}
        for summary in self.store.list_d8_handoffs(self.state.task_run.task_run_id):
            data = self.app.handoff.evidence.read("handoff", summary.evidence_ref)
            if data is None or conclusion_digest(data) != summary.evidence_sha256:
                raise ExecutionRecoveryRequired("d8_revision_summary_evidence_unavailable")
            watermarks[summary.turn_id] = data.get("revision_sequence", 0)
        turns = set()
        all_items = {i.ref: i for i in self._history()}
        for record in records:
            _, action, _ = self._payload(record)
            actor = next(i for i in self.state.intents if i.intent_id == record.actor_intent_id)
            affected = {actor.turn_id}
            for target in action.targets:
                item = all_items.get(target.ref)
                if item and item.turn_id:
                    affected.add(item.turn_id)
            if action.relation_owner_id:
                owner = next(i for i in self.state.intents if i.intent_id == action.relation_owner_id)
                affected.add(owner.turn_id)
            turns.update(t for t in affected if watermarks.get(t, 0) < record.sequence)
        return tuple(sorted(turns))

    def summary_stale(self, data):
        latest = max((r.sequence for r in self.records() if r.decision == "accept"), default=0)
        return data is not None and data.get("revision_sequence", 0) < latest

    async def refresh_summaries(self, token):
        pending = self._dirty_turns()
        for turn_id in pending:
            await self.app.handoff.summarize(turn_id, token)

    def reconcile(self):
        for call in self.store.list_d4_operations(self.state.task_run.task_run_id):
            raw = self.evidence.read("request", call.start.operation_id)
            if raw is not None and (self.evidence.read("result", call.start.operation_id) is not None
                    or (self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}
                        and self.app.output_boundary.evidence.read('selection-binding', call.start.operation_id) is not None)
                    or (self.app.output_boundary is not None
                        and self.app.output_boundary.failure(D8ChangeRequest.from_dict(raw)) is not None)):
                self._known(D8ChangeRequest.from_dict(raw))

    async def repair(self, actor_id, request_id, token):
        """Explicit one-request maintenance; never dispatches a tool or completion."""
        _uuid(request_id, "request_id")
        self.reconcile()
        self._actor(actor_id)
        if self.app.sessions is not None and self.store.load_d4_operation(request_id) is None:
            await self.app.sessions.before_decision(actor_id, (), token, restricted=True)
        prior = next((r for r in self.records() if r.proposal_request_id == request_id), None)
        if prior is None:
            correction = self._id("correction/" + request_id)
            prior = next((r for r in self.records() if r.proposal_request_id == correction), None)
        if prior is not None:
            if prior.actor_intent_id != actor_id:
                raise ExecutionRecoveryRequired("d8_repair_request_owner_changed")
            await self.refresh_summaries(token)
            return prior
        raw = self.evidence.read("request", request_id)
        if raw:
            request = D8ChangeRequest.from_dict(raw)
        else:
            actor = self._actor(actor_id)
            selected, _, packet = self.app.handoff.select("conclusion_repair", actor.turn_id, actor_id)
            packet["retrieved"] = self.app.handoff.retrieved_for(actor_id)
            if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
                from agent_core.governance_policy import freeze_policy
                packet['s5_policy'] = freeze_policy(self.app, packet)
            capacity = self.app.handoff.capacity().capacity
            if len(handoff_json(packet)) > capacity.visible_history:
                self.app.handoff._pause("visible_history", (), capacity.visible_history, len(handoff_json(packet)))
            request = D8ChangeRequest(request_id, self.state.task_run.task_run_id, actor_id, "fact_proposal",
                                      self.app.config.inner_role.role_config_ref, self.state.latest_checkpoint.sequence,
                                      tuple(selected), history_context=handoff_json(packet), prior_changes=self.prior_changes())
        if (request.actor_intent_id, request.task_run_id) != (actor_id, self.state.task_run.task_run_id):
            raise ExecutionRecoveryRequired("d8_repair_request_owner_changed")
        if self.app.sessions is not None:
            request = self.app.sessions.wrap(request)
        final, action = await self._call(request, token)
        from agent_core.dual_llm_modeling import D8HistoryReadAction
        if isinstance(action, D8HistoryReadAction):
            self.app.handoff.validate_read_action(final, action)
            identity = self._id("repair-read/" + final.model_request_id)
            mode = {"history_query": "query", "history_details": "refs", "history_next_page": "cursor"}[action.kind]
            start = self.store.load_d8_read(identity)
            if start is not None and start[1] is None:
                response = self.app.handoff.resume_read(identity)
            else:
                response = self.app.handoff.read_history(**{mode: action.value}, request_id=identity,
                                                         actor_intent_id=actor_id)
            self.store.mark_d4_operation_applied(final.model_request_id, identity)
            return response
        visible = self.visible_from_request(final) if final.history_context else final.history
        return await self.review(action, actor_id, final.model_request_id, visible, token)

    @staticmethod
    def visible_from_request(request):
        return ConclusionRevisions.visible_from_context(request.history_context)

    def visible_for_execution(self, request_id):
        saved = self.app.handoff.evidence.read("execution-context", request_id)
        if saved is None:
            raise ExecutionRecoveryRequired("d8_change_execution_context_missing")
        return self.visible_from_context(saved["history_context"])

    @staticmethod
    def visible_from_context(history_context):
        import json
        packet = json.loads(history_context)
        entries = list(packet["items"])
        for read in packet.get("retrieved", ()):
            # Read results retain their old snapshot. Current validity is checked separately.
            entries.extend(read["read"]["items"])
        by_ref = {}
        for raw in entries:
            value = dict(raw)
            value.pop("ref", None)
            if "content" not in value:
                continue
            if "start" in value:
                if value["start"] != 0 or value["end"] != value["total"]:
                    continue
                for key in ("start", "end", "total"):
                    value.pop(key)
            entry = D8HistoryItem.from_dict(value)
            existing = by_ref.get(raw["ref"])
            if existing is None or getattr(entry, "revision", 1) >= getattr(existing, "revision", 1):
                by_ref[raw["ref"]] = entry
        return tuple(by_ref.values())
