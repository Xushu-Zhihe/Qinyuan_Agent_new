"""Recoverable D8 review gate, independent of model and storage SDKs."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, replace

from agent_core.conclusion_contracts import (
    MAX_FACT_CONTENT_LENGTH, D22FactReviewRequest, D8Candidate, D8Conclusion, D8Feedback, D8InferenceView, D8ModelRequest, D8Observation,
    D8ReviewDecision, D8ReviewRequestAction, conclusion_digest, parse_d8_proposal, fact_evidence_matches,
)
from agent_core.runtime import TaskRunStatus
from agent_core.domain import FactStatus, IntentStatus, PlanStatus, TurnStatus
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4OperationCompletion, D4OperationStart
from agent_core.turn_modeling import stable_id
from agent_core.evidence_conclusions import EvidenceReviewRequest, EvidenceObservation, resource


class OptionalConclusionFormatError(D3RecoveryRequired):
    """A known complete optional output exhausted its one correction."""
    def __init__(self, request_id, proposal_id=None):
        self.request_id, self.proposal_id = request_id, proposal_id or request_id
        super().__init__("d22_optional_conclusion_format_exhausted")


class ConclusionReview:
    def __init__(self, controller, store, evidence, proofs, workspace, config,
                 inner_model, outer_model, redactor, clock, stopped):
        self.controller, self.store, self.evidence = controller, store, evidence
        self.proofs, self.workspace, self.config = proofs, workspace, config
        self.inner_model, self.outer_model = inner_model, outer_model
        self.redactor, self.clock, self.stopped = redactor, clock, stopped
        self.request_capacity_check = None
        self.prepare_inner_request = None
        self.business_diagnostics = None

    def _id(self, name):
        return stable_id(self.controller.control_state.task_run.task_run_id, f"d8/{name}")

    def observation(self, proof_id: str, *, historical: bool = False) -> D8Observation:
        http = getattr(self, 'http', None)
        if http is not None and http.result(proof_id) is not None:
            return http.observation(proof_id, historical=historical)
        proof = self.proofs.proofs.read(proof_id)
        if proof is None:
            raise D3RecoveryRequired("d8_proof_missing")
        checked, _ = self.proofs.read_verified(proof.source_execution_id, proof_id,
                                             allow_inactive_plan=historical)
        state = self.controller.control_state
        if checked.task_run_id != state.task_run.task_run_id:
            raise D3RecoveryRequired("d8_source_owner_mismatch")
        receipt = self.workspace.read_digest(proof.observation_id)
        binding = self.store.load_d3_plan_turn_binding(proof.plan_id)
        if receipt is None or binding is None:
            raise D3RecoveryRequired("d8_source_unavailable")
        version = conclusion_digest({"proof": asdict(checked), "receipt": asdict(receipt),
                                     "binding": asdict(binding)})
        source = D8Observation(proof.task_run_id, proof.turn_id, proof.intent_id,
                             proof.plan_id, proof.artifact_version_id, proof.observation_id,
                             proof.proof_id, proof.source_execution_id, proof.observed_sha256,
                             binding.expected_byte_size, receipt.completed_at, version)
        if self.config.contract_version in {16, 17, 18, 19, 20, 21, 22}:
            from agent_core.conclusion_contracts import D16Observation
            return D16Observation(**asdict(source), expected_sha256=binding.expected_sha256,
                digest_matches_expected=proof.observed_sha256 == binding.expected_sha256,
                rule_binding_sha256=proof.rule_binding_sha256)
        return source

    def _scope(self, source: D8Observation) -> None:
        state = self.controller.control_state
        if isinstance(source,EvidenceObservation):
            self.app.evidence_conclusions.validate_sources(source)
            if self.stopped() or not any(i.intent_id==source.intent_id and i.status is IntentStatus.RUNNING and i.turn_id==source.turn_id for i in state.intents):
                raise D3RecoveryRequired("d22_evidence_scope_ended")
            return
        if (self.stopped() or self.observation(source.proof_id) != source
                or self.store.load_d3_run_config(source.task_run_id) != self.config):
            raise D3RecoveryRequired("d8_scope_or_source_changed")
        plan = next((p for p in state.plans if p.plan_id == source.plan_id), None)
        intent = next((i for i in state.intents if i.intent_id == source.intent_id), None)
        if (plan is None or plan.status is not PlanStatus.RUNNING or intent is None
                or intent.status is not IntentStatus.RUNNING or plan.intent_id != intent.intent_id):
            raise D3RecoveryRequired("d8_scope_ended")

    @staticmethod
    def _parse(request, raw):
        if request.stage == "fact_proposal":
            from agent_core.intent_execution import parse_working
            return parse_working(request, raw) or parse_d8_proposal(raw)
        if isinstance(request,EvidenceReviewRequest):
            from agent_core.evidence_conclusions import parse_evidence_review
            return parse_evidence_review(request,raw)
        decision = D8ReviewDecision.from_dict(raw)
        if (isinstance(request, D22FactReviewRequest) and decision.decision == "accept"
                and request.candidate.assertion is not None):
            source = request.observation
            content = ConclusionReview._final_fact_content(request.candidate, decision)
            for item in request.existing_facts:
                old = item["observation"]
                if (item["content"] == content and old["rule_id"] == source.rule_id
                        and old["observed_sha256"] == source.observed_sha256
                        and old["byte_size"] == source.byte_size
                        and old.get("response_status") == getattr(source, "response_status", None)):
                    raise ValueError("d22_ambiguous_duplicate_conclusion")
        return decision

    def _sensitive(self, raw):
        if isinstance(raw, str):
            return self.redactor.contains_sensitive(raw)
        if isinstance(raw, dict):
            return any(self._sensitive(k) or self._sensitive(v) for k, v in raw.items())
        if isinstance(raw, (list, tuple)):
            return any(self._sensitive(item) for item in raw)
        return False

    def _completion(self, request, result):
        try:
            return self._checked_completion(request, result)
        except (TypeError, ValueError, KeyError) as exc:
            raise D3RecoveryRequired("d8_saved_result_invalid") from exc

    def _checked_completion(self, request, result):
        fields = {"model_request_id", "request_sha256", "outcome", "raw", "finished_at"}
        if "rejection_code" in result:
            if (not isinstance(request, (D22FactReviewRequest,EvidenceReviewRequest)) or result["outcome"] != "invalid"
                    or result["rejection_code"] != "d22_ambiguous_duplicate_conclusion"):
                raise D3RecoveryRequired("d22_rejection_binding_changed")
            fields.add("rejection_code")
        bindings = getattr(self, "assembly_bindings", None)
        if bindings is not None and request.stage == "fact_proposal":
            bindings.verify_result(request, result)
            if "assembly_sha256" in result:
                fields.add("assembly_sha256")
        if self.business_diagnostics is not None and request.stage == "fact_proposal":
            self.business_diagnostics.verify_result(request, result)
            if "business_diagnostic_sha256" in result:
                fields.add("business_diagnostic_sha256")
        if (set(result) != fields
                or result["request_sha256"] != request.context_sha256
                or result["model_request_id"] != request.model_request_id
                or result["outcome"] not in {"valid", "invalid", "sensitive_rejected", "model_error", "stale"}):
            raise D3RecoveryRequired("d8_saved_result_binding_changed")
        if result["outcome"] == "valid":
            self._parse(request, result["raw"])
            if self._sensitive(result["raw"]):
                raise D3RecoveryRequired("d8_saved_result_not_safe")
        elif result["raw"] is not None:
            raise D3RecoveryRequired("d8_invalid_result_contains_text")
        return D4OperationCompletion(request.model_request_id, result["finished_at"],
                                     "completed" if result["outcome"] == "valid" else "rejected",
                                     request.model_request_id, conclusion_digest(result))

    def _known(self, request, *, repair=True):
        if isinstance(request,EvidenceReviewRequest):
            self.app.evidence_conclusions.validate_sources(request.observation)
        elif self.config.contract_version in {16, 17, 18, 19, 20, 21, 22} and request.observation.rule_id!='http_response_observed':
            from agent_core.conclusion_contracts import D16Observation
            if (not isinstance(request.observation, D16Observation)
                    or request.observation != self.observation(request.observation.proof_id, historical=True)):
                raise D3RecoveryRequired('s6_fact_source_projection_changed')
        record = self.store.load_d4_operation(request.model_request_id)
        result = self.evidence.read_result(request.model_request_id)
        if record is None:
            if result is not None:
                raise D3RecoveryRequired("d8_result_without_admission")
            return None
        expected = D4OperationStart(request.model_request_id, request.task_run_id,
                                   "action" if request.stage == "fact_proposal" else "fact_review",
                                   record.start.started_at, request.base_checkpoint_sequence,
                                   request.context_sha256, resource(request),
                                   self.config.d4_budget.budget_policy_ref,
                                   "inner_action" if request.stage == "fact_proposal" else "outer_planning",
                                   request.role_config_ref)
        role = self.config.inner_role if request.stage == "fact_proposal" else self.config.outer_role
        if record.start != expected or request.role_config_ref != role.role_config_ref:
            raise D3RecoveryRequired("d8_saved_request_binding_changed")
        if result is None:
            bindings = getattr(self, "assembly_bindings", None)
            if bindings is not None and request.stage == "fact_proposal":
                known = bindings.known_result(request)
                if known is not None:
                    self._parse(request, known["raw"])
                    if self._sensitive(known["raw"]):
                        raise D3RecoveryRequired("s2_saved_fact_not_safe")
                    self.evidence.save_result(request.model_request_id, known)
                    result = known
        if result is None:
            business = (self.business_diagnostics.read(request.model_request_id, check_operation=True)
                        if self.business_diagnostics is not None and request.stage == "fact_proposal" else None)
            if business is not None:
                result = self.business_diagnostics.rejected_result(request, business)
                self.evidence.save_result(request.model_request_id, result)
            boundary = getattr(self, "output_boundary", None)
            failure = boundary.failure(request) if boundary is not None and result is None else None
            if failure is not None:
                result = {"model_request_id": request.model_request_id,
                    "request_sha256": request.context_sha256,
                    "outcome": "model_error" if failure["terminal"] else "invalid", "raw": None,
                    "finished_at": failure["finished_at"]}
                self.evidence.save_result(request.model_request_id, result)
        if result is None:
            raise D3RecoveryRequired("d8_model_outcome_unknown")
        completion = self._completion(request, result)
        if record.completion is None:
            if not repair:
                raise D3RecoveryRequired("d8_saved_completion_unavailable")
            self.store.complete_d4_operation(completion)
        elif record.completion != completion:
            raise D3RecoveryRequired("d8_model_result_changed")
        return result

    def reconcile(self):
        task_id = self.controller.control_state.task_run.task_run_id
        for record in self.store.list_d4_operations(task_id):
            request = self.evidence.read_request(record.start.operation_id)
            boundary = getattr(self, "output_boundary", None)
            known_format = boundary is not None and boundary.diagnostic(record.start.operation_id) is not None
            known_business = (request is not None and request.stage == "fact_proposal"
                and self.business_diagnostics is not None
                and self.business_diagnostics.read(request.model_request_id, check_operation=True) is not None)
            bindings = getattr(self, "assembly_bindings", None)
            known_assembly = (request is not None and request.stage == "fact_proposal" and bindings is not None
                              and bindings.known_result(request) is not None)
            if request is not None and (self.evidence.read_result(request.model_request_id) is not None or known_format or known_business or known_assembly):
                self._known(request)
        for record in self.store.list_d8_conclusions(task_id):
            for request_id in (record.proposal_request_id, record.review_request_id):
                self.store.mark_d4_operation_applied(request_id, record.record_id)

    async def _invoke(self, request, token):
        adapter = self.inner_model if request.stage == "fact_proposal" else self.outer_model
        role = self.config.inner_role if request.stage == "fact_proposal" else self.config.outer_role
        if adapter.role_config != role or request.role_config_ref != role.role_config_ref:
            raise D3RecoveryRequired("d8_frozen_role_changed")
        timeout = min(180.0, self.controller.control_state.task_run.deadline_at - self.clock())
        finished, stopped, values = threading.Event(), threading.Event(), []

        class Token:
            @property
            def is_cancelled(self):
                return stopped.is_set() or token.is_cancelled

        def invoke():
            try:
                values.append((True, adapter.invoke(request, timeout, Token())))
            except BaseException as exc:
                values.append((False, exc))
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True, name="d8-model-call").start()
        expires = asyncio.get_running_loop().time() + timeout
        delay = 0.001
        try:
            while not finished.is_set():
                if self.stopped() or asyncio.get_running_loop().time() >= expires:
                    raise D3RecoveryRequired("d8_model_outcome_unknown")
                await asyncio.sleep(delay)
                delay = min(0.01, delay * 2)
            succeeded, raw = values[0]
            if not succeeded:
                if not isinstance(raw, Exception):
                    raise raw
                raise D3RecoveryRequired("d8_model_outcome_unknown") from raw
            if isinstance(raw, ModelError) and raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                raise D3RecoveryRequired("d8_model_outcome_unknown")
            return raw
        finally:
            stopped.set()

    async def _call(self, request, token):
        adapter = self.inner_model if request.stage == "fact_proposal" else self.outer_model
        role = self.config.inner_role if request.stage == "fact_proposal" else self.config.outer_role
        if adapter.role_config != role or request.role_config_ref != role.role_config_ref:
            raise D3RecoveryRequired("d8_frozen_role_changed")
        saved = self.evidence.read_request(request.model_request_id)
        if saved is not None:
            if saved != request:
                raise D3RecoveryRequired("d8_request_changed")
        else:
            self.evidence.save_request(request)
        result = self._known(request)
        if result is None:
            self._scope(request.observation)
            if self.request_capacity_check is not None:
                self.request_capacity_check(request)
            self.store.begin_d4_operation(D4OperationStart(
                request.model_request_id, request.task_run_id,
                "action" if request.stage == "fact_proposal" else "fact_review",
                self.clock(), request.base_checkpoint_sequence, request.context_sha256,
                resource(request), self.config.d4_budget.budget_policy_ref,
                "inner_action" if request.stage == "fact_proposal" else "outer_planning",
                request.role_config_ref))
            raw = await self._invoke(request, token)
            wire = raw
            bindings = getattr(self, "assembly_bindings", None)
            if (bindings is not None and request.stage == "fact_proposal"
                    and raw is not None and not isinstance(raw, ModelError)):
                from agent_core.planning_assertion import materialize
                raw = materialize(request, raw)
            outcome = "valid"
            issues = None
            rejection = None
            if isinstance(raw, ModelError):
                outcome, raw = "model_error", None
            elif self._sensitive(raw):
                outcome, raw = "sensitive_rejected", None
            else:
                try:
                    self._parse(request, raw)
                except (ValueError, TypeError, KeyError) as exc:
                    if str(exc) == "d22_ambiguous_duplicate_conclusion":
                        rejection = "d22_ambiguous_duplicate_conclusion"
                        from qinyuan_core import get_logger
                        get_logger("ctrl").info("fact.duplicate_requires_disambiguation",
                            run=request.task_run_id, intent=request.observation.intent_id,
                            code=rejection, category="business", request=request.model_request_id)
                    if self.business_diagnostics is not None and request.stage == "fact_proposal":
                        boundary = getattr(self, "output_boundary", None)
                        if boundary is None or boundary.diagnostic(request.model_request_id) is None:
                            from agent_core.fact_diagnostics import proposal_issues
                            issues = proposal_issues(raw)
                    outcome, raw = "invalid", None
            if self.stopped() or token.is_cancelled:
                outcome, raw = "stale", None
            result = {"model_request_id": request.model_request_id,
                      "request_sha256": request.context_sha256, "outcome": outcome,
                      "raw": raw, "finished_at": self.clock()}
            if rejection is not None and outcome == "invalid":
                result["rejection_code"] = rejection
            if outcome == "invalid" and issues is not None:
                business = self.business_diagnostics.save(request, issues, result["finished_at"])
                result = self.business_diagnostics.rejected_result(request, business)
            if bindings is not None and request.stage == "fact_proposal" and outcome == "valid":
                result = bindings.save(request, wire, result)
            self.evidence.save_result(request.model_request_id, result)
            self.store.complete_d4_operation(self._completion(request, result))
        boundary = getattr(self, "output_boundary", None)
        if boundary is not None and result["outcome"] != "stale":
            boundary.terminal_check(request)
        technical_retry = (result["outcome"] == "model_error" and boundary is not None
                           and boundary.retryable_technical_failure(request))
        if (result["outcome"] == "invalid" or technical_retry) and request.correction_of_model_request_id is None:
            self._scope(request.observation)
            correction_id = stable_id(request.task_run_id, f"d8/correction/{request.model_request_id}")
            correction = self.evidence.read_request(correction_id) or replace(
                request, model_request_id=correction_id,
                base_checkpoint_sequence=self.controller.control_state.latest_checkpoint.sequence,
                correction_of_model_request_id=request.model_request_id,
                rejection_code=result.get("rejection_code", "d8_response_contract_invalid"))
            return await self._call(correction, token)
        if result["outcome"] != "valid":
            if self.optional_format(request.model_request_id) is not None:
                raise OptionalConclusionFormatError(request.model_request_id)
            raise D3RecoveryRequired(result.get("rejection_code", f"d8_model_{result['outcome']}"))
        self._scope(request.observation)
        return request.model_request_id, self._parse(request, result["raw"])

    def optional_format(self, identity):
        if not self.config.intent_execution_policy:
            return None
        diagnostic = self.app.output_boundary.diagnostic(identity)
        if (diagnostic is None or diagnostic["terminal"] is not None
                or diagnostic["correction_of_model_request_id"] is None
                or any(i["code"] in {"output_truncated", "completion_invalid", "output_size_exceeded", "output_depth_exceeded"}
                       for i in diagnostic["issues"])):
            return None
        root_diagnostic = self.app.output_boundary.diagnostic(diagnostic["correction_of_model_request_id"])
        if root_diagnostic is not None and any(i["code"] == "output_truncated" for i in root_diagnostic["issues"]):
            # A failed regeneration cannot become a handled optional-format failure.
            return None
        request = self.evidence.read_request(identity)
        if diagnostic["stage"] == "inner_plan":
            if diagnostic.get("optional_output_kind") != "fact_candidate":
                return None
            saved = self.app.sessions.evidence.read("request", identity)
            evidence = self.app.model_evidence.read(identity)
            operation = self.store.load_d3_operation(identity)
            contract = self.app.output_boundary.evidence.read("contract", identity)
            if (saved is None or evidence is None or operation is None or operation.completion is None
                    or evidence.outcome != "invalid_structure" or evidence.raw is not None
                    or evidence.task_run_id != self.controller.control_state.task_run.task_run_id
                    or evidence.stage != "inner_plan" or evidence.role_config_ref != self.config.inner_role.role_config_ref
                    or contract["source_request_sha256"] != conclusion_digest(saved["request"])
                    or contract["model_audit"] != {k: getattr(evidence, k) for k in
                        ("stage", "resource_ref", "base_checkpoint_sequence", "context_sha256", "role_config_ref", "correction_of_model_request_id")}
                    or operation.start.context_sha256 != evidence.context_sha256
                    or operation.completion.evidence_sha256 != conclusion_digest(asdict(evidence))
                    or operation.completion.evidence_ref != identity or operation.completion.result_kind != "rejected"):
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
            root = self.store.load_d3_operation(diagnostic["correction_of_model_request_id"])
            original = self.app.model_evidence.read(diagnostic["correction_of_model_request_id"])
            if (root is None or root.completion is None or original is None
                    or original.task_run_id != evidence.task_run_id or original.stage != evidence.stage
                    or original.correction_of_model_request_id is not None
                    or original.role_config_ref != evidence.role_config_ref
                    or original.resource_ref != evidence.resource_ref
                    or root.completion.evidence_sha256 != conclusion_digest(asdict(original))):
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
            return diagnostic
        if request is None or request.stage not in {"fact_proposal", "fact_review"}:
            return None
        result = self._known(request, repair=False)
        if result is None or result["outcome"] != "invalid":
            return None
        root = self.evidence.read_request(request.correction_of_model_request_id)
        known = self._known(root, repair=False) if root is not None else None
        if (root is None or known is None or known["outcome"] != "invalid"
                or root.stage != request.stage or root.observation != request.observation
                or root.correction_of_model_request_id is not None):
            raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        return diagnostic

    def optional_failure(self, proposal_id):
        notice = self.app.sessions.evidence.read("pause", self._id("optional-format/" + proposal_id))
        if notice is None:
            return None
        if (set(notice) != {"proposal_request_id", "failed_request_id", "identity"}
                or notice["proposal_request_id"] != proposal_id
                or notice["identity"] != "unreviewed_optional_format_failure"
                or self.optional_format(notice["failed_request_id"]) is None):
            raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        request = self.evidence.read_request(notice["failed_request_id"])
        if request is None or request.stage == "fact_proposal":
            if notice["failed_request_id"] != proposal_id:
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        elif isinstance(request, EvidenceReviewRequest):
            candidate = self.app.validator._known_model(request.task_run_id, proposal_id, "inner_plan")
            if (request.correction_of_model_request_id != self.app.evidence_conclusions._id("evidence-review/" + proposal_id)
                    or candidate != request.candidate):
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        else:
            proposal = self.evidence.read_request(proposal_id)
            known = self._known(proposal, repair=False) if proposal is not None else None
            if (proposal is None or known is None or known["outcome"] != "valid"
                    or request.correction_of_model_request_id != self._id("review/" + proposal_id)
                    or proposal.observation != request.observation or self._parse(proposal, known["raw"]) != request.candidate):
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        return notice

    def record_optional_failure(self, proposal_id, failed_id):
        self.app._check_dispatch()
        notice = dict(proposal_request_id=proposal_id, failed_request_id=failed_id,
                      identity="unreviewed_optional_format_failure")
        self.app.sessions.evidence.save("pause", self._id("optional-format/" + proposal_id), notice)
        self.optional_failure(proposal_id)
        from qinyuan_core import get_logger
        get_logger("ctrl").info("fact.optional_format_exhausted", run=self.controller.control_state.task_run.task_run_id,
            request=failed_id, proposal=proposal_id, category="technical_result")

    def optional_ready(self, proposal_id):
        notice = self.optional_failure(proposal_id)
        if notice is None:
            return False
        request = self.evidence.read_request(notice["failed_request_id"])
        if request is not None and request.observation.plan_id is not None:
            result = self.store.load_d3_plan_result(request.observation.plan_id)
            if result is None:
                return False  # Let the original observation owner commit first.
            if result.outcome != "observed" or result.proof_id != request.observation.proof_id:
                raise D3RecoveryRequired("d22_optional_failure_observation_changed")
        return True

    def finish_optional_failure(self, proposal_id):
        if not self.optional_ready(proposal_id):
            return False
        # A rejected operation stays rejected/unapplied. Only a valid candidate can be consumed.
        operation = self.store.load_d4_operation(proposal_id)
        if operation.completion.result_kind == "completed":
            reference = self._id("optional-format/" + proposal_id)
            if operation.application_ref is None:
                self.store.mark_d4_operation_applied(proposal_id, reference)
            elif operation.application_ref != reference:
                raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        return True

    def _feedback(self, plan_id):
        result = []
        for record in self.store.list_d8_conclusions(self.controller.control_state.task_run.task_run_id):
            if record.plan_id != plan_id:
                continue
            request = self.evidence.read_request(record.review_request_id)
            raw = self.evidence.read_result(record.review_request_id)
            if request is None or raw is None:
                raise D3RecoveryRequired("d8_feedback_evidence_unavailable")
            self._known(request)
            decision = D8ReviewDecision.from_dict(raw["raw"])
            reason = decision.reason
            if record.outcome == "rejected" and decision.decision == "accept":
                reason = "d8_registered_assertion_not_satisfied"
            elif record.outcome == "reviewed_inference":
                # Tell the proposer why this stayed an inference, otherwise it
                # keeps restating the same semantic conclusion in new wording and
                # spends one review per attempt on unchanged evidence.
                reason = (
                    "this observation has no registered assertion for the proposed "
                    "content, so the conclusion is recorded as an inference; rewording "
                    "it cannot make it verified. Propose a mechanical observation or "
                    "use request_review. " + (decision.reason or "")
                )[:512]
            result.append(D8Feedback(record.record_id, record.outcome, reason, request.candidate.content))
        return tuple(result)

    @staticmethod
    def _outcome(candidate, source, decision):
        if decision.decision != "accept":
            return "rejected" if decision.decision == "reject" else "insufficient"
        if candidate.assertion is None:
            return "reviewed_inference"
        if (candidate.assertion.rule_id != source.rule_id
                or candidate.assertion.rule_version != source.rule_version
                or candidate.assertion.artifact_version_id != source.artifact_version_id
                or candidate.assertion.observation_id != source.observation_id):
            return "rejected"
        return "verified_fact"

    @staticmethod
    def _final_fact_content(candidate, decision):
        """
        Extract final Fact content from review decision.

        Lenient toward semantics:
        - If outer layer provided refined_content, use it (allows merging/simplification)
        - Otherwise, preserve inner layer's candidate.content verbatim

        Strict about output boundaries:
        - Current output Schema uses the Fact ceiling; legacy decisions may contain longer refinement.
        - merged_sources tracked separately for traceability
        """
        if decision.refined_content is not None and len(decision.refined_content) <= MAX_FACT_CONTENT_LENGTH:
            return decision.refined_content
        # A refinement longer than the Fact ceiling cannot be stored; fall back
        # to the accepted candidate, which the proposal schema already bounds.
        if candidate is None:
            # The caller must supply the accepted candidate; a missing one is a
            # binding error, not something to crash on with an AttributeError.
            raise ValueError("d8_final_content_candidate_missing")
        return candidate.content

    def accepted_content(self, record):
        request = self.evidence.read_request(record.review_request_id)
        result = self._known(request)
        return self._final_fact_content(request.candidate, self._parse(request, result["raw"]))

    def reusable_facts(self, source):
        """Current same-Intent conclusions; response equality is only a hint."""
        app = self.app
        history = {i.record_id: i for i in app.revisions._history()}
        result = []
        for fact in app.state.facts:
            view = history.get(fact.fact_id)
            if (fact.status is not FactStatus.VERIFIED or not app.revisions.reliable(view)
                    or getattr(view, "current_status", "effective") != "effective"
                    or view.intent_id != source.intent_id or fact.turn_id != source.turn_id):
                continue
            if not (fact.verification_ref or "").startswith("d3:"):continue
            old = self.observation(fact.verification_ref[3:], historical=True)
            if old.task_run_id != source.task_run_id or old.intent_id != source.intent_id:
                raise D3RecoveryRequired("d22_fact_source_scope_changed")
            result.append({"fact_id": fact.fact_id, "content": fact.content, "ref": view.ref,
                           "observation": asdict(old)})
        return tuple(result)

    def fact_choice(self, request, candidate, decision):
        source = request.observation
        if not isinstance(request, D22FactReviewRequest):
            if decision.decision in {"reuse", "no_new_information"}:
                raise D3RecoveryRequired("d22_review_not_enabled")
            return stable_id(source.task_run_id, f"d3/fact/{source.plan_id}")
        if decision.decision == "no_new_information":
            return None
        selected = decision.reuse_fact_id
        if selected is None:
            return stable_id(source.task_run_id, f"d3/fact/{source.plan_id}")
        frozen = next((i for i in request.existing_facts if i["fact_id"] == selected), None)
        current = next((i for i in self.reusable_facts(source) if i["fact_id"] == selected), None)
        if frozen is None or current != frozen:
            raise D3RecoveryRequired("d22_reused_fact_not_effective")
        return selected

    def check_support(self, state, binding, proof_id, fact_id):
        """Verify an explicit additional support edge without changing Fact origin."""
        record = next((r for r in self.store.list_d8_conclusions(binding.task_run_id)
                       if r.plan_id == binding.plan_id and r.fact_id == fact_id), None)
        if record is None:
            raise D3RecoveryRequired("d22_fact_support_missing")
        source = self.validate_record(record, state, historical=True)
        if source.proof_id != proof_id or record.outcome != "verified_fact":
            raise D3RecoveryRequired("d22_fact_support_changed")
        fact = next((f for f in state.facts if f.fact_id == fact_id), None)
        if fact is None or fact_id not in {i["fact_id"] for i in self.reusable_facts(source)}:
            raise D3RecoveryRequired("d22_reused_fact_not_effective")
        return source, fact

    async def propose(self, proof_id, token):
        source = self.observation(proof_id)
        self._scope(source)
        # The number of proposals an observation may attract is bounded by the
        # review budget that pays for them, never by a separate magic number:
        # every proposal costs one fact_review.
        from agent_core.operation_accounting import D4BudgetPolicy
        allowance = D4BudgetPolicy().category_limits.get("fact_review", 32)
        budget = getattr(self.config, "d4_budget", None)
        if budget is not None and getattr(budget, "category_limits", None):
            allowance = max(allowance, budget.category_limits.get("fact_review", 0))
        # One extra ordinal so the paying budget is always the authority that
        # stops the loop; the allowance only guards a misconfigured budget.
        from itertools import count
        from agent_core.intent_execution import visits, continuous
        visit = visits(self.store, source.task_run_id, source.intent_id) if continuous(self.config) else 0
        for ordinal in (count(1) if continuous(self.config) else range(1, allowance + 2)):
            if self.stopped() or token.is_cancelled:raise D3RecoveryRequired("d8_proposal_stopped")
            root_id = self._id(f"proposal/{source.plan_id}/{ordinal}" + (f"/revisit/{visit}" if visit else ""))
            correction_id = stable_id(source.task_run_id, f"d8/correction/{root_id}")
            records = (self.store.load_d4_operation(root_id), self.store.load_d4_operation(correction_id))
            if any(item is not None and item.application_ref is not None for item in records):
                continue
            request = self.evidence.read_request(root_id) or D8ModelRequest(
                root_id, source, "fact_proposal", self.config.inner_role.role_config_ref,
                self.controller.control_state.latest_checkpoint.sequence, feedback=self._feedback(source.plan_id),
                goal=self.controller.control_state.task_run.request.goal,
                intent_description=next(i.description for i in self.controller.control_state.intents
                                        if i.intent_id == source.intent_id))
            if self.prepare_inner_request is not None:
                request = self.prepare_inner_request(request)
            proposal_id, candidate = await self._call(request, token)
            from agent_core.intent_execution import WorkingRecord, validate_working
            if isinstance(candidate, WorkingRecord):
                validate_working(self.app, self.evidence.read_request(proposal_id), candidate)
                return proposal_id, candidate
            if isinstance(candidate, D8ReviewRequestAction):
                return proposal_id, candidate
            if not fact_evidence_matches(candidate, source):
                raise D3RecoveryRequired("d8_candidate_source_not_in_context")
            review_id = self._id(f"review/{proposal_id}")
            review = self.evidence.read_request(review_id) or D8ModelRequest(
                review_id, source, "fact_review", self.config.outer_role.role_config_ref,
                self.controller.control_state.latest_checkpoint.sequence, candidate=candidate,
                goal=request.goal, intent_description=request.intent_description)
            if self.config.contract_version == 22 and not isinstance(review, D22FactReviewRequest) and self.evidence.read_request(review_id) is None:
                from dataclasses import fields
                review = D22FactReviewRequest(**{f.name: getattr(review, f.name) for f in fields(review)},
                                              existing_facts=self.reusable_facts(source))
            try:
                actual_id, decision = await self._call(review, token)
            except OptionalConclusionFormatError as exc:
                raise OptionalConclusionFormatError(exc.request_id, proposal_id) from exc
            outcome = ("no_new_information" if decision.decision == "no_new_information" else
                       "verified_fact" if decision.decision == "reuse" else self._outcome(candidate, source, decision))
            fact_id = self.fact_choice(review, candidate, decision) if outcome in {"verified_fact", "no_new_information"} else None
            record = D8Conclusion(self._id(f"conclusion/{proposal_id}"), source.task_run_id,
                                  source.turn_id, source.intent_id, source.plan_id, proposal_id,
                                  actual_id, conclusion_digest(asdict(candidate)), source.source_version_sha256,
                                  outcome, fact_id)
            return proposal_id, record
        raise D3RecoveryRequired("d8_proposal_allowance_exhausted")

    def validate_record(self, record, state, *, historical=False):
        if record.plan_id is None:return self.app.evidence_conclusions.validate_record(record,state)
        proposal = self.evidence.read_request(record.proposal_request_id)
        review = self.evidence.read_request(record.review_request_id)
        if proposal is None or review is None:
            raise D3RecoveryRequired("d8_approval_evidence_unavailable")
        raw_proposal, raw_review = self._known(proposal), self._known(review)
        if raw_proposal is None or raw_review is None or raw_proposal["outcome"] != "valid" or raw_review["outcome"] != "valid":
            raise D3RecoveryRequired("d8_approval_not_confirmed")
        candidate = self._parse(proposal, raw_proposal["raw"])
        decision = self._parse(review, raw_review["raw"])
        source = self.observation(review.observation.proof_id, historical=historical)
        outcome = ("no_new_information" if decision.decision == "no_new_information" else
                   "verified_fact" if decision.decision == "reuse" else self._outcome(candidate, source, decision))
        expected_fact = self.fact_choice(review, candidate, decision) if outcome in {"verified_fact", "no_new_information"} else None
        if (not isinstance(candidate, D8Candidate) or proposal.stage != "fact_proposal"
                or review.stage != "fact_review" or review.candidate != candidate
                or proposal.observation != source or review.observation != source
                or (record.task_run_id, record.turn_id, record.intent_id, record.plan_id)
                != (source.task_run_id, source.turn_id, source.intent_id, source.plan_id)
                or record.source_version_sha256 != source.source_version_sha256
                or record.candidate_sha256 != conclusion_digest(asdict(candidate))
                or record.outcome != outcome or record.fact_id != expected_fact
                or not fact_evidence_matches(candidate, source)):
            raise D3RecoveryRequired("d8_approval_binding_changed")
        if not historical:
            self._scope(source)
        if (proposal.base_checkpoint_sequence > state.latest_checkpoint.sequence
                or review.base_checkpoint_sequence > state.latest_checkpoint.sequence
                or not historical and review.base_checkpoint_sequence != state.latest_checkpoint.sequence):
            raise D3RecoveryRequired("d8_approval_checkpoint_changed")
        return source

    def review_trigger(self, reference, intent_id):
        request = self.evidence.read_request(reference)
        if request is None:
            return None
        raw = self._known(request)
        if (raw is None or raw["outcome"] != "valid" or request.stage != "fact_proposal"
                or request.observation.intent_id != intent_id
                or not isinstance(self._parse(request, raw["raw"]), D8ReviewRequestAction)):
            raise D3RecoveryRequired("d8_review_trigger_invalid")
        self._scope(request.observation)
        return "fact_proposal", request.observation.plan_id

    def read_inferences(self, task_run_id):
        if task_run_id != self.controller.control_state.task_run.task_run_id:
            raise D3RecoveryRequired("d8_query_owner_mismatch")
        result = []
        state = self.controller.control_state
        for record in self.store.list_d8_conclusions(task_run_id):
            if record.plan_id is None:continue
            if record.outcome != "reviewed_inference":
                continue
            plan = next((p for p in state.plans if p.plan_id == record.plan_id), None)
            intent = next((i for i in state.intents if i.intent_id == record.intent_id), None)
            turn = next((t for t in state.turns if t.turn_id == record.turn_id), None)
            active = (plan is not None and plan.status is PlanStatus.RUNNING
                      and intent is not None and intent.status is IntentStatus.RUNNING
                      and turn is not None and turn.status is TurnStatus.RUNNING
                      and state.task_run.status is TaskRunStatus.RUNNING
                      and self.clock() < state.task_run.deadline_at)
            scope_status = "active" if active else "ended"
            try:
                request = self.evidence.read_request(record.review_request_id)
                proposal = self.evidence.read_request(record.proposal_request_id)
                if request is None or proposal is None:
                    raise D3RecoveryRequired("d8_inference_source_unavailable")
                known = self._known(request, repair=False)
                proposed = self._known(proposal, repair=False)
                if (known is None or known["outcome"] != "valid" or request.candidate is None
                        or proposed is None or proposed["outcome"] != "valid"):
                    raise D3RecoveryRequired("d8_inference_evidence_unavailable")
                source = self.observation(request.observation.proof_id, historical=True)
                if (request.stage != "fact_review" or proposal.stage != "fact_proposal"
                        or self._parse(proposal, proposed["raw"]) != request.candidate
                        or proposal.observation != source or request.observation != source
                        or (record.task_run_id, record.turn_id, record.intent_id, record.plan_id)
                        != (source.task_run_id, source.turn_id, source.intent_id, source.plan_id)
                        or record.candidate_sha256 != conclusion_digest(asdict(request.candidate))
                        or record.source_version_sha256 != source.source_version_sha256
                        or request.candidate.assertion is not None
                        or not fact_evidence_matches(request.candidate, source)
                        or D8ReviewDecision.from_dict(known["raw"]).decision != "accept"):
                    raise D3RecoveryRequired("d8_inference_binding_changed")
                result.append(D8InferenceView(record, "available", scope_status, request.candidate, source))
            except D3RecoveryRequired:
                result.append(D8InferenceView(record, "unavailable", scope_status, None, None,
                                              "d8_inference_source_unavailable"))
        return tuple(result)
