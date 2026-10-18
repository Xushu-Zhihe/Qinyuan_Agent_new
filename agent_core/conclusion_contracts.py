"""D8-1 candidate, safe observation, and append-only review contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Literal

from agent_core.operation_accounting import _sha, _text, _time, _uuid


class EvidenceStrength(Enum):
    """D22: Evidence quality classification for Facts"""
    VERIFIED = "verified"  # Program-verified observation (D8Observation)
    INFERENCE = "inference"  # LLM-inferred from verified sources
    UNVALIDATED = "unvalidated"  # Not yet reviewed


def conclusion_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _bounded(value: object, name: str, limit: int) -> None:
    _text(value, name)
    if len(value) > limit:
        raise ValueError(f"{name} exceeds its limit")


def _fields(raw: object, fields: set[str]) -> dict:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError("d8_response_fields_invalid")
    return raw


def d8_fact_content(artifact_version_id: str, observed_sha256: str, proof_id: str) -> str:
    return (f"First saved observation: artifact {artifact_version_id} SHA-256 "
            f"{observed_sha256} matched frozen import; proof {proof_id}. "
            "Whole task not evaluated.")


@dataclass(frozen=True, slots=True)
class D8Assertion:
    rule_id: str
    rule_version: int
    artifact_version_id: str
    observation_id: str

    def __post_init__(self) -> None:
        if self.rule_id not in {"attachment_copy_sha256", "http_response_observed"} or type(self.rule_version) is not int or self.rule_version != 1:
            raise ValueError("d8_assertion_rule_unsupported")
        _uuid(self.artifact_version_id, "artifact_version_id")
        _uuid(self.observation_id, "observation_id")


@dataclass(frozen=True, slots=True)
class D8Candidate:
    content: str
    reason: str
    evidence_refs: tuple[str, ...]
    assertion: D8Assertion | None

    def __post_init__(self) -> None:
        _text(self.content, "content")
        # 与 domain.MAX_FACT_CONTENT_LENGTH 对齐：候选正文超长必须拒绝，不能悄悄截断。
        _bounded(self.content, "content", 256)
        _bounded(self.reason, "reason", 512)
        if not isinstance(self.evidence_refs, tuple) or not 1 <= len(self.evidence_refs) <= 8:
            raise ValueError("d8_candidate_sources_invalid")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("d8_candidate_sources_duplicate")
        for ref in self.evidence_refs:
            _uuid(ref, "evidence_ref")
        if self.assertion is not None and not isinstance(self.assertion, D8Assertion):
            raise ValueError("d8_candidate_assertion_invalid")

    @classmethod
    def from_dict(cls, raw: object) -> D8Candidate:
        value = _fields(raw, {"content", "reason", "evidence_refs", "assertion"})
        if not isinstance(value["evidence_refs"], (list, tuple)):
            raise ValueError("d8_evidence_refs_must_be_array")
        assertion = value["assertion"]
        if assertion is not None:
            assertion = D8Assertion(**_fields(assertion, {
                "rule_id", "rule_version", "artifact_version_id", "observation_id"}))
        return cls(value["content"], value["reason"], tuple(value["evidence_refs"]), assertion)


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """D22: Extended candidate format for Fact organization layer"""
    content: str
    layer: str  # FactLayer enum value ("evidence" | "business" | "process")
    evidence_strength: EvidenceStrength
    source_proof_id: str
    inferred: bool
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.content, "content")
        if self.layer not in {"evidence", "business", "process"}:
            raise ValueError("fact_candidate_layer_invalid")
        if not isinstance(self.evidence_strength, EvidenceStrength):
            raise ValueError("fact_candidate_strength_invalid")
        _uuid(self.source_proof_id, "source_proof_id")
        if not isinstance(self.inferred, bool):
            raise ValueError("fact_candidate_inferred_must_be_bool")
        if self.reason:
            _bounded(self.reason, "reason", 512)
        if self.evidence_refs:
            if not isinstance(self.evidence_refs, tuple) or not all(isinstance(r, str) for r in self.evidence_refs):
                raise ValueError("fact_candidate_evidence_refs_invalid")
            for ref in self.evidence_refs:
                _uuid(ref, "evidence_ref")

    @classmethod
    def from_d8_candidate(cls, d8: D8Candidate, layer: str = "evidence",
                         strength: EvidenceStrength = EvidenceStrength.VERIFIED,
                         source_proof_id: str = "") -> FactCandidate:
        """Convert D8Candidate to FactCandidate with D22 metadata"""
        return cls(
            content=d8.content,
            layer=layer,
            evidence_strength=strength,
            source_proof_id=source_proof_id,
            inferred=False,
            reason=d8.reason,
            evidence_refs=d8.evidence_refs,
        )


@dataclass(frozen=True, slots=True)
class D8ReviewRequestAction:
    reason: str

    def __post_init__(self) -> None:
        _bounded(self.reason, "reason", 512)


def parse_d8_proposal(raw: object) -> D8Candidate | D8ReviewRequestAction:
    if isinstance(raw, dict) and raw.get("kind") == "request_review":
        return D8ReviewRequestAction(_fields(raw, {"kind", "reason"})["reason"])
    value = _fields(raw, {"kind", "content", "reason", "evidence_refs", "assertion"})
    if value["kind"] != "candidate":
        raise ValueError("d8_proposal_kind_invalid")
    return D8Candidate.from_dict({key: item for key, item in value.items() if key != "kind"})


@dataclass(frozen=True, slots=True)
class D8ReviewDecision:
    decision: Literal["accept", "reject", "insufficient"]
    reason: str
    refined_content: str | None = None
    merged_sources: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "reject", "insufficient"}:
            raise ValueError("d8_review_decision_invalid")
        _bounded(self.reason, "reason", 512)
        if self.refined_content is not None:
            _bounded(self.refined_content, "refined_content", 2048)
        if self.merged_sources is not None:
            if not isinstance(self.merged_sources, tuple):
                raise ValueError("d8_review_merged_sources_must_be_tuple")
            if not all(isinstance(s, str) for s in self.merged_sources):
                raise ValueError("d8_review_merged_sources_invalid")
            if len(self.merged_sources) > 8:
                raise ValueError("d8_review_too_many_merged_sources")

    @classmethod
    def from_dict(cls, raw: object) -> D8ReviewDecision:
        fields = {"decision", "reason"}
        optional_fields = {"refined_content", "merged_sources"}
        all_fields = fields | optional_fields

        if not isinstance(raw, dict):
            raise ValueError("d8_review_decision_not_dict")

        provided = set(raw.keys())
        if not fields.issubset(provided):
            raise ValueError("d8_review_decision_missing_required")
        if not provided.issubset(all_fields):
            raise ValueError("d8_review_decision_unknown_fields")

        merged_sources = raw.get("merged_sources")
        if merged_sources is not None:
            merged_sources = tuple(merged_sources)

        return cls(
            decision=raw["decision"],
            reason=raw["reason"],
            refined_content=raw.get("refined_content"),
            merged_sources=merged_sources,
        )


@dataclass(frozen=True, slots=True)
class D8Observation:
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    artifact_version_id: str
    observation_id: str
    proof_id: str
    execution_id: str
    observed_sha256: str
    byte_size: int
    observed_at: float
    source_version_sha256: str
    rule_id: str = "attachment_copy_sha256"
    rule_version: int = 1
    verification_status: str = "verified"
    observation_scope: str = "first_saved_observation"
    execution_status: str = "succeeded"

    def __post_init__(self) -> None:
        for name in ("task_run_id", "turn_id", "intent_id", "plan_id", "artifact_version_id",
                     "observation_id", "proof_id", "execution_id"):
            _uuid(getattr(self, name), name)
        _sha(self.observed_sha256, "observed_sha256")
        _sha(self.source_version_sha256, "source_version_sha256")
        if type(self.byte_size) is not int or self.byte_size < 0:
            raise ValueError("d8_observation_size_invalid")
        _time(self.observed_at, "observed_at")
        if (self.rule_id != "attachment_copy_sha256" or type(self.rule_version) is not int
                or self.rule_version != 1 or self.verification_status != "verified"
                or self.observation_scope != "first_saved_observation"
                or self.execution_status != "succeeded"):
            raise ValueError("d8_observation_projection_invalid")

    def fact_content(self) -> str:
        return d8_fact_content(self.artifact_version_id, self.observed_sha256, self.proof_id)


@dataclass(frozen=True, slots=True)
class D16Observation(D8Observation):
    """Program-verified comparison; outer acceptance remains an independent gate."""
    expected_sha256: str = ''
    digest_matches_expected: bool = True
    rule_binding_sha256: str = ''
    source_version_kind: str = 'proof_receipt_turn_binding_sha256'

    def __post_init__(self):
        super(D16Observation, self).__post_init__()
        _sha(self.expected_sha256, 'expected_sha256')
        _sha(self.rule_binding_sha256, 'rule_binding_sha256')
        if (type(self.digest_matches_expected) is not bool or not self.digest_matches_expected
                or self.observed_sha256 != self.expected_sha256
                or self.source_version_kind != 'proof_receipt_turn_binding_sha256'):
            raise ValueError('s6_observation_comparison_invalid')


def allowed_fact_evidence_refs(observation: D8Observation) -> tuple[str, ...]:
    """Registered proof citations; object identifiers are not proof references."""
    if not isinstance(observation, D8Observation):
        raise ValueError("d8_fact_observation_required")
    return (observation.proof_id,)


def fact_evidence_matches(candidate: D8Candidate, observation: D8Observation) -> bool:
    return set(candidate.evidence_refs) == set(allowed_fact_evidence_refs(observation))


@dataclass(frozen=True, slots=True)
class D8Feedback:
    record_id: str
    outcome: str
    reason: str
    content: str

    def __post_init__(self) -> None:
        _uuid(self.record_id, "record_id")
        if self.outcome not in {"verified_fact", "reviewed_inference", "rejected", "insufficient"}:
            raise ValueError("d8_feedback_outcome_invalid")
        _bounded(self.reason, "reason", 512)
        _text(self.content, "content")


@dataclass(frozen=True, slots=True)
class D8ModelRequest:
    model_request_id: str
    observation: D8Observation
    stage: Literal["fact_proposal", "fact_review"]
    role_config_ref: str
    base_checkpoint_sequence: int
    candidate: D8Candidate | None = None
    feedback: tuple[D8Feedback, ...] = ()
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None
    goal: str = ""
    intent_description: str = ""

    def __post_init__(self) -> None:
        _uuid(self.model_request_id, "model_request_id")
        if not isinstance(self.role_config_ref, str) or not self.role_config_ref.startswith("sha256:"):
            raise ValueError("d8_request_role_invalid")
        _sha(self.role_config_ref[7:], "role_config_ref")
        if not isinstance(self.observation, D8Observation) or self.stage not in {"fact_proposal", "fact_review"}:
            raise ValueError("d8_request_context_invalid")
        if (self.stage == "fact_review") != isinstance(self.candidate, D8Candidate):
            raise ValueError("d8_review_requires_candidate")
        if not isinstance(self.feedback, tuple) or any(not isinstance(f, D8Feedback) for f in self.feedback):
            raise ValueError("d8_request_feedback_invalid")
        _text(self.goal, "goal")
        _text(self.intent_description, "intent_description")
        if type(self.base_checkpoint_sequence) is not int or self.base_checkpoint_sequence < 1:
            raise ValueError("d8_request_checkpoint_invalid")
        if self.correction_of_model_request_id is not None:
            _uuid(self.correction_of_model_request_id, "correction_of_model_request_id")
            _text(self.rejection_code, "rejection_code")
        elif self.rejection_code is not None:
            raise ValueError("d8_request_correction_invalid")

    @property
    def task_run_id(self) -> str:
        return self.observation.task_run_id

    @property
    def context_sha256(self) -> str:
        return conclusion_digest(asdict(self))

    @classmethod
    def from_dict(cls, raw: dict) -> D8ModelRequest:
        value = dict(raw)
        observation = value['observation']
        from agent_core.http_contracts import HttpObservation
        cls_observation = HttpObservation if observation.get('rule_id') == 'http_response_observed' else D16Observation if any(key in observation for key in (
            'expected_sha256', 'digest_matches_expected', 'rule_binding_sha256', 'source_version_kind')) else D8Observation
        value["observation"] = cls_observation(**observation)
        value["candidate"] = D8Candidate.from_dict(value["candidate"]) if value["candidate"] is not None else None
        value["feedback"] = tuple(D8Feedback(**item) for item in value["feedback"])
        if "session_id" in value:
            from agent_core.session_contracts import D8SessionConclusionRequest
            return D8SessionConclusionRequest(**value)
        return cls(**value)


@dataclass(frozen=True, slots=True)
class D8Conclusion:
    """Control metadata; candidate and decision text stay in evidence files."""

    record_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    proposal_request_id: str
    review_request_id: str
    candidate_sha256: str
    source_version_sha256: str
    outcome: Literal["verified_fact", "reviewed_inference", "rejected", "insufficient"]
    fact_id: str | None

    def __post_init__(self) -> None:
        for name in ("record_id", "task_run_id", "turn_id", "intent_id", "plan_id",
                     "proposal_request_id", "review_request_id"):
            _uuid(getattr(self, name), name)
        _sha(self.candidate_sha256, "candidate_sha256")
        _sha(self.source_version_sha256, "source_version_sha256")
        if self.outcome not in {"verified_fact", "reviewed_inference", "rejected", "insufficient"}:
            raise ValueError("d8_conclusion_outcome_invalid")
        if self.outcome == "verified_fact":
            _uuid(self.fact_id, "fact_id")
        elif self.fact_id is not None:
            raise ValueError("d8_inference_cannot_claim_fact")


@dataclass(frozen=True, slots=True)
class D8InferenceView:
    record: D8Conclusion
    source_status: Literal["available", "unavailable"]
    scope_status: Literal["active", "ended"]
    candidate: D8Candidate | None
    observation: D8Observation | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.record, D8Conclusion) or self.record.outcome != "reviewed_inference"
                or self.source_status not in {"available", "unavailable"}
                or self.scope_status not in {"active", "ended"}):
            raise ValueError("d8_inference_view_invalid")
        if self.source_status == "available":
            if (not isinstance(self.candidate, D8Candidate) or not isinstance(self.observation, D8Observation)
                    or self.unavailable_reason is not None):
                raise ValueError("d8_inference_view_sources_required")
        elif self.candidate is not None or self.observation is not None or self.unavailable_reason != "d8_inference_source_unavailable":
            raise ValueError("d8_inference_view_unavailable_invalid")
