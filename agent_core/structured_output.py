"""D8-5 frozen output instructions and safe, immutable format diagnostics."""

from dataclasses import asdict
import json
import math
import re
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.output_contracts import D8ModelResponse, D8OutputRequest, D8FormatIssue, decode_response, output_schema, matches_output_contract
from agent_core.handoff_contracts import handoff_json
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.review_contracts import D2ReviewRequest, D2ModelFailure, D2ModelResponse
from agent_core.process_facts import D5RefinementRequest
from agent_core.turn_modeling import digest, stable_id


def _instruction(correcting, *, length_limited=False):
    text = "Return exactly one complete JSON object matching output_contract.schema. Do not use Markdown, add fields, or repair types implicitly."
    if correcting:
        if length_limited:
            text += (" The previous generation ended at a length limit without a complete usable decision. "
                     "This is the only regeneration opportunity. Use the current Intent, preserved progress and evidence "
                     "to finish only this decision and return complete JSON. Do not repeat executed tools or treat "
                     "this technical failure as an Intent outcome; all business restrictions still apply.")
        else:
            text += " The preceding proposal was rejected. Generate the complete response again using the declared structure and safe correction information; all business restrictions still apply."
    return text


def format_stop_reason(diagnostic):
    """One classification for scheduler stops and the public recovery view."""
    if diagnostic is None:
        return None
    if diagnostic["terminal"] is not None:
        return "d8_model_" + diagnostic["terminal"]
    if (diagnostic["correction_of_model_request_id"] is not None
            and any(issue["code"] == "output_truncated" for issue in diagnostic["issues"])):
        return "d8_model_output_truncated"
    return None


def ended_transport_failure(transport):
    """Only the model transport child is covered; no tool replay permission."""
    return (transport.get("worker_exit_confirmed") is True and transport.get("transport_end") in {
        "timeout", "transport_error", "incomplete_response", "worker_lost"})


