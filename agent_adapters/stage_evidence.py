"""Original stage model bytes and same-run source verification, using the existing recorder."""

import asyncio
import json
import sqlite3
import threading
from time import monotonic
from dataclasses import asdict, replace
from contextlib import closing

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.command_contracts import T6EvidenceCapacityError
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.conclusion_revision_contracts import D8ChangeDecision
from agent_core.modeling import CancellationToken, ModelError, ModelErrorKind, model_call_timeout
from agent_core.operation_accounting import D4OperationCompletion, D4OperationRecord, D4OperationStart
from agent_core.output_contracts import D8ModelResponse, D8OutputRequest
from agent_core.stage_contracts import (
    PlanningExhausted, RoadCandidate, StageConclusionChange, StageFactSelection, StageHistoryRead,
    StageFactProposal, StageModelEvidence, StageSessionSummary, TurnIntent,
    TurnPlanningContext, TurnPlanningProposal, TurnPlanningReview, StagePlanningDecision,
    StageFactBatch, StageFactBatchReview, fact_candidates, reviewed_fact, fact_review_parts,
)
from agent_core.stage_planning import BUSINESS_ISSUE_CODES, INNER_STAGES, MAX_STAGE_CORRECTIONS, STAGE_CALL_POLICY, StageModelRequest, model_contract, stage_output_issues, parse, request_from_dict, review_members, conversation_operation_ids
from agent_core.stage_storage import StageRecoveryRequired, StageStateError, proposal_digest, fact_scope_failure, _fail
from agent_core.turn_modeling import digest, stable_id
from qinyuan_core import Category, Layer, get_logger


# Transport cleanup joins its worker for at most 2 x 0.5s. The watchdog
# allows that cleanup plus a bounded diagnostic write, never more generation.
MODEL_DIAGNOSTIC_GRACE_SECONDS = 2.0
TASK_DEADLINE_STAGES = frozenset({"stage_intent", "stage_roads", "stage_continuation"})


class StageModelTransportFailure(StageStateError):
    category = Category.DEPENDENCY
    layer = Layer.MODEL
    code = "stage.model_transport_failed"


class StageModelSuperseded(Exception):
    """Local generation ended; its unused result cannot become a decision."""


def _envelope(request, contract):
    return D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": contract["schema"]},
        contract["instruction"], contract["correction"])


class _StageCallToken:
    def __init__(self, token, expires, clock, deadline, *, deadline_reader=None, task_deadline_only=False):
        self.token, self.expires, self.clock, self.deadline = token, expires, clock, deadline
        self.stopped = threading.Event()
        self.deadline_reader = deadline_reader
        self.task_deadline_only = task_deadline_only

    @property
    def remaining_seconds(self):
        # Recheck at the old boundary through a separate read-only connection:
        # an acknowledged extension must not race the owner's polling interval.
        if self.clock() >= self.deadline and self.deadline_reader is not None:
            self.deadline = self.deadline_reader()
        return max(0, self.deadline - self.clock())

    @property
    def is_timed_out(self):
        return monotonic() >= self.expires or self.remaining_seconds <= 0

    @property
    def is_cancelled(self):
        return self.stopped.is_set() or self.token.is_cancelled or self.is_timed_out


