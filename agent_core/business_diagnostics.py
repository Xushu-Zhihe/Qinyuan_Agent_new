"""Contracts7/8 tree-planning rejection diagnostics, without rejected values."""

from dataclasses import asdict
import math
from types import SimpleNamespace

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import D8HandoffRequest
from agent_core.turn_modeling import digest


ISSUES = {
    "planning_shape_invalid": ("$", "valid_business_proposal", "invalid"),
    "planning_kind_invalid": ("$/kind", "registered_kind", "invalid"),
    "planning_target_invalid": ("$/turn_target", "bounded_nonempty_text", "invalid"),
    "planning_target_mismatch": ("$/turn_target", "matches_frozen_goal", "mismatch"),
    "planning_rule_unregistered": ("$/completion_rule", "registered_rule", "unregistered"),
    "planning_attachment_invalid": ("$/completion_rule/parameters/attachment_index", "registered_attachment_index", "invalid"),
    "planning_attachment_unauthorized": ("$/completion_rule/parameters/attachment_index", "authorized_attachment", "unauthorized"),
    "planning_intent_count_invalid": ("$/intents", "allowed_count", "invalid"),
    "planning_exhaustion_reason_invalid": ("$/reason", "bounded_nonempty_text", "invalid"),
    "planning_evidence_refs_invalid": ("$/evidence_refs", "unique_visible_refs", "invalid"),
    "planning_business_rejected": ("$", "valid_business_proposal", "rejected"),
}
CANDIDATE_ISSUES = {
    "planning_intent_invalid": ("description", "nonempty_text", "invalid"),
    "planning_intent_type_invalid": ("intent_type", "null_or_nonempty_text", "invalid"),
    "planning_dependency_invalid": ("dependency_refs", "unique_visible_refs", "invalid"),
    "planning_dependency_duplicate": ("dependency_refs", "unique_visible_refs", "duplicate"),
    "planning_dependency_unknown": ("dependency_refs", "unique_visible_refs", "unknown_ref"),
    "planning_dependency_unavailable": ("dependency_refs", "available_reliable_refs", "unavailable"),
}


def issue(code, index=None):
    if index is None:
        path, expected, actual = ISSUES[code]
    else:
        suffix, expected, actual = CANDIDATE_ISSUES[code]
        path = f"$/intents/{index}/{suffix}"
    return {"code": code, "path": path, "expected": expected, "actual": actual}


def planning_issues(request, raw, authorization, reliable, *, maximum=2):
    """Explain an actual rejection; this function never grants acceptance."""
    issues = []
    add = lambda code, index=None: issues.append(issue(code, index))
    if not isinstance(raw, dict):
        return [issue("planning_shape_invalid")]
    available = {item.ref: item for item in request.history}
    def refs_valid(refs):
        return (isinstance(refs, list) and all(isinstance(r, str) for r in refs)
                and len(set(refs)) == len(refs) and set(refs).issubset(available))
    if raw.get("kind") == "exhausted":
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            add("planning_exhaustion_reason_invalid")
        if not refs_valid(raw.get("evidence_refs")):
            add("planning_evidence_refs_invalid")
        if set(raw) != {"kind", "reason", "evidence_refs"}:
            add("planning_shape_invalid")
        return issues[:8] or [issue("planning_business_rejected")]
    if raw.get("kind") != "candidates":
        add("planning_kind_invalid")
    if set(raw) != {"kind", "turn_target", "completion_rule", "intents"}:
        add("planning_shape_invalid")
    target = raw.get("turn_target")
    if not isinstance(target, str) or not target.strip() or len(target.encode("utf-8")) > 4096:
        add("planning_target_invalid")
    elif target != request.goal:
        add("planning_target_mismatch")
    rule = raw.get("completion_rule")
    if (not isinstance(rule, dict) or set(rule) != {"rule_id", "version", "parameters"}
            or rule.get("rule_id") != "attachment_copy_sha256"
            or type(rule.get("version")) is not int or rule["version"] != 1):
        add("planning_rule_unregistered")
    else:
        params = rule["parameters"]
        if (not isinstance(params, dict) or set(params) != {"attachment_index"}
                or type(params.get("attachment_index")) is not int
                or params["attachment_index"] not in {0, 1}):
            add("planning_attachment_invalid")
        elif params["attachment_index"] not in authorization.attachment_indices:
            add("planning_attachment_unauthorized")
    candidates = raw.get("intents")
    if not isinstance(candidates, list) or not candidates or maximum is not None and len(candidates) > maximum:
        add("planning_intent_count_invalid")
    else:
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict) or set(candidate) != {"description", "intent_type", "dependency_refs"}:
                add("planning_shape_invalid")
                continue
            if not isinstance(candidate["description"], str) or not candidate["description"].strip():
                add("planning_intent_invalid", index)
            kind = candidate["intent_type"]
            if kind is not None and (not isinstance(kind, str) or not kind.strip()):
                add("planning_intent_type_invalid", index)
            refs = candidate["dependency_refs"]
            if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                add("planning_dependency_invalid", index)
            elif len(set(refs)) != len(refs):
                add("planning_dependency_duplicate", index)
            elif set(refs) - available.keys():
                add("planning_dependency_unknown", index)
            elif any(available[r].source_status != "available" or available[r].conflict
                     or not reliable(available[r]) for r in refs):
                add("planning_dependency_unavailable", index)
    return issues[:8] or [issue("planning_business_rejected")]


