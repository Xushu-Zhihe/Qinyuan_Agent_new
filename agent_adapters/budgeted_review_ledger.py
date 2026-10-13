"""SQLite budget admission layered onto the existing review evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from agent_adapters.review_ledger import FileD2ReviewLedger
from agent_core.dual_llm_contracts import D3RunConfig
from agent_core.operation_accounting import (
    D4BudgetExhausted,
    D4OperationCompletion,
    D4OperationResult,
    D4OperationStart,
    D4RecoveryRequired,
)
from agent_core.review_contracts import D2ReviewOutput, D2ReviewRequest
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest

_KNOWN_FAILURES: dict[str, D4OperationResult] = {
    "rejected": "rejected",
    "empty_response": "rejected",
    "invalid_response": "rejected",
    "authentication_failed": "failed",
    "refused": "failed",
    "provider_failure": "failed",
}


class D4ReviewLedger(FileD2ReviewLedger):
    """Keep D2's root ledger while making D4's SQLite ledger authoritative for starts."""

    def __init__(
        self,
        directory: Path,
        *,
        store: SqliteControlStore,
        config: D3RunConfig,
        clock: Callable[[], float],
    ) -> None:
        if config.d4_budget is None:
            raise ValueError("D4ReviewLedger requires an explicit D4 budget")
        super().__init__(directory)
        self.store = store
        self.config = config
        self.budget = config.d4_budget
        self.clock = clock
        self.request_capacity_check = None
        self.output_boundary = None

    def read_root(self, review_root_id):
        root = super().read_root(review_root_id)
        if (self.output_boundary is not None and root is not None
                and root.get("last_outcome") == "started"):
            value = root["last_request"]
            request = D2ReviewRequest(**{**value, "source_versions": tuple(value["source_versions"])})
            failure = self.output_boundary.failure(request)
            if failure is not None:
                terminal = failure["terminal"]
                outcome = "provider_failure" if terminal else "invalid_response"
                reason = "d8_" + terminal if terminal else "d8_output_format_invalid"
                self.finish_attempt(request, outcome=outcome,
                    response={"kind": outcome, "reason": reason, "provider_request_id": None},
                    provider_request_id=None, reason=reason)
                root = super().read_root(review_root_id)
        return root

    @staticmethod
    def _items(root: dict[str, object] | None, key: str) -> tuple[dict, ...]:
        if root is None:
            return ()
        items = root.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise D4RecoveryRequired(f"d4_review_{key}_invalid")
        return tuple(items)

    def _start(self, request: D2ReviewRequest) -> D4OperationStart:
        existing = self.store.load_d4_operation(request.review_id)
        if existing is not None:
            start = existing.start
            if (
                start.task_run_id != request.task_run_id
                or start.category != "review"
                or start.resource_ref != request.review_id
                or start.context_sha256 != digest(asdict(request))
                or start.base_checkpoint_sequence != request.checkpoint_id
                or start.role != "outer_planning"
                or start.role_config_ref != self.config.outer_role.role_config_ref
                or start.budget_policy_ref != self.budget.budget_policy_ref
            ):
                raise D4RecoveryRequired("d4_review_operation_binding_changed")
            return start
        return D4OperationStart(
            operation_id=request.review_id,
            task_run_id=request.task_run_id,
            category="review",
            started_at=self.clock(),
            base_checkpoint_sequence=request.checkpoint_id,
            context_sha256=digest(asdict(request)),
            resource_ref=request.review_id,
            budget_policy_ref=self.budget.budget_policy_ref,
            role="outer_planning",
            role_config_ref=self.config.outer_role.role_config_ref,
        )

    @staticmethod
    def _completion_values(call: dict) -> tuple[D4OperationResult, str]:
        outcome = call.get("outcome")
        if not isinstance(outcome, str):
            raise D4RecoveryRequired("d4_review_call_outcome_unknown")
        result_kind: D4OperationResult | None
        if outcome == "accepted":
            result_kind = "completed"
        else:
            result_kind = _KNOWN_FAILURES.get(outcome)
        if result_kind is None:
            raise D4RecoveryRequired("d4_review_call_outcome_unknown")
        evidence = {
            "outcome": outcome,
            "response": call.get("response"),
            "provider_request_id": call.get("provider_request_id"),
            "reason": call.get("reason"),
        }
        return result_kind, digest(evidence)

    def _save_pending_request(self, request: D2ReviewRequest) -> None:
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            pending = root.setdefault("d4_pending_requests", {})
            if not isinstance(pending, dict):
                raise D4RecoveryRequired("d4_review_pending_requests_invalid")
            payload = json.loads(json.dumps(asdict(request)))
            prior = pending.get(request.review_id)
            if prior is not None and prior != payload:
                raise ValueError("d4_review_pending_request_immutable")
            if prior is None:
                pending[request.review_id] = payload
                self._write(data)

    def _drop_pending_request(self, request: D2ReviewRequest) -> None:
        with self._lock:
            data = self._read()
            root = data["roots"].get(request.review_root_id)
            pending = (
                root.get("d4_pending_requests") if isinstance(root, dict) else None
            )
            if isinstance(pending, dict) and request.review_id in pending:
                del pending[request.review_id]
                self._write(data)

    def _repair_completion(self, request: D2ReviewRequest, call: dict) -> None:
        self._start(request)
        result_kind, evidence_sha256 = self._completion_values(call)
        record = self.store.load_d4_operation(request.review_id)
        if record is None:
            raise D4RecoveryRequired("d4_review_admission_missing")
        if record.completion is None:
            self.store.complete_d4_operation(
                D4OperationCompletion(
                    request.review_id,
                    max(self.clock(), record.start.started_at),
                    result_kind,
                    request.review_id,
                    evidence_sha256,
                )
            )
            return
        if (
            record.completion.result_kind != result_kind
            or record.completion.evidence_ref != request.review_id
            or record.completion.evidence_sha256 != evidence_sha256
        ):
            raise D4RecoveryRequired("d4_review_completion_evidence_changed")

    def begin_attempt(self, request: D2ReviewRequest) -> None:
        root = self.read_root(request.review_root_id)
        call = next(
            (
                item
                for item in self._items(root, "calls")
                if isinstance(item, dict) and item.get("review_id") == request.review_id
            ),
            None,
        )
        prior = self.store.load_d4_operation(request.review_id)
        if call is None and prior is None:
            if root is not None:
                attempts = root.get("attempts_used")
                if type(attempts) is not int or attempts < 0:
                    raise D4RecoveryRequired("d4_review_attempt_count_invalid")
                if attempts >= 2:
                    raise ValueError("review_retry_exhausted")
            if self.review_call_count(request.task_run_id) >= 8:
                raise ValueError("review_budget_exhausted")
        if prior is not None and call is None:
            raise D4RecoveryRequired("d4_review_evidence_missing")
        if call is not None and call.get("outcome") not in {"started", "unknown"}:
            self._repair_completion(request, call)
            raise D4RecoveryRequired("d4_review_known_result_requires_replay")
        if prior is None:
            if self.request_capacity_check is not None:
                self.request_capacity_check(request)
            self._save_pending_request(request)
        try:
            self.store.begin_d4_operation(self._start(request))
        except D4BudgetExhausted:
            self._drop_pending_request(request)
            raise
        super().begin_attempt(request)
        self._drop_pending_request(request)

    def finish_attempt(
        self,
        request: D2ReviewRequest,
        *,
        outcome: str,
        response: object,
        provider_request_id: str | None,
        reason: str | None = None,
    ) -> None:
        super().finish_attempt(
            request,
            outcome=outcome,
            response=response,
            provider_request_id=provider_request_id,
            reason=reason,
        )
        if outcome == "unknown":
            return
        root = self.read_root(request.review_root_id)
        call = next(
            (
                item
                for item in self._items(root, "calls")
                if isinstance(item, dict) and item.get("review_id") == request.review_id
            ),
            None,
        )
        if call is None:
            raise D4RecoveryRequired("d4_review_completion_evidence_missing")
        self._repair_completion(request, call)
        self._drop_pending_request(request)

    def record_application(
        self, request: D2ReviewRequest, output: D2ReviewOutput, application_ref: str
    ) -> None:
        super().record_application(request, output, application_ref)
        record = self.store.load_d4_operation(request.review_id)
        if record is None or record.completion is None:
            raise D4RecoveryRequired("d4_review_application_without_completion")
        if record.application_ref is None:
            self.store.mark_d4_operation_applied(request.review_id, application_ref)
        elif record.application_ref != application_ref:
            raise D4RecoveryRequired("d4_review_application_ref_changed")

    def reconcile_known_completions(self, task_run_id: str) -> None:
        for record in self.store.list_d4_operations(task_run_id):
            if record.start.category != "review":
                continue
            call = None
            request_payload = None
            for root in self.list_roots(task_run_id):
                call = next(
                    (
                        item
                        for item in self._items(root, "calls")
                        if isinstance(item, dict)
                        and item.get("review_id") == record.start.operation_id
                    ),
                    None,
                )
                if call is not None:
                    request_payload = call.get("request")
                    break
            if call is None:
                if record.completion is not None:
                    raise D4RecoveryRequired("d4_review_call_missing")
                saved_request = next(
                    (
                        pending[record.start.operation_id]
                        for root in self.list_roots(task_run_id)
                        if isinstance(
                            (pending := root.get("d4_pending_requests")), dict
                        )
                        and record.start.operation_id in pending
                    ),
                    None,
                )
                if not isinstance(saved_request, dict):
                    raise D4RecoveryRequired("d4_review_call_missing")
                request = D2ReviewRequest(
                    **{
                        **saved_request,
                        "source_versions": tuple(saved_request["source_versions"]),
                    }
                )
                # SQLite admission precedes the File ledger and provider call. An
                # absent file call therefore proves no provider dispatch occurred.
                super().begin_attempt(request)
                super().finish_attempt(
                    request,
                    outcome="rejected",
                    response=None,
                    provider_request_id=None,
                    reason="d4_admission_repaired_before_provider_dispatch",
                )
                no_dispatch = next(
                    item
                    for item in self._items(
                        self.read_root(request.review_root_id), "calls"
                    )
                    if isinstance(item, dict)
                    and item.get("review_id") == request.review_id
                )
                self._repair_completion(request, no_dispatch)
                self._drop_pending_request(request)
                call = no_dispatch
                request_payload = call.get("request")
            if call.get("outcome") in {"started", "unknown"}:
                if self.output_boundary is not None and call.get("outcome") == "started":
                    root = self.read_root(request_payload["review_root_id"])
                    call = next(i for i in self._items(root, "calls") if i["review_id"] == record.start.operation_id)
            if call.get("outcome") in {"started", "unknown"}:
                if record.completion is not None:
                    raise D4RecoveryRequired("d4_review_outcome_changed")
                raise D4RecoveryRequired("d4_review_call_outcome_unknown")
            if (
                not isinstance(request_payload, dict)
                or request_payload.get("task_run_id") != task_run_id
            ):
                raise D4RecoveryRequired("d4_review_request_binding_changed")
            request = D2ReviewRequest(
                **{
                    **request_payload,
                    "source_versions": tuple(request_payload["source_versions"]),
                }
            )
            self._repair_completion(request, call)
            if record.application_ref is None:
                application = next(
                    (
                        item
                        for root in self.list_roots(task_run_id)
                        for item in self._items(root, "applied")
                        if isinstance(item, dict)
                        and item.get("review_id") == record.start.operation_id
                    ),
                    None,
                )
                if application is not None:
                    application_ref = application.get("application_ref")
                    if (
                        not isinstance(application_ref, str)
                        or not application_ref.strip()
                    ):
                        raise D4RecoveryRequired("d4_review_application_ref_invalid")
                    self.store.mark_d4_operation_applied(
                        record.start.operation_id,
                        application_ref,
                    )
