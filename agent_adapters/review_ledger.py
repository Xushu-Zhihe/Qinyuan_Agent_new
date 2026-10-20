"""Atomic append-only evidence for D2 reviews; control state remains Controller-owned."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from agent_core.review_contracts import D2ReviewOutput, D2ReviewRequest, d2_payload, MAX_REVIEW_ATTEMPTS, LEGACY_REVIEW_CALL_LIMIT


class FileD2ReviewLedger:
    """Persist a call before dispatch and retain immutable completed results."""

    review_call_limit = LEGACY_REVIEW_CALL_LIMIT

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "review-ledger.json"
        self._lock = RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {
                "roots": {},
                "review_calls": [],
                "applications": [],
                "not_called": [],
            }
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "roots",
                "review_calls",
                "applications",
                "not_called",
            }
            or not isinstance(value["roots"], dict)
            or any(
                not isinstance(value[name], list)
                for name in ("review_calls", "applications", "not_called")
            )
        ):
            raise ValueError("d2_review_ledger_shape_invalid")
        return value

    def _write(self, value: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, prefix=".review-", delete=False
        ) as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _root(data: dict, request: D2ReviewRequest) -> dict:
        root = data["roots"].setdefault(
            request.review_root_id,
            {
                "review_root_id": request.review_root_id,
                "task_run_id": request.task_run_id,
                "turn_id": request.turn_id,
                "intent_id": request.intent_id,
                "review_scope": request.review_scope,
                "action_candidate_id": request.action_candidate_id,
                "attempts_used": 0,
                "current_generation": request.review_generation,
                "applied": [],
                "active_constraints": [],
                "calls": [],
                "trigger_audit": [],
                "pause_audit": [],
                "constraint_version": request.constraint_version,
            },
        )
        if not isinstance(root, dict) or any(
            root.get(name) != getattr(request, name)
            for name in (
                "task_run_id",
                "turn_id",
                "intent_id",
                "review_scope",
                "action_candidate_id",
            )
        ):
            raise ValueError("d2_review_root_identity_mismatch")
        return root

    def read_root(self, review_root_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._read()["roots"].get(review_root_id)

    def list_roots(self, task_run_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                root
                for root in self._read()["roots"].values()
                if root["task_run_id"] == task_run_id
            )

    def review_call_count(self, task_run_id: str | None = None) -> int:
        with self._lock:
            return sum(
                task_run_id is None or call["request"]["task_run_id"] == task_run_id
                for call in self._read()["review_calls"]
            )

    def begin_attempt(self, request: D2ReviewRequest) -> None:
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            prior = next(
                (
                    call
                    for call in data["review_calls"]
                    if call["review_id"] == request.review_id
                ),
                None,
            )
            if prior is not None:
                if prior["request"] != json.loads(json.dumps(d2_payload(request))):
                    raise ValueError("d2_review_attempt_identity_mismatch")
                return
            used = root["attempts_used"]
            if used >= MAX_REVIEW_ATTEMPTS or request.review_attempt != used + 1:
                raise ValueError("review_retry_exhausted")
            if (
                self.review_call_limit is not None and sum(
                    call["request"]["task_run_id"] == request.task_run_id
                    for call in data["review_calls"]
                )
                >= self.review_call_limit
            ):
                raise ValueError("review_budget_exhausted")
            call = {
                "review_id": request.review_id,
                "review_root_id": request.review_root_id,
                "review_generation": request.review_generation,
                "review_attempt": request.review_attempt,
                "request": d2_payload(request),
                "outcome": "started",
                "response": None,
                "provider_request_id": None,
                "reason": None,
            }
            data["review_calls"].append(call)
            root.setdefault("calls", []).append(call)
            root.update(
                attempts_used=used + 1,
                current_generation=request.review_generation,
                last_outcome="started",
                last_context_version=request.context_version,
                last_request=d2_payload(request),
            )
            self._write(data)

    def finish_attempt(
        self,
        request: D2ReviewRequest,
        *,
        outcome: str,
        response: object,
        provider_request_id: str | None,
        reason: str | None = None,
    ) -> None:
        if outcome not in {
            "accepted",
            "rejected",
            "empty_response",
            "invalid_response",
            "authentication_failed",
            "refused",
            "provider_failure",
            "unknown",
        }:
            raise ValueError("d2_review_outcome_invalid")
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            call = next(
                (
                    item
                    for item in data["review_calls"]
                    if item["review_id"] == request.review_id
                ),
                None,
            )
            if call is None or call["request"] != json.loads(
                json.dumps(d2_payload(request))
            ):
                raise ValueError("d2_review_attempt_not_started")
            completion = {
                "outcome": outcome,
                "response": response,
                "provider_request_id": provider_request_id,
                "reason": reason,
            }
            if call["outcome"] != "started":
                if any(call.get(key) != value for key, value in completion.items()):
                    raise ValueError("d2_review_completion_immutable")
                return
            call.update(completion)
            for stored in root.setdefault("calls", []):
                if stored["review_id"] == request.review_id:
                    stored.update(completion)
            root["last_outcome"] = outcome
            root["last_response"] = response
            root["last_reason"] = reason
            root["last_provider_request_id"] = provider_request_id
            self._write(data)

    def record_attempt(
        self,
        request: D2ReviewRequest,
        *,
        outcome: str,
        response: object,
        provider_request_id: str | None,
    ) -> None:
        """Compatibility helper for already-completed synthetic records."""
        self.begin_attempt(request)
        self.finish_attempt(
            request,
            outcome=outcome,
            response=response,
            provider_request_id=provider_request_id,
        )

    def record_application(
        self, request: D2ReviewRequest, output: D2ReviewOutput, application_ref: str
    ) -> None:
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            application = {
                "review_id": request.review_id,
                "review_root_id": request.review_root_id,
                "review_generation": request.review_generation,
                "context_version": request.context_version,
                "source_versions": list(request.source_versions),
                "application_ref": application_ref,
                "output": d2_payload(output),
            }
            application = json.loads(json.dumps(application))
            prior = next(
                (
                    item
                    for item in root["applied"]
                    if item["review_id"] == request.review_id
                ),
                None,
            )
            if prior is not None:
                if prior != application:
                    raise ValueError("d2_review_application_immutable")
                return
            call = next(
                (
                    item
                    for item in root.get("calls", [])
                    if item["review_id"] == request.review_id
                ),
                None,
            )
            if call is None or call["outcome"] != "accepted":
                raise ValueError("d2_review_application_requires_accepted_result")
            data["applications"].append(application)
            root["applied"].append(application)
            root["active_constraints"].extend(application["output"]["constraints"])
            root["constraint_version"] += bool(output.constraints)
            self._write(data)

    def record_trigger(
        self, request: D2ReviewRequest, reasons: tuple[str, ...]
    ) -> None:
        if any(not isinstance(item, str) or not item.strip() for item in reasons):
            raise ValueError("d2_review_trigger_reason_invalid")
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            reasons_list = list(dict.fromkeys(reasons))
            entry = {
                "context_version": request.context_version,
                "review_generation": request.review_generation,
                "reasons": reasons_list,
            }
            contexts = root.setdefault("trigger_reasons", {})
            merged = contexts.setdefault(request.context_version, [])
            if not isinstance(merged, list) or any(
                not isinstance(item, str) for item in merged
            ):
                raise ValueError("d2_review_trigger_audit_invalid")
            merged.extend(reason for reason in reasons_list if reason not in merged)
            if entry not in root.setdefault("trigger_audit", []):
                root["trigger_audit"].append(entry)
                self._write(data)

    def record_pause(self, request: D2ReviewRequest, reason: str, outcome: str) -> None:
        with self._lock:
            data = self._read()
            root = self._root(data, request)
            entry = {
                "review_id": request.review_id,
                "context_version": request.context_version,
                "reason": reason,
                "outcome": outcome,
            }
            if entry not in root.setdefault("pause_audit", []):
                root["pause_audit"].append(entry)
                self._write(data)

    def record_not_called(self, request: D2ReviewRequest, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("not_called_reason_invalid")
        with self._lock:
            data = self._read()
            entry = {
                "review_id": request.review_id,
                "review_root_id": request.review_root_id,
                "review_generation": request.review_generation,
                "review_scope": request.review_scope,
                "reason": reason,
            }
            if entry not in data["not_called"]:
                data["not_called"].append(entry)
                self._write(data)