class StageEvidence:
    def __init__(self, directory, store, *, create=True):
        self.records = T6CommandRecorder(directory, create=create)
        self.store = store
        self.historical = not create
        self.http_validator = None

    def save(self, kind, identity, value):
        try:
            self.records._write("stage-" + kind, identity,
                json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False)))
        except T6EvidenceCapacityError as error:
            self.capacity_error(error)
        except T6EvidenceError as error:
            if isinstance(error.__cause__, OSError):
                _fail("stage.evidence_io_failed", Category.DEPENDENCY)
            raise

    def capacity_error(self, error):
        operation = self.store.load_d4_operation(error.record_id)
        failure = StageStateError("stage.evidence_capacity_exceeded", code="stage.evidence_capacity_exceeded")
        failure.category = Category.CAPACITY
        failure.evidence_capacity = dict(record_kind=error.record_kind, record_id=error.record_id,
            actual_bytes=error.actual_bytes, limit_bytes=error.limit_bytes, dispatched=operation is not None)
        get_logger("store").warning(failure.code, category="capacity", **failure.evidence_capacity)
        raise failure from error

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
        except T6EvidenceCapacityError as error:
            self.capacity_error(error)
        except T6EvidenceError as error:
            if isinstance(error.__cause__, OSError):
                _fail("stage.evidence_io_failed", Category.DEPENDENCY)
            _fail("stage.evidence_corrupt", Category.UNKNOWN)

    def request(self, identity):
        saved = self.read("request", identity)
        if saved is None:
            _fail("stage.request_missing", Category.UNKNOWN)
        request = request_from_dict(saved["request"], saved["proposal_kind"])
        if request.model_request_id != identity:
            _fail("stage.request_identity_changed", Category.UNKNOWN)
        contract = self.read("contract", identity)
        # Historical evidence binds its original immutable contract. Rebuilding
        # today's prompts here would make a prompt/schema upgrade unreadable.
        # New dispatch still verifies today's complete contract in _freeze.
        if (contract is None or request.frozen_contract_ref != "sha256:" + digest(contract)
                or not self.historical and contract != self.contract(request)):
            _fail("stage.model_contract_changed", Category.POLICY)
        return request

    def contract(self, request):
        self.correction_depth(request)
        feedback = None
        source_id = request.correction_of_model_request_id
        if source_id is not None:
            source = self.request(source_id)
            operation = self.store.load_d4_operation(source_id)
            result = self.read("result", source_id)
            fields = ("task_run_id", "stage", "role_config_ref", "base_checkpoint_sequence", "context",
                "tools", "proposal_operation_id", "proposal", "history_catalog", "execution_context", "summary_coverage")
            before, after = asdict(source), asdict(request)
            if (operation is None or result is None
                    or operation.completion is None or operation.completion.result_kind != "failed"
                    or digest({field: before[field] for field in fields}) != digest({field: after[field] for field in fields})
                    or operation.start.context_sha256 != digest(asdict(source))
                    or result["request_sha256"] != operation.start.context_sha256
                    or operation.completion.evidence_ref != "stage-result/" + source_id
                    or operation.completion.evidence_sha256 != digest(result)):
                _fail("stage.correction_binding_changed", Category.UNKNOWN)
            feedback = {"correction_of_model_request_id": source_id, "source_result_sha256": digest(result)}
            if result["outcome"] == "invalid":
                issues = stage_output_issues(source, D8ModelResponse(result["content"], result["finish_reason"], result.get("tool_calls")),
                    normalization_policy=self.read("contract", source_id).get("output_normalization"))
                feedback.update(reason="output_business_invalid" if any(i.code in BUSINESS_ISSUE_CODES for i in issues) else "output_format_invalid",
                    issues=[asdict(i) for i in issues]
                        or [{"code": "stage_value_invalid", "path": "$", "expected": "valid_stage_value", "actual": "invalid"}])
            elif result["outcome"] == "model_error" and self.transport_failure(source, result) is not None:
                feedback.update(reason="previous_model_transport_failed_no_usable_decision", issues=[])
            else:
                _fail("stage.correction_binding_changed", Category.UNKNOWN)
        return {**model_contract(request), "correction": feedback}

    def correction_depth(self, request):
        """Count persisted parent links, never a process-local retry counter."""
        seen = {request.model_request_id}
        for depth in range(MAX_STAGE_CORRECTIONS + 1):
            source_id = request.correction_of_model_request_id
            if source_id is None:
                return depth
            if depth == MAX_STAGE_CORRECTIONS or source_id in seen:
                _fail("stage.correction_limit_exceeded", Category.POLICY)
            seen.add(source_id)
            saved = self.read("request", source_id)
            if saved is None:
                _fail("stage.request_missing", Category.UNKNOWN)
            request = request_from_dict(saved["request"], saved["proposal_kind"])
            if request.model_request_id != source_id:
                _fail("stage.request_identity_changed", Category.UNKNOWN)

    def _provider_result(self, request, settled=None):
        contract = self.read("contract", request.model_request_id)
        if contract is None or contract.get("call_policy") != STAGE_CALL_POLICY:
            return None
        if settled is not None and "provider_result_sha256" not in settled:
            return None
        sent = self.read("provider-request", request.model_request_id)
        result = self.read("provider-result", request.model_request_id)
        if sent is None or result is None:
            return None
        operation = self.store.load_d4_operation(request.model_request_id)
        if operation is not None and (operation.application_ref is not None
                or operation.completion is not None and operation.completion.result_kind != "failed"):
            return None
        if (operation is None or operation.start.context_sha256 != digest(asdict(request))
                or operation.start.task_run_id != request.task_run_id
                or operation.start.role_config_ref != request.role_config_ref
                or operation.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                or operation.start.resource_ref != "stage/" + request.context.session.scope + "/" + request.context.session.owner_id
                or sent["model_request_id"] != request.model_request_id or sent["task_run_id"] != request.task_run_id
                or sent["stage"] != request.stage or sent["role_config_ref"] != request.role_config_ref
                or sent["envelope_sha256"] != digest(asdict(_envelope(request, contract)))
                or result["model_request_id"] != request.model_request_id or result["request_sha256"] != digest(sent)
                or settled is not None and settled["provider_result_sha256"] != digest(result)):
            if settled is not None:
                raise StageRecoveryRequired("stage.provider_result_binding_changed")
            return None
        return result

    def transport_failure(self, request, settled=None):
        """Authorize local failure settlement only from bound, terminated IO."""
        from agent_core.structured_output import ended_transport_failure
        result = self._provider_result(request, settled)
        if result is None:
            return None
        error = result.get("error") or {}
        if (not ended_transport_failure(result["transport"]) or error.get("kind") not in {"timed_out", "provider_failure"}
                or error.get("code") not in {"model_connect_timeout", "model_first_output_timeout", "model_idle_timeout",
                    "model_total_timeout", "http_timeout", "http_transport_failure", "model_worker_lost"}):
            if settled is not None:
                _fail("stage.model_result_changed", Category.UNKNOWN)
            return None
        return result

    def branch_stop(self, request):
        stop = self.read("branch-stop", request.model_request_id)
        if stop is None:
            return None
        operation = self.store.load_d4_operation(request.model_request_id)
        config = self.store.load_stage_run_config(request.task_run_id)
        review_id = stop["review_operation_id"]
        if (operation is None or request.stage not in INNER_STAGES or operation.start.role != "inner_action" or operation.start.category not in {"action", "planning"}
                or operation.start.task_run_id != request.task_run_id or operation.start.context_sha256 != digest(asdict(request))
                or operation.start.role_config_ref != request.role_config_ref
                or operation.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                or operation.start.resource_ref != "stage/" + request.context.session.scope + "/" + request.context.session.owner_id
                or request.role_config_ref != config.inner_role.role_config_ref
                or operation.start.budget_policy_ref != config.budget.budget_policy_ref
                or stop["request_sha256"] != digest(asdict(request))
                or not set(self.review_interruptions(review_id)).intersection(conversation_operation_ids(request))
                or stop["local_invoke_exited"] is not True or stop["returned_kind"] not in {"response", "cancelled", "transport_failure"}):
            raise StageRecoveryRequired("stage.branch_stop_binding_changed")
        if stop["returned_kind"] == "cancelled":
            provider = self.cancelled_transport(request)
            if provider is None or digest(provider) != stop["provider_result_sha256"]:
                raise StageRecoveryRequired("stage.branch_stop_unconfirmed")
        if stop["returned_kind"] == "transport_failure" and self.transport_failure(request,
                {"provider_result_sha256": stop["provider_result_sha256"]}) is None:
            raise StageRecoveryRequired("stage.branch_stop_unconfirmed")
        return stop

    def cancelled_transport(self, request):
        provider = self._provider_result(request)
        if (provider is not None and provider["transport"].get("worker_exit_confirmed") is True
                and provider["transport"].get("transport_end") in {"cancelled", "not_dispatched"}
                and provider.get("error", {}).get("kind") == "cancelled"
                and provider["error"].get("code") == "model_cancelled"):
            return provider
        return None

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
        contract, diagnostics = self.read("contract", identity), {}
        value = parse(request, D8ModelResponse(result["content"], result["finish_reason"], result.get("tool_calls")),
            result_schema=contract["schema"], normalization_policy=contract.get("output_normalization"), diagnostics=diagnostics)
        if result.get("normalization") != diagnostics.get("normalization"):
            _fail("stage.original_evidence_changed", Category.UNKNOWN)
        if request.proposal_operation_id is not None:
            self.review_sources(request)
        return StageModelEvidence(request.context.session, operation.start.context_sha256,
            operation.completion.evidence_sha256, value, request.proposal_operation_id,
            proposal_digest(request.proposal) if request.proposal_operation_id else None)

    def review_sources(self, request):
        """Authenticate the program-assembled batch against every original."""
        members = review_members(request)
        if not members or members[0] != request.proposal_operation_id or len(set(members)) != len(members):
            _fail("stage.review_members_changed", Category.UNKNOWN)
        sources, target = [], None
        config = self.store.load_stage_run_config(request.task_run_id)
        for identity in members:
            operation = self.store.load_d4_operation(identity)
            if (operation is None or operation.start.task_run_id != request.task_run_id
                    or operation.start.role != "inner_action" or operation.completion is None
                    or operation.completion.result_kind != "completed"
                    or operation.start.role_config_ref != config.inner_role.role_config_ref
                    or operation.start.budget_policy_ref != config.budget.budget_policy_ref):
                _fail("stage.proposal_missing", Category.UNKNOWN)
            proposed = self.read_model_result(operation)
            if proposed.session != request.context.session:
                _fail("stage.proposal_changed", Category.UNKNOWN)
            if len(members) > 1:
                original = self.request(identity).context.turn
                road = next((r for r in original.roads if r.road_id == proposed.session.owner_id), None) if original else None
                if not fact_candidates(proposed.result) or original is None or road is None:
                    _fail("stage.review_members_changed", Category.UNKNOWN)
                version = digest({"turn": original.turn_id, "intent": asdict(original.intent), "road": asdict(road.candidate)})
                if target is not None and target != version:
                    _fail("stage.review_members_changed", Category.UNKNOWN)
                target = version
            sources.append((identity, proposed))
        value = sources[0][1].result if len(sources) == 1 else StageFactBatch(tuple(
            claim for _, proposed in sources for _, claim in fact_candidates(proposed.result)))
        if request.proposal != value:
            _fail("stage.proposal_changed", Category.UNKNOWN)
        return tuple(sources)

    def review_claims(self, request):
        return tuple((identity, index, claim) for identity, source in self.review_sources(request)
            for index, claim in fact_candidates(source.result))

    def review_rejections(self, review_id):
        operation = self.store.load_d4_operation(review_id)
        if operation is None or operation.completion is None or operation.completion.result_kind != "completed":
            _fail("stage.review_not_ready", Category.UNKNOWN)
        reviewed = self.read_model_result(operation)
        sources = self.review_sources(self.request(review_id))
        if isinstance(reviewed.result, StageFactBatchReview):
            owners = [identity for identity, source in sources for _ in fact_candidates(source.result)]
            return tuple(dict.fromkeys(identity for identity, decision in zip(owners, reviewed.result.decisions, strict=True)
                if decision.decision != "accept"))
        rejected = reviewed.result.decision != "accept" or isinstance(self.request(review_id).proposal, tuple) and (
            len(reviewed.result.accepted_road_indices) != len(self.request(review_id).proposal))
        return tuple(identity for identity, _ in sources) if rejected else ()

    def review_interruptions(self, review_id):
        rejected = self.review_rejections(review_id)
        request = self.request(review_id)
        reviewed = self.read_model_result(self.store.load_d4_operation(review_id)).result
        if isinstance(request.proposal, tuple) and reviewed.decision == "accept" and (
                reviewed.accepted_road_indices != tuple(sorted(reviewed.accepted_road_indices))):
            return (request.proposal_operation_id,)
        return rejected

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
        from agent_core.operator_controls import user_hints
        for hint in user_hints(self.store, task_id):
            item = D8HistoryItem("user/" + hint["operation_id"], task_id, "progress", None, None, None,
                json.dumps({k: hint[k] for k in ("author", "created_at", "content")}, ensure_ascii=False),
                (hint["operation_id"],), hint["source_version"], 1,
                "user_hint_not_verified", "current", "available")
            items[item.ref] = item
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
            if fact_candidates(request.proposal):
                reviewed = self.read_model_result(operation)
                for index, candidate in fact_candidates(request.proposal):
                    if candidate.inferred and reviewed_fact(request.proposal, reviewed.result, index)[1].decision == "accept":
                        identity = (request.proposal_operation_id if index is None else stable_id(task_id,
                            "stage/inference/" + request.proposal_operation_id + "/" + str(index)))
                        inferences[identity] = (operation.start.operation_id, index)
        visiting = set()
        built = {}
        def conclusion(identity, kind):
            key = kind, identity
            if key in built:
                return built[key]
            if key in visiting:
                _fail("stage.knowledge_source_cycle", Category.UNKNOWN)
            visiting.add(key)
            candidate_index = None
            row = facts.get(identity) if kind == "verified_fact" else None
            if kind == "verified_fact":
                if row is None or not (row["verification_ref"] or "").startswith("stage:") or row["status"] != "verified":
                    _fail("stage.fact_binding_changed", Category.UNKNOWN)
                review_id, candidate_index = fact_review_parts(row["verification_ref"])
            else:
                source_review = inferences.get(identity)
                review_id, candidate_index = source_review if source_review is not None else (None, None)
                if source_review is None:
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
            candidate, decision = reviewed_fact(request.proposal, reviewed.result, candidate_index)
            if (decision.decision != "accept"
                    or candidate.inferred != (kind == "reviewed_inference") or row and candidate.content != row["content"]):
                _fail("stage.fact_binding_changed", Category.UNKNOWN)
            turn = owners.get(reviewed.session.owner_id)
            if turn is None or row and row["turn_id"] != turn.turn_id:
                _fail("stage.fact_source_changed", Category.UNKNOWN)
            supports = []
            for ref in candidate.support_refs:
                item = resolve(ref)
                if item.record_type == "progress" and (item.intent_id != reviewed.session.owner_id or item.turn_id != turn.turn_id):
                    fact_scope_failure(item, reviewed.session, expected_turn_id=turn.turn_id,
                        fact_id=identity if row else None, proposal_id=reviewed.proposal_operation_id, review_id=review_id)
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
                binding = {"proposal": reviewed.proposal_operation_id, "review": review_id, "evidence": reviewed.evidence_sha256}
                if candidate_index is not None:
                    binding["candidate_index"] = candidate_index
                version, sequence = digest(binding), sequence_row[0]
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
        self.live_operations = set()

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
                            cancellation_token: CancellationToken) -> tuple[RoadCandidate, ...] | PlanningExhausted | StageFactSelection | StageHistoryRead | StageConclusionChange | StageFactProposal | StageFactBatch | StagePlanningDecision | ModelError:
        if context.turn is None or context.session.scope != "turn_planning" or context.session.owner_id != context.turn.turn_id:
            _fail("stage.planning_scope_changed", Category.POLICY)
        return (await self.call(self._port_request("stage_roads", context, operation_id), cancellation_token))[1]

    async def review(self, context: TurnPlanningContext, proposal: TurnPlanningProposal | StageFactProposal | StageFactBatch,
                     proposal_operation_id: str, operation_id: str,
                     cancellation_token: CancellationToken) -> TurnPlanningReview | StageFactBatchReview | D8ChangeDecision | ModelError:
        proposal_operation_id = self.resolved_operation_id(proposal_operation_id)
        return (await self.call(self._port_request("stage_review", context, operation_id,
            proposal_operation_id, proposal), cancellation_token))[1]

    def resolved_operation_id(self, operation_id: str) -> str:
        """Resolve only saved capacity/correction bindings; never dispatch a call."""
        for _ in range(MAX_STAGE_CORRECTIONS + 2):
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
            if (record is not None and record.completion is not None and result is not None
                    and (result["outcome"] == "invalid" or result["outcome"] == "model_error"
                         and self.evidence.transport_failure(request, result) is not None)
                    and self.evidence.correction_depth(request) < MAX_STAGE_CORRECTIONS):
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
            if result is None and self.evidence.read("request", record.start.operation_id) is None:
                continue
            request = self.evidence.request(record.start.operation_id)
            if result is None:
                stopped = self.evidence.branch_stop(request)
                if stopped is not None:
                    result = self._superseded_result(request, stopped)
                    self.evidence.save("result", record.start.operation_id, result)
            if result is None:
                provider = self.evidence.transport_failure(request)
                if provider is None:
                    continue
                result = self._transport_result(request, provider)
                self.evidence.save("result", record.start.operation_id, result)
            self._complete_result(request, record, result)
            if request.stage == "stage_supervision" and record.application_ref is None:
                if result["outcome"] == "valid":
                    self.evidence.read_model_result(self.store.load_d4_operation(record.start.operation_id))
                    self.store.mark_d4_operation_applied(record.start.operation_id, "stage-advice-recovered/" + record.start.operation_id)

    def _transport_result(self, request, provider):
        return {"request_sha256": digest(asdict(request)), "outcome": "model_error", "content": None,
            "finish_reason": "error", "finished_at": self.clock(), "provider_result_sha256": digest(provider)}

    def _superseded_result(self, request, stopped):
        return {"request_sha256": digest(asdict(request)), "outcome": "superseded", "content": None,
            "finish_reason": "cancelled", "finished_at": stopped["finished_at"], "branch_stop_sha256": digest(stopped)}

    def _complete_result(self, request, record, result):
        if digest(asdict(request)) != record.start.context_sha256 or result["request_sha256"] != record.start.context_sha256:
            raise StageStateError("stage.model_result_changed", code="stage.model_result_changed")
        if result["outcome"] == "superseded":
            stop = self.evidence.branch_stop(request)
            if stop is None or digest(stop) != result.get("branch_stop_sha256") or result != self._superseded_result(request, stop):
                raise StageRecoveryRequired("stage.branch_stop_binding_changed")
        if "provider_result_sha256" in result and (result["outcome"] != "model_error" or result["content"] is not None
                or result["finish_reason"] != "error" or self.evidence.transport_failure(request, result) is None):
            raise StageRecoveryRequired("stage.provider_result_binding_changed")
        completion = D4OperationCompletion(record.start.operation_id, result["finished_at"],
            "completed" if result["outcome"] == "valid" else "failed", "stage-result/" + record.start.operation_id, digest(result))
        if record.completion is None:
            self.store.complete_d4_operation(completion)
        elif record.completion != completion:
            raise StageStateError("stage.model_result_changed", code="stage.model_result_changed")

    def _model_unknown(self, envelope, reason, started, timeout, *, model_error=None, exception=None):
        """Attach only bound provider metadata; a diagnostic never settles a call."""
        from agent_adapters.output_evidence import SAFE_EXCEPTION_TYPES, safe_model_error_code
        request = envelope.request
        identity = request.model_request_id
        detail = dict(stage=request.stage, operation_id=identity, reason=reason,
            elapsed_seconds=max(0, monotonic() - started), limit_seconds=timeout,
            limit_scope="model_call", deadline_at=self.store.load_task_run(request.task_run_id).deadline_at,
            diagnostic_status="missing")
        if request.stage in TASK_DEADLINE_STAGES:
            detail.update(limit_scope="task_deadline", limit_seconds=None)
        if model_error is not None:
            detail["model_error_code"] = safe_model_error_code(model_error.short_reason)
        if exception is not None:
            detail["exception_type"] = exception if exception in SAFE_EXCEPTION_TYPES else "OtherError"
        try:
            sent = self.evidence.read("provider-request", identity)
            result = self.evidence.read("provider-result", identity)
            if sent is not None and result is not None:
                if (sent["model_request_id"] != identity or sent["task_run_id"] != request.task_run_id
                        or sent["stage"] != request.stage or sent["role_config_ref"] != request.role_config_ref
                        or sent["envelope_sha256"] != digest(asdict(envelope))
                        or result["model_request_id"] != identity or result["request_sha256"] != digest(sent)):
                    detail["diagnostic_status"] = "binding_mismatch"
                else:
                    detail.update(diagnostic_status="bound", provider_result_ref="stage-provider-result/" + identity,
                        provider_result_sha256=digest(result))
                    transport = result["transport"]
                    # No content, reasoning text, exception text or provider payload.
                    detail["transport"] = {k: transport[k] for k in (
                        "elapsed_seconds", "connected_seconds", "first_byte_seconds", "last_byte_seconds",
                        "first_output_seconds", "last_output_seconds", "content_chars", "content_utf8_bytes",
                        "reasoning_chars", "reasoning_utf8_bytes", "response_utf8_bytes", "worker_exit_confirmed",
                        "http_status", "finish_seen", "done_seen") if k in transport
                        and (transport[k] is None or type(transport[k]) in {int, float, bool})}
                    error = result["error"]
                    if error is not None and "code" in error:
                        detail["model_error_code"] = safe_model_error_code(error["code"])
            elif sent is not None:
                detail["diagnostic_status"] = "result_pending"
        except (StageStateError, T6EvidenceError, KeyError, TypeError, ValueError):
            detail["diagnostic_status"] = "unreadable"
        code = detail.get("model_error_code")
        if code in {"model_connect_timeout", "model_first_output_timeout", "model_idle_timeout",
                    "model_total_timeout", "http_timeout", "model_budget_exhausted"}:
            detail["timeout_kind"] = code
        get_logger("model").warning("stage.model_call_unconfirmed", run=request.task_run_id,
            operation=identity, phase=request.stage, category="unknown", model_call=detail)
        return StageRecoveryRequired("stage.model_outcome_unknown", model_call=detail)

    async def _invoke(self, model, envelope, timeout, token, deadline):
        finished, values = threading.Event(), []
        started = monotonic()
        planning = envelope.request.stage in TASK_DEADLINE_STAGES
        expires = float("inf") if planning else started + timeout
        def read_deadline():
            with closing(sqlite3.connect(self.store.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                return connection.execute("SELECT deadline_at FROM task_runs WHERE task_run_id=?",
                    (envelope.request.task_run_id,)).fetchone()[0]
        call_token = _StageCallToken(token, expires, self.clock, deadline,
            deadline_reader=read_deadline, task_deadline_only=planning)
        refresh_at, cancelled_at = started, None
        def invoke():
            try:
                remaining = call_token.remaining_seconds if planning else max(0, expires - monotonic())
                value = model.invoke(envelope, remaining, call_token)
                values.append((True, value, monotonic()))
            except Exception as exc:
                values.append((False, type(exc).__name__, monotonic()))
            finally:
                finished.set()
        identity = envelope.request.model_request_id
        self.live_operations.add(identity)
        try:
            threading.Thread(target=invoke, daemon=True, name="qinyuan-stage-model").start()
            while not finished.is_set():
                await asyncio.sleep(0.02)
                if finished.is_set():
                    break
                if monotonic() >= refresh_at:
                    call_token.deadline = self.store.load_task_run(envelope.request.task_run_id).deadline_at
                    refresh_at = monotonic() + .25
                user_cancelled = getattr(token, "is_user_cancelled", token.is_cancelled)
                if user_cancelled or call_token.remaining_seconds <= 0:
                    reason = "user_cancelled" if user_cancelled else "task_deadline"
                    raise self._model_unknown(envelope, reason, started, timeout)
                if getattr(token, "superseded_by", None) is not None:
                    call_token.stopped.set()
                    cancelled_at = cancelled_at or monotonic()
                    if monotonic() >= cancelled_at + MODEL_DIAGNOSTIC_GRACE_SECONDS:
                        raise self._model_unknown(envelope, "branch_cancel_unconfirmed", started, timeout)
                if monotonic() >= expires + MODEL_DIAGNOSTIC_GRACE_SECONDS:
                    raise self._model_unknown(envelope, "watchdog_timeout", started, timeout)
            # Preserve a result already returned before the stop was observed.
            # Existing dispatch/commit gates still prevent its application or
            # a next call after task cancellation/deadline.
            okay, value, returned_at = values[0]
            if not okay:
                raise self._model_unknown(envelope, "adapter_exception", started, timeout, exception=value)
            if isinstance(value, ModelError) and value.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                if value.kind is ModelErrorKind.CANCELLED and getattr(token, "superseded_by", None) is not None:
                    if self.evidence.cancelled_transport(envelope.request) is not None:
                        return value
                if self.evidence.transport_failure(envelope.request) is not None:
                    return value
                raise self._model_unknown(envelope, "transport_interrupted", started, timeout, model_error=value)
            if returned_at >= expires:
                # The grace is exclusively for diagnostics, not accepting late proposals.
                raise self._model_unknown(envelope, "generation_budget_expired", started, timeout)
            return value
        finally:
            call_token.stopped.set()
            self.live_operations.discard(identity)

    async def call(self, request, token):
        request = self._freeze(request)
        config = self.store.load_stage_run_config(request.task_run_id)
        inner = request.stage in INNER_STAGES
        role = config.inner_role if inner else config.outer_role
        model = self.inner if inner else self.outer
        if model.role_config != role or request.role_config_ref != role.role_config_ref:
            _fail("stage.model_configuration_changed", Category.POLICY)
        for _ in range(MAX_STAGE_CORRECTIONS + 1):
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
            identity = request.model_request_id
            request = self.prepare(request)
            record = self.store.load_d4_operation(identity)
            result = self.evidence.read("result", identity)
            category = "handoff_summary" if request.stage == "stage_summary" else "planning" if request.stage in {"stage_intent", "stage_roads", "stage_continuation"} else "action" if inner else (
                "fact_review" if isinstance(request.proposal, (StageFactProposal, StageFactBatch, StageConclusionChange)) else "review")
            if record is None:
                task = self.store.load_task_run(request.task_run_id)
                if token.is_cancelled or self.clock() >= task.deadline_at:
                    raise StageRecoveryRequired("stage.dispatch_stopped", category=Category.POLICY)
                contract = self.evidence.read("contract", identity)
                envelope = _envelope(request, contract)
                policy = self.capacity_config.inner_capacity if inner else self.capacity_config.outer_capacity
                storage_error = None
                try:
                    self.evidence.records.encode("stage-request", identity,
                        {"request": asdict(request), "proposal_kind": type(request.proposal).__name__}, limit=1024 * 1024)
                except T6EvidenceCapacityError as error:
                    storage_error = error
                if storage_error is not None or not self.capacity_port.fits(envelope, policy):
                    if storage_error is not None:
                        get_logger("store").info("stage.evidence_compaction_required", category="capacity",
                            record_kind=storage_error.record_kind, record_id=identity,
                            actual_bytes=storage_error.actual_bytes, limit_bytes=storage_error.limit_bytes, dispatched=False)
                    if request.stage != "stage_summary" and request.capacity_recovery_of is None and self.recover_capacity is not None:
                        recovered = await self.recover_capacity(request, token)
                        if recovered is not None:
                            return await self.call(recovered, token)
                    if storage_error is not None:
                        self.evidence.capacity_error(storage_error)
                    _fail("stage.model_capacity_blocked", Category.CAPACITY)
                start = D4OperationStart(identity, request.task_run_id, category, self.clock(),
                    request.base_checkpoint_sequence, digest(asdict(request)),
                    "stage/" + request.context.session.scope + "/" + request.context.session.owner_id,
                    config.budget.budget_policy_ref, role.role, role.role_config_ref)
                self.store.begin_d4_operation(start)
                get_logger("model").info("stage.call_started", run=request.task_run_id,
                    operation=identity, role=role.role, phase=request.stage)
                if request.correction_of_model_request_id is not None:
                    reason = contract["correction"]["reason"]
                    get_logger("model").info("stage.call_retried", run=request.task_run_id, operation=identity,
                        source_operation=request.correction_of_model_request_id, phase=request.stage,
                        category="dependency" if reason == "previous_model_transport_failed_no_usable_decision" else "input",
                        correction_count=self.evidence.correction_depth(request), code=reason)
                # The model's own cap and the extendable TaskRun deadline are
                # separate. The cancellation view enforces the latter live.
                timeout = model_call_timeout(model, float("inf"))
                raw = await self._invoke(model, envelope, timeout, token, task.deadline_at)
                if getattr(token, "superseded_by", None) is not None:
                    provider = self.evidence._provider_result(request) if isinstance(raw, ModelError) else None
                    failure = self.evidence.transport_failure(request) if isinstance(raw, ModelError) else None
                    if isinstance(raw, ModelError) and raw.kind is not ModelErrorKind.CANCELLED and failure is None:
                        raise self._model_unknown(envelope, "branch_cancel_unconfirmed", monotonic(), timeout, model_error=raw)
                    stopped = {"request_sha256": start.context_sha256, "review_operation_id": token.superseded_by,
                        "local_invoke_exited": True, "returned_kind": ("transport_failure" if failure is not None else "cancelled") if isinstance(raw, ModelError) else "response",
                        "provider_result_sha256": digest(provider) if provider is not None else None, "finished_at": self.clock()}
                    self.evidence.save("branch-stop", identity, stopped)
                    self.evidence.branch_stop(request)
                    result = self._superseded_result(request, stopped)
                elif isinstance(raw, ModelError):
                    result = {"request_sha256": start.context_sha256, "outcome": "model_error",
                        "content": None, "finish_reason": "error", "finished_at": self.clock()}
                    provider = self.evidence.transport_failure(request)
                    if provider is not None:
                        result = self._transport_result(request, provider)
                        get_logger("model").warning("stage.model_transport_failed", run=request.task_run_id,
                            operation=identity, phase=request.stage, category="dependency", code=provider["error"]["code"],
                            correction_count=self.evidence.correction_depth(request))
                else:
                    content = raw.content if isinstance(raw, D8ModelResponse) else raw
                    finish = raw.finish_reason if isinstance(raw, D8ModelResponse) else "stop"
                    tool_calls = raw.tool_calls if isinstance(raw, D8ModelResponse) else None
                    diagnostics = {}
                    try:
                        parse(request, D8ModelResponse(content, finish, tool_calls),
                            normalization_policy=contract.get("output_normalization"), diagnostics=diagnostics)
                        outcome = "valid"
                    except ValueError as error:
                        outcome = "invalid"
                        code = "stage.output_envelope_invalid" if error.args == ("stage.output_envelope_invalid",) else "stage.output_invalid"
                        issues = stage_output_issues(request, D8ModelResponse(content, finish, tool_calls))
                        if any(i.code in BUSINESS_ISSUE_CODES for i in issues):
                            code = "stage.proposal_business_invalid"
                        get_logger("model").warning("stage.output_rejected", run=request.task_run_id,
                            operation=identity, phase=request.stage, code=code, category="input",
                            issue_codes=",".join(i.code for i in issues) or "stage_value_invalid")
                    result = {"request_sha256": start.context_sha256, "outcome": outcome,
                        "content": content, "finish_reason": finish, "finished_at": self.clock()}
                    if tool_calls is not None:
                        result["tool_calls"] = tool_calls
                    if outcome == "valid" and diagnostics.get("normalization"):
                        result["normalization"] = diagnostics["normalization"]
                self.evidence.save("result", identity, result)
                if result.get("normalization"):
                    get_logger("model").info("stage.output_normalized", run=request.task_run_id,
                        operation=identity, phase=request.stage, category="input",
                        code="json_trailing_data_trimmed", **result["normalization"])
                record = self.store.load_d4_operation(identity)
            if result is None:
                raise StageRecoveryRequired("stage.model_outcome_unknown")
            self._complete_result(request, record, result)
            if result["outcome"] == "superseded":
                get_logger("model").info("stage.generation_superseded", run=request.task_run_id,
                    operation=identity, phase=request.stage, category="branch_correction")
                raise StageModelSuperseded(identity)
            if result["outcome"] == "valid":
                evidence = self.evidence.read_model_result(self.store.load_d4_operation(identity))
                return identity, evidence.result
            provider = self.evidence.transport_failure(request, result) if result["outcome"] == "model_error" else None
            depth = self.evidence.correction_depth(request)
            if (result["outcome"] != "invalid" and provider is None) or depth == MAX_STAGE_CORRECTIONS:
                if provider is not None:
                    raise StageModelTransportFailure("stage.model_transport_failed", retryable=False, model_call={
                        "stage": request.stage, "operation_id": identity, "reason": "transport_retries_exhausted",
                        "model_error_code": provider["error"]["code"], "correction_count": depth,
                        "provider_result_ref": "stage-provider-result/" + identity,
                        "provider_result_sha256": digest(provider), "diagnostic_status": "bound"})
                get_logger("model").warning("stage.call_rejected", run=request.task_run_id,
                    operation=identity, phase=request.stage, code="stage.model_output_unusable",
                    category="input" if result["outcome"] == "invalid" else "dependency")
                _fail("stage.model_output_unusable", Category.INPUT if result["outcome"] == "invalid" else Category.DEPENDENCY)
            request = replace(request, model_request_id=stable_id(request.task_run_id, "stage/correction/" + identity),
                correction_of_model_request_id=identity, frozen_contract_ref=None)
        raise AssertionError("bounded correction did not terminate")

    def prepare(self, request):
        """Persist an immutable request without registering or dispatching IO."""
        request = self._freeze(request)
        self.evidence.save("request", request.model_request_id, {"request": asdict(request),
            "proposal_kind": type(request.proposal).__name__})
        return self.evidence.request(request.model_request_id)

    def _freeze(self, request):
        contract = self.evidence.contract(request)
        reference = "sha256:" + digest(contract)
        if request.frozen_contract_ref is not None and request.frozen_contract_ref != reference:
            _fail("stage.model_contract_changed", Category.POLICY)
        self.evidence.save("contract", request.model_request_id, contract)
        return replace(request, frozen_contract_ref=reference)
