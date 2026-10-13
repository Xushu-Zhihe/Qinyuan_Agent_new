"""Recoverable D8 review gate, independent of model and storage SDKs."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, replace

from agent_core.conclusion_contracts import (
    D8Candidate, D8Conclusion, D8Feedback, D8InferenceView, D8ModelRequest, D8Observation,
    D8ReviewDecision, D8ReviewRequestAction, conclusion_digest, parse_d8_proposal,
)
from agent_core.runtime import TaskRunStatus
from agent_core.domain import IntentStatus, PlanStatus, TurnStatus
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4OperationCompletion, D4OperationStart
from agent_core.turn_modeling import stable_id


class ConclusionReview:
    def __init__(self, controller, store, evidence, proofs, workspace, config,
                 inner_model, outer_model, redactor, clock, stopped):
        self.controller, self.store, self.evidence = controller, store, evidence
        self.proofs, self.workspace, self.config = proofs, workspace, config
        self.inner_model, self.outer_model = inner_model, outer_model
        self.redactor, self.clock, self.stopped = redactor, clock, stopped
        self.request_capacity_check = None
        self.prepare_inner_request = None

    def _id(self, name):
        return stable_id(self.controller.control_state.task_run.task_run_id, f"d8/{name}")

    def observation(self, proof_id: str, *, historical: bool = False) -> D8Observation:
        proof = self.proofs.proofs.read(proof_id)
        if proof is None:
            raise D3RecoveryRequired("d8_proof_missing")
        checked, _ = self.proofs.read_verified(proof.source_execution_id, proof_id,
                                             allow_inactive_plan=historical)
        state = self.controller.control_state
        if checked.task_run_id != state.task_run.task_run_id:
            raise D3RecoveryRequired("d8_source_owner_mismatch")
        receipt = self.workspace.read_digest(proof.observation_id)
        binding = self.store.load_d3_turn_binding(proof.turn_id)
        if receipt is None or binding is None:
            raise D3RecoveryRequired("d8_source_unavailable")
        version = conclusion_digest({"proof": asdict(checked), "receipt": asdict(receipt),
                                     "binding": asdict(binding)})
        return D8Observation(proof.task_run_id, proof.turn_id, proof.intent_id,
                             proof.plan_id, proof.artifact_version_id, proof.observation_id,
                             proof.proof_id, proof.source_execution_id, proof.observed_sha256,
                             binding.expected_byte_size, receipt.completed_at, version)

    def _scope(self, source: D8Observation) -> None:
        state = self.controller.control_state
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
        return parse_d8_proposal(raw) if request.stage == "fact_proposal" else D8ReviewDecision.from_dict(raw)

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
        if (set(result) != {"model_request_id", "request_sha256", "outcome", "raw", "finished_at"}
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
        record = self.store.load_d4_operation(request.model_request_id)
        result = self.evidence.read_result(request.model_request_id)
        if record is None:
            if result is not None:
                raise D3RecoveryRequired("d8_result_without_admission")
            return None
        expected = D4OperationStart(request.model_request_id, request.task_run_id,
                                   "action" if request.stage == "fact_proposal" else "fact_review",
                                   record.start.started_at, request.base_checkpoint_sequence,
                                   request.context_sha256, request.observation.plan_id,
                                   self.config.d4_budget.budget_policy_ref,
                                   "inner_action" if request.stage == "fact_proposal" else "outer_planning",
                                   request.role_config_ref)
        role = self.config.inner_role if request.stage == "fact_proposal" else self.config.outer_role
        if record.start != expected or request.role_config_ref != role.role_config_ref:
            raise D3RecoveryRequired("d8_saved_request_binding_changed")
        if result is None:
            boundary = getattr(self, "output_boundary", None)
            failure = boundary.failure(request) if boundary is not None else None
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
            if request is not None and (self.evidence.read_result(request.model_request_id) is not None or known_format):
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
        try:
            while not finished.is_set():
                if self.stopped() or asyncio.get_running_loop().time() >= expires:
                    raise D3RecoveryRequired("d8_model_outcome_unknown")
                await asyncio.sleep(0.01)
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
                request.observation.plan_id, self.config.d4_budget.budget_policy_ref,
                "inner_action" if request.stage == "fact_proposal" else "outer_planning",
                request.role_config_ref))
            raw = await self._invoke(request, token)
            outcome = "valid"
            if isinstance(raw, ModelError):
                outcome, raw = "model_error", None
            elif self._sensitive(raw):
                outcome, raw = "sensitive_rejected", None
            else:
                try:
                    self._parse(request, raw)
                except (ValueError, TypeError, KeyError):
                    outcome, raw = "invalid", None
            if self.stopped() or token.is_cancelled:
                outcome, raw = "stale", None
            result = {"model_request_id": request.model_request_id,
                      "request_sha256": request.context_sha256, "outcome": outcome,
                      "raw": raw, "finished_at": self.clock()}
            self.evidence.save_result(request.model_request_id, result)
            self.store.complete_d4_operation(self._completion(request, result))
        boundary = getattr(self, "output_boundary", None)
        if boundary is not None and result["outcome"] != "stale":
            boundary.terminal_check(request)
        if result["outcome"] == "invalid" and request.correction_of_model_request_id is None:
            self._scope(request.observation)
            correction_id = stable_id(request.task_run_id, f"d8/correction/{request.model_request_id}")
            correction = self.evidence.read_request(correction_id) or replace(
                request, model_request_id=correction_id,
                base_checkpoint_sequence=self.controller.control_state.latest_checkpoint.sequence,
                correction_of_model_request_id=request.model_request_id,
                rejection_code="d8_response_contract_invalid")
            return await self._call(correction, token)
        if result["outcome"] != "valid":
            raise D3RecoveryRequired(f"d8_model_{result['outcome']}")
        self._scope(request.observation)
        return request.model_request_id, self._parse(request, result["raw"])

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
            result.append(D8Feedback(record.record_id, record.outcome, reason, request.candidate.content))
        return tuple(result)

    @staticmethod
    def _outcome(candidate, source, decision):
        if decision.decision != "accept":
            return "rejected" if decision.decision == "reject" else "insufficient"
        if candidate.assertion is None:
            return "reviewed_inference"
        if (candidate.assertion.artifact_version_id != source.artifact_version_id
                or candidate.assertion.observation_id != source.observation_id):
            return "rejected"
        return "verified_fact"

    async def propose(self, proof_id, token):
        source = self.observation(proof_id)
        self._scope(source)
        for ordinal in range(1, 17):
            root_id = self._id(f"proposal/{source.plan_id}/{ordinal}")
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
            if isinstance(candidate, D8ReviewRequestAction):
                return proposal_id, candidate
            if set(candidate.evidence_refs) != {source.proof_id}:
                raise D3RecoveryRequired("d8_candidate_source_not_in_context")
            review_id = self._id(f"review/{proposal_id}")
            review = self.evidence.read_request(review_id) or D8ModelRequest(
                review_id, source, "fact_review", self.config.outer_role.role_config_ref,
                self.controller.control_state.latest_checkpoint.sequence, candidate=candidate,
                goal=request.goal, intent_description=request.intent_description)
            actual_id, decision = await self._call(review, token)
            outcome = self._outcome(candidate, source, decision)
            record = D8Conclusion(self._id(f"conclusion/{proposal_id}"), source.task_run_id,
                                  source.turn_id, source.intent_id, source.plan_id, proposal_id,
                                  actual_id, conclusion_digest(asdict(candidate)), source.source_version_sha256,
                                  outcome, stable_id(source.task_run_id, f"d3/fact/{source.plan_id}")
                                  if outcome == "verified_fact" else None)
            return proposal_id, record
        raise D3RecoveryRequired("d8_proposal_allowance_exhausted")

    def validate_record(self, record, state):
        proposal = self.evidence.read_request(record.proposal_request_id)
        review = self.evidence.read_request(record.review_request_id)
        if proposal is None or review is None:
            raise D3RecoveryRequired("d8_approval_evidence_unavailable")
        raw_proposal, raw_review = self._known(proposal), self._known(review)
        if raw_proposal is None or raw_review is None or raw_proposal["outcome"] != "valid" or raw_review["outcome"] != "valid":
            raise D3RecoveryRequired("d8_approval_not_confirmed")
        candidate = self._parse(proposal, raw_proposal["raw"])
        decision = self._parse(review, raw_review["raw"])
        source = self.observation(review.observation.proof_id)
        if (not isinstance(candidate, D8Candidate) or proposal.stage != "fact_proposal"
                or review.stage != "fact_review" or review.candidate != candidate
                or proposal.observation != source or review.observation != source
                or (record.task_run_id, record.turn_id, record.intent_id, record.plan_id)
                != (source.task_run_id, source.turn_id, source.intent_id, source.plan_id)
                or record.source_version_sha256 != source.source_version_sha256
                or record.candidate_sha256 != conclusion_digest(asdict(candidate))
                or record.outcome != self._outcome(candidate, source, decision)
                or set(candidate.evidence_refs) != {source.proof_id}):
            raise D3RecoveryRequired("d8_approval_binding_changed")
        self._scope(source)
        if (proposal.base_checkpoint_sequence > state.latest_checkpoint.sequence
                or review.base_checkpoint_sequence != state.latest_checkpoint.sequence):
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
                        or set(request.candidate.evidence_refs) != {source.proof_id}
                        or D8ReviewDecision.from_dict(known["raw"]).decision != "accept"):
                    raise D3RecoveryRequired("d8_inference_binding_changed")
                result.append(D8InferenceView(record, "available", scope_status, request.candidate, source))
            except D3RecoveryRequired:
                result.append(D8InferenceView(record, "unavailable", scope_status, None, None,
                                              "d8_inference_source_unavailable"))
        return tuple(result)
