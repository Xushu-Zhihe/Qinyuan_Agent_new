"""Only frozen output descriptions and safe diagnostics, never model error text."""

from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_core.model_call_phase import MODEL_CALL_PHASES


SAFE_EXCEPTION_TYPES = frozenset({
    "D3RecoveryRequired", "D4RecoveryRequired", "D7HttpError", "TaskRunSandboxError",
    "TimeoutError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "BrokenPipeError", "OSError", "PermissionError", "FileNotFoundError", "TypeError",
    "ValueError", "RuntimeError", "APIError", "DockerException", "HTTPError",
    "ReadTimeout", "ConnectTimeout", "SSLError", "gaierror", "OtherError",
    "AttributeError", "KeyError", "IndexError", "CancelledError", "UnboundLocalError", "ProgrammingError",
    "OperationalError", "IntegrityError", "D5EvidenceError", "D5SensitiveDataError", "T6TargetNetworkError",
    "StorageError", "T6EvidenceError", "JSONDecodeError", "IncompleteRead",
})
SAFE_HTTP_ERROR_CODES = frozenset({
    "d20_http_outcome_unknown", "d20_http_cancelled_after_result",
    "d20_http_failure_diagnostic_write_failed", "outcome_unknown",
    "d7_http_dispatch_rejected", "d7_http_transport_failed", "d7_http_session_closed",
    "d7_http_transport_response_invalid", "d7_http_response_invalid",
    "d7_http_response_too_large", "d7_http_evidence_protection_failed",
    "d7_http_evidence_store_invalid", "d7_http_evidence_conflict",
    "d7_http_evidence_operation_conflict", "d8_handoff_evidence_write_recovery_required",
    "d7_http_authorization_or_ownership_mismatch", "d7_http_request_rejected",
    "d7_http_sandbox_profile_mismatch", "d7_http_target_network_adapter_required",
    "d7_http_origin_invalid", "d7_http_request_envelope_too_large",
    "d7_http_headers_invalid", "d7_http_header_rejected", "d7_http_headers_too_large",
    "d7_http_response_headers_invalid", "d7_http_response_header_invalid",
    "d7_http_response_headers_too_large", "d20_http_cookie_owner_changed",
    "d20_http_cookie_snapshot_invalid", "d8_frozen_configuration_mismatch",
    "d20_http_failure_diagnostic_invalid",
    "docker_unavailable", "docker_permission_denied",
    "sandbox_experiment_start_unknown", "sandbox_experiment_stop_unknown",
    "docker_address_pool_exhausted", "docker_network_in_use", "docker_resource_conflict", "docker_resource_outcome_unknown",
})
SAFE_MODEL_ERROR_CODES = SAFE_HTTP_ERROR_CODES | frozenset({
    "d3_review_provider_failure", "d3_review_call_outcome_unknown", "d4_review_dispatch_stopped",
    "d3_model_call_outcome_unknown", "d3_model_call_interrupted", "d22_strategy_result_processing_failed",
    "d8_output_cancelled_call_binding_changed", "d8_output_interruption_binding_changed", "d8_output_model_audit_changed",
    "d8_model_context_rejected", "d8_provider_envelope_invalid", "provider_envelope_invalid",
    "provider_content_invalid", "provider_refused", "d8_transport_outcome_unknown",
    "http_transport_failure", "model_worker_lost", "d8_output_request_not_frozen",
    "d20_http_model_projection_not_frozen", "d8_review_diagnostic_write_failed",
    "d9_review_contract_binding_changed", "d9_review_request_missing",
    "d9_review_request_invalid", "d9_review_request_binding_changed",
    "d5_redaction_configuration_unavailable", "model_call_failed",
    "http_timeout", "model_total_timeout", "model_connect_timeout", "model_cancelled",
    "model_budget_exhausted", "response_rejected", "empty_response",
    "d8_model_output_budget_exhausted", "d8_model_output_truncated",
    "d22_evidence_source_unavailable", "d22_evidence_source_scope_changed",
    "d22_evidence_source_version_changed", "d22_evidence_review_missing",
    "d22_evidence_review_unknown", "d22_evidence_proposal_changed",
    "d22_evidence_request_missing_or_changed", "d22_evidence_conclusion_binding_changed",
    "d22_evidence_decision_changed", "d22_reused_fact_not_effective",
    "d22_fact_approval_missing", "d22_fact_content_or_source_changed",
    "d22_strategy_interruption_owner_changed", "d22_strategy_interruption_source_changed",
    "d22_intent_selection_context_changed", "d22_intent_selection_source_missing",
})


