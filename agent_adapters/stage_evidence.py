"""Original stage model bytes and same-run source verification, using the existing recorder."""

import asyncio
import json
import threading
from time import monotonic
from dataclasses import asdict, replace

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.conclusion_revision_contracts import D8ChangeDecision
from agent_core.modeling import CancellationToken, ModelError, ModelErrorKind, model_call_timeout
from agent_core.operation_accounting import D4OperationCompletion, D4OperationRecord, D4OperationStart
from agent_core.output_contracts import D8ModelResponse, D8OutputRequest
from agent_core.stage_contracts import (
    PlanningExhausted, RoadCandidate, StageConclusionChange, StageFactSelection, StageHistoryRead,
    StageFactProposal, StageModelEvidence, StageSessionSummary, TurnIntent,
    TurnPlanningContext, TurnPlanningProposal, TurnPlanningReview, StagePlanningDecision,
)
from agent_core.stage_planning import INNER_STAGES, StageModelRequest, model_contract, stage_output_issues, parse, request_from_dict
from agent_core.stage_storage import StageRecoveryRequired, StageStateError, proposal_digest, _fail
from agent_core.turn_modeling import digest, stable_id
from qinyuan_core import Category, get_logger


class StageEvidence:
    def __init__(self, directory, store, *, create=True):
        self.records = T6CommandRecorder(directory, create=create)
        self.store = store
        self.http_validator = None

    def save(self, kind, identity, value):
        self.records._write("stage-" + kind, identity,
            json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def bind_http_reader(self, task_id):
        from types import SimpleNamespace
        from agent_core.web_contracts import D7WebAuthorization
        from agent_adapters.http_capability import HttpCapability
        from agent_adapters.http_experiments import HttpExperiments
        policy = self.read("policy", task_id)
        raw = policy["web_authorization"]
        if raw is None:
            return
        auth = D7WebAuthorization(**{**raw, "allowed_methods": tuple(raw["allowed_methods"]),
            "attachment_indices": tuple(raw["attachment_indices"])})
        config = SimpleNamespace(web_authorization=auth, contract_version=22,
            authorization=SimpleNamespace(attachment_indices=tuple(policy["authorization"]["attachment_indices"])))
        reader = SimpleNamespace(config=config, store=self.store, task_id=task_id,
            directory=self.records.directory.parent, state=SimpleNamespace(task_run=self.store.load_task_run(task_id)),
            output_boundary=SimpleNamespace(evidence=self))
        reader.web = HttpCapability(reader)
        reader.web.read_only = True
        self.http_validator = HttpExperiments(SimpleNamespace(app=reader, evidence=self)).validate_receipt

    def read(self, kind, identity):
        try:
            return self.records._read("stage-" + kind, identity)
        except T6EvidenceError:
            _fail("stage.evidence_corrupt", Category.UNKNOWN)

    def request(self, identity):
        saved = self.read("request", identity)
        if saved is None:
            _fail("stage.request_missing", Category.UNKNOWN)
        request = request_from_dict(saved["request"], saved["proposal_kind"])
        if request.model_request_id != identity:
            _fail("stage.request_identity_changed", Category.UNKNOWN)
        contract = self.read("contract", identity)
        if (contract is None or request.frozen_contract_ref != "sha256:" + digest(contract)
                or contract != self.contract(request)):
            _fail("stage.model_contract_changed", Category.POLICY)
        return request

    def contract(self, request):
        feedback = None
        source_id = request.correction_of_model_request_id
        if source_id is not None:
            source = self.request(source_id)
            operation = self.store.load_d4_operation(source_id)
            result = self.read("result", source_id)
            fields = ("task_run_id", "stage", "role_config_ref", "base_checkpoint_sequence", "context",
                "tools", "proposal_operation_id", "proposal", "history_catalog")
            before, after = asdict(source), asdict(request)
            if (source.correction_of_model_request_id is not None or operation is None or result is None
                    or operation.completion is None or operation.completion.result_kind != "failed" or result["outcome"] != "invalid"
                    or digest({field: before[field] for field in fields}) != digest({field: after[field] for field in fields})
                    or operation.start.context_sha256 != digest(asdict(source))
                    or result["request_sha256"] != operation.start.context_sha256
                    or operation.completion.evidence_ref != "stage-result/" + source_id
                    or operation.completion.evidence_sha256 != digest(result)):
                _fail("stage.correction_binding_changed", Category.UNKNOWN)
            issues = stage_output_issues(source, D8ModelResponse(result["content"], result["finish_reason"], result.get("tool_calls")))
            feedback = {"correction_of_model_request_id": source_id, "source_result_sha256": digest(result),
                "reason": "output_business_invalid" if any(i.code == "stage.formal_fact_required" for i in issues) else "output_format_invalid",
                "issues": [asdict(i) for i in issues]
                    or [{"code": "stage_value_invalid", "path": "$", "expected": "valid_stage_value", "actual": "invalid"}]}
        return {**model_contract(request), "correction": feedback}

    def read_model_result(self, operation: D4OperationRecord) -> StageModelEvidence:
        identity = operation.start.operation_id
        request = self.request(identity)
        result = self.read("result", identity)
        if (request.task_run_id != operation.start.task_run_id or digest(asdict(request)) != operation.start.context_sha256
                or request.base_checkpoint_sequence != operation.start.base_checkpoint_sequence
                or request.role_config_ref != operation.start.role_config_ref
                or operation.start.resource_ref != "stage/" + request.context.session.scope + "/" + request.context.session.owner_id
                or operation.completion is None or result is None
                or operation.completion.evidence_ref != "stage-result/" + identity
                or digest(result) != operation.completion.evidence_sha256
                or result["request_sha256"] != operation.start.context_sha256 or result["outcome"] != "valid"):
            _fail("stage.original_evidence_changed", Category.UNKNOWN)
        value = parse(request, D8ModelResponse(result["content"], result["finish_reason"], result.get("tool_calls")))
        if request.proposal_operation_id is not None:
            source = self.store.load_d4_operation(request.proposal_operation_id)
            if source is None:
                _fail("stage.proposal_missing", Category.UNKNOWN)
            proposed = self.read_model_result(source)
            if proposed.result != request.proposal or proposed.session != request.context.session:
                _fail("stage.proposal_changed", Category.UNKNOWN)
        return StageModelEvidence(request.context.session, operation.start.context_sha256,
            operation.completion.evidence_sha256, value, request.proposal_operation_id,
            proposal_digest(request.proposal) if request.proposal_operation_id else None)

    def observation(self, operation):
        identity = operation.start.operation_id
        claim = self.read("mcp-call", identity)
        result = self.read("mcp-result", identity)
        parts = operation.start.resource_ref.split("/")
        experiment = len(parts) == 5 and parts[0] == "mcp" and parts[2] == "experiment"
        if operation.completion is None or not (len(parts) == 3 and parts[0] == "mcp" or experiment):
            _fail("stage.observation_binding_changed", Category.UNKNOWN)
        available = claim is not None and result is not None
        if available and (
                operation.start.context_sha256 != digest(claim)
                or result["request_sha256"] != digest(claim)
                or operation.completion.evidence_ref != "mcp-result/" + identity
                or operation.completion.evidence_sha256 != digest(result)
                or claim["task_run_id"] != operation.start.task_run_id
                or parts[1] != claim["intent_id"] or parts[3 if experiment else 2] != claim["model_request_id"]):
            _fail("stage.observation_binding_changed", Category.UNKNOWN)
        if available and experiment:
            if self.http_validator is None:
                self.bind_http_reader(operation.start.task_run_id)
            if self.http_validator is None:
                _fail("stage.http_evidence_unconfigured", Category.DEPENDENCY)
            self.http_validator(operation, claim, result)
        turn = next((t for t in self.store.list_stage_turns(operation.start.task_run_id)
            if any(r.road_id == parts[1] for r in t.roads)), None)
        if turn is None:
            _fail("stage.observation_scope_changed", Category.UNKNOWN)
        return D8HistoryItem(identity, operation.start.task_run_id, "progress", turn.turn_id,
            parts[1], None, json.dumps(result["payload"], ensure_ascii=False) if available else "", (identity,),
            digest({"request": operation.start.context_sha256, "result": operation.completion.evidence_sha256}),
            operation.start.base_checkpoint_sequence,
            "public_reference_not_target_observation" if available and result["payload"].get("identity") == "public_reference_not_target_observation" else "tool_observation_not_fact", "current",
            "available" if available else "unavailable")

    def raw_history(self, task_id, *, extra_refs=()):
        items = {}
        imports = (self.read("imports", task_id) or {}).get("items", ())
        if imports:
            from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
            recorder = FileAttachmentImportRecorder(self.records.directory.parent / "attachment-imports", read_only=True)
            for entry in imports:
                receipt = recorder.read_receipt(entry["import_id"])
                if receipt is None:
                    _fail("stage.attachment_receipt_unavailable", Category.UNKNOWN)
                if receipt.task_run_id != task_id or receipt.sandbox_path != entry["sandbox_path"] or receipt.attachment_index != entry["index"]:
                    _fail("stage.attachment_binding_changed", Category.UNKNOWN)
                data = {"index": receipt.attachment_index, "path": receipt.sandbox_path,
                    "sha256": receipt.sha256, "byte_size": receipt.byte_size}
                item = D8HistoryItem("import/" + entry["import_id"], task_id, "progress", None, None, None,
                    json.dumps(data, ensure_ascii=False), (entry["import_id"],), digest(asdict(receipt)),
                    1, "imported_attachment_not_fact", "current", "available")
                items[item.ref] = item
        operations = self.store.list_d4_operations(task_id)
        turns = self.store.list_stage_turns(task_id)
        owners = {t.turn_id: t for t in turns}
        owners.update({r.road_id: t for t in turns for r in t.roads})
        for operation in operations:
            if operation.start.category == "tool_dispatch" and operation.completion is not None:
                item = self.observation(operation)
                items[item.ref] = item
        facts = {row["fact_id"]: dict(row) for row in self.store._connection.execute(
            "SELECT f.* FROM facts f JOIN stage_turns t ON t.turn_id=f.turn_id WHERE t.task_run_id=? "
            "ORDER BY f.created_event_sequence,f.created_event_ordinal", (task_id,))}
        inferences = {}
        for operation in operations:
            if operation.start.category != "fact_review" or operation.application_ref is None:
                continue
            request = self.request(operation.start.operation_id)
            if isinstance(request.proposal, StageFactProposal) and request.proposal.inferred:
                reviewed = self.read_model_result(operation)
                if reviewed.result.decision == "accept":
                    inferences[request.proposal_operation_id] = operation.start.operation_id
        visiting = set()
        built = {}
        def conclusion(identity, kind):
            key = kind, identity
            if key in built:
                return built[key]
            if key in visiting:
                _fail("stage.knowledge_source_cycle", Category.UNKNOWN)
            visiting.add(key)
            row = facts.get(identity) if kind == "verified_fact" else None
            if kind == "verified_fact":
                if row is None or not (row["verification_ref"] or "").startswith("stage:") or row["status"] != "verified":
                    _fail("stage.fact_binding_changed", Category.UNKNOWN)
                review_id = row["verification_ref"][6:]
            else:
                review_id = inferences.get(identity)
                if review_id is None:
                    revision = next((r for r in self.store.list_d8_revisions(task_id) if r.record_id == identity), None)
                    if revision is None:
                        _fail("stage.inference_source_missing", Category.UNKNOWN)
                    data, action, decision = self.knowledge(task_id)._payload(revision)
                    if decision.decision != "accept" or action.content is None:
                        _fail("stage.inference_source_missing", Category.UNKNOWN)
                    source = self.read_model_result(self.store.load_d4_operation(revision.proposal_request_id))
                    owner = self.knowledge(task_id).turn_for(source.session)
                    supports = [resolve(ref) for ref in action.evidence_refs]
                    item = D8HistoryItem(identity, task_id, kind, owner.turn_id,
                        source.session.owner_id if source.session.scope == "road_execution" else None, None,
                        action.content, action.evidence_refs, revision.evidence_sha256, data["commit_sequence"],
                        "reviewed_inference_not_verified", "current",
                        "available" if all(i.source_status == "available" for i in supports) else "unavailable")
                    built[key] = item
                    visiting.remove(key)
                    return item
            review = self.store.load_d4_operation(review_id) if review_id else None
            if review is None or review.application_ref is None:
                _fail("stage.fact_review_unavailable", Category.UNKNOWN)
            reviewed = self.read_model_result(review)
            request = self.request(review_id)
            candidate = request.proposal
            if (not isinstance(candidate, StageFactProposal) or reviewed.result.decision != "accept"
                    or candidate.inferred != (kind == "reviewed_inference") or row and candidate.content != row["content"]):
                _fail("stage.fact_binding_changed", Category.UNKNOWN)
            turn = owners.get(reviewed.session.owner_id)
            if turn is None or row and row["turn_id"] != turn.turn_id:
                _fail("stage.fact_source_changed", Category.UNKNOWN)
            supports = []
            for ref in candidate.support_refs:
                item = resolve(ref)
                if item.record_type == "progress" and (item.intent_id != reviewed.session.owner_id or item.turn_id != turn.turn_id):
                    _fail("stage.fact_source_scope_changed", Category.POLICY)
                supports.append(item)
            observations = [i for i in supports if i.record_type == "progress"]
            if row and (not observations or observations[0].record_id != row["source_ref"]
                    or any(i.record_type == "reviewed_inference" for i in supports)):
                _fail("stage.fact_source_changed", Category.UNKNOWN)
            if row:
                version, sequence = digest(row), row["created_event_sequence"]
            else:
                sequence_row = self.store._connection.execute("SELECT checkpoint_sequence FROM control_mutations "
                    "WHERE task_run_id=? AND mutation_id=?", (task_id, review.application_ref)).fetchone()
                if sequence_row is None:
                    _fail("stage.inference_application_missing", Category.UNKNOWN)
                version, sequence = digest({"proposal": reviewed.proposal_operation_id,
                    "review": review_id, "evidence": reviewed.evidence_sha256}), sequence_row[0]
            item = D8HistoryItem(identity, task_id, kind, turn.turn_id,
                reviewed.session.owner_id if reviewed.session.scope == "road_execution" else None, None,
                candidate.content, candidate.support_refs, version, sequence,
                "reviewed_formal_fact" if row else "reviewed_inference_not_verified", "current",
                "available" if all(i.source_status == "available" for i in supports) else "unavailable")
            visiting.remove(key)
            built[key] = item
            items[item.ref] = item
            return item
        def resolve(ref):
            item = items.get(ref)
            if item is None:
                source_kind, source_id, _ = ref.split(":", 2)
                if source_kind not in {"verified_fact", "reviewed_inference"}:
                    _fail("stage.fact_source_changed", Category.UNKNOWN)
                item = conclusion(source_id, source_kind)
            if item.ref != ref:
                _fail("stage.fact_source_changed", Category.UNKNOWN)
            return item
        for identity in facts:
            conclusion(identity, "verified_fact")
        for identity in inferences:
            conclusion(identity, "reviewed_inference")
        for request, review, sequence in self.applied_reviews(task_id):
            value = request.proposal
            if not isinstance(value, PlanningExhausted) and not (isinstance(value, StagePlanningDecision)
                    and value.kind in {"return_to_parent", "request_final_validation"}):
                continue
            if review.result.decision != "accept":
                continue
            owner = owners.get(request.context.session.owner_id)
            item = D8HistoryItem("feedback/" + request.model_request_id, task_id, "progress",
                owner.turn_id if owner else request.context.session.owner_id,
                request.context.session.owner_id if request.context.session.scope == "road_execution" else None,
                None, json.dumps({"kind": "exhausted" if isinstance(value, PlanningExhausted) else value.kind,
                    "reason": value.reason, "review_reason": review.result.reason}, ensure_ascii=False),
                (request.proposal_operation_id, request.model_request_id, *value.support_refs),
                review.evidence_sha256, sequence, "reviewed_feedback_not_fact", "historical", "available")
            items[item.ref] = item
        for candidate in self.store.list_d7_answer_candidates(task_id):
            binding = self.read("goal-candidate", candidate.candidate_id)
            if binding is None:
                _fail("stage.candidate_binding_changed", Category.UNKNOWN)
            request = self.request(binding["proposal_operation_id"])
            for feedback in self.store.list_d7_validation_feedback(task_id, candidate.candidate_id, candidate.evidence_version):
                row = self.store._connection.execute("SELECT created_event_sequence FROM d7_validation_feedback WHERE operation_id=?",
                    (feedback.operation_id,)).fetchone()
                data = feedback.to_dict()
                item = D8HistoryItem("validation/" + feedback.operation_id, task_id, "progress",
                    request.context.session.owner_id, None, None, json.dumps(data, ensure_ascii=False),
                    (candidate.candidate_id, feedback.source_ref), digest(data), row[0],
                    "trusted_validation_feedback_not_fact", "historical", "available")
                items[item.ref] = item
        additional = [resolve(ref) for ref in extra_refs if ref not in items]
        return (*items.values(), *additional)

    def applied_reviews(self, task_id):
        result = []
        for operation in self.store.list_d4_operations(task_id):
            if operation.start.category != "review" or operation.application_ref is None:
                continue
            request = self.request(operation.start.operation_id)
            if request.stage != "stage_review":
                continue
            review = self.read_model_result(operation)
            row = self.store._connection.execute("SELECT checkpoint_sequence FROM control_mutations "
                "WHERE task_run_id=? AND mutation_id=?", (task_id, operation.application_ref)).fetchone()
            if row is None:
                _fail("stage.feedback_application_missing", Category.UNKNOWN)
            result.append((request, review, row[0]))
        return tuple(result)

    def knowledge(self, task_id):
        from agent_core.stage_knowledge import StageKnowledge
        return StageKnowledge(self, task_id)

    def history(self, task_id):
        return self.knowledge(task_id).project()

    def read_history(self, task_run_id: str, refs: tuple[str, ...], *, observations_only=False) -> tuple[D8HistoryItem, ...]:
        if observations_only:
            items = tuple(self.observation(op) for op in self.store.list_d4_operations(task_run_id)
                if op.start.category == "tool_dispatch" and op.completion is not None)
        else:
            items = self.history(task_run_id)
        by_ref = {item.ref: item for item in items}
        if any(ref not in by_ref for ref in refs):
            _fail("stage.source_unavailable", Category.POLICY)
        return tuple(by_ref[ref] for ref in refs)

    def perform_history_read(self, operation):
        source = self.read_model_result(operation)
        if not isinstance(source.result, StageHistoryRead):
            _fail("stage.history_operation_invalid", Category.INPUT)
        identity = operation.start.operation_id
        if self.read("history", identity) is None:
            items = self.read_history(operation.start.task_run_id, source.result.refs)
            self.save("history", identity, {"request_sha256": operation.start.context_sha256,
                "items": [asdict(i) for i in items]})
        return self.read_history_result(operation)

    def read_history_result(self, operation: D4OperationRecord) -> tuple[D8HistoryItem, ...]:
        source = self.read_model_result(operation)
        data = self.read("history", operation.start.operation_id)
        if not isinstance(source.result, StageHistoryRead) or data is None or data["request_sha256"] != operation.start.context_sha256:
            _fail("stage.history_receipt_changed", Category.UNKNOWN)
        items = tuple(D8HistoryItem.from_dict(i) for i in data["items"])
        if tuple(i.ref for i in items) != source.result.refs or any(i.task_run_id != operation.start.task_run_id for i in items):
            _fail("stage.history_receipt_changed", Category.UNKNOWN)
        return items


class StageModelCalls:
    """One admitted invocation identity, immutable result, at most one format correction."""
    def __init__(self, store, evidence, inner, outer, clock, capacity_port, capacity_config):
        self.store, self.evidence = store, evidence
        self.inner, self.outer, self.clock = inner, outer, clock
        self.capacity_port, self.capacity_config = capacity_port, capacity_config
        self.messages = lambda session: ()
        self.recover_capacity = None

    def _port_request(self, stage, context, operation_id, proposal_id=None, proposal=None):
        config = self.store.load_stage_run_config(context.task_run_id)
        role = config.inner_role if stage in INNER_STAGES else config.outer_role
        sequence = self.store.load_control_state_rows(context.task_run_id).latest_checkpoint.sequence
        return StageModelRequest(operation_id, context.task_run_id, stage, role.role_config_ref,
            sequence, context, self.messages(context.session), (), proposal_id, proposal)

    async def propose_turn(self, context: TurnPlanningContext, operation_id: str,
                           cancellation_token: CancellationToken) -> TurnIntent | PlanningExhausted | ModelError:
        if context.session.scope != "turn_planning":
            _fail("stage.planning_scope_changed", Category.POLICY)
        return (await self.call(self._port_request("stage_intent", context, operation_id), cancellation_token))[1]

    async def propose_roads(self, context: TurnPlanningContext, operation_id: str,
                            cancellation_token: CancellationToken) -> tuple[RoadCandidate, ...] | PlanningExhausted | StageFactSelection | StageHistoryRead | StageConclusionChange | StageFactProposal | StagePlanningDecision | ModelError:
        if context.turn is None or context.session.scope != "turn_planning" or context.session.owner_id != context.turn.turn_id:
            _fail("stage.planning_scope_changed", Category.POLICY)
        return (await self.call(self._port_request("stage_roads", context, operation_id), cancellation_token))[1]

    async def review(self, context: TurnPlanningContext, proposal: TurnPlanningProposal | StageFactProposal,
                     proposal_operation_id: str, operation_id: str,
                     cancellation_token: CancellationToken) -> TurnPlanningReview | D8ChangeDecision | ModelError:
        proposal_operation_id = self.resolved_operation_id(proposal_operation_id)
        return (await self.call(self._port_request("stage_review", context, operation_id,
            proposal_operation_id, proposal), cancellation_token))[1]

    def resolved_operation_id(self, operation_id: str) -> str:
        """Resolve only saved capacity/correction bindings; never dispatch a call."""
        for _ in range(4):
            request = self.evidence.request(operation_id)
            recovery = self.evidence.read("capacity-recovery", operation_id)
            if recovery is not None:
                if recovery["source_request_sha256"] != digest(asdict(request)):
                    _fail("stage.capacity_binding_changed", Category.UNKNOWN)
                operation_id = stable_id(request.task_run_id,
                    "stage/capacity/" + operation_id + "/" + recovery["summary_operation_id"])
                continue
            record = self.store.load_d4_operation(operation_id)
            result = self.evidence.read("result", operation_id)
            if record is not None and record.completion is not None and result is not None and result["outcome"] == "invalid":
                corrected = stable_id(request.task_run_id, "stage/correction/" + operation_id)
                if self.evidence.request(corrected).correction_of_model_request_id != operation_id:
                    _fail("stage.correction_binding_changed", Category.UNKNOWN)
                operation_id = corrected
                continue
            if record is not None:
                self.evidence.read_model_result(record)
                return operation_id
            break
        _fail("stage.model_operation_not_ready", Category.UNKNOWN)

    def reconcile(self, task_id):
        for record in self.store.list_d4_operations(task_id):
            result = self.evidence.read("result", record.start.operation_id)
            if result is None:
                continue
            request = self.evidence.request(record.start.operation_id)
            if digest(asdict(request)) != record.start.context_sha256 or result["request_sha256"] != record.start.context_sha256:
                raise StageStateError("stage.model_result_changed", code="stage.model_result_changed")
            completion = D4OperationCompletion(record.start.operation_id, result["finished_at"],
                "completed" if result["outcome"] == "valid" else "failed",
                "stage-result/" + record.start.operation_id, digest(result))
            if record.completion is None:
                self.store.complete_d4_operation(completion)
            elif record.completion != completion:
                raise StageStateError("stage.model_result_changed", code="stage.model_result_changed")
            if request.stage == "stage_supervision" and record.application_ref is None:
                if result["outcome"] == "valid":
                    self.evidence.read_model_result(self.store.load_d4_operation(record.start.operation_id))
                    self.store.mark_d4_operation_applied(record.start.operation_id, "stage-advice-recovered/" + record.start.operation_id)

    async def _invoke(self, model, envelope, timeout, token, deadline):
        finished, values = threading.Event(), []
        expires = monotonic() + timeout
        def invoke():
            try:
                values.append((True, model.invoke(envelope, timeout, token)))
            except Exception as exc:
                values.append((False, type(exc).__name__))
            finally:
                finished.set()
        threading.Thread(target=invoke, daemon=True, name="qinyuan-stage-model").start()
        while not finished.is_set():
            await asyncio.sleep(0.02)
            if token.is_cancelled or self.clock() >= deadline or monotonic() >= expires:
                raise StageRecoveryRequired("stage.model_outcome_unknown")
        okay, value = values[0]
        if not okay:
            raise StageRecoveryRequired("stage.model_outcome_unknown")
        return value

    async def call(self, request, token):
        request = self._freeze(request)
        recovery = self.evidence.read("capacity-recovery", request.model_request_id)
        if recovery is not None:
            if recovery["source_request_sha256"] != digest(asdict(request)) or request.capacity_recovery_of is not None:
                _fail("stage.capacity_binding_changed", Category.UNKNOWN)
            summary_id = recovery["summary_operation_id"]
            summary = self.store.load_d4_operation(summary_id)
            if (summary is None or summary.application_ref is None or summary.start.task_run_id != request.task_run_id
                    or summary.start.category != "handoff_summary"):
                _fail("stage.summary_unavailable", Category.UNKNOWN)
            metadata = self.evidence.read_model_result(summary)
            if metadata.session != request.context.session or not isinstance(metadata.result, StageSessionSummary):
                _fail("stage.summary_scope_changed", Category.UNKNOWN)
            identity = stable_id(request.task_run_id, "stage/capacity/" + request.model_request_id + "/" + summary_id)
            saved = self.evidence.read("request", identity)
            replacement = (self.evidence.request(identity) if saved else replace(request, model_request_id=identity,
                messages=self.messages(request.context.session), capacity_recovery_of=request.model_request_id))
            return await self.call(replacement, token)
        config = self.store.load_stage_run_config(request.task_run_id)
        inner = request.stage in INNER_STAGES
        role = config.inner_role if inner else config.outer_role
        model = self.inner if inner else self.outer
        if model.role_config != role or request.role_config_ref != role.role_config_ref:
            _fail("stage.model_configuration_changed", Category.POLICY)
        original = request
        for attempt in range(1 if request.correction_of_model_request_id else 0, 2):
            request = self._freeze(request)
            identity = request.model_request_id
            self.evidence.save("request", identity, {"request": asdict(request),
                "proposal_kind": type(request.proposal).__name__})
            # Read back through the same bounded/checksummed reader before dispatch.
            request = self.evidence.request(identity)
            record = self.store.load_d4_operation(identity)
            result = self.evidence.read("result", identity)
            category = "handoff_summary" if request.stage == "stage_summary" else "planning" if request.stage in {"stage_intent", "stage_roads", "stage_continuation"} else "action" if inner else (
                "fact_review" if isinstance(request.proposal, (StageFactProposal, StageConclusionChange)) else "review")
            if record is None:
                task = self.store.load_task_run(request.task_run_id)
                if token.is_cancelled or self.clock() >= task.deadline_at:
                    raise StageRecoveryRequired("stage.dispatch_stopped", category=Category.POLICY)
                contract = self.evidence.read("contract", identity)
                envelope = D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": contract["schema"]},
                    contract["instruction"], contract["correction"])
                policy = self.capacity_config.inner_capacity if inner else self.capacity_config.outer_capacity
                if not self.capacity_port.fits(envelope, policy):
                    if request.stage != "stage_summary" and request.capacity_recovery_of is None and self.recover_capacity is not None:
                        recovered = await self.recover_capacity(request, token)
                        if recovered is not None:
                            return await self.call(recovered, token)
                    _fail("stage.model_capacity_blocked", Category.CAPACITY)
                start = D4OperationStart(identity, request.task_run_id, category, self.clock(),
                    request.base_checkpoint_sequence, digest(asdict(request)),
                    "stage/" + request.context.session.scope + "/" + request.context.session.owner_id,
                    config.budget.budget_policy_ref, role.role, role.role_config_ref)
                self.store.begin_d4_operation(start)
                get_logger("model").info("stage.call_started", run=request.task_run_id,
                    operation=identity, role=role.role, phase=request.stage)
                timeout = model_call_timeout(model, task.deadline_at - self.clock())
                raw = await self._invoke(model, envelope, timeout, token, task.deadline_at)
                if isinstance(raw, ModelError):
                    if raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                        raise StageRecoveryRequired("stage.model_outcome_unknown")
                    result = {"request_sha256": start.context_sha256, "outcome": "model_error",
                        "content": None, "finish_reason": "error", "finished_at": self.clock()}
                else:
                    content = raw.content if isinstance(raw, D8ModelResponse) else raw
                    finish = raw.finish_reason if isinstance(raw, D8ModelResponse) else "stop"
                    tool_calls = raw.tool_calls if isinstance(raw, D8ModelResponse) else None
                    try:
                        parse(request, D8ModelResponse(content, finish, tool_calls))
                        outcome = "valid"
                    except ValueError as error:
                        outcome = "invalid"
                        code = "stage.output_envelope_invalid" if error.args == ("stage.output_envelope_invalid",) else "stage.output_invalid"
                        issues = stage_output_issues(request, D8ModelResponse(content, finish, tool_calls))
                        if any(i.code == "stage.formal_fact_required" for i in issues):
                            code = "stage.proposal_business_invalid"
                        get_logger("model").warning("stage.output_rejected", run=request.task_run_id,
                            operation=identity, phase=request.stage, code=code, category="input",
                            issue_codes=",".join(i.code for i in issues) or "stage_value_invalid")
                    result = {"request_sha256": start.context_sha256, "outcome": outcome,
                        "content": content, "finish_reason": finish, "finished_at": self.clock()}
                    if tool_calls is not None:
                        result["tool_calls"] = tool_calls
                self.evidence.save("result", identity, result)
                record = self.store.load_d4_operation(identity)
            if result is None:
                raise StageRecoveryRequired("stage.model_outcome_unknown")
            completion = D4OperationCompletion(identity, result["finished_at"],
                "completed" if result["outcome"] == "valid" else "failed", "stage-result/" + identity, digest(result))
            if record.completion is None:
                self.store.complete_d4_operation(completion)
            elif record.completion != completion:
                raise StageStateError("stage.model_result_changed", code="stage.model_result_changed")
            if result["outcome"] == "valid":
                evidence = self.evidence.read_model_result(self.store.load_d4_operation(identity))
                return identity, evidence.result
            if result["outcome"] != "invalid" or attempt:
                get_logger("model").warning("stage.call_rejected", run=request.task_run_id,
                    operation=identity, phase=request.stage, code="stage.model_output_unusable",
                    category="input" if result["outcome"] == "invalid" else "dependency")
                _fail("stage.model_output_unusable", Category.INPUT if result["outcome"] == "invalid" else Category.DEPENDENCY)
            request = replace(original, model_request_id=stable_id(original.task_run_id, "stage/correction/" + original.model_request_id),
                correction_of_model_request_id=original.model_request_id, frozen_contract_ref=None)
        raise AssertionError("bounded correction did not terminate")

    def _freeze(self, request):
        contract = self.evidence.contract(request)
        reference = "sha256:" + digest(contract)
        if request.frozen_contract_ref is not None and request.frozen_contract_ref != reference:
            _fail("stage.model_contract_changed", Category.POLICY)
        self.evidence.save("contract", request.model_request_id, contract)
        return replace(request, frozen_contract_ref=reference)