class BusinessDiagnostics:
    def __init__(self, config, evidence, handoff_evidence, store):
        if config.contract_version not in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            raise ValueError("business_diagnostics_require_contract7")
        self.config, self.evidence, self.handoff, self.store = config, evidence, handoff_evidence, store

    def read(self, identity, *, check_operation=False):
        record = self.evidence.read("business-diagnostic", identity)
        if record is None:
            return None
        contract = self.evidence.read("contract", identity)
        payload = self.handoff.read("request", identity)
        required = {"diagnostic_version", "contract_version", "task_run_id", "model_request_id",
                    "root_request_id", "correction_of_model_request_id", "stage", "role_config_ref",
                    "base_checkpoint_sequence", "source_request_sha256", "context_sha256",
                    "contract_sha256", "issues", "rejection_reason", "rejected_candidate_indices", "finished_at"}
        if (not isinstance(record, dict) or not isinstance(contract, dict)
                or not isinstance(payload, dict) or set(record) != required):
            raise D3RecoveryRequired("d8_business_diagnostic_evidence_missing")
        request = D8HandoffRequest.from_dict(payload)
        if (type(record["diagnostic_version"]) is not int or record["diagnostic_version"] != 1
                or type(record["contract_version"]) is not int or record["contract_version"] != self.config.contract_version
                or contract["contract_version"] != self.config.contract_version or request.stage != "tree_planning"
                or record["model_request_id"] != identity or request.model_request_id != identity
                or record["task_run_id"] != request.task_run_id
                or record["stage"] != request.stage or record["stage"] != contract["stage"]
                or record["root_request_id"] != contract["root_request_id"]
                or record["root_request_id"] != (request.correction_of_model_request_id or identity)
                or record["correction_of_model_request_id"] != request.correction_of_model_request_id
                or record["correction_of_model_request_id"] != contract["correction_of_model_request_id"]
                or record["role_config_ref"] != request.role_config_ref
                or request.role_config_ref != self.config.outer_role.role_config_ref
                or type(record["base_checkpoint_sequence"]) is not int
                or record["base_checkpoint_sequence"] != request.base_checkpoint_sequence
                or record["source_request_sha256"] != digest(payload)
                or record["source_request_sha256"] != contract["source_request_sha256"]
                or record["context_sha256"] != request.context_sha256
                or record["contract_sha256"] != digest(contract)
                or contract["task_run_id"] != request.task_run_id
                or contract["role_config_ref"] != request.role_config_ref
                or contract["model_request_id"] != identity
                or type(record["finished_at"]) not in {int, float}
                or not math.isfinite(record["finished_at"])):
            raise D3RecoveryRequired("d8_business_diagnostic_binding_changed")
        if request.correction_of_model_request_id == identity:
            raise D3RecoveryRequired("d8_business_diagnostic_root_changed")
        if request.correction_of_model_request_id is not None:
            root = request.correction_of_model_request_id
            root_contract = self.evidence.read("contract", root)
            root_payload = self.handoff.read("request", root)
            if (not isinstance(root_contract, dict) or not isinstance(root_payload, dict)
                    or root_contract.get("model_request_id") != root
                    or root_contract.get("contract_version") != self.config.contract_version
                    or root_contract.get("correction_of_model_request_id") is not None
                    or root_payload.get("correction_of_model_request_id") is not None
                    or root_payload.get("task_run_id") != request.task_run_id
                    or root_payload.get("stage") != request.stage
                    or root_payload.get("role_config_ref") != request.role_config_ref
                    or root_payload.get("base_checkpoint_sequence") != request.base_checkpoint_sequence):
                raise D3RecoveryRequired("d8_business_diagnostic_root_changed")
            root_business = self.read(root, check_operation=check_operation)
            expected_error = {"correction_of_model_request_id": root}
            if root_business is not None:
                expected_error.update(reason="output_business_invalid", issues=root_business["issues"],
                    business_diagnostic_version=1, business_diagnostic_sha256=digest(root_business))
            else:
                from agent_core.structured_output import StructuredOutput
                root_format = StructuredOutput(SimpleNamespace(config=self.config), self.evidence).diagnostic(root)
                if root_format is None:
                    raise D3RecoveryRequired("d8_business_diagnostic_root_missing")
                expected_error.update(reason="output_format_invalid", issues=root_format["issues"])
            if contract.get("correction") != expected_error:
                raise D3RecoveryRequired("d8_business_diagnostic_correction_changed")
        if self.evidence.read("diagnostic", identity) is not None:
            raise D3RecoveryRequired("d8_business_and_format_diagnostic_conflict")
        issues = record["issues"]
        if not isinstance(issues, list) or not 1 <= len(issues) <= 8:
            raise D3RecoveryRequired("d8_business_diagnostic_issues_invalid")
        allowed = list(ISSUES) + list(CANDIDATE_ISSUES)
        for value in issues:
            if not isinstance(value, dict) or value.get("code") not in allowed:
                raise D3RecoveryRequired("d8_business_diagnostic_issues_invalid")
            code = value["code"]
            if self.config.contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and code in {"planning_target_invalid", "planning_target_mismatch"}:
                raise D3RecoveryRequired("s2_planning_diagnostic_program_field")
            if self.config.contract_version == 22 and code in CANDIDATE_ISSUES:
                parts = value.get('path', '').split('/')
                index = int(parts[2]) if len(parts) == 4 and parts[2].isdigit() else -1
                valid = index >= 0 and value == issue(code, index)
            else:
                valid = value in ([issue(code, i) for i in range(2)] if code in CANDIDATE_ISSUES else [issue(code)])
            if not valid:
                raise D3RecoveryRequired("d8_business_diagnostic_issues_invalid")
        indices = record["rejected_candidate_indices"]
        if (record["rejection_reason"] not in {"d8_output_contract_rejected", "d8_all_dependency_candidates_rejected"}
                or not isinstance(indices, list) or any(type(i) is not int or i < 0
                    or self.config.contract_version != 22 and i not in {0, 1} for i in indices)
                or len(indices) != len(set(indices))):
            raise D3RecoveryRequired("d8_business_diagnostic_result_invalid")
        if check_operation:
            op = self.store.load_d4_operation(identity)
            if (op is None or op.start.task_run_id != request.task_run_id or op.start.category != "planning"
                    or op.start.role != "outer_planning" or op.start.role_config_ref != request.role_config_ref
                    or op.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                    or op.start.context_sha256 != request.context_sha256
                    or op.start.resource_ref != (request.turn_id or request.task_run_id)
                    or op.start.budget_policy_ref != self.config.d4_budget.budget_policy_ref
                    or record["finished_at"] < op.start.started_at
                    or op.completion is not None and op.completion.result_kind != "rejected"):
                raise D3RecoveryRequired("d8_business_diagnostic_operation_changed")
        return record

    def save(self, request, issues, reason, indices, finished_at):
        contract = self.evidence.read("contract", request.model_request_id)
        record = {"diagnostic_version": 1, "contract_version": self.config.contract_version,
            "task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
            "root_request_id": contract["root_request_id"],
            "correction_of_model_request_id": request.correction_of_model_request_id,
            "stage": request.stage, "role_config_ref": request.role_config_ref,
            "base_checkpoint_sequence": request.base_checkpoint_sequence,
            "source_request_sha256": digest(asdict(request)), "context_sha256": request.context_sha256,
            "contract_sha256": digest(contract), "issues": issues,
            "rejection_reason": reason, "rejected_candidate_indices": indices, "finished_at": finished_at}
        self.evidence.save("business-diagnostic", request.model_request_id, record)
        return self.read(request.model_request_id, check_operation=True)

    def rejected_result(self, request, record):
        return {"model_request_id": request.model_request_id, "request_sha256": request.context_sha256,
                "outcome": "invalid", "raw": None, "reason": record["rejection_reason"],
                "rejected_candidate_indices": record["rejected_candidate_indices"],
                "finished_at": record["finished_at"], "business_diagnostic_sha256": digest(record)}

    def verify_result(self, request, result):
        record = self.read(request.model_request_id, check_operation=True)
        if record is not None:
            if result != self.rejected_result(request, record):
                raise D3RecoveryRequired("d8_business_diagnostic_result_changed")
        elif "business_diagnostic_sha256" in result:
            raise D3RecoveryRequired("d8_business_diagnostic_required")
        elif result["outcome"] == "invalid":
            from agent_core.structured_output import StructuredOutput
            if StructuredOutput(SimpleNamespace(config=self.config), self.evidence).diagnostic(request.model_request_id) is None:
                raise D3RecoveryRequired("d8_business_diagnostic_required")
        return record

    def projection(self, operations):
        rows = []
        for op in operations:
            if op.start.category != "planning":
                continue
            payload = self.handoff.read("request", op.start.operation_id)
            if payload is None:
                raise D3RecoveryRequired("d8_business_planning_request_missing")
            request = D8HandoffRequest.from_dict(payload)
            if request.stage != "tree_planning":
                continue
            result = self.handoff.read("result", op.start.operation_id)
            record = (self.read(op.start.operation_id, check_operation=True) if result is None
                      else self.verify_result(request, result))
            if record is not None:
                if op.completion is not None and (result is None
                        or op.completion.evidence_sha256 != digest(result)
                        or op.completion.finished_at != result["finished_at"]):
                    raise D3RecoveryRequired("d8_business_diagnostic_completion_changed")
                rows.append({k: record[k] for k in ("diagnostic_version", "model_request_id", "root_request_id", "stage", "issues")}
                            | {"diagnostic_sha256": digest(record)})
        return rows
