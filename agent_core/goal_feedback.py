"""Core contracts for trusted external validation feedback and reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias
from uuid import UUID

ExternalValidationVerdict = Literal["accepted", "rejected", "unknown"]
ReconciliationVerdict = Literal["accepted", "rejected"]
ExternalValidationFeedbackKind = Literal["submission", "reconciliation"]
FeedbackProjectionState = Literal[
    "pending", "unknown", "accepted", "rejected", "conflict"
]
EXTERNAL_VALIDATION_VERDICTS = frozenset({"accepted", "rejected", "unknown"})
RECONCILIATION_VERDICTS = frozenset({"accepted", "rejected"})
EXTERNAL_VALIDATION_FEEDBACK_SCHEMA_VERSION = 1
MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES = 4096
MAX_EXTERNAL_VALIDATION_FEEDBACK_JSON_BYTES = 1024 * 1024


class ExternalValidationFeedbackConflict(RuntimeError):
    """An operation ID or feedback decision conflicts with an append-only record."""


def _uuid4(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4")


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")


def _source_ref(value: object) -> None:
    _text(value, "source_ref")
    try:
        size = len(value.encode("utf-8"))  # type: ignore[union-attr]
    except UnicodeEncodeError as exc:
        raise ValueError("source_ref must be valid UTF-8 text") from exc
    if size > MAX_EXTERNAL_VALIDATION_SOURCE_REF_BYTES:
        raise ValueError("source_ref exceeds 4096 UTF-8 bytes")


def _sha256(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _strict_fields(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("external validation feedback fields are invalid")
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("external validation feedback JSON has duplicate fields")
        result[key] = value
    return result


def _from_json(value: object) -> object:
    if not isinstance(value, str):
        raise TypeError("external validation feedback JSON must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(
            "external validation feedback JSON must be valid UTF-8"
        ) from exc
    if size > MAX_EXTERNAL_VALIDATION_FEEDBACK_JSON_BYTES:
        raise ValueError("external validation feedback JSON exceeds 1 MiB")
    return json.loads(value, object_pairs_hook=_json_object)


def _to_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_EXTERNAL_VALIDATION_FEEDBACK_JSON_BYTES:
        raise ValueError("external validation feedback JSON exceeds 1 MiB")
    return encoded


_COMMON_FIELDS = {
    "schema_version",
    "feedback_kind",
    "task_run_id",
    "candidate_id",
    "evidence_version",
    "verdict",
    "source_ref",
    "operation_id",
}


@dataclass(frozen=True, slots=True)
class ExternalValidationConflictSource:
    """Exact immutable feedback body included in an authoritative conflict query."""

    operation_id: str
    body_sha256: str
    source_ref: str

    def __post_init__(self) -> None:
        _uuid4(self.operation_id, "operation_id")
        _sha256(self.body_sha256, "body_sha256")
        _source_ref(self.source_ref)

    @classmethod
    def from_record(cls, record: ExternalValidationFeedbackRecord) -> ExternalValidationConflictSource:
        return cls(record.operation_id, hashlib.sha256(record.to_json().encode("utf-8")).hexdigest(), record.source_ref)

    def to_dict(self) -> dict[str, object]:
        return {"operation_id": self.operation_id, "body_sha256": self.body_sha256, "source_ref": self.source_ref}

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationConflictSource:
        data = _strict_fields(value, {"operation_id", "body_sha256", "source_ref"})
        return cls(**data)  # type: ignore[arg-type]


def _conflict_sources(value: object) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, ExternalValidationConflictSource) for item in value):
        raise ValueError("conflict_sources must be a tuple of typed source bindings")
    if value and (len(value) < 2 or tuple(item.operation_id for item in value) != tuple(sorted({item.operation_id for item in value}))):
        raise ValueError("conflict_sources must contain at least two unique, sorted operations")


def _parse_conflict_sources(value: object) -> tuple[ExternalValidationConflictSource, ...]:
    if not isinstance(value, list):
        raise ValueError("conflict_sources must be an array")
    return tuple(ExternalValidationConflictSource.from_dict(item) for item in value)


@dataclass(frozen=True, slots=True)
class ExternalValidationFeedback:
    """Original submission feedback; source_ref is correlation text, not authentication."""

    task_run_id: str
    candidate_id: str
    evidence_version: str
    verdict: ExternalValidationVerdict
    source_ref: str
    operation_id: str
    feedback_kind: Literal["submission"] = field(default="submission", init=False)

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4(self.candidate_id, "candidate_id")
        _sha256(self.evidence_version, "evidence_version")
        if (
            not isinstance(self.verdict, str)
            or self.verdict not in EXTERNAL_VALIDATION_VERDICTS
        ):
            raise ValueError("verdict must be accepted, rejected, or unknown")
        _source_ref(self.source_ref)
        _uuid4(self.operation_id, "operation_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXTERNAL_VALIDATION_FEEDBACK_SCHEMA_VERSION,
            "feedback_kind": self.feedback_kind,
            "task_run_id": self.task_run_id,
            "candidate_id": self.candidate_id,
            "evidence_version": self.evidence_version,
            "verdict": self.verdict,
            "source_ref": self.source_ref,
            "operation_id": self.operation_id,
        }

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationFeedback:
        data = _strict_fields(value, _COMMON_FIELDS)
        _schema(data)
        if data["feedback_kind"] != "submission":
            raise ValueError("feedback_kind must be submission")
        return cls(
            task_run_id=data["task_run_id"],  # type: ignore[arg-type]
            candidate_id=data["candidate_id"],  # type: ignore[arg-type]
            evidence_version=data["evidence_version"],  # type: ignore[arg-type]
            verdict=data["verdict"],  # type: ignore[arg-type]
            source_ref=data["source_ref"],  # type: ignore[arg-type]
            operation_id=data["operation_id"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_json(cls, value: str) -> ExternalValidationFeedback:
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True, slots=True)
class ExternalValidationReconciliation:
    """Append a read-only resolution; source_ref is correlation text, not authentication."""

    task_run_id: str
    candidate_id: str
    evidence_version: str
    verdict: ReconciliationVerdict
    source_ref: str
    operation_id: str
    resolves_operation_id: str
    conflict_sources: tuple[ExternalValidationConflictSource, ...] = ()
    feedback_kind: Literal["reconciliation"] = field(
        default="reconciliation", init=False
    )

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4(self.candidate_id, "candidate_id")
        _sha256(self.evidence_version, "evidence_version")
        if (
            not isinstance(self.verdict, str)
            or self.verdict not in RECONCILIATION_VERDICTS
        ):
            raise ValueError("reconciliation verdict must be accepted or rejected")
        _source_ref(self.source_ref)
        _uuid4(self.operation_id, "operation_id")
        _uuid4(self.resolves_operation_id, "resolves_operation_id")
        _conflict_sources(self.conflict_sources)
        if self.conflict_sources and self.resolves_operation_id not in {item.operation_id for item in self.conflict_sources}:
            raise ValueError("resolved operation must be included in conflict_sources")
        if self.operation_id == self.resolves_operation_id:
            raise ValueError(
                "query operation_id must differ from resolved submission ID"
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": EXTERNAL_VALIDATION_FEEDBACK_SCHEMA_VERSION,
            "feedback_kind": self.feedback_kind,
            "task_run_id": self.task_run_id,
            "candidate_id": self.candidate_id,
            "evidence_version": self.evidence_version,
            "verdict": self.verdict,
            "source_ref": self.source_ref,
            "operation_id": self.operation_id,
            "resolves_operation_id": self.resolves_operation_id,
        }
        if self.conflict_sources:
            result["conflict_sources"] = [item.to_dict() for item in self.conflict_sources]
        return result

    def to_json(self) -> str:
        return _to_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationReconciliation:
        fields = _COMMON_FIELDS | {"resolves_operation_id"}
        if isinstance(value, Mapping) and "conflict_sources" in value:
            fields = fields | {"conflict_sources"}
        data = _strict_fields(value, fields)
        _schema(data)
        if data["feedback_kind"] != "reconciliation":
            raise ValueError("feedback_kind must be reconciliation")
        return cls(
            task_run_id=data["task_run_id"],  # type: ignore[arg-type]
            candidate_id=data["candidate_id"],  # type: ignore[arg-type]
            evidence_version=data["evidence_version"],  # type: ignore[arg-type]
            verdict=data["verdict"],  # type: ignore[arg-type]
            source_ref=data["source_ref"],  # type: ignore[arg-type]
            operation_id=data["operation_id"],  # type: ignore[arg-type]
            resolves_operation_id=data["resolves_operation_id"],  # type: ignore[arg-type]
            conflict_sources=_parse_conflict_sources(data["conflict_sources"]) if "conflict_sources" in data else (),
        )

    @classmethod
    def from_json(cls, value: str) -> ExternalValidationReconciliation:
        return cls.from_dict(_from_json(value))


@dataclass(frozen=True, slots=True)
class ExternalValidationCheckRequest:
    """Immutable read-only query binding persisted before a trusted adapter call."""

    query_id: str
    task_run_id: str
    submission_operation_id: str
    candidate_id: str
    evidence_version: str
    checkpoint_sequence: int
    conflict_sources: tuple[ExternalValidationConflictSource, ...] = ()

    def __post_init__(self) -> None:
        _uuid4(self.query_id, "query_id")
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4(self.submission_operation_id, "submission_operation_id")
        if self.query_id == self.submission_operation_id:
            raise ValueError("query_id must differ from the resolved submission ID")
        _uuid4(self.candidate_id, "candidate_id")
        _sha256(self.evidence_version, "evidence_version")
        _positive_integer(self.checkpoint_sequence, "checkpoint_sequence")
        _conflict_sources(self.conflict_sources)
        if self.conflict_sources and self.submission_operation_id not in {item.operation_id for item in self.conflict_sources}:
            raise ValueError("submission operation must be included in conflict_sources")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "query_id": self.query_id,
            "task_run_id": self.task_run_id,
            "submission_operation_id": self.submission_operation_id,
            "candidate_id": self.candidate_id,
            "evidence_version": self.evidence_version,
            "checkpoint_sequence": self.checkpoint_sequence,
        }
        if self.conflict_sources:
            result["conflict_sources"] = [item.to_dict() for item in self.conflict_sources]
        return result

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationCheckRequest:
        fields = {
            "schema_version", "query_id", "task_run_id", "submission_operation_id",
            "candidate_id", "evidence_version", "checkpoint_sequence",
        }
        if isinstance(value, Mapping) and "conflict_sources" in value:
            fields = fields | {"conflict_sources"}
        data = _strict_fields(value, fields)
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported external validation check request schema")
        return cls(
            query_id=data["query_id"],  # type: ignore[arg-type]
            task_run_id=data["task_run_id"],  # type: ignore[arg-type]
            submission_operation_id=data["submission_operation_id"],  # type: ignore[arg-type]
            candidate_id=data["candidate_id"],  # type: ignore[arg-type]
            evidence_version=data["evidence_version"],  # type: ignore[arg-type]
            checkpoint_sequence=data["checkpoint_sequence"],  # type: ignore[arg-type]
            conflict_sources=_parse_conflict_sources(data["conflict_sources"]) if "conflict_sources" in data else (),
        )


@dataclass(frozen=True, slots=True)
class ExternalValidationCheckResult:
    """One immutable adapter result for a previously persisted query request."""

    request: ExternalValidationCheckRequest
    verdict: ExternalValidationVerdict
    source_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExternalValidationCheckRequest):
            raise ValueError("request must be an ExternalValidationCheckRequest")
        if not isinstance(self.verdict, str) or self.verdict not in EXTERNAL_VALIDATION_VERDICTS:
            raise ValueError("verdict must be accepted, rejected, or unknown")
        _source_ref(self.source_ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "request": self.request.to_dict(),
            "verdict": self.verdict,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationCheckResult:
        data = _strict_fields(value, {"schema_version", "request", "verdict", "source_ref"})
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported external validation check result schema")
        return cls(
            request=ExternalValidationCheckRequest.from_dict(data["request"]),
            verdict=data["verdict"],  # type: ignore[arg-type]
            source_ref=data["source_ref"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ExternalValidationCheckCompletion:
    """Atomic ledger result plus its trusted reconciliation, when determinate."""

    result: ExternalValidationCheckResult
    reconciliation: ExternalValidationReconciliation | None

    def __post_init__(self) -> None:
        if not isinstance(self.result, ExternalValidationCheckResult):
            raise ValueError("result must be an ExternalValidationCheckResult")
        request = self.result.request
        if self.result.verdict == "unknown":
            if self.reconciliation is not None:
                raise ValueError("unknown check result cannot carry reconciliation")
            return
        reconciliation = self.reconciliation
        if not isinstance(reconciliation, ExternalValidationReconciliation):
            raise ValueError("determinate check result requires reconciliation")
        if (
            reconciliation.task_run_id != request.task_run_id
            or reconciliation.candidate_id != request.candidate_id
            or reconciliation.evidence_version != request.evidence_version
            or reconciliation.verdict != self.result.verdict
            or reconciliation.source_ref != self.result.source_ref
            or reconciliation.operation_id != request.query_id
            or reconciliation.resolves_operation_id != request.submission_operation_id
            or reconciliation.conflict_sources != request.conflict_sources
        ):
            raise ValueError("reconciliation differs from its check result binding")

    @classmethod
    def from_result(
        cls, result: ExternalValidationCheckResult
    ) -> ExternalValidationCheckCompletion:
        reconciliation = None
        if result.verdict in {"accepted", "rejected"}:
            request = result.request
            reconciliation = ExternalValidationReconciliation(
                task_run_id=request.task_run_id,
                candidate_id=request.candidate_id,
                evidence_version=request.evidence_version,
                verdict=result.verdict,
                source_ref=result.source_ref,
                operation_id=request.query_id,
                resolves_operation_id=request.submission_operation_id,
                conflict_sources=request.conflict_sources,
            )
        return cls(result, reconciliation)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "check_completion",
            "result": self.result.to_dict(),
            "reconciliation": (
                None if self.reconciliation is None else self.reconciliation.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> ExternalValidationCheckCompletion:
        data = _strict_fields(value, {"kind", "result", "reconciliation"})
        if data["kind"] != "check_completion":
            raise ValueError("kind must be check_completion")
        reconciliation = data["reconciliation"]
        return cls(
            result=ExternalValidationCheckResult.from_dict(data["result"]),
            reconciliation=(
                None
                if reconciliation is None
                else ExternalValidationReconciliation.from_dict(reconciliation)
            ),
        )


@dataclass(frozen=True, slots=True)
class ExternalValidationCheckRecord:
    """Read projection of the append-only start and optional result ledger."""

    request: ExternalValidationCheckRequest
    result: ExternalValidationCheckResult | None
    started_event_sequence: int
    result_event_sequence: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExternalValidationCheckRequest):
            raise ValueError("request must be an ExternalValidationCheckRequest")
        _positive_integer(self.started_event_sequence, "started_event_sequence")
        if self.result is None:
            if self.result_event_sequence is not None:
                raise ValueError("incomplete check cannot have a result event")
        else:
            if self.result.request != self.request:
                raise ValueError("check result differs from its persisted request")
            _positive_integer(self.result_event_sequence, "result_event_sequence")


def external_validation_check_result_mutation_id(
    request: ExternalValidationCheckRequest,
) -> str:
    """Return the stable UUID4 ControlMutation ID for a query's result phase."""
    digest = bytearray(
        hashlib.sha256(
            f"qinyuan/d7/check-result/v1/{request.task_run_id}/{request.query_id}".encode()
        ).digest()[:16]
    )
    digest[6] = (digest[6] & 15) | 64
    digest[8] = (digest[8] & 63) | 128
    from uuid import UUID

    return str(UUID(bytes=bytes(digest)))