def transient_provider_result(evidence, identity):
    transport = (evidence.read("provider-result", identity) or {}).get("transport", {})
    return ((transport.get("transport_end") == "http_error"
             and transport.get("http_status") in {429, 500, 502, 503}
             and transport.get("worker_exit_confirmed") is not False)
            or ended_transport_failure(transport))


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
                    raise ExecutionRecoveryRequired("d8_output_review_root_unavailable")
                correction = first["first_model_request_id"]
        return root, correction

    def _contract(self, request):
        from agent_core.dual_llm_modeling import D3ModelRequest
        task, identity = self.identity(request)
        stage, schema = output_schema(request, contract_version=self.app.config.contract_version)
        root, correction = self._correction(request, identity)
        role_ref = getattr(request, "role_config_ref", None)
        if isinstance(request, D5RefinementRequest):
            role_ref = request.attempt.role_config_ref
        if isinstance(request, D2ReviewRequest):
            role_ref = self.app.config.outer_role.role_config_ref
        error = self._error(correction)
        instruction = _instruction(correction is not None, length_limited=error is not None and error["reason"] == "output_length_limit")
        safe = self.app.redactor.redact_model_input(request)
        shape = {"version": 1, "stage": stage, "schema": schema}
        prior = self.evidence.read("contract", identity) or (self.evidence.read("contract", correction) if correction else None)
        if self.app.config.contract_version == 22:
            projection = (prior["output_contract"].get("input_projection") if prior else
                          "append_segments" if self.app.config.intent_execution_policy == "continuous_v1" else "shared_refs_v1")
            if projection in {"shared_refs_v1", "stable_prefix", "append_segments"}:
                shape["input_projection"] = projection
        envelope = D8OutputRequest(safe, shape, instruction, error)
        audit = ({"stage": request.stage, "resource_ref": request.resource_ref,
                  "base_checkpoint_sequence": request.base_checkpoint_sequence,
                  "context_sha256": request.context_sha256, "role_config_ref": request.role_config_ref,
                  "correction_of_model_request_id": request.correction_of_model_request_id}
                 if isinstance(request, D3ModelRequest) else None)
        policy=({"incomplete_response_policy":"bounded_once_v1"} if
            (prior is None and self.app.config.contract_version==22 or prior is not None and prior.get("incomplete_response_policy")=="bounded_once_v1") else {})
        return envelope, {**policy,"contract_version": self.app.config.contract_version, "model_request_id": identity,
            "task_run_id": task, "role_config_ref": role_ref, "stage": stage,
            "root_request_id": root, "correction_of_model_request_id": correction,
            "source_request_sha256": digest(asdict(request)),
            "safe_request_sha256": digest(asdict(safe)),
            "output_contract": shape, "instruction": instruction, "correction": error,
            "complete_input_utf8_bytes": len(handoff_json(envelope).encode("utf-8")),
            "complete_input_unicode_characters": len(handoff_json(envelope)),
            "input_tokens": None, "model_audit": audit}

    def _error(self, correction):
        if correction is None:
            return None
        diagnostic = self.diagnostic(correction)
        error = {"correction_of_model_request_id": correction,
                 "reason": "output_format_invalid" if diagnostic is not None else "proposal_rejected",
                 "issues": diagnostic["issues"] if diagnostic is not None else []}
        contract = self.evidence.read("contract", correction)
        interruption = self.evidence.read("call-interruption", correction)
        if diagnostic is None and self.evidence.read("provider-result", correction) is not None:
            error["reason"] = "previous_model_call_failed"
            if ended_transport_failure(self.evidence.read("provider-result", correction).get("transport", {})):
                error["reason"] = "previous_model_transport_failed_no_usable_decision"
        if diagnostic is None and interruption is not None and interruption.get("outcome") == "cancelled":
            error["reason"] = "previous_model_call_interrupted_no_usable_decision"
        if (diagnostic is not None and contract is not None
                and contract["role_config_ref"] == self.app.config.inner_role.role_config_ref
                and any(issue["code"] == "output_truncated" for issue in diagnostic["issues"])):
            error["reason"] = "output_length_limit"
        if self.app.config.intent_execution_policy and contract is not None and contract["stage"] == "inner_plan" and diagnostic is None:
            rejection = self.app.model_evidence.read_rejection(correction)
            paths = {"d22_strategy_stop_summary_required": "$/outcome",
                     "d22_resolution_source_unavailable": "$/evidence_refs",
                     "d22_resolution_fact_not_effective": "$/fact_ids",
                     "d22_working_source_changed": "$/evidence_refs"}
            if rejection is not None and rejection.code in paths:
                error.update(reason=rejection.code, issues=[{"code": "json_value_invalid",
                    "path": paths[rejection.code], "expected": "allowed_choice", "actual": "invalid"}])
        diagnostics = (getattr(self.app, "evaluation_bindings", None)
                       if contract is not None and contract["stage"] == "intent_evaluation" and self.app.config.contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}
                       else getattr(self.app, "review_diagnostics", None) if contract is not None and (
                          contract["stage"] == "review" or self.app.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and contract["stage"] == "progress_review")
                       else getattr(self.app, "fact_business_diagnostics", None)
                       if contract is not None and contract["stage"] == "fact_proposal"
                       else self.app.business_diagnostics)
        if diagnostics is not None:
            business = diagnostics.read(correction)
            if business is not None:
                error.update(reason="output_business_invalid", issues=business["issues"],
                             business_diagnostic_version=1, business_diagnostic_sha256=digest(business))
        if self.app.config.contract_version in {17, 18, 19, 20, 21, 22} and contract is not None and contract['stage'] in {'review', 'progress_review'}:
            from agent_core.execution_feasibility import KEY
            snapshot = self.evidence.read('review-request', correction)
            if not isinstance(snapshot, dict) or digest(snapshot) != contract['source_request_sha256']:
                raise ExecutionRecoveryRequired('d17_review_correction_request_changed')
            if snapshot['review_scope'] != 'turn_end':
                context = json.loads(snapshot['context_summary'])
                if KEY in context:
                    error[KEY] = context[KEY]
        if self.app.config.contract_version in {16, 17, 18, 19, 20, 21, 22} and contract is not None and contract['stage'] == 'inner_plan':
            from agent_core.plan_policy import policy_for as plan_policy
            snapshot = self.app.sessions.evidence.read('request', correction)
            if not isinstance(snapshot, dict) or digest(snapshot['request']) != contract['source_request_sha256']:
                raise ExecutionRecoveryRequired('s6_plan_correction_request_changed')
            if getattr(self.app, 'web', None) is not None:
                error['http_request_policy'] = self.app.web.policy()
            else:
                error['allowed_plan_choices'] = plan_policy(snapshot['request'])['allowed_choices']
            from agent_core.execution_feasibility import KEY
            feasibility = json.loads(snapshot['request']['history_context']).get(KEY)
            if feasibility is not None:
                error[KEY] = feasibility
            rejection = self.app.model_evidence.read_rejection(correction)
            if rejection is not None and rejection.code == 'd3_action_rejected_by_review_constraint':
                error['reason'] = rejection.code
        if self.app.config.contract_version >= 11 and contract is not None and contract["stage"] in {"inner_size", "inner_sha256"}:
            from agent_core.fixed_probe import fixed_probe_policy
            original, _ = self.app.fixed_probe_bindings._request(correction)
            error["fixed_probe_policy"] = fixed_probe_policy(original)
            rejection = self.app.model_evidence.read_rejection(correction)
            if rejection is not None and rejection.code == "d3_action_rejected_by_review_constraint":
                error["reason"] = rejection.code
        return error

    def prepare(self, request):
        envelope, record = self._contract(request)
        saved = self.evidence.read("contract", record["model_request_id"])
        if saved is not None and saved != record:
            raise ExecutionRecoveryRequired("d8_output_frozen_contract_changed")
        policy = (self.app.config.handoff_config.inner_capacity
                  if record["role_config_ref"] == self.app.config.inner_role.role_config_ref
                  else self.app.config.handoff_config.outer_capacity)
        fits = self.app.handoff.capacity_port.fits(envelope, policy)
        measurement = getattr(self.app.handoff.capacity_port, "last_measurement", None)
        if not fits:
            # A rejected draft is not a frozen model request. Keep its measurement
            # in the existing pause receipt, so changed input can later be admitted
            # without overwriting immutable per-request evidence.
            self.app.handoff._pause("complete_model_request", (), policy.total_units, None,
                                    reason="d8_output_complete_request_capacity_paused")
        if self.app.config.intent_execution_policy and measurement is not None:
            current = {"task_run_id": record["task_run_id"], "model_request_id": record["model_request_id"],
                       "stage": record["stage"], "fits": fits, **measurement}
            measured = self.evidence.read("input-measurement", record["model_request_id"])
            if measured is not None:
                if any(current.get(key) != value for key, value in measured.items()):
                    raise ExecutionRecoveryRequired("d8_input_measurement_changed")
            else:
                self.evidence.save("input-measurement", record["model_request_id"], current)
        self.evidence.save("contract", record["model_request_id"], record)
        if self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
            self.app.selection_bindings.freeze(envelope.request, record)
        if (self.app.config.contract_version in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and isinstance(request, D2ReviewRequest)
                and (record["stage"] == "review" or self.app.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and record["stage"] == "progress_review")):
            self.evidence.save("review-request", request.review_id, asdict(request))

    def envelope(self, safe_request):
        task, identity = self.identity(safe_request)
        saved = self.evidence.read("contract", identity)
        if saved is None or saved["task_run_id"] != task or saved["safe_request_sha256"] != digest(asdict(safe_request)):
            raise ExecutionRecoveryRequired("d8_output_request_not_frozen")
        stage, schema = output_schema(safe_request, contract_version=self.app.config.contract_version)
        if not matches_output_contract(saved["output_contract"], stage, schema, self.app.config.contract_version):
            raise ExecutionRecoveryRequired("d8_output_schema_changed")
        correction = saved["correction_of_model_request_id"]
        expected_error = self._error(correction)
        if saved["instruction"] != _instruction(correction is not None, length_limited=expected_error is not None and expected_error["reason"] == "output_length_limit") or saved["correction"] != expected_error:
            raise ExecutionRecoveryRequired("d8_output_frozen_description_changed")
        # Model workers read frozen files only; SQLite belongs to the dispatch thread.
        if isinstance(safe_request, D5RefinementRequest) and correction is not None:
            if identity != stable_id(task, "d8/refinement/correction/" + correction):
                raise ExecutionRecoveryRequired("d8_output_correction_owner_changed")
        elif isinstance(safe_request, D2ReviewRequest):
            if safe_request.review_attempt > 1:
                first = self.evidence.read("review-root", safe_request.review_root_id)
                if first is None or first["first_model_request_id"] != correction or first["task_run_id"] != task:
                    raise ExecutionRecoveryRequired("d8_output_correction_owner_changed")
        elif getattr(safe_request, "correction_of_model_request_id", None) != correction:
            raise ExecutionRecoveryRequired("d8_output_correction_owner_changed")
        if isinstance(safe_request, D2ReviewRequest) and safe_request.review_attempt == 1:
            self.evidence.save("review-root", safe_request.review_root_id,
                {"task_run_id": safe_request.task_run_id, "first_model_request_id": safe_request.review_id})
        return D8OutputRequest(safe_request, saved["output_contract"], saved["instruction"], saved["correction"])

    def diagnostic(self, identity):
        result = self.evidence.read("diagnostic", identity)
        if result is None:
            return None
        contract = self.evidence.read("contract", identity)
        fields = {"contract_version", "task_run_id", "model_request_id", "root_request_id",
                  "correction_of_model_request_id", "stage", "contract_sha256", "terminal", "issues",
                  "output_measurement", "finished_at"}
        if "optional_output_kind" in result:
            fields.add("optional_output_kind")
            if result["optional_output_kind"] != "fact_candidate" or result.get("stage") != "inner_plan":
                raise ExecutionRecoveryRequired("d8_output_diagnostic_binding_changed")
        if (contract is None or set(result) != fields or result.get("contract_version") != self.app.config.contract_version
                or result.get("model_request_id") != identity or result.get("contract_sha256") != digest(contract)
                or result.get("task_run_id") != contract["task_run_id"] or result.get("stage") != contract["stage"]
                or result.get("root_request_id") != contract["root_request_id"]
                or result.get("correction_of_model_request_id") != contract["correction_of_model_request_id"]
                or result.get("terminal") not in {None, "output_truncated", "completion_invalid", "output_size_exceeded", "output_depth_exceeded", "output_budget_exhausted"}
                or not isinstance(result.get("issues"), list) or not 1 <= len(result["issues"]) <= 8):
            raise ExecutionRecoveryRequired("d8_output_diagnostic_binding_changed")
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
        if self.app.config.contract_version in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            codes |= {"enum_mismatch", "array_duplicate", "number_out_of_range"}
            labels |= {"allowed_choice", "allowed_range"}
        if self.app.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            codes.add("array_required_item_missing")
            labels.add("required_current_reference")
        for issue in result["issues"]:
            if (not isinstance(issue, dict) or set(issue) != {"code", "path", "expected", "actual"}
                    or issue["code"] not in codes or not isinstance(issue["path"], str)
                    or issue["path"].split("/")[0] != "$" or len(issue["path"]) > 128
                    or not isinstance(issue["expected"], str) or not isinstance(issue["actual"], str)
                    or not (all(label in labels for label in issue["expected"].split("|")) and issue["actual"] in labels
                        or issue["code"] == "string_too_long"
                        and re.fullmatch(r"unicode_codepoints_le_[0-9]{1,5}", issue["expected"])
                        and re.fullmatch(r"unicode_codepoints_[0-9]{1,5}", issue["actual"])
                        and 0 <= int(issue["expected"].rsplit("_", 1)[1]) < int(issue["actual"].rsplit("_", 1)[1]) <= 65536)
                    or any(p not in names and p != "<unknown>" and not re.fullmatch(r"[0-9]+", p)
                           for p in issue["path"].split("/")[1:])):
                raise ExecutionRecoveryRequired("d8_output_diagnostic_fields_invalid")
        measure = result["output_measurement"]
        if (measure is not None and (not isinstance(measure, dict)
                or set(measure) != {"utf8_bytes", "unicode_characters", "tokens"}
                or any(type(measure[k]) is not int or measure[k] < 0 for k in ("utf8_bytes", "unicode_characters"))
                or measure["tokens"] is not None)
                or type(result["finished_at"]) not in {int, float} or not math.isfinite(result["finished_at"])):
            raise ExecutionRecoveryRequired("d8_output_diagnostic_measurement_invalid")
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
            interruption = self.evidence.read("call-interruption", identity)
            provider = self.evidence.read("provider-result", identity)
            transport = (provider or {}).get("transport", {})
            cancelled = ((provider or {}).get("error") == {"kind":"cancelled", "code":"model_cancelled"}
                         and transport.get("transport_end") == "cancelled"
                         and transport.get("worker_exit_confirmed") is True)
            retained = interruption is not None and interruption.get("outcome") == "cancelled"
            if failure is None and not cancelled and not retained:
                continue
            engine = self.app.outer if record.start.category == "outer_model" else self.app.inner
            active = engine.active_calls.get(identity)
            if active is not None:
                if not active[0].is_set():
                    continue
                engine.active_calls.pop(identity)
            contract = self.evidence.read("contract", identity)
            if contract is None or contract.get("model_request_id") != identity:
                raise ExecutionRecoveryRequired("d8_output_model_audit_changed")
            audit = contract["model_audit"]
            if (audit is None or contract["task_run_id"] != task
                    or contract["stage"] != audit["stage"] or contract["role_config_ref"] != audit["role_config_ref"]
                    or audit["stage"] not in {"outer_turn", "inner_plan", "inner_size", "inner_sha256", "inner_complete", "inner_candidate"}
                    or record.start.category != ("outer_model" if audit["stage"] == "outer_turn" else "inner_model")
                    or record.start.role != ("outer_planning" if audit["stage"] == "outer_turn" else "inner_action")
                    or audit["base_checkpoint_sequence"] != record.start.base_checkpoint_sequence
                    or audit["context_sha256"] != record.start.context_sha256
                    or audit["role_config_ref"] != record.start.role_config_ref
                    or audit["resource_ref"] != record.start.resource_ref):
                raise ExecutionRecoveryRequired("d8_output_model_audit_changed")
            raw, outcome, reason = None, "invalid_structure", "d8_output_format_invalid"
            finished_at = failure["finished_at"] if failure is not None else self.app.clock()
            if cancelled or retained:
                if interruption is not None and (interruption.get("task_run_id") != task
                        or interruption.get("model_request_id") != identity
                        or interruption.get("request_sha256") != contract["source_request_sha256"]
                        or retained and interruption.get("adapter_exit_confirmed") is not True):
                    raise ExecutionRecoveryRequired("d8_output_interruption_binding_changed")
                if cancelled:
                    frozen, _ = self.app.selection_bindings._request(identity)
                    expected_envelope = dict(request=frozen["request"], output_contract=contract["output_contract"],
                        instruction=contract["instruction"], correction=contract["correction"])
                    sent = self.evidence.read("provider-request", identity)
                    if (sent is None or sent.get("envelope_sha256") != digest(expected_envelope) or sent.get("model_request_id") != identity or sent.get("task_run_id") != task
                            or sent.get("stage") != audit["stage"] or sent.get("role_config_ref") != audit["role_config_ref"]
                            or provider.get("model_request_id") != identity or provider.get("request_sha256") != digest(sent)):
                        raise ExecutionRecoveryRequired("d8_output_cancelled_call_binding_changed")
                binding = self.app.selection_bindings.read(identity) if retained else None
                if binding is not None:
                    raw, outcome, reason = binding["canonical_response"], "accepted", None
                    finished_at = binding["finished_at"]
                else:
                    error = ModelError(ModelErrorKind.CANCELLED, "model_cancelled")
                    raw, outcome, reason = error.to_dict(), "model_error", error.short_reason
                    if retained:
                        finished_at = interruption["finished_at"]
            elif failure["terminal"]:
                error = ModelError(ModelErrorKind.INVALID_RESPONSE, "d8_" + failure["terminal"])
                raw, outcome, reason = error.to_dict(), "model_error", error.short_reason
            self.app.model_evidence.save(D3ModelEvidence(identity, task, audit["stage"],
                audit["role_config_ref"], audit["resource_ref"], audit["base_checkpoint_sequence"],
                audit["context_sha256"], audit["correction_of_model_request_id"], outcome,
                raw, reason, finished_at))

    def failure(self, request):
        task, identity = self.identity(request)
        result = self.diagnostic(identity)
        if result is None:
            return None
        contract = self.evidence.read("contract", identity)
        if result["task_run_id"] != task or contract != self._contract(request)[1]:
            raise ExecutionRecoveryRequired("d8_output_diagnostic_request_changed")
        return result

    def retryable_technical_failure(self, request):
        # Only a definitely ended transient response may share the one correction.
        if self.app.config.intent_execution_policy != "continuous_v1":return False
        _, identity = self.identity(request)
        _, correction = self._correction(request, identity)
        if correction is not None:return False
        interruption = self.evidence.read("call-interruption", identity)
        if interruption is not None and interruption.get("outcome") == "cancelled" and interruption.get("phase") == "supervision_poll":
            contract = self.evidence.read("contract", identity)
            if (interruption.get("task_run_id") != request.task_run_id or interruption.get("model_request_id") != identity
                    or interruption.get("request_sha256") != contract["source_request_sha256"]
                    or interruption.get("adapter_exit_confirmed") is not True):
                raise ExecutionRecoveryRequired("d8_output_interruption_binding_changed")
            return True
        return transient_provider_result(self.evidence, identity)

    def terminal_check(self, request):
        reason = format_stop_reason(self.failure(request))
        if reason is not None:
            raise ExecutionRecoveryRequired(reason)

    def normalize(self, request, response):
        task, identity = self.identity(request)
        if isinstance(response, (ModelError, D2ModelFailure)):
            return response
        if isinstance(response, D2ModelResponse):
            response = response.response
        envelope = self.envelope(request)
        raw, issues, terminal, units = decode_response(response, envelope.output_contract["schema"])
        contract=self.evidence.read("contract",identity)
        if terminal=="output_truncated" and contract.get("incomplete_response_policy")=="bounded_once_v1":
            provider=self.evidence.read("provider-result",identity)
            sent=self.evidence.read("provider-request",identity)
            transport=(provider or {}).get("transport",{})
            used=(transport.get("usage") or {}).get("completion_tokens")
            limit=(sent or {}).get("max_tokens")
            if sent is not None:limit=sent.get("parameters",{}).get("max_tokens",limit)
            known_end = provider is None or transport.get("transport_end") == "response_received"
            if type(used) is int and type(limit) is int and used>=limit:
                terminal="output_budget_exhausted"
                if (known_end and contract["role_config_ref"] == self.app.config.inner_role.role_config_ref
                        and contract["correction_of_model_request_id"] is None):
                    terminal=None
            elif known_end:
                # Share the original single correction; never parse partial output.
                terminal=None
        if not issues and self.app.config.contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.selection_bindings import STAGES
            from agent_core.governance_policy import requires_binding, MixedClaimSources
            stage = envelope.output_contract['stage']
            if stage in STAGES and requires_binding(stage, raw):
                if self.app.conclusions._sensitive(raw):
                    return ModelError(ModelErrorKind.INVALID_RESPONSE, 's5_sensitive_output_rejected')
                try:
                    return self.app.selection_bindings.normalize(request, raw, self.app.clock)
                except MixedClaimSources:
                    # Preserve D5's no-retry business rejection; do not create a format diagnostic.
                    return None
                except (ValueError, TypeError, KeyError):
                    if stage == 'refinement':
                        # Native D5 candidate rejection cannot create a format retry.
                        return None
                    issues = [D8FormatIssue('json_value_invalid', '$', 'allowed_shape', 'invalid')]
        if not issues:
            http = getattr(self.app, 'web', None)
            if (http is not None and envelope.output_contract['stage'] == 'inner_plan'
                    and raw.get('kind') == 'http_request'):
                try:
                    return http.normalize_plan(request, raw)
                except (ValueError, TypeError, KeyError):
                    issues = [D8FormatIssue('json_value_invalid', '$', 'allowed_shape', 'invalid')]
            if (http is not None and envelope.output_contract['stage'] == 'fact_proposal'
                    and raw.get('kind') == 'candidate' and raw.get('assertion') is not None):
                raw['assertion'].update(rule_version=1,
                    artifact_version_id=request.observation.artifact_version_id,
                    observation_id=request.observation.observation_id)
            if (self.app.config.contract_version in {11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and envelope.output_contract["stage"] in {"inner_size", "inner_sha256"}
                    and raw.get("kind") != "conclusion_change"):
                return self.app.fixed_probe_bindings.normalize(request, raw)
            if (self.app.config.contract_version in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and isinstance(request, D2ReviewRequest)
                    and (envelope.output_contract["stage"] == "review"
                         or self.app.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and envelope.output_contract["stage"] == "progress_review")):
                # envelope() has checked the safe request; business binding uses the
                # frozen original, whose JSON text can differ after redaction.
                original = self.app.review_diagnostics._request(identity)
                contract = self.evidence.read("contract", identity)
                if contract["source_request_sha256"] != digest(asdict(original)):
                    raise ExecutionRecoveryRequired("d9_review_contract_binding_changed")
                return self.app.review_diagnostics.normalize(original, raw)
            if not issues:
                return raw
        contract = self.evidence.read("contract", identity)
        diagnostic = {"contract_version": self.app.config.contract_version, "task_run_id": task,
            "model_request_id": identity, "root_request_id": contract["root_request_id"],
            "correction_of_model_request_id": contract["correction_of_model_request_id"],
            "stage": contract["stage"], "contract_sha256": digest(contract),
            "terminal": terminal, "issues": [asdict(i) for i in issues],
            "output_measurement": units, "finished_at": self.app.clock()}
        if envelope.output_contract["stage"] == "inner_plan" and terminal is None:
            candidate = response.content if isinstance(response, D8ModelResponse) else response
            if not isinstance(response, D8ModelResponse) or response.finish_reason == "stop":
                if isinstance(candidate, str):
                    try:
                        candidate = json.loads(candidate)
                    except ValueError:
                        candidate = None
                if isinstance(candidate, dict) and candidate.get("kind") == "fact_candidate":
                    # Keep only the trusted optional-branch discriminator, never rejected prose.
                    diagnostic["optional_output_kind"] = "fact_candidate"
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
        bind = getattr(adapter, "bind_diagnostics", None)
        if bind is not None:
            bind(boundary.evidence)

    @property
    def role_config(self):
        return self.adapter.role_config

    def invoke(self, request, *args):
        from agent_core.model_call_phase import model_call_phase
        with model_call_phase("request_assembly"):
            envelope = self.boundary.envelope(request)
        response = self.adapter.invoke(envelope, *args)
        with model_call_phase("response_processing"):
            return self.boundary.normalize(request, response)
