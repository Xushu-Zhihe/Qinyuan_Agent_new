"""Contract12 choices, deterministic bindings and original-ID result recovery."""

from dataclasses import asdict
import json
import math

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import D8HandoffRequest
from agent_core.operation_accounting import D4OperationStart, D4OperationCompletion
from agent_core.turn_modeling import digest, stable_id


RULE_ID = "attachment_copy_sha256"
RULE_VERSION = 1
KINDS = {"tree_planning": "planning-binding", "fact_proposal": "fact-assertion-binding"}


def planning_policy(request):
    try:
        policy = json.loads(request.prior_summary)["s2_planning_policy"]
        indices = policy["attachment_indices"]
        if (set(policy) != {"rule_id", "rule_version", "attachment_indices", "authorization_sha256", "imports"}
                or policy["rule_id"] != RULE_ID or type(policy["rule_version"]) is not int
                or policy["rule_version"] != RULE_VERSION or indices not in [[0], [0, 1]]
                or any(type(i) is not int for i in indices)
                or not isinstance(policy["imports"], list) or len(policy["imports"]) != len(indices)):
            raise ValueError("policy")
    except (ValueError, TypeError, KeyError) as exc:
        raise D3RecoveryRequired("s2_planning_policy_invalid") from exc
    return policy


def freeze_planning_policy(app):
    task = app.state.task_run.task_run_id
    imports = []
    for index in app.config.authorization.attachment_indices:
        identity = stable_id(task, f"d3/import/{index}")
        receipt = app.imports.read_receipt(identity)
        op = app.store.load_d3_operation(identity)
        if (receipt is None or receipt.task_run_id != task or receipt.attachment_index != index
                or op is None or op.completion is None
                or op.start.category != "attachment_import" or op.start.task_run_id != task
                or op.start.resource_ref != receipt.sandbox_path
                or op.completion.evidence_sha256 != digest(asdict(receipt))):
            raise D3RecoveryRequired("s2_planning_import_unavailable")
        imports.append({"attachment_index": index, "import_id": identity, "receipt_sha256": digest(asdict(receipt))})
    return {"rule_id": RULE_ID, "rule_version": RULE_VERSION,
            "attachment_indices": list(app.config.authorization.attachment_indices),
            "authorization_sha256": digest(asdict(app.config.authorization)), "imports": imports}


def planning_schema(request):
    from agent_core.output_contracts import tagged, obj, array, INTENT, REFS, text
    from agent_core.intent_planning import candidate_limit
    policy = planning_policy(request)
    rule = obj({"rule_id": {"type": "string", "enum": [policy["rule_id"]]},
                "parameters": obj({"attachment_index": {"type": "integer", "enum": policy["attachment_indices"]}})})
    return {"anyOf": [tagged("candidates", {"completion_rule": rule, "intents": array(INTENT, 1, candidate_limit(request))}),
                      tagged("exhausted", {"reason": text(512), "evidence_refs": REFS})]}


def assertion_schema():
    from agent_core.output_contracts import obj
    return obj({"rule_id": {"type": "string", "enum": [RULE_ID]}})


def materialize(request, wire):
    from agent_core.output_contracts import output_schema, decode_response
    from agent_core.intent_execution import parse_working
    if request.stage == "fact_proposal" and parse_working(request, wire) is not None:
        return json.loads(json.dumps(wire, ensure_ascii=False))
    raw, issues, terminal, _ = decode_response(wire, output_schema(request, contract_version=12)[1])
    if issues or terminal:
        raise D3RecoveryRequired("s2_wire_not_validated")
    # Work on a detached JSON value; never mutate the model's chosen references.
    canonical = json.loads(json.dumps(raw, ensure_ascii=False))
    if request.stage == "tree_planning" and raw["kind"] == "candidates":
        canonical["turn_target"] = request.goal
        canonical["completion_rule"]["version"] = planning_policy(request)["rule_version"]
    elif request.stage == "fact_proposal" and raw["kind"] == "candidate" and raw["assertion"] is not None:
        canonical["assertion"].update(rule_version=RULE_VERSION,
            artifact_version_id=request.observation.artifact_version_id,
            observation_id=request.observation.observation_id)
    return canonical


