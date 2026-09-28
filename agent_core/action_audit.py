"""Metadata-only durable audit for unbounded incremental model actions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from agent_core.actions import T6Action
from agent_core.modeling import ModelError


class ActionAuditConflict(RuntimeError):
    pass


class ActionRecoveryRequired(RuntimeError):
    pass


def _uuid4(value: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("action_id_must_be_uuid4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("action_id_must_be_uuid4")


@dataclass(frozen=True, slots=True)
class ActionResourceIds:
    plan_id: str
    command_request_id: str
    proof_id: str
    fact_id: str
    candidate_id: str
    register_mutation_id: str
    start_mutation_id: str
    complete_plan_mutation_id: str
    fact_mutation_id: str
    complete_intent_mutation_id: str

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        for value in values:
            _uuid4(value)
        if len(set(values)) != len(values):
            raise ValueError("duplicate_action_resource_id")

    @classmethod
    def create(cls) -> ActionResourceIds:
        return cls(*(str(uuid4()) for _ in range(10)))


@dataclass(frozen=True, slots=True)
class ActionInvocationStart:
    model_request_id: str
    task_run_id: str
    intent_id: str
    base_checkpoint_sequence: int
    context_sha256: str
    resource_ids: ActionResourceIds
    started_at: float
    correction_of_model_request_id: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id)
        _uuid4(self.task_run_id)
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("action_intent_required")
        if type(self.base_checkpoint_sequence) is not int or self.base_checkpoint_sequence < 1:
            raise ValueError("invalid_action_checkpoint")
        if not isinstance(self.context_sha256, str) or len(self.context_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.context_sha256
        ):
            raise ValueError("invalid_action_context_digest")
        if not isinstance(self.resource_ids, ActionResourceIds):
            raise TypeError("action_resource_ids_required")
        if not isinstance(self.started_at, (int, float)) or not math.isfinite(self.started_at):
            raise ValueError("invalid_action_start_time")
        if self.correction_of_model_request_id is not None:
            _uuid4(self.correction_of_model_request_id)


ActionResultKind = Literal["action", "invalid_structure", "model_error"]
ActionResult = T6Action | ModelError


@dataclass(frozen=True, slots=True)
class ActionInvocationCompletion:
    model_request_id: str
    finished_at: float
    result_kind: ActionResultKind
    result_sha256: str

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id)
        if not isinstance(self.finished_at, (int, float)) or not math.isfinite(self.finished_at):
            raise ValueError("invalid_action_finish_time")
        if self.result_kind not in {"action", "invalid_structure", "model_error"}:
            raise ValueError("invalid_action_result_kind")
        if not isinstance(self.result_sha256, str) or len(self.result_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.result_sha256
        ):
            raise ValueError("invalid_action_result_digest")


@dataclass(frozen=True, slots=True)
class ActionInvocationRecord:
    start: ActionInvocationStart
    completion: ActionInvocationCompletion | None
    applied_at: float | None


class ActionAuditPort(Protocol):
    def begin_t6_action(self, start: ActionInvocationStart) -> ActionInvocationRecord | None: ...
    def complete_t6_action(self, completion: ActionInvocationCompletion) -> ActionInvocationRecord: ...
    def mark_t6_action_applied(self, model_request_id: str, applied_at: float) -> ActionInvocationRecord: ...
    def list_t6_actions(self, task_run_id: str, intent_id: str) -> tuple[ActionInvocationRecord, ...]: ...


class ActionEvidencePort(Protocol):
    def save(self, model_request_id: str, finished_at: float, kind: ActionResultKind,
             result: ActionResult) -> str: ...
    def read(self, model_request_id: str) -> tuple[float, ActionResultKind, ActionResult, str] | None: ...


class T6ActionJournal:
    """Recover a saved action before permitting another model invocation."""

    def __init__(self, audit: ActionAuditPort, evidence: ActionEvidencePort) -> None:
        self._audit = audit
        self._evidence = evidence

    def begin(self, start: ActionInvocationStart) -> ActionInvocationRecord | None:
        return self._audit.begin_t6_action(start)

    def list_for_intent(
        self, task_run_id: str, intent_id: str,
    ) -> tuple[ActionInvocationRecord, ...]:
        return self._audit.list_t6_actions(task_run_id, intent_id)

    def read_result(self, record: ActionInvocationRecord) -> ActionResult:
        if record.completion is None:
            raise ActionRecoveryRequired("model action has no committed result")
        saved = self._evidence.read(record.start.model_request_id)
        if saved is None:
            raise ActionRecoveryRequired("model action evidence is missing")
        finished_at, kind, result, digest = saved
        if record.completion != ActionInvocationCompletion(
            record.start.model_request_id, finished_at, kind, digest,
        ):
            raise ActionRecoveryRequired("model action evidence differs from control audit")
        return result

    def save_result(
        self, start: ActionInvocationStart, finished_at: float,
        kind: ActionResultKind, result: ActionResult,
    ) -> ActionInvocationRecord:
        records = self._audit.list_t6_actions(start.task_run_id, start.intent_id)
        record = next((item for item in records if item.start.model_request_id == start.model_request_id), None)
        if record is None or record.start != start:
            raise ActionAuditConflict("action result has no matching invocation")
        digest = self._evidence.save(start.model_request_id, finished_at, kind, result)
        return self._audit.complete_t6_action(
            ActionInvocationCompletion(start.model_request_id, finished_at, kind, digest)
        )

    def resume_unapplied(
        self, task_run_id: str, intent_id: str,
    ) -> tuple[ActionInvocationRecord, ActionResult] | None:
        records = self._audit.list_t6_actions(task_run_id, intent_id)
        if not records or records[-1].applied_at is not None:
            return None
        record = records[-1]
        saved = self._evidence.read(record.start.model_request_id)
        if saved is None:
            raise ActionRecoveryRequired("model action has no complete evidence")
        finished_at, kind, result, digest = saved
        completion = ActionInvocationCompletion(
            record.start.model_request_id, finished_at, kind, digest
        )
        if record.completion is None:
            record = self._audit.complete_t6_action(completion)
        elif record.completion != completion:
            raise ActionRecoveryRequired("model action evidence differs from control audit")
        return record, result

    def mark_applied(
        self, model_request_id: str, applied_at: float,
    ) -> ActionInvocationRecord:
        return self._audit.mark_t6_action_applied(model_request_id, applied_at)
