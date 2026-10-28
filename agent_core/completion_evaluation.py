"""Contract14 explicit support choices and source-bound local observations."""

from agent_core.http_contracts import plan_turn_binding
from dataclasses import asdict
import hashlib
import json

from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.turn_modeling import digest
from agent_core.operation_accounting import D4OperationStart, D4OperationCompletion


def completion_schema(request, *, maximum=2):
    from agent_core.output_contracts import obj, tagged, array, B, SUGGESTION, CHANGE
    allowed = list(request.context.supporting_fact_ids)
    if len(allowed) != 1 or len(set(allowed)) != 1:
        raise ExecutionRecoveryRequired("s4_completion_support_unavailable")
    return {"anyOf": [obj({"action": tagged("complete_intent", {
        "fact_ids": {**array({"type": "string", "enum": allowed}, 1, 1), "uniqueItems": True}}),
        "review_requested": B, "next_intent_candidates": array(SUGGESTION, 0, maximum)}), CHANGE]}


def execution_projection(app, execution, binding):
    if getattr(binding,'kind',None)=='http_request':
        return app.web.execution_projection(execution, binding)
    """Never read Sandbox or project arbitrary output; recheck saved size evidence."""
    content = {"kind": "execution", "status": execution.status.value,
               "step_index": execution.step_index}
    version = {"execution": asdict(execution)}
    if (binding is None or binding.template != "size_then_sha256"
            or execution.step_index != 0 or execution.status.value != "succeeded"):
        return content, version
    from agent_core.artifact_operations import fixed_size_script, parse_size_output
    observed = app.workspace_evidence.read_size(execution.execution_id)
    completed = app.commands.read_completed(execution.execution_id)
    turn = plan_turn_binding(app.store,binding)
    artifact = app.workspace.read_artifact(binding.artifact_version_id)
    command = app.commands.read_command(execution.command_ref)
    if observed is None or completed is None or command is None or turn is None:
        raise ExecutionRecoveryRequired("s4_size_source_unavailable")
    record = completed.record
    status, size = parse_size_output(record.stdout)
    if (binding.task_run_id != app.state.task_run.task_run_id
            or turn.task_run_id != binding.task_run_id or turn.turn_id != binding.turn_id
            or artifact.claim.task_run_id != binding.task_run_id
            or artifact.claim.turn_id != binding.turn_id or artifact.claim.intent_id != binding.intent_id
            or artifact.claim.plan_id != binding.plan_id or artifact.claim.import_id != turn.import_id
            or artifact.claim.attachment_index != turn.attachment_index
            or artifact.claim.expected_byte_size != turn.expected_byte_size
            or artifact.claim.expected_sha256 != turn.expected_sha256
            or artifact.claim.sandbox_id != app.state.task_run.sandbox_id
            or observed.task_run_id != binding.task_run_id or observed.plan_id != binding.plan_id
            or observed.artifact_version_id != binding.artifact_version_id
            or observed.execution_id != execution.execution_id
            or execution.plan_id != binding.plan_id
            or observed.expected_byte_size != turn.expected_byte_size
            or observed.observed_status != status or observed.observed_byte_size != size
            or observed.output_sha256 != hashlib.sha256(record.stdout).hexdigest()
            or observed.status != "confirmed" or status != "present"
            or record.request_id != execution.execution_id or record.task_run_id != binding.task_run_id
            or record.sandbox_id != app.state.task_run.sandbox_id
            or record.plan_id != binding.plan_id or record.step_index != 0
            or record.command_ref != execution.command_ref
            or record.status != "completed" or record.exit_code != 0
            or record.stdout_truncated or not record.termination_confirmed
            or command.request_id != execution.execution_id or command.task_run_id != binding.task_run_id
            or command.sandbox_id != record.sandbox_id
            or command.argv != ("bash", "-c", fixed_size_script(artifact.claim.path))
            or command.authorization_id != app.config.authorization.authorization_id
            or record.authorization_id != command.authorization_id
            or command.stdin_size != 0 or command.stdin_sha256 != hashlib.sha256(b'').hexdigest()
            or command.working_directory != artifact.claim.path.rsplit("/", 1)[0]):
        raise ExecutionRecoveryRequired("s4_size_source_binding_changed")
    content["observation"] = {"kind": "file_size", "byte_size": size,
        "expected_byte_size": observed.expected_byte_size, "comparison": observed.status,
        "identity": "saved_execution_observation_not_verified_fact",
        "scope": "saved_copy_version_only", "artifact_version_id": binding.artifact_version_id}
    record_metadata = {k: v for k, v in asdict(record).items() if k not in {"stdout", "stderr"}}
    record_metadata.update(stdout_sha256=hashlib.sha256(record.stdout).hexdigest(),
                           stderr_sha256=hashlib.sha256(record.stderr).hexdigest())
    version.update(size=asdict(observed), command=asdict(command), completed_sha256=digest(record_metadata))
    return content, version