ExternalValidationFeedbackRecord: TypeAlias = (
    ExternalValidationFeedback | ExternalValidationReconciliation
)


def _schema(data: Mapping[str, object]) -> None:
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != EXTERNAL_VALIDATION_FEEDBACK_SCHEMA_VERSION
    ):
        raise ValueError("unsupported external validation feedback schema")


def external_validation_feedback_from_dict(
    value: object,
) -> ExternalValidationFeedbackRecord:
    if not isinstance(value, Mapping):
        raise TypeError("external validation feedback must be an object")
    kind = value.get("feedback_kind")
    if kind == "submission":
        return ExternalValidationFeedback.from_dict(value)
    if kind == "reconciliation":
        return ExternalValidationReconciliation.from_dict(value)
    raise ValueError("unsupported external validation feedback kind")


def external_validation_feedback_from_json(
    value: str,
) -> ExternalValidationFeedbackRecord:
    return external_validation_feedback_from_dict(_from_json(value))


def require_exact_feedback_replay(
    existing: ExternalValidationFeedbackRecord,
    incoming: ExternalValidationFeedbackRecord,
) -> ExternalValidationFeedbackRecord:
    """Return a replay only when its operation ID and complete body match."""
    if existing.operation_id != incoming.operation_id or existing != incoming:
        raise ExternalValidationFeedbackConflict(
            "operation_id was already recorded for different feedback"
        )
    return existing


