"""Contract9 ordinary review evidence, targeted diagnosis and known-result recovery."""

from dataclasses import asdict
import json
import math

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.review_contracts import D2ReviewRequest, D2ModelFailure
from agent_core.review_policy import ISSUES, issue, policy_for, validate_and_bind, business_issues
from agent_core.turn_modeling import digest


def completion_evidence(call, result=None):
    value = {k: call.get(k) for k in ("outcome", "response", "provider_request_id", "reason")}
    if result is not None:
        value["review_result_sha256"] = digest(result)
    return value


class ReviewDiagnostics:
    def __init__(self, config, output, ledger, store, clock):
        if config.contract_version not in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            raise ValueError("review_diagnostics_require_contract9")
        self.config, self.output, self.ledger, self.store, self.clock = config, output, ledger, store, clock
        self.needs_repair = False

    def ordinary(self, request):
        return policy_for(request) is not None

    def progress(self, request):
        return self.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and "d8_progress" in json.loads(request.context_summary)

    def supported(self, request):
        return self.progress(request) or self.ordinary(request)

    def _policy(self, request):
        if self.progress(request):
            from agent_core.progress_policy import policy_for as progress_policy
            return progress_policy(request)
        return policy_for(request)

    def _validate(self, wire, request):
        if self.progress(request):
            from agent_core.progress_policy import validate_and_bind as bind_progress
            return bind_progress(wire, request)
        return validate_and_bind(wire, request)

    def _issues(self, wire, request):
        if self.progress(request):
            from agent_core.progress_policy import business_issues as progress_issues
            return progress_issues(wire, request)
        return business_issues(wire, request)

    def _reason_fields(self, wire):
        if self.config.contract_version not in {16, 17, 18, 19, 20, 21, 22}:
            return {}
        lengths = {'rationale': len(wire['rationale'])}
        if 'progress' in wire:
            lengths['progress.reason'] = len(wire['progress']['reason'])
        return {'reason_lengths': lengths}

    def _request(self, identity):
        raw = self.output.read("review-request", identity)
        if not isinstance(raw, dict):
            raise D3RecoveryRequired("d9_review_request_missing")
        try:
            request = D2ReviewRequest(**{**raw, "source_versions": tuple(raw["source_versions"])})
        except (TypeError, ValueError, KeyError) as exc:
            raise D3RecoveryRequired("d9_review_request_invalid") from exc
        if request.review_id != identity or not self.supported(request):
            raise D3RecoveryRequired("d9_review_request_binding_changed")
        return request

    def _binding(self, request, finished_at):
        contract = self.output.read("contract", request.review_id)
        stage = "progress_review" if self.progress(request) else "review"
        if (not isinstance(contract, dict) or contract.get("contract_version") != self.config.contract_version
                or contract.get("stage") != stage
                or contract.get("model_request_id") != request.review_id
                or contract.get("task_run_id") != request.task_run_id
                or contract.get("role_config_ref") != self.config.outer_role.role_config_ref
                or contract.get("source_request_sha256") != digest(asdict(request))
                or contract.get("root_request_id") != request.review_root_id
                or type(finished_at) not in {int, float} or not math.isfinite(finished_at)):
            raise D3RecoveryRequired("d9_review_contract_binding_changed")
        from agent_core.output_contracts import output_schema, matches_output_contract
        context = json.loads(request.context_summary)
        context.pop("checkpoint", None)
        if (digest(context) != request.context_version
                or not matches_output_contract(contract["output_contract"], stage, output_schema(request, contract_version=self.config.contract_version)[1], self.config.contract_version)):
            raise D3RecoveryRequired("d9_review_frozen_policy_changed")
        root = self.output.read("review-root", request.review_root_id)
        if not isinstance(root, dict) or root.get("task_run_id") != request.task_run_id:
            raise D3RecoveryRequired("d9_review_root_missing")
        first = self._request(root["first_model_request_id"])
        if (first.review_attempt != 1 or first.review_root_id != request.review_root_id
                or first.task_run_id != request.task_run_id or first.turn_id != request.turn_id
                or first.intent_id != request.intent_id or first.review_scope != request.review_scope
                or first.action_candidate_id != request.action_candidate_id
                or contract["correction_of_model_request_id"] != (first.review_id if request.review_attempt == 2 else None)
                or request.review_attempt == 1 and first != request):
            raise D3RecoveryRequired("d9_review_correction_binding_changed")
        base = {"contract_version": self.config.contract_version, "stage": stage, "task_run_id": request.task_run_id,
                "model_request_id": request.review_id, "root_request_id": request.review_root_id,
                "correction_of_model_request_id": contract["correction_of_model_request_id"],
                "role_config_ref": self.config.outer_role.role_config_ref,
                "base_checkpoint_sequence": request.checkpoint_id,
                "source_request_sha256": digest(asdict(request)), "context_version": request.context_version,
                "policy_sha256": digest(self._policy(request)), "contract_sha256": digest(contract),
                "finished_at": finished_at}
        if self.progress(request):
            p = self._policy(request)
            base.update(progress_watermark=p["decision_watermark"], progress_window_sha256=p["window_sha256"])
        return base

    def _read(self, identity):
        diagnostic = self.output.read("business-diagnostic", identity)
        binding = self.output.read("review-binding", identity)
        result = self.output.read("review-result", identity)
        if diagnostic is None and binding is None:
            if result is not None:
                raise D3RecoveryRequired("d9_review_result_evidence_missing")
            return None, None, None
        request = self._request(identity)
        if diagnostic is not None and binding is not None or self.output.read("diagnostic", identity) is not None:
            raise D3RecoveryRequired("d9_review_diagnostic_conflict")
        value = diagnostic if diagnostic is not None else binding
        if (not isinstance(value, dict) or type(value.get("base_checkpoint_sequence")) is not int
                or type(value.get("diagnostic_version" if diagnostic is not None else "binding_version")) is not int):
            raise D3RecoveryRequired("d9_review_record_invalid")
        base = self._binding(request, value.get("finished_at"))
        if diagnostic is not None:
            issues = diagnostic.get("issues")
            catalog, make_issue = ISSUES, issue
            if self.progress(request):
                from agent_core.progress_policy import ISSUES as catalog, issue as make_issue
                if self.config.contract_version in {16, 17, 18, 19, 20, 21, 22}:
                    from functools import partial
                    make_issue = partial(make_issue, unbounded=True)
            if (not isinstance(issues, list) or not 1 <= len(issues) <= 8
                    or any(not isinstance(i, dict) or i.get("code") not in catalog or i != make_issue(i["code"]) for i in issues)
                    or diagnostic != {**base, "diagnostic_version": 1, "issues": issues}):
                raise D3RecoveryRequired("d9_review_diagnostic_invalid")
            expected = self.rejected_result(request, diagnostic)
        else:
            from agent_core.output_contracts import decode_response, output_schema
            wire = binding.get("wire_response")
            decoded, issues, terminal, _ = decode_response(wire, output_schema(request, contract_version=self.config.contract_version)[1])
            if issues or terminal:
                raise D3RecoveryRequired("d9_review_wire_binding_invalid")
            try:
                canonical = self._validate(decoded, request)
            except (TypeError, ValueError) as exc:
                raise D3RecoveryRequired("d9_review_canonical_binding_invalid") from exc
            if binding != {**base, "binding_version": 1, "wire_response": wire,
                           "canonical_response": canonical, **self._reason_fields(wire)}:
                raise D3RecoveryRequired("d9_review_binding_changed")
            expected = self.accepted_result(request, binding)
        if result is not None and result != expected:
            raise D3RecoveryRequired("d9_review_result_changed")
        return diagnostic, binding, expected

    def read(self, identity, *, check_operation=False):
        diagnostic, binding, result = self._read(identity)
        if check_operation and result is not None:
            self._check_operation(self._request(identity), result)
        return diagnostic

    @staticmethod
    def rejected_result(request, record):
        return {"review_id": request.review_id, "outcome": "invalid_response", "response": None,
                "reason": "d9_review_business_invalid", "finished_at": record["finished_at"],
                "business_diagnostic_sha256": digest(record)}

    @staticmethod
    def accepted_result(request, record):
        return {"review_id": request.review_id, "outcome": "accepted",
                "response": record["canonical_response"], "reason": None,
                "finished_at": record["finished_at"], "binding_sha256": digest(record)}

    @staticmethod
    def call_values(result):
        response = result["response"]
        if result["outcome"] == "invalid_response" and "business_diagnostic_sha256" in result:
            response = {"kind": "invalid_response", "reason": result["reason"],
                        "provider_request_id": None, "business_diagnostic_sha256": result["business_diagnostic_sha256"]}
        return {"outcome": result["outcome"], "response": response,
                "provider_request_id": None, "reason": result["reason"]}

    def normalize(self, request, wire):
        finished = self.clock()
        try:
            canonical = self._validate(wire, request)
        except (TypeError, ValueError):
            record = {**self._binding(request, finished), "diagnostic_version": 1,
                      "issues": self._issues(wire, request)}
            self.output.save("business-diagnostic", request.review_id, record)
            self.output.save("review-result", request.review_id, self.rejected_result(request, record))
            return D2ModelFailure("invalid_response", "d9_review_business_invalid")
        record = {**self._binding(request, finished), "binding_version": 1,
                  "wire_response": wire, "canonical_response": canonical, **self._reason_fields(wire)}
        self.output.save("review-binding", request.review_id, record)
        self.output.save("review-result", request.review_id, self.accepted_result(request, record))
        return canonical

    def known(self, request, *, repair=False):
        diagnostic, binding, expected = self._read(request.review_id)
        if expected is None and self.progress(request):
            from types import SimpleNamespace
            from agent_core.structured_output import StructuredOutput
            failure = StructuredOutput(SimpleNamespace(config=self.config), self.output).diagnostic(request.review_id)
            if failure is not None:
                self._binding(request, failure["finished_at"])
                op = self._operation(request, failure["finished_at"])
                if op.completion is not None:
                    from agent_adapters.review_ledger import FileD2ReviewLedger
                    root = FileD2ReviewLedger.read_root(self.ledger, request.review_root_id)
                    call = next((c for c in root.get("calls", []) if c.get("review_id") == request.review_id), None) if root else None
                    if call is None:
                        raise D3RecoveryRequired("d9_review_call_missing")
                    self.verify_call(request, call)
                outcome = "provider_failure" if failure["terminal"] else "invalid_response"
                reason = "d8_" + failure["terminal"] if failure["terminal"] else "d8_output_format_invalid"
                return {"review_id": request.review_id, "outcome": outcome,
                    "response": {"kind": outcome, "reason": reason, "provider_request_id": None},
                    "reason": reason, "finished_at": failure["finished_at"],
                    "format_diagnostic_sha256": digest(failure)}
        if expected is not None:
            if self._request(request.review_id) != request:
                raise D3RecoveryRequired("d9_review_known_request_changed")
            self._check_operation(request, expected)
            if repair:
                self.output.save("review-result", request.review_id, expected)
        return expected

    def verify_call(self, request, call, *, readonly=False):
        if not self.supported(request):
            return None
        _, _, expected = self._read(request.review_id)
        if expected is not None:
            if self.output.read("review-result", request.review_id) is None:
                if not readonly or not self.progress(request):
                    raise D3RecoveryRequired("d9_review_result_missing")
                self.needs_repair = True
            if any(call.get(k) != v for k, v in self.call_values(expected).items()):
                raise D3RecoveryRequired("d9_review_call_binding_changed")
        else:
            if call.get("reason") == "d4_admission_repaired_before_provider_dispatch":
                return None
            from agent_core.structured_output import StructuredOutput
            from types import SimpleNamespace
            diagnostic = StructuredOutput(SimpleNamespace(config=self.config), self.output).diagnostic(request.review_id)
            if diagnostic is None:
                if call.get("outcome") in {"accepted", "rejected", "invalid_response"}:
                    raise D3RecoveryRequired("d9_review_evidence_required")
                return None
            self._binding(request, diagnostic["finished_at"])
            outcome = "provider_failure" if diagnostic["terminal"] else "invalid_response"
            reason = "d8_" + diagnostic["terminal"] if diagnostic["terminal"] else "d8_output_format_invalid"
            values = {"outcome": outcome, "reason": reason, "provider_request_id": None,
                      "response": {"kind": outcome, "reason": reason, "provider_request_id": None}}
            if any(call.get(k) != v for k, v in values.items()):
                raise D3RecoveryRequired("d9_review_format_call_changed")
            op = self._operation(request, diagnostic["finished_at"])
            if op.completion is not None and (op.completion.evidence_sha256 != digest(completion_evidence(call))
                    or op.completion.result_kind != ("failed" if diagnostic["terminal"] else "rejected")
                    or op.completion.evidence_ref != request.review_id
                    or op.completion.finished_at < diagnostic["finished_at"]):
                raise D3RecoveryRequired("d9_review_format_completion_changed")
        return expected

    def _operation(self, request, finished_at):
        op = self.store.load_d4_operation(request.review_id)
        if (op is None or op.start.task_run_id != request.task_run_id or op.start.category != "review"
                or op.start.role != "outer_planning" or op.start.role_config_ref != self.config.outer_role.role_config_ref
                or op.start.resource_ref != request.review_id or op.start.context_sha256 != digest(asdict(request))
                or op.start.base_checkpoint_sequence != request.checkpoint_id
                or op.start.budget_policy_ref != self.config.d4_budget.budget_policy_ref
                or finished_at < op.start.started_at):
            raise D3RecoveryRequired("d9_review_operation_binding_changed")
        return op

    def _check_operation(self, request, result):
        op = self._operation(request, result["finished_at"])
        if op.completion is not None:
            from agent_adapters.review_ledger import FileD2ReviewLedger
            root = FileD2ReviewLedger.read_root(self.ledger, request.review_root_id)
            call = next((c for c in root.get("calls", []) if c["review_id"] == request.review_id), None) if root else None
            if (call is None or any(call.get(k) != v for k, v in self.call_values(result).items())
                    or op.completion.result_kind != ("completed" if result["outcome"] == "accepted" else "rejected")
                    or op.completion.evidence_ref != request.review_id
                    or op.completion.evidence_sha256 != digest(completion_evidence(call, result))
                    or op.completion.finished_at < result["finished_at"]):
                raise D3RecoveryRequired("d9_review_completion_changed")

    def projection(self, operations):
        rows = []
        for op in operations:
            if op.start.category != "review": continue
            identity = op.start.operation_id
            request = self.output.read("review-request", identity)
            if request is None:
                if any(self.output.read(k, identity) is not None for k in ("review-binding", "review-result")):
                    raise D3RecoveryRequired("d9_review_request_missing")
                contract = self.output.read("contract", identity)
                if contract is not None and (contract.get("stage") == "review"
                        or self.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and contract.get("stage") == "progress_review"):
                    raise D3RecoveryRequired("d9_review_request_missing")
                diagnostic = self.output.read("business-diagnostic", identity)
                if diagnostic is not None and (diagnostic.get("stage") == "review"
                        or self.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and diagnostic.get("stage") == "progress_review"):
                    raise D3RecoveryRequired("d9_review_request_missing")
                for root in self.ledger.list_roots(op.start.task_run_id):
                    for call in root.get("calls", []):
                        if call.get("review_id") == identity:
                            fields = call["request"]
                            saved = D2ReviewRequest(**{**fields, "source_versions": tuple(fields["source_versions"])})
                            if self.supported(saved):
                                raise D3RecoveryRequired("d9_review_request_missing")
                continue
            record = self.read(identity, check_operation=True)
            from agent_adapters.review_ledger import FileD2ReviewLedger
            saved = self._request(identity)
            root = FileD2ReviewLedger.read_root(self.ledger, saved.review_root_id)
            call = next((c for c in root.get("calls", []) if c.get("review_id") == identity), None) if root else None
            if call is None and op.completion is not None:
                raise D3RecoveryRequired("d9_review_call_missing")
            if call is not None and call.get("outcome") not in {"started", "unknown"}:
                self.verify_call(saved, call, readonly=True)
            if record is not None:
                rows.append({k: record[k] for k in ("diagnostic_version", "model_request_id", "root_request_id", "stage", "issues")}
                            | {"diagnostic_sha256": digest(record)})
        return rows
