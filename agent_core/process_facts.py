"""Typed contracts for D5 process summaries and their provenance."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Literal
from uuid import UUID

D5SourceKind = Literal[
    "execution",
    "digest_observation",
    "observation_proof",
    "execution_rejection",
]
D5ConflictKind = Literal["candidate_disagreement", "model_proposed"]
D5Outcomes = Literal["accepted", "failed", "invalid", "response_unknown", "not_applied"]
D5_SOURCE_KINDS = frozenset(
    {"execution", "digest_observation", "observation_proof", "execution_rejection"}
)
D5_OUTCOMES = frozenset(
    {"accepted", "failed", "invalid", "response_unknown", "not_applied"}
)


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")


def _uuid(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5")


def _sha256(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _timestamp(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite timestamp")


def _strict_fields(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def d5_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def d5_json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return d5_json_value(asdict(value))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: d5_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [d5_json_value(item) for item in value]
    raise ValueError("value is not JSON-compatible")


@dataclass(frozen=True, slots=True)
class D5Source:
    kind: D5SourceKind
    object_id: str
    content_sha256: str
    subject_ref: str
    observation_scope: str
    artifact_version_id: str | None = None
    rule_version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in D5_SOURCE_KINDS:
            raise ValueError("unsupported D5 source kind")
        _uuid(self.object_id, "object_id")
        _sha256(self.content_sha256, "content_sha256")
        _text(self.subject_ref, "subject_ref")
        _text(self.observation_scope, "observation_scope")
        expected_subject = self.artifact_version_id or self.object_id
        if self.subject_ref != expected_subject:
            raise ValueError("subject_ref must identify the source object version")
        if self.artifact_version_id is not None:
            _uuid(self.artifact_version_id, "artifact_version_id")
        if self.rule_version is not None and (
            type(self.rule_version) is not int or self.rule_version < 1
        ):
            raise ValueError("rule_version must be a positive integer")

    @property
    def version_key(self) -> str:
        return d5_digest(asdict(self))

    @classmethod
    def from_dict(cls, value: object) -> D5Source:
        data = _strict_fields(
            value,
            {
                "kind",
                "object_id",
                "content_sha256",
                "subject_ref",
                "observation_scope",
                "artifact_version_id",
                "rule_version",
            },
            "D5Source",
        )
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D5ClaimRef:
    process_fact_id: str
    claim_index: int

    def __post_init__(self) -> None:
        _uuid(self.process_fact_id, "process_fact_id")
        if type(self.claim_index) is not int or self.claim_index < 0:
            raise ValueError("claim_index must be a non-negative zero-based index")

    @classmethod
    def from_dict(cls, value: object) -> D5ClaimRef:
        data = _strict_fields(value, {"process_fact_id", "claim_index"}, "D5ClaimRef")
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D5Claim:
    subject_ref: str
    observation_scope: str
    claim_key: str
    claim_value: str
    source_refs: tuple[str, ...]
    conflicts_with: tuple[D5ClaimRef, ...] = ()

    def __post_init__(self) -> None:
        _text(self.subject_ref, "subject_ref")
        _text(self.observation_scope, "observation_scope")
        _text(self.claim_key, "claim_key")
        _text(self.claim_value, "claim_value")
        if len(self.claim_key) > 128 or len(self.claim_value) > 512:
            raise ValueError("D5 claim key or value exceeds its character limit")
        if (
            not isinstance(self.source_refs, tuple)
            or not self.source_refs
            or len(self.source_refs) > 32
            or any(not isinstance(ref, str) for ref in self.source_refs)
            or len(set(self.source_refs)) != len(self.source_refs)
        ):
            raise ValueError("source_refs must contain 1..32 unique source IDs")
        for ref in self.source_refs:
            _uuid(ref, "source_ref")
        if (
            not isinstance(self.conflicts_with, tuple)
            or any(not isinstance(ref, D5ClaimRef) for ref in self.conflicts_with)
            or len(set(self.conflicts_with)) != len(self.conflicts_with)
        ):
            raise ValueError("conflicts_with must contain unique typed references")

    @classmethod
    def from_dict(cls, value: object) -> D5Claim:
        required = {
            "subject_ref",
            "observation_scope",
            "claim_key",
            "claim_value",
            "source_refs",
        }
        if not isinstance(value, Mapping) or set(value) not in (
            required,
            required | {"conflicts_with"},
        ):
            raise ValueError("D5Claim fields are invalid")
        data = value
        source_refs = data["source_refs"]
        conflicts = data.get("conflicts_with", ())
        if not isinstance(source_refs, (list, tuple)) or not isinstance(
            conflicts, (list, tuple)
        ):
            raise TypeError("D5 claim references must be arrays")
        return cls(
            subject_ref=data["subject_ref"],  # type: ignore[arg-type]
            observation_scope=data["observation_scope"],  # type: ignore[arg-type]
            claim_key=data["claim_key"],  # type: ignore[arg-type]
            claim_value=data["claim_value"],  # type: ignore[arg-type]
            source_refs=tuple(source_refs),  # type: ignore[arg-type]
            conflicts_with=tuple(D5ClaimRef.from_dict(item) for item in conflicts),
        )


@dataclass(frozen=True, slots=True)
class D5Candidate:
    summary_text: str
    claims: tuple[D5Claim, ...]

    def __post_init__(self) -> None:
        _text(self.summary_text, "summary_text")
        if len(self.summary_text) > 2048:
            raise ValueError("summary_text exceeds 2048 characters")
        if (
            not isinstance(self.claims, tuple)
            or len(self.claims) > 8
            or any(not isinstance(claim, D5Claim) for claim in self.claims)
        ):
            raise ValueError("claims must contain at most 8 typed claims")

    @classmethod
    def from_dict(cls, value: object) -> D5Candidate:
        data = _strict_fields(value, {"summary_text", "claims"}, "D5Candidate")
        claims = data["claims"]
        if not isinstance(claims, (list, tuple)):
            raise TypeError("claims must be an array")
        return cls(
            summary_text=data["summary_text"],  # type: ignore[arg-type]
            claims=tuple(D5Claim.from_dict(item) for item in claims),
        )


@dataclass(frozen=True, slots=True)
class D5Conflict:
    claim_index: int
    other_process_fact_id: str
    other_claim_index: int
    kind: D5ConflictKind

    def __post_init__(self) -> None:
        if type(self.claim_index) is not int or self.claim_index < 0:
            raise ValueError("claim_index must be a non-negative zero-based index")
        _uuid(self.other_process_fact_id, "other_process_fact_id")
        if type(self.other_claim_index) is not int or self.other_claim_index < 0:
            raise ValueError(
                "other_claim_index must be a non-negative zero-based index"
            )
        if not isinstance(self.kind, str) or self.kind not in {
            "candidate_disagreement",
            "model_proposed",
        }:
            raise ValueError("unsupported D5 conflict kind")

    @classmethod
    def from_dict(cls, value: object) -> D5Conflict:
        data = _strict_fields(
            value,
            {"claim_index", "other_process_fact_id", "other_claim_index", "kind"},
            "D5Conflict",
        )
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D5ProcessFact:
    process_fact_id: str
    task_run_id: str
    version: int
    review_id: str
    model_request_id: str
    created_at: float
    sources: tuple[D5Source, ...]
    evidence_sha256: str
    conflicts: tuple[D5Conflict, ...] = ()

    def __post_init__(self) -> None:
        for name in ("process_fact_id", "task_run_id", "review_id", "model_request_id"):
            _uuid(getattr(self, name), name)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be a positive integer")
        _timestamp(self.created_at, "created_at")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _validate_sources(self.sources, allow_empty=False)
        if (
            not isinstance(self.conflicts, tuple)
            or any(not isinstance(conflict, D5Conflict) for conflict in self.conflicts)
            or len(set(self.conflicts)) != len(self.conflicts)
        ):
            raise ValueError("conflicts must contain unique typed conflicts")

    @classmethod
    def from_dict(cls, value: object) -> D5ProcessFact:
        data = _strict_fields(
            value,
            {
                "process_fact_id",
                "task_run_id",
                "version",
                "review_id",
                "model_request_id",
                "created_at",
                "sources",
                "evidence_sha256",
                "conflicts",
            },
            "D5ProcessFact",
        )
        sources, conflicts = data["sources"], data["conflicts"]
        if not isinstance(sources, (list, tuple)) or not isinstance(
            conflicts, (list, tuple)
        ):
            raise TypeError("D5 process fact fields must be arrays")
        return cls(
            process_fact_id=data["process_fact_id"],  # type: ignore[arg-type]
            task_run_id=data["task_run_id"],  # type: ignore[arg-type]
            version=data["version"],  # type: ignore[arg-type]
            review_id=data["review_id"],  # type: ignore[arg-type]
            model_request_id=data["model_request_id"],  # type: ignore[arg-type]
            created_at=data["created_at"],  # type: ignore[arg-type]
            sources=tuple(D5Source.from_dict(item) for item in sources),
            evidence_sha256=data["evidence_sha256"],  # type: ignore[arg-type]
            conflicts=tuple(D5Conflict.from_dict(item) for item in conflicts),
        )


@dataclass(frozen=True, slots=True)
class D5Attempt:
    model_request_id: str
    task_run_id: str
    review_id: str
    base_checkpoint_sequence: int
    started_at: float
    role_config_ref: str
    sources: tuple[D5Source, ...]
    context_sha256: str

    def __post_init__(self) -> None:
        for name in ("model_request_id", "task_run_id", "review_id"):
            _uuid(getattr(self, name), name)
        if (
            type(self.base_checkpoint_sequence) is not int
            or self.base_checkpoint_sequence < 1
        ):
            raise ValueError("base_checkpoint_sequence must be positive")
        _timestamp(self.started_at, "started_at")
        _text(self.role_config_ref, "role_config_ref")
        if not self.role_config_ref.startswith("sha256:"):
            raise ValueError("role_config_ref must bind a SHA-256 digest")
        _sha256(self.role_config_ref[7:], "role_config_ref")
        _sha256(self.context_sha256, "context_sha256")
        _validate_sources(self.sources, allow_empty=False)

    @classmethod
    def from_dict(cls, value: object) -> D5Attempt:
        data = _strict_fields(
            value,
            {
                "model_request_id",
                "task_run_id",
                "review_id",
                "base_checkpoint_sequence",
                "started_at",
                "role_config_ref",
                "sources",
                "context_sha256",
            },
            "D5Attempt",
        )
        sources = data["sources"]
        if not isinstance(sources, (list, tuple)):
            raise TypeError("sources must be an array")
        return cls(
            model_request_id=data["model_request_id"],  # type: ignore[arg-type]
            task_run_id=data["task_run_id"],  # type: ignore[arg-type]
            review_id=data["review_id"],  # type: ignore[arg-type]
            base_checkpoint_sequence=data["base_checkpoint_sequence"],  # type: ignore[arg-type]
            started_at=data["started_at"],  # type: ignore[arg-type]
            role_config_ref=data["role_config_ref"],  # type: ignore[arg-type]
            sources=tuple(D5Source.from_dict(item) for item in sources),
            context_sha256=data["context_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class D5RefinementRequest:
    attempt: D5Attempt
    safe_sources: tuple[dict[str, object], ...]
    process_facts: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, D5Attempt):
            raise TypeError("attempt must be a D5Attempt")
        if (
            not isinstance(self.safe_sources, tuple)
            or len(self.safe_sources) != len(self.attempt.sources)
            or any(not isinstance(source, dict) for source in self.safe_sources)
        ):
            raise ValueError("safe_sources must align with the frozen source list")
        if not isinstance(self.process_facts, tuple):
            raise TypeError("process_facts must be an immutable tuple")
        safe_sources = tuple(d5_json_value(source) for source in self.safe_sources)
        process_facts = tuple(d5_json_value(item) for item in self.process_facts)
        if any(not isinstance(source, dict) for source in safe_sources):
            raise ValueError("safe_sources must contain JSON objects")
        object.__setattr__(self, "safe_sources", safe_sources)
        object.__setattr__(self, "process_facts", process_facts)

    @classmethod
    def from_dict(cls, value: object) -> D5RefinementRequest:
        data = _strict_fields(
            value, {"attempt", "safe_sources", "process_facts"}, "D5RefinementRequest"
        )
        safe_sources, process_facts = data["safe_sources"], data["process_facts"]
        if not isinstance(safe_sources, (list, tuple)) or not isinstance(
            process_facts, (list, tuple)
        ):
            raise TypeError("request contexts must be arrays")
        return cls(
            attempt=D5Attempt.from_dict(data["attempt"]),
            safe_sources=tuple(safe_sources),  # type: ignore[arg-type]
            process_facts=tuple(process_facts),
        )


def _validate_sources(sources: object, *, allow_empty: bool) -> tuple[D5Source, ...]:
    if not isinstance(sources, tuple) or any(
        not isinstance(source, D5Source) for source in sources
    ):
        raise ValueError("sources must be an immutable tuple of D5Source")
    if (not allow_empty and not sources) or len(
        {source.object_id for source in sources}
    ) != len(sources):
        raise ValueError("sources must be non-empty and have unique object IDs")
    return sources


class D5CandidateRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_d5_candidate(
    raw: object,
    sources: tuple[D5Source, ...],
    prior: tuple[D5ProcessFact, ...],
    prior_candidates: tuple[D5Candidate, ...] | None = None,
) -> D5Candidate:
    _validate_sources(sources, allow_empty=False)
    if not isinstance(prior, tuple) or any(
        not isinstance(item, D5ProcessFact) for item in prior
    ):
        raise D5CandidateRejected("d5_prior_facts_invalid")
    if len({item.task_run_id for item in prior}) > 1:
        raise D5CandidateRejected("d5_prior_fact_owner_mismatch")
    if prior_candidates is not None and (
        not isinstance(prior_candidates, tuple)
        or len(prior_candidates) != len(prior)
        or any(not isinstance(item, D5Candidate) for item in prior_candidates)
    ):
        raise D5CandidateRejected("d5_prior_candidates_invalid")

    try:
        value = _strict_fields(raw, {"summary_text", "claims"}, "D5Candidate")
        raw_claims = value["claims"]
        if not isinstance(raw_claims, (list, tuple)):
            raise TypeError("claims must be a JSON array")
        candidate = D5Candidate(
            summary_text=value["summary_text"],  # type: ignore[arg-type]
            claims=tuple(D5Claim.from_dict(item) for item in raw_claims),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise D5CandidateRejected("d5_candidate_invalid") from exc

    by_id = {source.object_id: source for source in sources}
    prior_by_id = {
        fact.process_fact_id: (index, fact) for index, fact in enumerate(prior)
    }
    for claim in candidate.claims:
        for ref in claim.source_refs:
            source = by_id.get(ref)
            if source is None:
                raise D5CandidateRejected("d5_claim_source_not_allowed")
            if (
                source.subject_ref != claim.subject_ref
                or source.observation_scope != claim.observation_scope
            ):
                raise D5CandidateRejected("d5_claim_scope_mismatch")
        for conflict in claim.conflicts_with:
            found = prior_by_id.get(conflict.process_fact_id)
            if found is None or prior_candidates is None:
                raise D5CandidateRejected("d5_claim_conflict_not_found")
            fact_index, _fact = found
            if conflict.claim_index >= len(prior_candidates[fact_index].claims):
                raise D5CandidateRejected("d5_claim_conflict_not_found")
    return candidate


def derive_d5_conflicts(
    candidate: D5Candidate,
    current_sources: tuple[D5Source, ...],
    prior: tuple[tuple[D5ProcessFact, D5Candidate], ...],
) -> tuple[D5Conflict, ...]:
    if not isinstance(candidate, D5Candidate) or not isinstance(prior, tuple):
        raise TypeError("candidate and prior summaries must be typed values")
    _validate_sources(current_sources, allow_empty=False)
    if any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], D5ProcessFact)
        or not isinstance(item[1], D5Candidate)
        for item in prior
    ):
        raise ValueError("prior summaries must pair metadata with a candidate")
    if len({fact.task_run_id for fact, _ in prior}) > 1:
        raise ValueError("prior summaries must have one TaskRun owner")

    conflicts: set[D5Conflict] = set()
    prior_by_id = {fact.process_fact_id: (fact, summary) for fact, summary in prior}
    current_source_by_id = {source.object_id: source for source in current_sources}
    for current_index, claim in enumerate(candidate.claims):
        current_claim_sources = [
            current_source_by_id[ref]
            for ref in claim.source_refs
            if ref in current_source_by_id
        ]
        if len(current_claim_sources) != len(claim.source_refs) or any(
            source.subject_ref != claim.subject_ref
            or source.observation_scope != claim.observation_scope
            for source in current_claim_sources
        ):
            raise ValueError("candidate source binding is invalid")
        for ref in claim.conflicts_with:
            prior_item = prior_by_id.get(ref.process_fact_id)
            if prior_item is None or ref.claim_index >= len(prior_item[1].claims):
                raise ValueError("model conflict does not reference an existing claim")
            conflicts.add(
                D5Conflict(
                    current_index,
                    ref.process_fact_id,
                    ref.claim_index,
                    "model_proposed",
                )
            )
        for fact, previous in prior:
            prior_source_by_id = {source.object_id: source for source in fact.sources}
            for previous_index, old_claim in enumerate(previous.claims):
                if (
                    claim.subject_ref != old_claim.subject_ref
                    or claim.observation_scope != old_claim.observation_scope
                    or claim.claim_key != old_claim.claim_key
                    or claim.claim_value == old_claim.claim_value
                ):
                    continue
                old_sources = [
                    prior_source_by_id[ref]
                    for ref in old_claim.source_refs
                    if ref in prior_source_by_id
                ]
                if len(old_sources) != len(old_claim.source_refs) or any(
                    source.subject_ref != old_claim.subject_ref
                    or source.observation_scope != old_claim.observation_scope
                    for source in old_sources
                ):
                    raise ValueError("prior claim source binding is invalid")
                if _same_object_version(old_sources, current_claim_sources):
                    conflicts.add(
                        D5Conflict(
                            current_index,
                            fact.process_fact_id,
                            previous_index,
                            "candidate_disagreement",
                        )
                    )
    return tuple(
        sorted(
            conflicts,
            key=lambda item: (
                item.claim_index,
                item.other_process_fact_id,
                item.other_claim_index,
                item.kind,
            ),
        )
    )


def _same_object_version(left: list[D5Source], right: list[D5Source]) -> bool:
    return any(
        first.subject_ref == second.subject_ref
        and first.rule_version == second.rule_version
        and first.artifact_version_id == second.artifact_version_id
        for first in left
        for second in right
    )
