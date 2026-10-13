"""D8-5 frozen output instructions and safe, immutable format diagnostics."""

from dataclasses import asdict
import math
import re
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.output_contracts import D8OutputRequest, decode_response, output_schema
from agent_core.handoff_contracts import handoff_json
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.review_contracts import D2ReviewRequest, D2ModelFailure, D2ModelResponse
from agent_core.process_facts import D5RefinementRequest
from agent_core.turn_modeling import digest, stable_id


def _instruction(correcting):
    text = "Return exactly one complete JSON object matching output_contract.schema. Do not use Markdown, add fields, or repair types implicitly."
    if correcting:
        text += " The preceding proposal was rejected. Generate the complete response again using the declared structure and safe correction information; all business restrictions still apply."
    return text


class StructuredOutput:
    def __init__(self, app, evidence):
        self.app, self.evidence = app, evidence

    def identity(self, request):
        if isinstance(request, D5RefinementRequest):
            return request.attempt.task_run_id, request.attempt.model_request_id
        if isinstance(request, D2ReviewRequest):
            return request.task_run_id, request.review_id
        return request.task_run_id, request.model_request_id

    def _correction(self, request, identity):
        correction = getattr(request, "correction_of_model_request_id", None)
        root = correction or identity
        if isinstance(request, D5RefinementRequest):
            correction = self.app.store.d8_refinement_root(identity)
            if correction is None:
                correction = next((a.model_request_id for a in self.app.store.list_d5_attempts(request.attempt.task_run_id)
                    if stable_id(a.task_run_id, "d8/refinement/correction/" + a.model_request_id) == identity), None)
            root = correction or identity
        if isinstance(request, D2ReviewRequest):
            root = request.review_root_id
            first = self.evidence.read("review-root", root)
            if request.review_attempt > 1:
                if first is None or first["task_run_id"] != request.task_run_id:
                    raise D3RecoveryRequired("d8_output_review_root_unavailable")
                correction = first["first_model_request_id"]
        return root, correction

    def _contract(self, request):
        from agent_core.dual_llm_modeling import D3ModelRequest
        task, identity = self.identity(request)
        stage, schema = output_schema(request)
        root, correction = self._correction(request, identity)
        role_ref = getattr(request, "role_config_ref", None)
        if isinstance(request, D5RefinementRequest):
            role_ref = request.attempt.role_config_ref
        if isinstance(request, D2ReviewRequest):
            role_ref = self.app.config.outer_role.role_config_ref
        error = None
        if correction is not None:
            diagnostic = self.diagnostic(correction)
            # Semantic retry uses a fixed instruction; never echo a model/error string.
            error = {"correction_of_model_request_id": correction,
                     "reason": "output_format_invalid" if diagnostic is not None else "proposal_rejected",
                     "issues": diagnostic["issues"] if diagnostic is not None else []}
        instruction = _instruction(correction is not None)
        safe = self.app.redactor.redact_model_input(request)
        shape = {"version": 1, "stage": stage, "schema": schema}
        envelope = D8OutputRequest(safe, shape, instruction, error)
        audit = ({"stage": request.stage, "resource_ref": request.resource_ref,
                  "base_checkpoint_sequence": request.base_checkpoint_sequence,
                  "context_sha256": request.context_sha256, "role_config_ref": request.role_config_ref,
                  "correction_of_model_request_id": request.correction_of_model_request_id}
                 if isinstance(request, D3ModelRequest) else None)
        return envelope, {"contract_version": 6, "model_request_id": identity,
            "task_run_id": task, "role_config_ref": role_ref, "stage": stage,
            "root_request_id": root, "correction_of_model_request_id": correction,
            "source_request_sha256": digest(asdict(request)),
            "safe_request_sha256": digest(asdict(safe)),
            "output_contract": shape, "instruction": instruction, "correction": error,
            "complete_input_utf8_bytes": len(handoff_json(envelope).encode("utf-8")),
            "complete_input_unicode_characters": len(handoff_json(envelope)),
            "input_tokens": None, "model_audit": audit}

    def prepare(self, request):
        envelope, record = self._contract(request)
        saved = self.evidence.read("contract", record["model_request_id"])
        if saved is not None and saved != record:
            raise D3RecoveryRequired("d8_output_frozen_contract_changed")
        policy = (self.app.config.handoff_config.inner_capacity
                  if record["role_config_ref"] == self.app.config.inner_role.role_config_ref
                  else self.app.config.handoff_config.outer_capacity)
        if not self.app.handoff.capacity_port.fits(envelope, policy):
            raise D3RecoveryRequired("d8_output_complete_request_capacity_paused")
        self.evidence.save("contract", record["model_request_id"], record)

    def envelope(self, safe_request):
        task, identity = self.identity(safe_request)
        saved = self.evidence.read("contract", identity)
        if saved is None or saved["task_run_id"] != task or saved["safe_request_sha256"] != digest(asdict(safe_request)):
            raise D3RecoveryRequired("d8_output_request_not_frozen")
        stage, schema = output_schema(safe_request)
        if saved["output_contract"] != {"version": 1, "stage": stage, "schema": schema}:
            raise D3RecoveryRequired("d8_output_schema_changed")
        correction = saved["correction_of_model_request_id"]
        expected_error = None
        if correction is not None:
            failure = self.diagnostic(correction)
            expected_error = {"correction_of_model_request_id": correction,
                "reason": "output_format_invalid" if failure is not None else "proposal_rejected",
                "issues": failure["issues"] if failure is not None else []}
        if saved["instruction"] != _instruction(correction is not None) or saved["correction"] != expected_error:
            raise D3RecoveryRequired("d8_output_frozen_description_changed")
        # Model workers read frozen files only; SQLite belongs to the dispatch thread.
        if isinstance(safe_request, D5RefinementRequest) and correction is not None:
            if identity != stable_id(task, "d8/refinement/correction/" + correction):
                raise D3RecoveryRequired("d8_output_correction_owner_changed")
        elif isinstance(safe_request, D2ReviewRequest):
            if safe_request.review_attempt > 1:
                first = self.evidence.read("review-root", safe_request.review_root_id)
                if first is None or first["first_model_request_id"] != correction or first["task_run_id"] != task:
                    raise D3RecoveryRequired("d8_output_correction_owner_changed")
        elif getattr(safe_request, "correction_of_model_request_id", None) != correction:
            raise D3RecoveryRequired("d8_output_correction_owner_changed")
        if isinstance(safe_request, D2ReviewRequest) and safe_request.review_attempt == 1:
            self.evidence.save("review-root", safe_request.review_root_id,
                {"task_run_id": safe_request.task_run_id, "first_model_request_id": safe_request.review_id})
        return D8OutputRequest(safe_request, saved["output_contract"], saved["instruction"], saved["correction"])

    def diagnostic(self, identity):
        result = self.evidence.read("diagnostic", identity)
        if result is None:
            return None
        contract = self.evidence.read("contract", identity)
        if (contract is None or set(result) != {"contract_version", "task_run_id", "model_request_id",
                "root_request_id", "correction_of_model_request_id", "stage", "contract_sha256",
                "terminal", "issues", "output_measurement", "finished_at"}
                or result.get("contract_version") != 6 or result.get("model_request_id") != identity
                or result.get("contract_sha256") != digest(contract)
                or result.get("task_run_id") != contract["task_run_id"]
                or result.get("stage") != contract["stage"]
                or result.get("root_request_id") != contract["root_request_id"]
                or result.get("correction_of_model_request_id") != contract["correction_of_model_request_id"]
                or result.get("terminal") not in {None, "output_truncated", "completion_invalid", "output_size_exceeded", "output_depth_exceeded"}
                or not isinstance(result.get("issues"), list) or not 1 <= len(result["issues"]) <= 8):
            raise D3RecoveryRequired("d8_output_diagnostic_binding_changed")
        names = set()
        def collect(schema):
            names.update(schema.get("properties", {}))
            for value in schema.get("properties", {}).values():
                collect(value)
            for value in schema.get("anyOf", []):
                collect(value)
            if "items" in schema:
                collect(schema["items"])
        collect(contract["output_contract"]["schema"])
        codes = {"type_mismatch", "constant_mismatch", "field_missing", "field_unknown",
                 "array_too_short", "array_too_long", "string_too_long", "json_value_invalid",
                 "json_duplicate_key", "json_parse_failed", "output_truncated", "completion_invalid",
                 "output_size_exceeded", "output_depth_exceeded"}
        labels = {"null", "boolean", "integer", "number", "string", "object", "array", "unsupported",
                  "missing", "unknown", "invalid", "duplicate", "incomplete", "oversized", "too_deep",
                  "fixed_value", "allowed_shape", "declared_field", "allowed_count", "allowed_length",
                  "finite_json", "unique_fields", "valid_json", "complete_response", "bounded_response"}
        for issue in result["issues"]:
            if (not isinstance(issue, dict) or set(issue) != {"code", "path", "expected", "actual"}
                    or issue["code"] not in codes or not isinstance(issue["path"], str)
                    or issue["path"].split("/")[0] != "$" or len(issue["path"]) > 128
                    or not isinstance(issue["expected"], str) or not isinstance(issue["actual"], str)
                    or any(label not in labels for label in issue["expected"].split("|"))
                    or issue["actual"] not in labels
                    or any(p not in names and p != "<unknown>" and not re.fullmatch(r"[0-9]+", p)
                           for p in issue["path"].split("/")[1:])):
                raise D3RecoveryRequired("d8_output_diagnostic_fields_invalid")
        measure = result["output_measurement"]
        if (measure is not None and (not isinstance(measure, dict)
                or set(measure) != {"utf8_bytes", "unicode_characters", "tokens"}
                or any(type(measure[k]) is not int or measure[k] < 0 for k in ("utf8_bytes", "unicode_characters"))
                or measure["tokens"] is not None)
                or type(result["finished_at"]) not in {int, float} or not math.isfinite(result["finished_at"])):
            raise D3RecoveryRequired("d8_output_diagnostic_measurement_invalid")
        return result

    def reconcile_formats(self):
        """Repair only known failed D3 results; never dispatch or reconstruct a prompt."""
        from agent_core.dual_llm_modeling import D3ModelEvidence
        task = self.app.state.task_run.task_run_id
        for record in self.app.store.list_d3_operations(task):
            identity = record.start.operation_id
            if self.app.model_evidence.read(identity) is not None:
                continue
            failure = self.diagnostic(identity)
            if failure is None:
                continue
            contract = self.evidence.read("contract", identity)
            audit = contract["model_audit"]
            if (audit is None or contract["task_run_id"] != task
                    or audit["stage"] not in {"outer_turn", "inner_plan", "inner_size", "inner_sha256", "inner_complete", "inner_candidate"}
                    or record.start.category != ("outer_model" if audit["stage"] == "outer_turn" else "inner_model")
                    or record.start.role != ("outer_planning" if audit["stage"] == "outer_turn" else "inner_action")
                    or audit["base_checkpoint_sequence"] != record.start.base_checkpoint_sequence
                    or audit["context_sha256"] != record.start.context_sha256
                    or audit["role_config_ref"] != record.start.role_config_ref
                    or audit["resource_ref"] != record.start.resource_ref):
                raise D3RecoveryRequired("d8_output_model_audit_changed")
            raw, outcome, reason = None, "invalid_structure", "d8_output_format_invalid"
            if failure["terminal"]:
                error = ModelError(ModelErrorKind.INVALID_RESPONSE, "d8_" + failure["terminal"])
                raw, outcome, reason = error.to_dict(), "model_error", error.short_reason
            self.app.model_evidence.save(D3ModelEvidence(identity, task, audit["stage"],
                audit["role_config_ref"], audit["resource_ref"], audit["base_checkpoint_sequence"],
                audit["context_sha256"], audit["correction_of_model_request_id"], outcome,
                raw, reason, failure["finished_at"]))

    def failure(self, request):
        task, identity = self.identity(request)
        result = self.diagnostic(identity)
        if result is None:
            return None
        contract = self.evidence.read("contract", identity)
        if result["task_run_id"] != task or contract != self._contract(request)[1]:
            raise D3RecoveryRequired("d8_output_diagnostic_request_changed")
        return result

    def terminal_check(self, request):
        result = self.failure(request)
        if result is not None and result["terminal"] is not None:
            raise D3RecoveryRequired("d8_model_" + result["terminal"])

    def normalize(self, request, response):
        task, identity = self.identity(request)
        if isinstance(response, (ModelError, D2ModelFailure)):
            return response
        if isinstance(response, D2ModelResponse):
            response = response.response
        envelope = self.envelope(request)
        raw, issues, terminal, units = decode_response(response, envelope.output_contract["schema"])
        if not issues:
            return raw
        contract = self.evidence.read("contract", identity)
        diagnostic = {"contract_version": 6, "task_run_id": task,
            "model_request_id": identity, "root_request_id": contract["root_request_id"],
            "correction_of_model_request_id": contract["correction_of_model_request_id"],
            "stage": contract["stage"], "contract_sha256": digest(contract),
            "terminal": terminal, "issues": [asdict(i) for i in issues],
            "output_measurement": units, "finished_at": self.app.clock()}
        self.evidence.save("diagnostic", identity, diagnostic)
        if terminal is not None:
            if isinstance(request, D2ReviewRequest):
                return D2ModelFailure("provider_failure", "d8_" + terminal)
            return ModelError(ModelErrorKind.INVALID_RESPONSE, "d8_" + terminal)
        if isinstance(request, D2ReviewRequest):
            return D2ModelFailure("invalid_response", "d8_output_format_invalid")
        return None


class StructuredModel:
    def __init__(self, boundary, adapter):
        self.boundary, self.adapter = boundary, adapter

    @property
    def role_config(self):
        return self.adapter.role_config

    def invoke(self, request, *args):
        envelope = self.boundary.envelope(request)
        response = self.adapter.invoke(envelope, *args)
        return self.boundary.normalize(request, response)