def safe_model_error_code(value):
    if not isinstance(value, str):
        return "model_call_failed"
    if value in SAFE_MODEL_ERROR_CODES:
        return value
    suffix = value.removeprefix("deepseek_http_")
    if value.startswith("deepseek_http_") and len(suffix) == 3 and suffix.isascii() and suffix.isdigit() and 100 <= int(suffix) <= 599:
        return value
    return "model_call_failed"


def _leading_errno(error):
    """Return a leading Windows errno from tuple-shaped exception args, else None."""
    args = getattr(error, "args", ())
    if args and isinstance(args[0], tuple) and args[0] and isinstance(args[0][0], int):
        return args[0][0]
    return None


def safe_failure_details(error, *, include_chain=False):
    """Describe a bounded exception chain without persisting exception text."""
    import os
    if os.environ.get("QINYUAN_DEBUG_ERRORS") and not include_chain:
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
    kinds, codes, seen, chain = [], [], set(), []
    current = error
    while current is not None and id(current) not in seen and len(kinds) < 6:
        seen.add(id(current))
        name = type(current).__name__
        kinds.append(name if name in SAFE_EXCEPTION_TYPES else "OtherError")
        if name == "TaskRunSandboxError" and getattr(current, "outcome", None) == "outcome_unknown":
            code = {"experiment_start": "sandbox_experiment_start_unknown",
                    "experiment_stop": "sandbox_experiment_stop_unknown"}.get(getattr(current, "phase", None))
            if code is not None and code not in codes:
                codes.append(code)
        resource_code = getattr(current, "code", None)
        if resource_code in SAFE_HTTP_ERROR_CODES and resource_code not in codes:
            codes.append(resource_code)
        if len(current.args) == 1 and isinstance(current.args[0], str):
            code = current.args[0]
            if code in SAFE_HTTP_ERROR_CODES and code not in codes:
                codes.append(code)
        if name == "DockerException":
            # A Docker failure is infrastructure, not model text: keep one
            # stable code so the operator can tell it from a model failure.
            code = ("docker_permission_denied"
                    if _leading_errno(current) == 5 else "docker_unavailable")
            if code not in codes:
                codes.append(code)
        if include_chain:
            location = None
            frame = current.__traceback__
            while frame is not None:
                module = frame.tb_frame.f_globals.get("__name__", "")
                if isinstance(module, str) and module.startswith(("agent_core.", "agent_adapters.", "Sandbox.")):
                    location = {"module": module, "line": frame.tb_lineno}
                frame = frame.tb_next
            code = current.args[0] if len(current.args) == 1 and isinstance(current.args[0], str) else None
            allowed = SAFE_MODEL_ERROR_CODES | {
                "d8_handoff_evidence_unavailable", "d8_output_diagnostic_binding_changed",
                "d8_output_diagnostic_fields_invalid", "d8_output_diagnostic_measurement_invalid",
                "evidence_read_failed", "invalid_evidence_checksum", "oversized_evidence"}
            node = {"exception_type": kinds[-1], "code": code if code in allowed else None,
                    "source_location": location}
            for key in ("errno", "winerror", "sqlite_errorcode"):
                value = getattr(current, key, None)
                if type(value) is int:
                    node[key] = value
            if node["code"] is not None and node["code"] not in codes:
                codes.append(node["code"])
            chain.append(node)
        current = current.__cause__ or current.__context__
    result = {"exception_types": kinds, "error_codes": codes}
    if include_chain:
        result["exception_chain"] = chain
    return result


