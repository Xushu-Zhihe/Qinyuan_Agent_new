"""Contract8 Fact proposal diagnostics; no rejected values or verification grants."""

from dataclasses import asdict
import math
from types import SimpleNamespace

from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.operation_accounting import _uuid
from agent_core.turn_modeling import digest, stable_id


ISSUES = {
    "fact_shape_invalid": ("$", "declared_fields", "invalid"),
    "fact_kind_invalid": ("$/kind", "candidate_or_request_review", "invalid"),
    "fact_content_invalid": ("$/content", "nonempty_text_without_nul", "invalid"),
    "fact_content_too_long": ("$/content", "at_most_256_unicode_characters", "over_limit"),
    "fact_reason_invalid": ("$/reason", "nonempty_text_without_nul", "invalid"),
    "fact_reason_too_long": ("$/reason", "at_most_512_unicode_characters", "over_limit"),
    "fact_evidence_count_invalid": ("$/evidence_refs", "one_to_eight_refs", "invalid"),
    "fact_evidence_duplicate": ("$/evidence_refs", "unique_refs", "duplicate"),
    "fact_evidence_uuid_invalid": ("$/evidence_refs", "canonical_uuid4_or_uuid5", "invalid"),
    "fact_assertion_invalid": ("$/assertion", "null_or_declared_assertion", "invalid"),
    "fact_assertion_rule_unregistered": ("$/assertion/rule_id", "attachment_copy_sha256", "unregistered"),
    "fact_assertion_version_unsupported": ("$/assertion/rule_version", "integer_one", "unsupported"),
    "fact_assertion_artifact_uuid_invalid": ("$/assertion/artifact_version_id", "canonical_uuid4_or_uuid5", "invalid"),
    "fact_assertion_observation_uuid_invalid": ("$/assertion/observation_id", "canonical_uuid4_or_uuid5", "invalid"),
    "fact_business_rejected": ("$", "valid_business_proposal", "rejected"),
}


def issue(code):
    path, expected, actual = ISSUES[code]
    return {"code": code, "path": path, "expected": expected, "actual": actual}


def proposal_issues(raw):
    """Explain a rejection by the existing parser, never decide admission."""
    result = []
    if not isinstance(raw, dict):
        return [issue("fact_shape_invalid")]
    def text(name, maximum=None):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            result.append(issue(f"fact_{name}_invalid"))
        elif maximum is not None and len(value) > maximum:
            result.append(issue(f"fact_{name}_too_long"))
    def uuid_valid(value):
        try:
            _uuid(value, "safe_ref")
            return True
        except (TypeError, ValueError):
            return False
    text("reason", 512)
    if raw.get("kind") == "request_review":
        if set(raw) != {"kind", "reason"}:
            result.append(issue("fact_shape_invalid"))
    else:
        if raw.get("kind") != "candidate":
            result.append(issue("fact_kind_invalid"))
        if set(raw) != {"kind", "content", "reason", "evidence_refs", "assertion"}:
            result.append(issue("fact_shape_invalid"))
        text("content", 256)
        refs = raw.get("evidence_refs")
        if not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 8:
            result.append(issue("fact_evidence_count_invalid"))
        elif any(not uuid_valid(ref) for ref in refs):
            result.append(issue("fact_evidence_uuid_invalid"))
        elif len(set(refs)) != len(refs):
            result.append(issue("fact_evidence_duplicate"))
        assertion = raw.get("assertion")
        if assertion is not None:
            if not isinstance(assertion, dict) or set(assertion) != {
                    "rule_id", "rule_version", "artifact_version_id", "observation_id"}:
                result.append(issue("fact_assertion_invalid"))
            else:
                if assertion["rule_id"] != "attachment_copy_sha256":
                    result.append(issue("fact_assertion_rule_unregistered"))
                if type(assertion["rule_version"]) is not int or assertion["rule_version"] != 1:
                    result.append(issue("fact_assertion_version_unsupported"))
                for name in ("artifact_version_id", "observation_id"):
                    if not uuid_valid(assertion[name]):
                        code = "fact_assertion_artifact_uuid_invalid" if name == "artifact_version_id" else "fact_assertion_observation_uuid_invalid"
                        result.append(issue(code))
    return result[:8] or [issue("fact_business_rejected")]