class PlanningAssertionBindings:
    def __init__(self, config, output, handoff, conclusions, store, imports):
        if config.contract_version not in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            raise ValueError("s2_bindings_require_contract12")
        self.config, self.output, self.handoff = config, output, handoff
        self.conclusions, self.store, self.imports = conclusions, store, imports

    def _request(self, stage, identity):
        if stage == "tree_planning":
            raw = self.handoff.read("request", identity)
            request = D8HandoffRequest.from_dict(raw) if raw is not None else None
        else:
            request = self.conclusions.read_request(identity)
        if request is None or request.model_request_id != identity or request.stage != stage:
            raise D3RecoveryRequired("s2_binding_request_missing")
        return request

    def _operation(self, request, finished):
        op = self.store.load_d4_operation(request.model_request_id)
        planning = request.stage == "tree_planning"
        resource = (request.turn_id or request.task_run_id) if planning else request.observation.plan_id
        if (op is None or op.start != D4OperationStart(request.model_request_id, request.task_run_id,
                "planning" if planning else "action", op.start.started_at, request.base_checkpoint_sequence,
                request.context_sha256, resource, self.config.d4_budget.budget_policy_ref,
                "outer_planning" if planning else "inner_action", request.role_config_ref)
                or finished < op.start.started_at):
            raise D3RecoveryRequired("s2_binding_operation_changed")
        return op

    def _base(self, request, finished):
        from agent_core.output_contracts import output_schema, matches_output_contract
        identity = request.model_request_id
        contract = self.output.read("contract", identity)
        role = self.config.outer_role if request.stage == "tree_planning" else self.config.inner_role
        root = request.correction_of_model_request_id or identity
        if (type(finished) not in {int, float} or not math.isfinite(finished)
                or self._request(request.stage, identity) != request
                or not isinstance(contract, dict) or contract.get("contract_version") != self.config.contract_version
                or contract.get("model_request_id") != identity or contract.get("task_run_id") != request.task_run_id
                or contract.get("stage") != request.stage or contract.get("role_config_ref") != role.role_config_ref
                or request.role_config_ref != role.role_config_ref
                or contract.get("source_request_sha256") != digest(asdict(request))
                or contract.get("root_request_id") != root
                or contract.get("correction_of_model_request_id") != request.correction_of_model_request_id
                or not matches_output_contract(contract.get("output_contract"), request.stage, output_schema(request, contract_version=self.config.contract_version)[1], self.config.contract_version)):
            raise D3RecoveryRequired("s2_binding_contract_changed")
        self._operation(request, finished)
        policy = planning_policy(request) if request.stage == "tree_planning" else {
            "rule_id": RULE_ID, "rule_version": RULE_VERSION,
            "observation_sha256": digest(asdict(request.observation)),
            "session_sha256": digest({"session_id": getattr(request, "session_id", None),
                                     "session_context": getattr(request, "session_context", None)})}
        if request.stage == "tree_planning":
            if (policy["authorization_sha256"] != digest(asdict(self.config.authorization))
                    or policy["attachment_indices"] != list(self.config.authorization.attachment_indices)):
                raise D3RecoveryRequired("s2_planning_authorization_changed")
            for index, item in zip(policy["attachment_indices"], policy["imports"], strict=True):
                import_id = stable_id(request.task_run_id, f"d3/import/{index}")
                receipt = self.imports.read_receipt(import_id)
                op = self.store.load_d3_operation(import_id)
                expected = {"attachment_index": index, "import_id": import_id,
                            "receipt_sha256": digest(asdict(receipt)) if receipt else None}
                if (receipt is None or receipt.task_run_id != request.task_run_id
                        or receipt.attachment_index != index or item != expected
                        or op is None or op.completion is None
                        or op.start.category != "attachment_import" or op.start.task_run_id != request.task_run_id
                        or op.start.resource_ref != receipt.sandbox_path
                        or op.completion.evidence_sha256 != expected["receipt_sha256"]):
                    raise D3RecoveryRequired("s2_planning_import_changed")
        if root != identity:
            original = self._request(request.stage, root)
            excluded = {"model_request_id", "correction_of_model_request_id", "rejection_code"}
            if request.stage == "fact_proposal": excluded.add("base_checkpoint_sequence")
            if (original.correction_of_model_request_id is not None
                    or {k: v for k, v in asdict(original).items() if k not in excluded}
                    != {k: v for k, v in asdict(request).items() if k not in excluded}
                    or identity != stable_id(request.task_run_id,
                        f"{'d8-2' if request.stage == 'tree_planning' else 'd8'}/correction/{root}")):
                raise D3RecoveryRequired("s2_binding_root_changed")
            root_contract = self.output.read("contract", root)
            if (not isinstance(root_contract, dict) or root_contract.get("source_request_sha256") != digest(asdict(original))
                    or root_contract.get("contract_version") != self.config.contract_version or root_contract.get("root_request_id") != root):
                raise D3RecoveryRequired("s2_binding_root_contract_changed")
            self._base(original, finished)
            from types import SimpleNamespace
            from agent_core.structured_output import StructuredOutput
            from agent_core.business_diagnostics import BusinessDiagnostics
            from agent_core.fact_diagnostics import FactBusinessDiagnostics
            boundary = StructuredOutput(SimpleNamespace(config=self.config,
                business_diagnostics=BusinessDiagnostics(self.config, self.output, self.handoff, self.store),
                fact_business_diagnostics=FactBusinessDiagnostics(self.config, self.output, self.conclusions, self.store)), self.output)
            if contract.get("correction") != boundary._error(root):
                raise D3RecoveryRequired("s2_binding_correction_changed")
        return {"binding_version": 1, "contract_version": self.config.contract_version, "stage": request.stage,
                "task_run_id": request.task_run_id, "model_request_id": identity,
                "root_request_id": root, "role_config_ref": request.role_config_ref,
                "base_checkpoint_sequence": request.base_checkpoint_sequence,
                "source_request_sha256": digest(asdict(request)), "context_sha256": request.context_sha256,
                "contract_sha256": digest(contract), "policy": policy, "finished_at": finished}

    def save(self, request, wire, result):
        if result["outcome"] != "valid" or result["raw"] != materialize(request, wire):
            raise D3RecoveryRequired("s2_binding_result_not_valid")
        record = {**self._base(request, result["finished_at"]), "wire_response": wire,
                  "canonical_response": result["raw"], "canonical_sha256": digest(result["raw"]),
                  "rejected_candidate_indices": result.get("rejected_candidate_indices", [])}
        self.output.save(KINDS[request.stage], request.model_request_id, record)
        return self.result(request, record)

    @staticmethod
    def result(request, record):
        result = {"model_request_id": request.model_request_id, "request_sha256": request.context_sha256,
                  "outcome": "valid", "raw": record["canonical_response"], "finished_at": record["finished_at"],
                  "assembly_sha256": digest(record)}
        if request.stage == "tree_planning":
            result.update(reason=None, rejected_candidate_indices=record["rejected_candidate_indices"])
        return result

    def read(self, request):
        record = self.output.read(KINDS[request.stage], request.model_request_id)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise D3RecoveryRequired("s2_binding_invalid")
        canonical = materialize(request, record.get("wire_response"))
        indices = record.get("rejected_candidate_indices")
        if (not isinstance(indices, list) or any(type(i) is not int or i < 0
                or self.config.contract_version != 22 and i not in {0, 1} for i in indices)
                or len(indices) != len(set(indices))
                or request.stage == "fact_proposal" and indices):
            raise D3RecoveryRequired("s2_binding_indices_invalid")
        expected = {**self._base(request, record.get("finished_at")), "wire_response": record["wire_response"],
                    "canonical_response": canonical, "canonical_sha256": digest(canonical),
                    "rejected_candidate_indices": indices}
        if (record != expected or any(self.output.read(k, request.model_request_id) is not None
                for k in ("diagnostic", "business-diagnostic"))):
            raise D3RecoveryRequired("s2_binding_changed_or_diagnostic_conflict")
        return record

    def verify_result(self, request, result):
        record = self.read(request)
        if record is None:
            if result["outcome"] == "valid" or "assembly_sha256" in result:
                raise D3RecoveryRequired("s2_binding_required")
        elif result != self.result(request, record):
            raise D3RecoveryRequired("s2_bound_result_changed")
        if record is not None and request.stage == "fact_proposal":
            from agent_core.conclusion_contracts import parse_d8_proposal
            try:
                from agent_core.intent_execution import parse_working
                parse_working(request, record["canonical_response"]) or parse_d8_proposal(record["canonical_response"])
            except (ValueError, TypeError, KeyError) as exc:
                raise D3RecoveryRequired("s2_saved_fact_business_invalid") from exc
        if record is not None and request.stage == "tree_planning":
            from agent_core.handoff_contracts import checked_refs
            from agent_core.dual_llm_modeling import parse_d3_turn_proposal
            raw = record["canonical_response"]
            try:
                if raw["kind"] == "exhausted":
                    if not raw["reason"].strip() or record["rejected_candidate_indices"]:
                        raise ValueError("exhaustion")
                    checked_refs(raw["evidence_refs"], {i.ref for i in request.history})
                else:
                    kept = []
                    if any(i >= len(raw["intents"]) for i in record["rejected_candidate_indices"]):
                        raise ValueError("indices")
                    for index, candidate in enumerate(raw["intents"]):
                        if index not in record["rejected_candidate_indices"]:
                            checked_refs(candidate["dependency_refs"], {i.ref for i in request.history})
                            kept.append({k: candidate[k] for k in ("description", "intent_type")})
                    parse_d3_turn_proposal({"turn_target": raw["turn_target"],
                                           "completion_rule": raw["completion_rule"], "intents": kept},
                                          maximum=None)
            except (ValueError, TypeError, KeyError) as exc:
                raise D3RecoveryRequired("s2_saved_planning_business_invalid") from exc
        return record

    def known_result(self, request):
        record = self.read(request)
        if record is None:
            return None
        expected = self.result(request, record)
        self.verify_result(request, expected)
        op = self._operation(request, record["finished_at"])
        if op.completion is not None and op.completion != D4OperationCompletion(request.model_request_id,
                expected["finished_at"], "completed", request.model_request_id, digest(expected)):
            raise D3RecoveryRequired("s2_binding_completion_changed")
        return expected

    def check_task(self, task):
        needs_repair = False
        for op in self.store.list_d4_operations(task):
            identity = op.start.operation_id
            contract = self.output.read("contract", identity)
            present = [s for s, k in KINDS.items() if self.output.read(k, identity) is not None]
            planning_request = self.handoff.read("request", identity)
            fact_request = self.conclusions.read_request(identity)
            source_stage = (planning_request.get("stage") if planning_request is not None
                            else fact_request.stage if fact_request is not None else None)
            stage = contract.get("stage") if isinstance(contract, dict) else source_stage
            if source_stage in KINDS and stage != source_stage:
                raise D3RecoveryRequired("s2_binding_contract_stage_changed")
            if present and (len(present) != 1 or present[0] != stage):
                raise D3RecoveryRequired("s2_binding_stage_changed")
            if stage not in KINDS:
                continue
            request = self._request(stage, identity)
            result = (self.handoff.read("result", identity) if stage == "tree_planning"
                      else self.conclusions.read_result(identity))
            expected = self.known_result(request) if result is None else result
            if expected is not None:
                self.verify_result(request, expected)
                if op.completion is not None and op.completion != D4OperationCompletion(identity, expected["finished_at"],
                        "completed" if expected["outcome"] == "valid" else "rejected", identity, digest(expected)):
                    raise D3RecoveryRequired("s2_binding_completion_changed")
                needs_repair |= result is None or op.completion is None
            elif op.completion is not None:
                raise D3RecoveryRequired("s2_completed_result_missing")
        return needs_repair
