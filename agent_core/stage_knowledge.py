"""Stage views over canonical knowledge and the existing append-only revision log."""

from dataclasses import replace
from types import SimpleNamespace

from agent_core.conclusion_revisions import ConclusionRevisions
from agent_core.conclusion_revision_contracts import D8RevisionRecord
from agent_core.domain import IntentStatus
from agent_core.stage_contracts import StageConclusionChange
from agent_core.stage_storage import _fail
from agent_core.turn_modeling import digest, stable_id
from qinyuan_core import Category


class StageKnowledge(ConclusionRevisions):
    """Reuse revision projection; stage ownership and IO stay in their own boundary."""

    def __init__(self, evidence, task_id, *, store=None):
        self.evidence, self.store, self.task_id = evidence, store if store is not None else evidence.store, task_id
        self.turns = {t.turn_id: t for t in self.store.list_stage_turns(task_id)}
        self._records = self.store.list_d8_revisions(task_id)
        self._raw = None

    def records(self):
        return self._records

    @property
    def state(self):
        # Read-only ownership projection, never a persisted second Turn/Intent tree.
        actors = [SimpleNamespace(intent_id=t.turn_id, turn_id=t.turn_id, status=IntentStatus.RUNNING)
                  for t in self.turns.values()]
        actors += [SimpleNamespace(intent_id=r.road_id, turn_id=t.turn_id, status=r.status)
                   for t in self.turns.values() for r in t.roads]
        return SimpleNamespace(task_run=self.store.load_task_run(self.task_id), intents=tuple(actors))

    def _base(self):
        if self._raw is None:
            self._raw = self.evidence.raw_history(self.task_id)
        return self._raw

    @staticmethod
    def _snapshot(item):
        return {"ref": item.ref, "source_status": item.source_status}

    def _source_snapshot(self, source):
        item = next((i for i in self._base() if i.ref == source["ref"]), None)
        if item is None:
            item = next((i for i in self.evidence.raw_history(self.task_id, extra_refs=(source["ref"],))
                         if i.ref == source["ref"]), None)
        return {"ref": source["ref"], "source_status": "unavailable"} if item is None else self._snapshot(item)

    def _registered_conflicts(self, items):
        return ()  # Conflicts require explicit reviewed scope; do not infer semantics.

    def _payload(self, record):
        data = self.evidence.read("revision", record.evidence_ref)
        proposal = self.evidence.read_model_result(self.store.load_d4_operation(record.proposal_request_id))
        review = self.evidence.read_model_result(self.store.load_d4_operation(record.review_request_id))
        if (data is None or digest(data) != record.evidence_sha256 or record.task_run_id != self.task_id
                or proposal.session.owner_id != record.actor_intent_id or proposal.session != review.session
                or not isinstance(proposal.result, StageConclusionChange)
                or review.proposal_operation_id != record.proposal_request_id
                or proposal.result.operation != record.operation or review.result.decision != record.decision):
            _fail("stage.revision_binding_changed", Category.UNKNOWN)
        frozen = self.evidence.request(record.proposal_request_id)
        by_ref = {i.ref: i for i in frozen.context.history}
        expected = [self._snapshot(by_ref[ref]) for ref in proposal.result.evidence_refs]
        if data["sources"] != expected:
            _fail("stage.revision_sources_changed", Category.UNKNOWN)
        return data, proposal.result, review.result

    def project(self, base=None):
        projected = super().project(self._base() if base is None else base)
        by_ref = {i.ref: i for i in projected}
        usable, visiting = {}, set()
        def effective(ref):
            if ref in usable:
                return usable[ref]
            if ref in visiting:
                _fail("stage.knowledge_source_cycle", Category.UNKNOWN)
            item = by_ref.get(ref)
            if item is None:
                return False
            visiting.add(ref)
            dependencies = [r for r in item.source_refs if r.startswith(("verified_fact:", "reviewed_inference:", "progress:"))]
            result = self.reliable(item) and all(effective(r) for r in dependencies)
            visiting.remove(ref)
            usable[ref] = result
            return result
        result = []
        for item in projected:
            if item.record_type in {"verified_fact", "reviewed_inference"} and self.reliable(item) and not effective(item.ref):
                item = replace(item, current_status="unavailable", effective_ref=None, effective_version_id=None)
            elif getattr(item, "effective_ref", None) and not effective(item.effective_ref):
                item = replace(item, effective_ref=None, effective_version_id=None)
            if item.intent_id in self.turns:
                item = replace(item, intent_id=None)
            result.append(item)
        return tuple(result)

    def turn_for(self, session):
        turn = self.turns.get(session.owner_id) if session.scope == "turn_planning" else next(
            (t for t in self.turns.values() if any(r.road_id == session.owner_id for r in t.roads)), None)
        if turn is None:
            _fail("stage.knowledge_actor_invalid", Category.POLICY)
        return turn

    def turn_facts(self, turn_id, *, include_ancestors=False):
        refs, seen = [], set()
        turn = self.turns.get(turn_id)
        if turn is None:
            _fail("stage.knowledge_turn_missing", Category.POLICY)
        while turn is not None:
            if turn.turn_id in seen:
                _fail("stage.parent_cycle", Category.UNKNOWN)
            seen.add(turn.turn_id)
            refs.extend(turn.fact_refs)
            turn = self.turns.get(turn.parent_turn_id) if include_ancestors else None
        by_ref = {i.ref: i for i in self.project()}
        if any(ref not in by_ref for ref in refs):
            _fail("stage.selected_fact_missing", Category.UNKNOWN)
        return tuple(by_ref[ref] for ref in dict.fromkeys(refs))

    def effective_premises(self, turn, road=None):
        if not self.records():
            return tuple(dict.fromkeys((*turn.intent.support_refs, *(road.candidate.support_refs if road else ()))))
        refs = [self.relation_ref("turn_premise", turn.turn_id, ref) for ref in turn.intent.support_refs]
        if road is not None:
            refs += [self.relation_ref("road_premise", road.road_id, ref) for ref in road.candidate.support_refs]
        return tuple(dict.fromkeys(refs))

    def context(self, session, retrieved=()):
        items = self.project()
        turn = self.turn_for(session)
        road = next((r for r in turn.roads if r.road_id == session.owner_id), None)
        premises = self.effective_premises(turn, road)
        refs = {i.ref for i in self.turn_facts(turn.turn_id, include_ancestors=True)} | set(premises) | set(turn.intent.support_refs) | set(retrieved)
        refs.update(i.ref for i in items if i.identity in {"imported_attachment_not_fact", "user_hint_not_verified"})
        if turn.achievement:
            refs.update(turn.achievement.completion.support_refs)
        for candidate in turn.roads:
            if candidate.status in {IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED}:
                refs.update(candidate.candidate.support_refs)
                refs.update(self.effective_premises(turn, candidate))
        local = [i for i in items if i.turn_id == turn.turn_id]
        refs.update(i.ref for i in [i for i in local if i.record_type in {"verified_fact", "reviewed_inference"}][-4:])
        refs.update(i.ref for i in [i for i in local if i.record_type == "progress"
                                  and not i.record_id.startswith("revision/")][-4:])
        feedback = [i for i in items if i.record_id.startswith("feedback/")
            and i.turn_id in self.turns and self.turns[i.turn_id].parent_turn_id == turn.turn_id]
        refs.update(i.ref for i in feedback[-4:])
        roots = {getattr(i, "logical_root_id", None) for i in items if i.ref in refs}
        for record in self.records():
            _, action, _ = self._payload(record)
            if any(t.root_id in roots for t in action.targets):
                refs.update(i.ref for i in items if i.record_id == "revision/" + record.record_id)
        return tuple(i for i in items if i.ref in refs), premises

    def relation_origin(self, action, session, items):
        turn = self.turn_for(session)
        if action.relation_kind == "turn_premise" and action.relation_owner_id == turn.turn_id:
            originals = turn.intent.support_refs
        elif action.relation_kind == "road_premise":
            road = next((r for r in turn.roads if r.road_id == action.relation_owner_id), None)
            if road is None:
                _fail("stage.support_owner_invalid", Category.POLICY)
            originals = road.candidate.support_refs
        else:
            _fail("stage.support_owner_invalid", Category.POLICY)
        by_ref = {i.ref: i for i in items}
        candidates = [self.relation_ref(action.relation_kind, action.relation_owner_id, ref) for ref in originals]
        matches = [ref for ref in candidates if ref in by_ref and getattr(by_ref[ref], "logical_root_id", None) == action.targets[0].root_id]
        if len(matches) != 1:
            _fail("stage.support_relation_changed", Category.CONFLICT)
        return matches[0]

    def validate_change(self, action, decision, session, frozen):
        self.turn_for(session)
        items = self.project()
        _, heads, conflicts, _ = self._projection(self._base())
        by_ref, visible = {i.ref: i for i in items}, {i.ref: i for i in frozen}
        for target in action.targets:
            head = heads.get(target.root_id)
            if (head is None or head["version"] != target.version or head["last_ref"] != target.ref
                    or not any(getattr(i, "logical_root_id", i.record_id) == target.root_id
                        and getattr(i, "revision", 1) == target.version
                        and getattr(i, "last_version_ref", i.ref) == target.ref for i in frozen)):
                _fail("stage.revision_head_changed", Category.CONFLICT)
        if any(ref not in visible or not self.reliable(by_ref.get(ref)) for ref in action.evidence_refs):
            _fail("stage.revision_evidence_unavailable", Category.POLICY)
        head = heads[action.targets[0].root_id]
        replacement = by_ref.get(action.replacement_ref)
        if action.replacement_ref is not None:
            restoring = (action.operation == "restore" and action.replacement_ref == action.targets[0].ref
                and replacement is not None and replacement.source_status == "available" and not replacement.conflict)
            if action.replacement_ref not in visible or not (self.reliable(replacement) or restoring):
                _fail("stage.replacement_unavailable", Category.POLICY)
            if action.operation in {"correct", "restore"} and replacement.record_type != head["kind"]:
                _fail("stage.revision_identity_change", Category.POLICY)
            if action.operation == "link_verified" and (head["kind"] != "reviewed_inference" or replacement.record_type != "verified_fact"):
                _fail("stage.validation_link_invalid", Category.POLICY)
        if action.content is not None and (head["kind"] != "reviewed_inference"
                or any(by_ref[ref].record_type not in {"verified_fact", "reviewed_inference"} for ref in action.evidence_refs)):
            _fail("stage.revision_cannot_create_fact", Category.POLICY)
        if action.operation == "restore" and head["ref"] is not None:
            _fail("stage.restore_requires_withdrawal", Category.CONFLICT)
        if action.operation in {"correct", "withdraw"} and head["ref"] is None:
            _fail("stage.revision_requires_current_head", Category.CONFLICT)
        if action.operation == "report_conflict" and any(heads[t.root_id]["ref"] is None for t in action.targets):
            _fail("stage.conflict_requires_current_heads", Category.CONFLICT)
        if action.operation == "resolve_conflict" and tuple(sorted(t.root_id for t in action.targets)) not in conflicts:
            _fail("stage.conflict_not_open", Category.CONFLICT)
        origin = None
        if action.operation == "replace_support":
            origin = self.relation_origin(action, session, items)
            if replacement.record_type != "verified_fact" or decision.goal_supported is not True or not decision.scope_explanation:
                _fail("stage.replacement_goal_not_supported", Category.POLICY)
            _, _, _, relations = self._projection(self._base())
            relations[(action.relation_kind, action.relation_owner_id, origin)] = action.replacement_ref
            ref, seen = origin, set()
            while (action.relation_kind, action.relation_owner_id, ref) in relations:
                if ref in seen:
                    _fail("stage.support_relation_cycle", Category.POLICY)
                seen.add(ref)
                next_ref = relations[(action.relation_kind, action.relation_owner_id, ref)]
                if next_ref == ref:
                    break
                ref = next_ref
        if action.operation == "resolve_conflict" and action.resolution == "different_scope" and not decision.scope_explanation:
            _fail("stage.conflict_scope_unexplained", Category.POLICY)
        return origin

    def prepare_revision(self, binding):
        proposal = self.evidence.read_model_result(self.store.load_d4_operation(binding.proposal_operation_id))
        review = self.evidence.read_model_result(self.store.load_d4_operation(binding.review_operation_id))
        request = self.evidence.request(binding.proposal_operation_id)
        action, decision = proposal.result, review.result
        from agent_core.stage_planning import frozen_history
        origin = (self.validate_change(action, decision, proposal.session, frozen_history(request))
                  if decision.decision == "accept" else None)
        sequence = self.store.load_control_state_rows(self.task_id).latest_checkpoint.sequence + 1
        by_ref = {i.ref: i for i in request.context.history}
        data = {"commit_sequence": sequence, "sources": [self._snapshot(by_ref[ref]) for ref in action.evidence_refs],
                "relation_old_ref": origin}
        identity = stable_id(self.task_id, "stage/revision/" + binding.proposal_operation_id)
        self.evidence.save("revision", identity, data)
        return D8RevisionRecord(identity, self.task_id, proposal.session.owner_id, len(self.records()) + 1,
            action.operation, decision.decision, binding.proposal_operation_id, binding.review_operation_id, identity, digest(data))