class FactBusinessDiagnostics:
    def __init__(self, config, output_evidence, conclusion_evidence, store):
        if config.contract_version not in {8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            raise ValueError("fact_diagnostics_require_contract8")
        self.config, self.output, self.conclusions, self.store = config, output_evidence, conclusion_evidence, store

    def _binding(self, request, contract, issues, finished_at):
        return {"diagnostic_version": 1, "contract_version": self.config.contract_version, "stage": "fact_proposal",
            "task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
            "root_request_id": request.correction_of_model_request_id or request.model_request_id,
            "correction_of_model_request_id": request.correction_of_model_request_id,
            "role_config_ref": request.role_config_ref,
            "base_checkpoint_sequence": request.base_checkpoint_sequence,
            "source_request_sha256": digest(asdict(request)), "context_sha256": request.context_sha256,
            "observation_sha256": digest(asdict(request.observation)),
            "session_sha256": digest({"session_id": getattr(request, "session_id", None),
                                     "session_context": getattr(request, "session_context", None)}),
            "contract_sha256": digest(contract), "issues": issues, "finished_at": finished_at}

    def read(self, identity, *, check_operation=False):
        record = self.output.read("business-diagnostic", identity)
        if record is None:
            return None
        request = self.conclusions.read_request(identity)
        contract = self.output.read("contract", identity)
        if (request is None or not isinstance(contract, dict) or not isinstance(record, dict)
                or request.stage != "fact_proposal" or request.model_request_id != identity
                or request.role_config_ref != self.config.inner_role.role_config_ref
                or contract.get("contract_version") != self.config.contract_version or contract.get("stage") != "fact_proposal"
                or contract.get("task_run_id") != request.task_run_id
                or contract.get("model_request_id") != identity
                or contract.get("role_config_ref") != request.role_config_ref
                or contract.get("source_request_sha256") != digest(asdict(request))
                or contract.get("root_request_id") != (request.correction_of_model_request_id or identity)
                or contract.get("correction_of_model_request_id") != request.correction_of_model_request_id):
            raise ExecutionRecoveryRequired("d8_fact_diagnostic_binding_changed")
        issues, finished = record.get("issues"), record.get("finished_at")
        if self.config.contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and isinstance(issues, list) and any(
                isinstance(value, dict) and value.get("code") in {"fact_assertion_version_unsupported",
                "fact_assertion_artifact_uuid_invalid", "fact_assertion_observation_uuid_invalid"} for value in issues):
            raise ExecutionRecoveryRequired("s2_fact_diagnostic_program_field")
        if (not isinstance(issues, list) or not 1 <= len(issues) <= 8
                or any(not isinstance(value, dict) or value.get("code") not in ISSUES
                       or value != issue(value["code"]) for value in issues)
                or type(finished) not in {int, float} or not math.isfinite(finished)
                or type(record.get("diagnostic_version")) is not int
                or type(record.get("contract_version")) is not int
                or type(record.get("base_checkpoint_sequence")) is not int
                or record != self._binding(request, contract, issues, finished)):
            raise ExecutionRecoveryRequired("d8_fact_diagnostic_record_invalid")
        if self.output.read("diagnostic", identity) is not None:
            raise ExecutionRecoveryRequired("d8_fact_business_and_format_conflict")
        if request.correction_of_model_request_id is not None:
            root = request.correction_of_model_request_id
            original = self.conclusions.read_request(root)
            root_contract = self.output.read("contract", root)
            if (original is None or original.correction_of_model_request_id is not None
                    or not isinstance(root_contract, dict) or root_contract.get("contract_version") != self.config.contract_version
                    or root_contract.get("stage") != "fact_proposal"
                    or root_contract.get("model_request_id") != root or root_contract.get("root_request_id") != root
                    or root_contract.get("correction_of_model_request_id") is not None
                    or root_contract.get("task_run_id") != request.task_run_id
                    or root_contract.get("role_config_ref") != request.role_config_ref
                    or root_contract.get("source_request_sha256") != digest(asdict(original))
                    or identity != stable_id(request.task_run_id, f"d8/correction/{root}")
                    or original.stage != request.stage or original.task_run_id != request.task_run_id
                    or original.role_config_ref != request.role_config_ref or original.observation != request.observation
                    or original.goal != request.goal or original.intent_description != request.intent_description
                    or getattr(original, "session_id", None) != getattr(request, "session_id", None)):
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_root_changed")
            business = self.read(root, check_operation=check_operation)
            if business is not None:
                expected = {"correction_of_model_request_id": root, "reason": "output_business_invalid",
                    "issues": business["issues"], "business_diagnostic_version": 1,
                    "business_diagnostic_sha256": digest(business)}
            else:
                from agent_core.structured_output import StructuredOutput
                failure = StructuredOutput(SimpleNamespace(config=self.config), self.output).diagnostic(root)
                if failure is None:
                    raise ExecutionRecoveryRequired("d8_fact_diagnostic_root_missing")
                expected = {"correction_of_model_request_id": root, "reason": "output_format_invalid", "issues": failure["issues"]}
            if contract.get("correction") != expected:
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_correction_changed")
        if check_operation:
            op = self.store.load_d4_operation(identity)
            if (op is None or op.start.task_run_id != request.task_run_id or op.start.category != "action"
                    or op.start.role != "inner_action" or op.start.role_config_ref != request.role_config_ref
                    or op.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                    or op.start.context_sha256 != request.context_sha256
                    or op.start.resource_ref != request.observation.plan_id
                    or op.start.budget_policy_ref != self.config.d4_budget.budget_policy_ref
                    or finished < op.start.started_at):
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_operation_changed")
            if op.completion is not None and (op.completion.result_kind != "rejected"
                    or op.completion.evidence_ref != identity or op.completion.finished_at != finished
                    or op.completion.evidence_sha256 != digest(self.rejected_result(request, record))):
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_completion_changed")
        return record

    def save(self, request, issues, finished_at):
        contract = self.output.read("contract", request.model_request_id)
        self.output.save("business-diagnostic", request.model_request_id,
                         self._binding(request, contract, issues, finished_at))
        return self.read(request.model_request_id, check_operation=True)

    @staticmethod
    def rejected_result(request, record):
        return {"model_request_id": request.model_request_id, "request_sha256": request.context_sha256,
            "outcome": "invalid", "raw": None, "finished_at": record["finished_at"],
            "business_diagnostic_sha256": digest(record)}

    def verify_result(self, request, result):
        record = self.read(request.model_request_id, check_operation=True)
        if record is not None:
            if result != self.rejected_result(request, record):
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_result_changed")
        elif "business_diagnostic_sha256" in result:
            raise ExecutionRecoveryRequired("d8_fact_diagnostic_required")
        elif result["outcome"] == "invalid":
            from agent_core.structured_output import StructuredOutput
            if StructuredOutput(SimpleNamespace(config=self.config), self.output).diagnostic(request.model_request_id) is None:
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_required")
        return record

    def projection(self, operations):
        rows = []
        for op in operations:
            if op.start.category != "action":
                continue
            request = self.conclusions.read_request(op.start.operation_id)
            contract = self.output.read("contract", op.start.operation_id)
            diagnostic = self.output.read("business-diagnostic", op.start.operation_id)
            if request is None and ((contract is not None and contract.get("stage") == "fact_proposal")
                    or (diagnostic is not None and diagnostic.get("stage") == "fact_proposal")):
                raise ExecutionRecoveryRequired("d8_fact_diagnostic_request_missing")
            if request is None or request.stage != "fact_proposal":
                continue
            result = self.conclusions.read_result(request.model_request_id)
            record = (self.read(request.model_request_id, check_operation=True) if result is None
                      else self.verify_result(request, result))
            if record is not None:
                if result is None and op.completion is not None:
                    raise ExecutionRecoveryRequired("d8_fact_diagnostic_result_missing")
                rows.append({key: record[key] for key in
                    ("diagnostic_version", "model_request_id", "root_request_id", "stage", "issues")}
                    | {"diagnostic_sha256": digest(record)})
        return rows