def freeze_evaluation_policy(app, intent_id, support_fact_id, history):
    intent = next(i for i in app.state.intents if i.intent_id == intent_id)
    plans = [p for p in app.state.plans if p.intent_id == intent_id]
    plan = plans[-1] if plans else None
    binding = app.store.load_d3_plan_binding(plan.plan_id) if plan else None
    if getattr(binding,'kind',None)=='http_request':
        from agent_core.http_policy import evaluation
        return evaluation(app,intent_id,support_fact_id,history)
    result = app.store.load_d3_plan_result(plan.plan_id) if plan else None
    historical = (app.config.contract_version == 22 and result is not None and result.outcome == "no_new_information")
    if (plan is None or binding is None or result is None or plan.status.value != "completed"
            or not historical and (result.outcome != "satisfied" or result.supporting_fact_id != support_fact_id)
            or binding.intent_id != intent_id or binding.turn_id != intent.turn_id):
        raise ExecutionRecoveryRequired("s4_evaluation_plan_unavailable")
    app.validator.completion_support(app.state, binding, result, support_fact_id)
    observed = app.conclusions.observation(result.proof_id, historical=True)
    support = next((i for i in history if i.record_id == support_fact_id), None)
    goal = next((i for i in history if i.record_id == intent_id), None)
    execution = next((i for i in history if i.record_id == observed.execution_id), None)
    if support is None or goal is None or execution is None:
        raise ExecutionRecoveryRequired("s4_evaluation_required_sources_missing")
    size_items = [i for i in history if i.intent_id == intent_id and i.record_type == "progress"
                  and i.source_status == "available" and i.artifact_version_id == observed.artifact_version_id
                  and isinstance(json.loads(i.content), dict)
                  and json.loads(i.content).get("observation", {}).get("kind") == "file_size"]
    return {"version": 1, "intent_id": intent_id, "local_goal": app.redactor.redact(intent.description),
        "plan_id": plan.plan_id, "template": binding.template, "observation_mode": binding.observation_mode,
        "capability_scope": "saved_authorized_copy_size_and_sha256_only_not_whole_task_verification",
        "goal_interpretation": (
            "local_goal states what this intent must establish, not how to establish "
            "it. The executed plan may use a different method or template than any "
            "wording in local_goal; judge whether the saved observation establishes or "
            "rules out the goal, not whether the execution matches that wording. Use "
            "inconclusive only when the evidence can neither establish nor rule out the "
            "goal."
        ),
        "goal_ref": goal.ref, "support_fact_ref": support.ref,
        "required_observation_refs": [execution.ref, *(i.ref for i in size_items)],
        "sha256_observation": {"proof_id": observed.proof_id, "execution_ref": execution.ref,
            "artifact_version_id": observed.artifact_version_id, "observation_id": observed.observation_id,
            "observed_sha256": observed.observed_sha256, "scope": observed.observation_scope,
            "source_version_sha256": observed.source_version_sha256},
        "history_sha256": digest([asdict(i) for i in history])}