@dataclass(frozen=True, slots=True)
class ExternalValidationFeedbackProjection:
    task_run_id: str
    candidate_id: str
    evidence_version: str
    state: FeedbackProjectionState
    accepted_operation_ids: tuple[str, ...]
    rejected_operation_ids: tuple[str, ...]
    unresolved_unknown_operation_ids: tuple[str, ...]
    conflict_operation_ids: tuple[str, ...]


def project_candidate_feedback(
    task_run_id: str,
    candidate_id: str,
    evidence_version: str,
    records: Iterable[ExternalValidationFeedbackRecord],
) -> ExternalValidationFeedbackProjection:
    """Project append-only records without treating unknown as a decision."""
    _uuid4(task_run_id, "task_run_id")
    _uuid4(candidate_id, "candidate_id")
    _sha256(evidence_version, "evidence_version")

    by_operation: dict[str, ExternalValidationFeedbackRecord] = {}
    for record in records:
        if not isinstance(
            record, (ExternalValidationFeedback, ExternalValidationReconciliation)
        ):
            raise TypeError("feedback projection requires typed records")
        if (
            record.task_run_id != task_run_id
            or record.candidate_id != candidate_id
            or record.evidence_version != evidence_version
        ):
            raise ValueError(
                "feedback record is outside the requested candidate version"
            )
        previous = by_operation.get(record.operation_id)
        if previous is not None:
            require_exact_feedback_replay(previous, record)
        else:
            by_operation[record.operation_id] = record

    submissions = {
        record.operation_id: record
        for record in by_operation.values()
        if isinstance(record, ExternalValidationFeedback)
    }
    resolved_submission_ids: set[str] = set()
    resolved_decision_ids: set[str] = set()
    seen: dict[str, ExternalValidationFeedbackRecord] = {}
    for record in by_operation.values():
        if isinstance(record, ExternalValidationReconciliation):
            if record.conflict_sources:
                active = [item for item in seen.values() if item.operation_id not in resolved_decision_ids and item.verdict in {"accepted", "rejected"}]
                expected = tuple(ExternalValidationConflictSource.from_record(item) for item in sorted(active, key=lambda item: item.operation_id))
                if {item.verdict for item in active} != {"accepted", "rejected"} or record.conflict_sources != expected:
                    raise ExternalValidationFeedbackConflict("conflict resolution must bind all active opposite decisions")
                resolved_decision_ids.update(item.operation_id for item in active)
            else:
                original = submissions.get(record.resolves_operation_id)
                if original is None or original.operation_id not in seen or original.verdict != "unknown":
                    raise ExternalValidationFeedbackConflict("reconciliation must append to a recorded unknown submission")
                resolved_submission_ids.add(original.operation_id)
        seen[record.operation_id] = record

    accepted_ids = tuple(
        sorted(
            record.operation_id
            for record in by_operation.values()
            if record.verdict == "accepted"
        )
    )
    rejected_ids = tuple(
        sorted(
            record.operation_id
            for record in by_operation.values()
            if record.verdict == "rejected"
        )
    )
    unresolved_unknown_ids = tuple(
        sorted(
            record.operation_id
            for record in submissions.values()
            if record.verdict == "unknown"
            and record.operation_id not in resolved_submission_ids
        )
    )
    active_accepted_ids = tuple(item for item in accepted_ids if item not in resolved_decision_ids)
    active_rejected_ids = tuple(item for item in rejected_ids if item not in resolved_decision_ids)
    conflicts = (
        tuple(sorted((*active_accepted_ids, *active_rejected_ids)))
        if active_accepted_ids and active_rejected_ids
        else ()
    )
    state: FeedbackProjectionState
    if conflicts:
        state = "conflict"
    elif unresolved_unknown_ids:
        state = "unknown"
    elif active_accepted_ids:
        state = "accepted"
    elif active_rejected_ids:
        state = "rejected"
    else:
        state = "pending"
    return ExternalValidationFeedbackProjection(
        task_run_id,
        candidate_id,
        evidence_version,
        state,
        accepted_ids,
        rejected_ids,
        unresolved_unknown_ids,
        conflicts,
    )


