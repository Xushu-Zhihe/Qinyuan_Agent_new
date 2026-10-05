"""Bounded model-summary refinement after a committed trusted review."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import (
    D4BudgetExhausted,
    D4OperationCompletion,
    D4OperationStart,
)
from agent_core.process_facts import (
    D5Attempt,
    D5Candidate,
    D5ProcessFact,
    D5RefinementRequest,
    D5Source,
    derive_d5_conflicts,
    parse_d5_candidate,
)
from agent_core.turn_modeling import digest, stable_id


class ProcessRefinement:
    def __init__(
        self,
        controller,
        store,
        evidence,
        sources,
        adapter,
        config,
        clock,
        stopped,
        redactor,
        *,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.controller = controller
        self.store = store
        self.evidence = evidence
        self.sources = sources
        self.adapter = adapter
        self.config = config
        self.clock = clock
        self.stopped = stopped
        self.redactor = redactor
        self.timeout_seconds = timeout_seconds

    def _prior(self, task_run_id: str) -> tuple[tuple[D5ProcessFact, D5Candidate], ...]:
        prior: list[tuple[D5ProcessFact, D5Candidate]] = []
        for fact in self.store.list_process_facts(task_run_id):
            candidate = self.evidence.read_candidate(fact.model_request_id)
            if candidate is None or digest(asdict(candidate)) != fact.evidence_sha256:
                raise D3RecoveryRequired("d5_committed_summary_evidence_unavailable")
            prior.append((fact, candidate))
        return tuple(prior)

    def context_for(self, resource_ref: str) -> tuple[dict[str, object], ...]:
        """Select available current summaries, then relevant historical summaries."""
        state = self.controller.control_state
        current: list[dict[str, object]] = []
        historical: list[dict[str, object]] = []
        related = {resource_ref}
        for plan in state.plans:
            if resource_ref in {plan.plan_id, plan.intent_id}:
                related.update((plan.plan_id, plan.intent_id))
        for intent in state.intents:
            if intent.intent_id in related or intent.turn_id in related:
                related.update((intent.intent_id, intent.turn_id))
        for fact in reversed(self.store.list_process_facts(state.task_run.task_run_id)):
            projections = tuple(
                self.sources.project(state.task_run.task_run_id, source)
                for source in fact.sources
            )
            if not projections or any(
                item["status"] != "available"
                or item["applicability"] not in {"current", "historical"}
                for item in projections
            ):
                continue
            candidate = self.evidence.read_candidate(fact.model_request_id)
            if candidate is None or digest(asdict(candidate)) != fact.evidence_sha256:
                continue
            scope = (
                "historical"
                if any(item["applicability"] == "historical" for item in projections)
                else "current"
            )
            if (
                scope == "historical"
                and resource_ref != state.task_run.task_run_id
                and not any(
                    isinstance(value, str) and value in related
                    for item in projections
                    for value in (item.get("origin") or {}).values()
                )
            ):
                continue
            if self.redactor.contains_sensitive(candidate.summary_text) or any(
                self.redactor.contains_sensitive(claim.claim_key)
                or self.redactor.contains_sensitive(claim.claim_value)
                for claim in candidate.claims
            ):
                continue
            value = {
                "identity": "model_summary",
                "process_fact_id": fact.process_fact_id,
                "version": fact.version,
                "applicability": scope,
                "summary_text": candidate.summary_text,
                "claims": tuple(asdict(claim) for claim in candidate.claims),
                "sources": tuple(asdict(source) for source in fact.sources),
                "conflicts": tuple(asdict(conflict) for conflict in fact.conflicts),
            }
            (current if scope == "current" else historical).append(value)
        return tuple((current + historical)[:4])

    def _read_source(
        self, task_run_id: str, object_id: str, kind: str | None = None
    ) -> D5Source | None:
        try:
            return self.sources.read(task_run_id, object_id, kind)
        except D3RecoveryRequired:
            raise
        except Exception as exc:
            raise D3RecoveryRequired("d5_source_read_unavailable") from exc

    def _result(self, attempt: D5Attempt, outcome: str, reason: str) -> None:
        self.evidence.save_result(
            attempt.model_request_id, outcome, self.clock(), reason
        )

    def _seal(self, attempt: D5Attempt) -> None:
        result = self.evidence.read_result(attempt.model_request_id)
        if result is None:
            self._result(attempt, "response_unknown", "not_applied_response_unknown")
            result = self.evidence.read_result(attempt.model_request_id)
        if (
            result is None
            or result["outcome"] != "response_unknown"
            or result["reason"] != "not_applied_response_unknown"
        ):
            raise D3RecoveryRequired("d5_unknown_disposition_evidence_changed")
        self.store.seal_unknown_refinement(
            attempt.model_request_id,
            decided_at=result["finished_at"],
            evidence_ref=attempt.model_request_id,
            evidence_sha256=digest(result),
        )
        if self.evidence.read_result(attempt.model_request_id) != result:
            raise D3RecoveryRequired("d5_unknown_disposition_evidence_changed")

    def _validate_candidate(
        self, candidate: D5Candidate, attempt: D5Attempt
    ) -> D5Candidate:
        prior = self._prior(attempt.task_run_id)
        parsed = parse_d5_candidate(
            asdict(candidate),
            attempt.sources,
            tuple(item[0] for item in prior),
            tuple(item[1] for item in prior),
        )
        if self.redactor.contains_sensitive(parsed.summary_text) or any(
            self.redactor.contains_sensitive(claim.claim_key)
            or self.redactor.contains_sensitive(claim.claim_value)
            for claim in parsed.claims
        ):
            raise ValueError("d5_sensitive_candidate")
        return parsed

    def validate(self, mutation, state) -> None:
        fact = mutation.state_changes[0]
        if (
            not isinstance(fact, D5ProcessFact)
            or fact.task_run_id != state.task_run.task_run_id
        ):
            raise D3RecoveryRequired("d5_summary_owner_changed")
        attempt = self.store.load_d5_attempt(fact.model_request_id)
        request = self.evidence.read_request(fact.model_request_id)
        result = self.evidence.read_result(fact.model_request_id)
        candidate = self.evidence.read_candidate(fact.model_request_id)
        if (
            attempt is None
            or request is None
            or request.attempt != attempt
            or result is None
            or result["outcome"] != "accepted"
            or candidate is None
            or attempt.sources != fact.sources
            or digest(asdict(candidate)) != fact.evidence_sha256
        ):
            raise D3RecoveryRequired("d5_summary_evidence_changed")
        for source in fact.sources:
            if (
                self._read_source(fact.task_run_id, source.object_id, source.kind)
                != source
            ):
                raise D3RecoveryRequired("d5_summary_source_changed")
        self._validate_candidate(candidate, attempt)
        expected = derive_d5_conflicts(
            candidate, attempt.sources, self._prior(fact.task_run_id)
        )
        if fact.conflicts != expected or fact.process_fact_id != stable_id(
            fact.task_run_id, f"d5/process/{fact.model_request_id}"
        ):
            raise D3RecoveryRequired("d5_summary_conflict_or_id_changed")

    def _finish_saved(self, attempt: D5Attempt) -> None:
        operation = self.store.load_d4_operation(attempt.model_request_id)
        request = self.evidence.read_request(attempt.model_request_id)
        if operation is None or request is None or request.attempt != attempt:
            raise D3RecoveryRequired("d5_attempt_binding_unavailable")
        if self.store.is_unknown_refinement_sealed(attempt.model_request_id):
            self._seal(attempt)
            return
        result = self.evidence.read_result(attempt.model_request_id)
        candidate = self.evidence.read_candidate(attempt.model_request_id)
        if result is None:
            if candidate is None:
                self._seal(attempt)
                return
            self._validate_candidate(candidate, attempt)
            self._result(attempt, "accepted", "candidate_accepted")
            result = self.evidence.read_result(attempt.model_request_id)
        if result["outcome"] == "response_unknown":
            self._seal(attempt)
            return
        accepted = result["outcome"] == "accepted"
        if accepted:
            if candidate is None:
                raise D3RecoveryRequired("d5_accepted_candidate_missing")
            self._validate_candidate(candidate, attempt)
        completion = D4OperationCompletion(
            attempt.model_request_id,
            result["finished_at"],
            "completed" if accepted else "failed",
            attempt.model_request_id,
            digest(asdict(candidate)) if accepted else digest(result),
        )
        if operation.completion is None:
            self.store.complete_d4_operation(completion)
        elif operation.completion != completion:
            raise D3RecoveryRequired("d5_completion_evidence_changed")
        if not accepted or operation.application_ref is not None or self.stopped():
            return
        prior = self._prior(attempt.task_run_id)
        fact = D5ProcessFact(
            stable_id(attempt.task_run_id, f"d5/process/{attempt.model_request_id}"),
            attempt.task_run_id,
            len(prior) + 1,
            attempt.review_id,
            attempt.model_request_id,
            result["finished_at"],
            attempt.sources,
            completion.evidence_sha256,
            derive_d5_conflicts(candidate, attempt.sources, prior),
        )
        self.controller.commit_process_fact(
            fact, stable_id(attempt.task_run_id, f"d5/apply/{attempt.model_request_id}")
        )

    def reconcile(self) -> None:
        if not self.config.d5_enabled:
            return
        for attempt in self.store.list_d5_attempts(
            self.controller.control_state.task_run.task_run_id
        ):
            self._finish_saved(attempt)

    def after_review(self, review_id: str) -> None:
        if not self.config.d5_enabled:
            return
        if not self.redactor.configured:
            raise D3RecoveryRequired("d5_redaction_configuration_unavailable")
        state = self.controller.control_state
        task_id = state.task_run.task_run_id
        existing = next(
            (
                item
                for item in self.store.list_d5_attempts(task_id)
                if item.review_id == review_id
            ),
            None,
        )
        if existing is not None:
            self._finish_saved(existing)
            return
        if self.stopped():
            return
        budget = self.config.d4_budget
        if budget is None:
            raise D3RecoveryRequired("d5_budget_configuration_unavailable")
        usage = self.store.d4_budget_usage(task_id)
        if (
            usage.total_used >= budget.total
            or dict(usage.category_counts)["refinement"] >= budget.refinement
        ):
            return
        review = self.store.load_d3_review_application(task_id, review_id)
        if review is None or review.paused_gate:
            raise D3RecoveryRequired("d5_review_not_committed")
        sources = tuple(
            source
            for ref in review.source_ids
            if (source := self._read_source(task_id, ref)) is not None
        )
        tried = {
            source.version_key
            for item in self.store.list_d5_attempts(task_id)
            for source in item.sources
        }
        if not sources or not any(
            source.version_key not in tried for source in sources
        ):
            return
        projected = tuple(self.sources.project(task_id, source) for source in sources)
        if any(item["status"] != "available" for item in projected):
            raise D3RecoveryRequired("d5_review_sources_unavailable")
        context = self.context_for(review.intent_id)
        safe_sources = tuple(
            self.redactor.redact_value(
                {"source": asdict(source), "content": item["content"]}
            )
            for source, item in zip(sources, projected, strict=True)
        )
        context_sha = digest({"sources": safe_sources, "process_facts": context})
        attempt = D5Attempt(
            stable_id(task_id, f"d5/refine/{review_id}"),
            task_id,
            review_id,
            state.latest_checkpoint.sequence,
            self.clock(),
            self.config.outer_role.role_config_ref,
            sources,
            context_sha,
        )
        request = D5RefinementRequest(attempt, safe_sources, context)
        if self.adapter.role_config != self.config.outer_role:
            raise D3RecoveryRequired("d5_frozen_role_changed")
        start = D4OperationStart(
            attempt.model_request_id,
            task_id,
            "refinement",
            attempt.started_at,
            attempt.base_checkpoint_sequence,
            context_sha,
            review_id,
            self.config.d4_budget.budget_policy_ref,
            "outer_planning",
            attempt.role_config_ref,
        )
        self.evidence.save_request(request)
        try:
            self.store.begin_d5_attempt(attempt, start)
        except D4BudgetExhausted:
            return
        finished = threading.Event()
        values = []
        stopped = threading.Event()
        owner = self

        class Token:
            @property
            def is_cancelled(self):
                return stopped.is_set() or owner.stopped()

        timeout = min(self.timeout_seconds, state.task_run.deadline_at - self.clock())

        def invoke():
            try:
                values.append((True, self.adapter.invoke(request, timeout, Token())))
            # Process-loss simulation must propagate without sealing an unknown result.
            except BaseException as exc:  # noqa: BLE001
                values.append((False, exc))
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True, name="d5-refinement").start()
        limit = time.monotonic() + timeout
        while not finished.wait(0.01):
            if self.stopped() or time.monotonic() >= limit:
                stopped.set()
                self._seal(attempt)
                return
        succeeded, raw = values[0]
        stopped.set()
        if not succeeded:
            if isinstance(raw, Exception):
                self._seal(attempt)
            else:
                raise raw
            return
        if isinstance(raw, ModelError):
            if raw.kind in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED}:
                self._seal(attempt)
            else:
                self._result(attempt, "failed", "model_failure")
                self._finish_saved(attempt)
            return
        try:
            prior = self._prior(task_id)
            candidate = parse_d5_candidate(
                raw,
                sources,
                tuple(item[0] for item in prior),
                tuple(item[1] for item in prior),
            )
            self._validate_candidate(candidate, attempt)
        except (ValueError, TypeError):
            self._result(attempt, "invalid", "candidate_rejected")
        else:
            self.evidence.save_candidate(attempt.model_request_id, candidate)
            self._result(attempt, "accepted", "candidate_accepted")
        self._finish_saved(attempt)