def evaluation_policy(request):
    try:
        policy = json.loads(request.prior_summary)["s4_evaluation_policy"]
        if policy.get("version") in {2, 3}:
            from agent_core.intent_execution import evaluation_policy as rolling_policy
            return rolling_policy(request, policy)
        by_ref = {i.ref: i for i in request.history}
        required = [policy["goal_ref"], policy["support_fact_ref"], *policy["required_observation_refs"]]
        if (policy["version"] != 1 or type(policy["version"]) is not int
                or policy["intent_id"] != request.intent_id
                or policy["history_sha256"] != digest([asdict(i) for i in request.history])
                or not required or any(ref not in by_ref for ref in required)
                or by_ref[policy["goal_ref"]].record_id != request.intent_id
                or json.loads(by_ref[policy["goal_ref"]].content)['goal'] != policy['local_goal']
                or by_ref[policy["support_fact_ref"]].record_id != request.support_fact_id
                or any(by_ref[ref].source_status != "available" or by_ref[ref].conflict for ref in required)):
            raise ValueError("binding")
    except (TypeError, ValueError, KeyError) as exc:
        raise ExecutionRecoveryRequired("s4_evaluation_policy_invalid") from exc
    return policy


def evaluation_schema(request):
    from agent_core.output_contracts import obj, array, text
    policy = evaluation_policy(request)
    if policy.get("version") in {2, 3}:
        from agent_core.intent_execution import evaluation_schema as rolling_schema
        return rolling_schema(request, policy)
    refs = [i.ref for i in request.history if i.source_status == "available" and not i.conflict]
    fields = {"reason": text(), "evidence_refs": {**array({"type": "string", "enum": refs}, 1),
                                                        "uniqueItems": True}}
    return {"anyOf": [obj({"decision": {"type": "string", "const": decision},
        **fields, "evidence_refs": {**fields["evidence_refs"], **(
            {"contains": {"type": "string", "const": policy["support_fact_ref"]}} if decision == "succeeded" else {})}})
        for decision in ("succeeded", "failed", "inconclusive")]}


def candidate_schema(request, *, allow_none=False):
    from agent_core.output_contracts import obj, tagged, text
    from agent_core.competition_candidates import MCPAnswerCandidateContext
    choice = {"source_ref": {"type":"string","enum":[request.context.source_ref]}} if isinstance(request.context,MCPAnswerCandidateContext) else {"execution_id":{"type":"string","enum":[request.context.execution_id]}}
    candidate = tagged("candidate_flag", {"value": text(), **choice})
    return obj({"action": {"anyOf": [candidate, tagged("no_candidate", {})]}
                if allow_none else candidate})


def materialize_candidate(request, wire, *, allow_none=False):
    from agent_core.output_contracts import decode_response
    value, issues, terminal, _ = decode_response(wire, candidate_schema(request, allow_none=allow_none))
    if issues or terminal:
        from agent_core.dual_llm_modeling import ExecutionProposalRejected
        raise ExecutionProposalRejected("s4_candidate_choice_invalid", structural=True)
    if allow_none and value["action"]["kind"] == "no_candidate":
        return value
    if allow_none:
        body = value['action']['value']
        if not body or '\x00' in body or len(body.encode('utf-8')) > 16 * 1024:
            from agent_core.dual_llm_modeling import ExecutionProposalRejected
            raise ExecutionProposalRejected('d9_candidate_value_invalid', structural=True)
    from agent_core.competition_candidates import provenance
    source=provenance(request.context)
    return {"action": {**value["action"], "execution_id": source["execution_id"], "evidence_version": source["evidence_version"],
                       "source_ref": source["source_ref"]},
            "review_requested": False}


def candidate_binding(output, request, candidate, body_sha256, byte_size):
    """The candidate value is deliberately absent, including from canonical data."""
    from agent_core.output_contracts import output_schema, matches_output_contract
    contract = output.read("contract", request.model_request_id)
    if (not isinstance(body_sha256, str) or len(body_sha256) != 64
            or any(c not in '0123456789abcdef' for c in body_sha256)
            or type(byte_size) is not int or not 1 <= byte_size <= 16 * 1024
            or request.correction_of_model_request_id is not None or not isinstance(contract, dict)
            or contract.get("contract_version") not in {14, 15, 16, 17, 18, 19, 20, 21, 22}
            or contract.get("source_request_sha256") != digest(asdict(request))
            or contract.get("model_request_id") != request.model_request_id
            or contract.get("root_request_id") != request.model_request_id
            or contract.get("task_run_id") != request.task_run_id
            or contract.get("role_config_ref") != request.role_config_ref
            or not matches_output_contract(contract.get("output_contract"), "inner_candidate", output_schema(request, contract_version=contract['contract_version'])[1], contract['contract_version'])):
        raise ExecutionRecoveryRequired("s4_candidate_contract_changed")
    return {"binding_version": 1, "contract_version": contract['contract_version'],
            "task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
            "source_request_sha256": digest(asdict(request)), "contract_sha256": digest(contract),
            "choice": {"source_ref": candidate.source_ref} if candidate.source_kind == "mcp" else {"execution_id": request.context.execution_id},
            "canonical_provenance": candidate.to_dict(), "review_requested": False,
            "body_sha256": body_sha256, "byte_size": byte_size}