def validate_external_check_binding(request: ExternalValidationCheckRequest, records: Iterable[ExternalValidationFeedbackRecord]) -> None:
    """Reject stale, partial or fabricated query bindings before any adapter call."""
    saved = tuple(records)
    projection = project_candidate_feedback(request.task_run_id, request.candidate_id, request.evidence_version, saved)
    by_id = {item.operation_id: item for item in saved}
    if request.conflict_sources:
        expected = tuple(ExternalValidationConflictSource.from_record(by_id[operation_id]) for operation_id in projection.conflict_operation_ids)
        if not expected or request.conflict_sources != expected:
            raise ExternalValidationFeedbackConflict("query must bind all current conflicting feedback bodies")
    elif request.submission_operation_id not in projection.unresolved_unknown_operation_ids:
        raise ExternalValidationFeedbackConflict("query must bind an unresolved unknown submission")


class ExternalValidationFeedbackPort(Protocol):
    """Append feedback records; this port never submits or queries a platform.

    Operation IDs are unique across both record kinds. Exact body replay returns
    the original record; reusing an ID with changed content raises a conflict.
    A reconciliation may only append to a matching, previously stored unknown
    submission and never changes that submission.
    """

    def record_feedback(
        self, feedback: ExternalValidationFeedback
    ) -> ExternalValidationFeedback: ...

    def record_reconciliation(
        self, reconciliation: ExternalValidationReconciliation
    ) -> ExternalValidationReconciliation: ...

    def read_feedback(
        self, operation_id: str
    ) -> ExternalValidationFeedbackRecord | None: ...

    def inspect_candidate_feedback(
        self,
        task_run_id: str,
        candidate_id: str,
        evidence_version: str,
    ) -> tuple[ExternalValidationFeedbackRecord, ...]: ...


class ExternalValidationCheckerPort(Protocol):
    """Trusted read-only adapter for checking one already-issued submission."""

    def check(
        self, request: ExternalValidationCheckRequest
    ) -> ExternalValidationCheckResult: ...


__all__ = [
    "ExternalValidationCheckCompletion",
    "ExternalValidationCheckRecord",
    "ExternalValidationCheckRequest",
    "ExternalValidationCheckResult",
    "ExternalValidationCheckerPort",
    "ExternalValidationConflictSource",
    "ExternalValidationFeedback",
    "ExternalValidationFeedbackConflict",
    "ExternalValidationFeedbackKind",
    "ExternalValidationFeedbackPort",
    "ExternalValidationFeedbackProjection",
    "ExternalValidationFeedbackRecord",
    "ExternalValidationReconciliation",
    "ExternalValidationVerdict",
    "FeedbackProjectionState",
    "ReconciliationVerdict",
    "external_validation_check_result_mutation_id",
    "external_validation_feedback_from_dict",
    "external_validation_feedback_from_json",
    "project_candidate_feedback",
    "require_exact_feedback_replay",
    "validate_external_check_binding",
]