class FileD8OutputEvidence(FileD8HandoffEvidence):
    # Session envelopes reuse this immutable evidence store, not new tables.
    KINDS = {"provider-request", "provider-result", "input-measurement", "mcp-registry", "mcp-catalog", "mcp-call", "mcp-result", "contract", "diagnostic", "review-root", "business-diagnostic", "review-request", "review-binding", "review-result", "fixed-probe-binding", "planning-binding", "fact-assertion-binding", "evaluation-binding", "evaluation-diagnostic", "candidate-binding", "selection-request", "selection-binding", "answer-decision", "http-plan-binding", "http-result", "http-failure", "review-failure"}

    KINDS = KINDS | {"experiment-session", "experiment-process", "experiment-result", "experiment-stop", "call-interruption"}

    @staticmethod
    def describe_failure(error):
        return safe_failure_details(error, include_chain=True)

    def save_review_failure(self, request, *, error=None, failure=None):
        from dataclasses import asdict
        from agent_core.turn_modeling import digest

        details = safe_failure_details(error)
        current, seen, location = error, set(), None
        while current is not None and id(current) not in seen and len(seen) < 6:
            seen.add(id(current))
            code = safe_model_error_code(current.args[0] if len(current.args) == 1 else None)
            if code != "model_call_failed" and code not in details["error_codes"]:
                details["error_codes"].append(code)
            frame = current.__traceback__
            while frame is not None:
                module = frame.tb_frame.f_globals.get("__name__", "")
                if isinstance(module, str) and module.startswith(("agent_core.", "agent_adapters.", "Sandbox.")):
                    location = {"module": module, "line": frame.tb_lineno}
                frame = frame.tb_next
            current = current.__cause__ or current.__context__
        phase = getattr(error, "_d8_model_call_phase", "review_adapter") if error is not None else "provider_call"
        if phase not in MODEL_CALL_PHASES:
            phase = "review_adapter"
        if failure is not None:
            if failure.reason == "d8_model_context_rejected":
                phase = "request_assembly"
            elif failure.reason in {"d8_provider_envelope_invalid", "provider_envelope_invalid", "provider_content_invalid"}:
                phase = "response_processing"
            code = safe_model_error_code(failure.reason)
            if code not in details["error_codes"]:
                details["error_codes"].append(code)
        self.save("review-failure", request.review_id, {
            "diagnostic_version": 1, "task_run_id": request.task_run_id,
            "model_request_id": request.review_id, "request_sha256": digest(asdict(request)),
            "phase": phase, "kind": failure.kind if failure is not None else (
                "unknown" if isinstance(error, (TimeoutError, ConnectionError)) else "provider_failure"),
            "exception_types": details["exception_types"], "error_codes": details["error_codes"],
            "source_location": location,
        })

    def review_failures(self, operations):
        import re
        from agent_core.dual_llm_contracts import D3RecoveryRequired

        rows = []
        fields = {"diagnostic_version", "task_run_id", "model_request_id", "request_sha256",
                  "phase", "kind", "exception_types", "error_codes", "source_location"}
        for operation in operations:
            if operation.start.category != "review":
                continue
            value = self.read("review-failure", operation.start.operation_id)
            if value is None:
                continue
            if (not isinstance(value, dict) or set(value) != fields
                    or type(value["diagnostic_version"]) is not int or value["diagnostic_version"] != 1
                    or value["task_run_id"] != operation.start.task_run_id
                    or value["model_request_id"] != operation.start.operation_id
                    or value["request_sha256"] != operation.start.context_sha256
                    or value["phase"] not in MODEL_CALL_PHASES
                    or value["kind"] not in {"empty_response", "invalid_response", "authentication_failed",
                                             "refused", "provider_failure", "unknown"}
                    or not isinstance(value["exception_types"], list) or len(value["exception_types"]) > 6
                    or any(not isinstance(v, str) or v not in SAFE_EXCEPTION_TYPES for v in value["exception_types"])
                    or not isinstance(value["error_codes"], list) or len(value["error_codes"]) > 7
                    or any(not isinstance(v, str) or safe_model_error_code(v) != v for v in value["error_codes"])):
                raise D3RecoveryRequired("d8_review_diagnostic_binding_changed")
            location = value["source_location"]
            if location is not None and (not isinstance(location, dict) or set(location) != {"module", "line"}
                    or not isinstance(location["module"], str) or len(location["module"]) > 160
                    or re.fullmatch(r"(?:agent_core|agent_adapters|Sandbox)\.[A-Za-z0-9_.]+", location["module"]) is None
                    or type(location["line"]) is not int or location["line"] < 1):
                raise D3RecoveryRequired("d8_review_diagnostic_binding_changed")
            rows.append(value)
        return rows