def verify_candidate_binding(output, request, candidate, metadata):
    expected = candidate_binding(output, request, candidate, metadata.get("body_sha256"), metadata.get("byte_size"))
    record = output.read("candidate-binding", request.model_request_id)
    if record != expected:
        raise ExecutionRecoveryRequired("s4_candidate_binding_missing_or_changed")
    return digest(record)


def check_candidate_task(config, output, sessions, models, store, task):
    """Read only: validate ordinary provenance without decrypting candidate text."""
    from agent_core.dual_llm_contracts import D3TurnBinding, D3PlanBinding, D3PlanResult, D3OperationStart
    from agent_core.dual_llm_modeling import D3CandidateContext, D8SessionExecutionRequest
    from agent_core.goal_candidates import D7AnswerCandidate
    needs_repair = False
    for op in store.list_d3_operations(task):
        identity = op.start.operation_id
        contract = output.read("contract", identity)
        if not isinstance(contract, dict) or contract.get("stage") != "inner_candidate":
            continue
        if contract.get('contract_version') != config.contract_version:
            raise ExecutionRecoveryRequired('s4_candidate_contract_changed')
        record = output.read("candidate-binding", identity)
        native = models.read(identity)
        if (config.contract_version in {19, 20, 21, 22} and native is not None and native.outcome == 'accepted'
                and native.raw == {'action': {'kind': 'no_candidate'}}):
            if record is not None:
                raise ExecutionRecoveryRequired('d9_candidate_result_conflict')
            from agent_core.answer_candidates import check_no_candidate
            needs_repair |= check_no_candidate(config, output, sessions, native, op)
            continue
        if record is None:
            if native is not None and native.outcome == "accepted":
                raise ExecutionRecoveryRequired("s4_candidate_binding_missing_or_changed")
            continue
        if not isinstance(record, dict):
            raise ExecutionRecoveryRequired('s4_candidate_binding_missing_or_changed')
        frozen = sessions.read("request", identity)
        if not isinstance(frozen, dict):
            raise ExecutionRecoveryRequired("s4_candidate_frozen_request_missing")
        try:
            value = dict(frozen["request"])
            context = dict(value["context"])
            from agent_core.competition_candidates import restore_context
            value["context"] = restore_context(context)
            value["process_facts"] = tuple(value["process_facts"])
            request = D8SessionExecutionRequest(**value)
            candidate = D7AnswerCandidate.from_dict(record["canonical_provenance"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ExecutionRecoveryRequired("s4_candidate_frozen_request_invalid") from exc
        from agent_core.competition_candidates import provenance,validate_mcp_source
        if candidate.source_kind == "mcp":
            try:validate_mcp_source(store,candidate,output)
            except ValueError as exc:raise ExecutionRecoveryRequired("mcp_candidate_source_changed") from exc
        if (request.task_run_id != task or request.model_request_id != identity
                or candidate.task_run_id != task
                or any(getattr(candidate,key) != val for key,val in provenance(request.context).items())
                or op.start != D3OperationStart(identity, task, 'inner_model', op.start.started_at,
                    request.base_checkpoint_sequence, request.context_sha256, request.resource_ref,
                    'inner_action', config.inner_role.role_config_ref)
                or op.start.context_sha256 != request.context_sha256
                or op.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                or op.start.resource_ref != request.resource_ref):
            raise ExecutionRecoveryRequired("s4_candidate_source_changed")
        checksum = verify_candidate_binding(output, request, candidate, record)
        if config.contract_version in {19, 20, 21, 22}:
            decision = output.read('answer-decision', identity)
            expected = {'version': 1, 'task_run_id': task, 'intent_id': candidate.intent_id,
                        'model_request_id': identity, 'request_sha256': digest(asdict(request)),
                        'result_sha256': digest(asdict(native)) if native is not None else None,
                        'candidate': asdict(candidate)}
            if decision is not None and decision != expected:
                raise ExecutionRecoveryRequired('d9_candidate_decision_changed')
            needs_repair |= decision is None or op.application_ref is None
            if op.application_ref is not None and op.application_ref != candidate.candidate_id:
                raise ExecutionRecoveryRequired('d9_candidate_application_changed')
        if native is not None and (native.outcome != "accepted" or not isinstance(native.raw, dict)
                or native.model_request_id != identity or native.task_run_id != task
                or native.stage != 'inner_candidate' or native.role_config_ref != request.role_config_ref
                or native.context_sha256 != request.context_sha256
                or native.base_checkpoint_sequence != request.base_checkpoint_sequence
                or native.raw.get("candidate_binding_sha256") != checksum
                or native.raw.get("body_sha256") != record["body_sha256"]
                or native.raw.get("byte_size") != record["byte_size"]
                or op.completion is not None and op.completion.evidence_sha256 != digest(asdict(native))):
            raise ExecutionRecoveryRequired("s4_candidate_result_changed")
        needs_repair |= native is None or op.completion is None
    return needs_repair


class EvaluationBindings:
    """Dispatch-thread persistence; safe correction reads use frozen files only."""

    def __init__(self, config, output, handoff, store):
        self.config, self.output, self.handoff, self.store = config, output, handoff, store

    def _request(self, identity):
        from agent_core.handoff_contracts import D8HandoffRequest
        raw = self.handoff.read("request", identity)
        if raw is None:
            raise ExecutionRecoveryRequired("s4_evaluation_request_missing")
        try:
            request = D8HandoffRequest.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired('s4_evaluation_request_changed') from exc
        if request.model_request_id != identity or request.stage != "intent_evaluation":
            raise ExecutionRecoveryRequired("s4_evaluation_request_changed")
        evaluation_policy(request)
        return request

    def _base(self, request, finished):
        import math
        from agent_core.output_contracts import output_schema, matches_output_contract
        contract = self.output.read("contract", request.model_request_id)
        root = request.correction_of_model_request_id or request.model_request_id
        if (self._request(request.model_request_id) != request
                or not isinstance(contract, dict) or self.config.contract_version not in {14, 15, 16, 17, 18, 19, 20, 21, 22}
                or contract.get("contract_version") != self.config.contract_version
                or contract.get("model_request_id") != request.model_request_id
                or contract.get("task_run_id") != request.task_run_id
                or contract.get("root_request_id") != root
                or contract.get("role_config_ref") != self.config.outer_role.role_config_ref
                or request.role_config_ref != self.config.outer_role.role_config_ref
                or contract.get("source_request_sha256") != digest(asdict(request))
                or not matches_output_contract(contract.get("output_contract"), request.stage, output_schema(request, contract_version=14)[1], contract["contract_version"])
                or type(finished) not in {int, float} or not math.isfinite(finished)):
            raise ExecutionRecoveryRequired("s4_evaluation_contract_changed")
        if root != request.model_request_id:
            original = self._request(root)
            excluded = {"model_request_id", "correction_of_model_request_id", "rejection_code"}
            if (original.correction_of_model_request_id is not None
                    or {k: v for k, v in asdict(original).items() if k not in excluded}
                    != {k: v for k, v in asdict(request).items() if k not in excluded}):
                raise ExecutionRecoveryRequired("s4_evaluation_root_changed")
            root_contract = self.output.read('contract', root)
            if (not isinstance(root_contract, dict) or root_contract.get('contract_version') != self.config.contract_version
                    or root_contract.get('source_request_sha256') != digest(asdict(original))
                    or root_contract.get('role_config_ref') != original.role_config_ref
                    or root_contract.get('root_request_id') != root):
                raise ExecutionRecoveryRequired('s4_evaluation_root_changed')
        return {"binding_version": 1, "contract_version": self.config.contract_version, "stage": request.stage,
                "task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
                "root_request_id": root, "source_request_sha256": digest(asdict(request)),
                "contract_sha256": digest(contract), "policy_sha256": digest(evaluation_policy(request)),
                "finished_at": finished}

    def _operation(self, request, finished):
        op = self.store.load_d4_operation(request.model_request_id)
        if (op is None or op.start != D4OperationStart(request.model_request_id, request.task_run_id,
                "review", op.start.started_at, request.base_checkpoint_sequence, request.context_sha256,
                request.turn_id or request.task_run_id, self.config.d4_budget.budget_policy_ref,
                "outer_planning", request.role_config_ref) or finished < op.start.started_at):
            raise ExecutionRecoveryRequired("s4_evaluation_operation_changed")
        return op

    def save(self, request, result):
        record = {**self._base(request, result["finished_at"]), "result": result}
        self._operation(request, result["finished_at"])
        self.output.save("evaluation-binding", request.model_request_id, record)
        return {**result, "evaluation_binding_sha256": digest(record)}

    @staticmethod
    def _check_result(request, result):
        required = {"model_request_id", "request_sha256", "outcome", "raw", "reason",
                    "rejected_candidate_indices", "finished_at"}
        if (not isinstance(result, dict) or set(result) != required
                or result["model_request_id"] != request.model_request_id
                or result["request_sha256"] != request.context_sha256
                or result["rejected_candidate_indices"] != []
                or result["outcome"] not in {"valid", "invalid"}
                or result["reason"] != (None if result["outcome"] == "valid" else "d8_output_contract_rejected")):
            raise ExecutionRecoveryRequired("s4_evaluation_result_binding_changed")

    def save_diagnostic(self, request, result, issue_code=None):
        issues = [{"code": "evaluation_business_rejected", "path": "$",
                   "expected": "valid_local_goal_decision", "actual": "rejected"}]
        version = 1
        if evaluation_policy(request).get("version") == 3:
            from agent_core.intent_execution import EVALUATION_ISSUES
            code = issue_code if issue_code in EVALUATION_ISSUES else "d22_resolution_review_invalid"
            path, expected = EVALUATION_ISSUES[code]
            issues = [{"code": code, "path": path, "expected": expected, "actual": "rejected"}]
            version = 2
        record = {**self._base(request, result["finished_at"]), "diagnostic_version": version,
                  "issues": issues, "result": result}
        self._operation(request, result["finished_at"])
        self.output.save("evaluation-diagnostic", request.model_request_id, record)
        return {**result, "evaluation_diagnostic_sha256": digest(record)}

    def read(self, identity, *, check_operation=False):
        record = self.output.read("evaluation-diagnostic", identity)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ExecutionRecoveryRequired('s4_evaluation_diagnostic_changed')
        request = self._request(identity)
        result = record.get("result")
        self._check_result(request, result)
        issues = [{"code": "evaluation_business_rejected", "path": "$",
                   "expected": "valid_local_goal_decision", "actual": "rejected"}]
        version = record.get("diagnostic_version")
        if version == 2 and evaluation_policy(request).get("version") == 3:
            from agent_core.intent_execution import EVALUATION_ISSUES
            saved_issues = record.get("issues", [])
            code = saved_issues[0].get("code") if len(saved_issues) == 1 else None
            if code not in EVALUATION_ISSUES:
                raise ExecutionRecoveryRequired("s4_evaluation_diagnostic_changed")
            path, expected = EVALUATION_ISSUES[code]
            issues = [{"code": code, "path": path, "expected": expected, "actual": "rejected"}]
        elif version != 1:
            raise ExecutionRecoveryRequired("s4_evaluation_diagnostic_changed")
        if (not isinstance(result, dict) or result.get("outcome") != "invalid" or result.get("raw") is not None
                or self.output.read("evaluation-binding", identity) is not None
                or self.output.read("diagnostic", identity) is not None
                or record != {**self._base(request, result.get("finished_at")), "diagnostic_version": version,
                              "issues": issues, "result": result}):
            raise ExecutionRecoveryRequired("s4_evaluation_diagnostic_changed")
        if check_operation:
            self._operation(request, result["finished_at"])
        return record

    def known_result(self, request):
        from agent_core.output_contracts import decode_response
        identity = request.model_request_id
        record = self.output.read("evaluation-binding", identity)
        if record is not None and not isinstance(record, dict):
            raise ExecutionRecoveryRequired('s4_evaluation_binding_changed')
        diagnostic = self.read(identity, check_operation=True)
        if record is None:
            if diagnostic is None:
                return None
            expected = {**diagnostic["result"], "evaluation_diagnostic_sha256": digest(diagnostic)}
            self._check_completion(request, expected)
            return expected
        result = record.get("result")
        self._check_result(request, result)
        if (not isinstance(result, dict) or result.get("outcome") != "valid"
                or record != {**self._base(request, result.get("finished_at")), "result": result}
                or diagnostic is not None or self.output.read("diagnostic", identity) is not None):
            raise ExecutionRecoveryRequired("s4_evaluation_binding_changed")
        _, issues, terminal, _ = decode_response(result.get("raw"), evaluation_schema(request))
        if issues or terminal or not result["raw"]["reason"].strip():
            raise ExecutionRecoveryRequired("s4_evaluation_saved_decision_invalid")
        self._operation(request, result["finished_at"])
        expected = {**result, "evaluation_binding_sha256": digest(record)}
        self._check_completion(request, expected)
        return expected

    def _check_completion(self, request, result):
        op = self._operation(request, result["finished_at"])
        if request.correction_of_model_request_id is not None:
            root = self._request(request.correction_of_model_request_id)
            root_result = self.handoff.read('result', root.model_request_id)
            if (not isinstance(root_result, dict) or root_result.get('outcome') != 'invalid'
                    or root_result.get('raw') is not None
                    or root_result.get('request_sha256') != root.context_sha256):
                raise ExecutionRecoveryRequired('s4_evaluation_root_result_changed')
            root_op = self._operation(root, root_result['finished_at'])
            if root_op.completion != D4OperationCompletion(root.model_request_id, root_result['finished_at'],
                    'rejected', root.model_request_id, digest(root_result)):
                raise ExecutionRecoveryRequired('s4_evaluation_root_completion_changed')
        if op.completion is not None and op.completion != D4OperationCompletion(
                request.model_request_id, result["finished_at"],
                "completed" if result["outcome"] == "valid" else "rejected",
                request.model_request_id, digest(result)):
            raise ExecutionRecoveryRequired("s4_evaluation_completion_changed")

    def verify_result(self, request, result):
        known = self.known_result(request)
        if known is not None and known != result:
            raise ExecutionRecoveryRequired("s4_evaluation_result_changed")
        if known is None and (result["outcome"] == "valid" or "evaluation_binding_sha256" in result
                              or "evaluation_diagnostic_sha256" in result):
            raise ExecutionRecoveryRequired("s4_evaluation_binding_missing")
        return known

    def check_task(self, task):
        needs_repair = False
        for op in self.store.list_d4_operations(task):
            contract = self.output.read("contract", op.start.operation_id)
            raw_request = self.handoff.read('request', op.start.operation_id)
            if not isinstance(raw_request, dict) or raw_request.get('stage') != 'intent_evaluation':
                continue
            if not isinstance(contract, dict) or contract.get('stage') != 'intent_evaluation':
                raise ExecutionRecoveryRequired('s4_evaluation_contract_changed')
            request = self._request(op.start.operation_id)
            result = self.handoff.read("result", request.model_request_id)
            known = self.known_result(request)
            if result is not None:
                self.verify_result(request, result)
                if op.completion is not None and op.completion != D4OperationCompletion(
                        request.model_request_id, result["finished_at"],
                        "completed" if result["outcome"] == "valid" else "rejected",
                        request.model_request_id, digest(result)):
                    raise ExecutionRecoveryRequired("s4_evaluation_completion_changed")
            needs_repair |= result is None and known is not None or op.completion is None
        return needs_repair

    def projection(self, operations):
        rows = []
        for op in operations:
            record = self.read(op.start.operation_id, check_operation=True)
            if record is not None:
                rows.append({"diagnostic_version": record["diagnostic_version"], "stage": "intent_evaluation",
                    "model_request_id": record["model_request_id"], "root_request_id": record["root_request_id"],
                    "issues": record["issues"], "diagnostic_sha256": digest(record)})
        return rows
