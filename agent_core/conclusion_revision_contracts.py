"""Narrow, append-only D8-3 conclusion and dependency changes."""

from dataclasses import asdict, dataclass

from agent_core.conclusion_contracts import _bounded, _fields, conclusion_digest
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.operation_accounting import _sha, _uuid


@dataclass(frozen=True, slots=True)
class D8RevisionTarget:
    root_id: str
    version: int
    ref: str

    def __post_init__(self):
        _uuid(self.root_id, "root_id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("d8_revision_version_invalid")
        _bounded(self.ref, "ref", 256)


@dataclass(frozen=True, slots=True)
class D8ConclusionChange:
    operation: str
    targets: tuple[D8RevisionTarget, ...]
    replacement_ref: str | None
    content: str | None
    evidence_refs: tuple[str, ...]
    reason: str
    resolution: str | None
    relation_kind: str | None
    relation_owner_id: str | None

    def __post_init__(self):
        if self.operation not in {"correct", "withdraw", "restore", "report_conflict",
                                  "resolve_conflict", "replace_support", "link_verified"}:
            raise ValueError("d8_revision_operation_invalid")
        count = 2 if self.operation in {"report_conflict", "resolve_conflict"} else 1
        if (not isinstance(self.targets, tuple) or len(self.targets) != count
                or any(not isinstance(t, D8RevisionTarget) for t in self.targets)
                or len({t.root_id for t in self.targets}) != count):
            raise ValueError("d8_revision_targets_invalid")
        _bounded(self.reason, "reason", 512)
        if self.content is not None:
            _bounded(self.content, "content", 256)
        if self.replacement_ref is not None:
            _bounded(self.replacement_ref, "replacement_ref", 256)
        if (not isinstance(self.evidence_refs, tuple) or len(self.evidence_refs) > 8
                or len(set(self.evidence_refs)) != len(self.evidence_refs)):
            raise ValueError("d8_revision_evidence_invalid")
        for ref in self.evidence_refs:
            _bounded(ref, "evidence_ref", 256)
        if self.operation in {"correct", "restore"}:
            if (self.content is None) == (self.replacement_ref is None):
                raise ValueError("d8_revision_requires_one_replacement")
            if self.content is not None and not self.evidence_refs:
                raise ValueError("d8_revision_inference_requires_sources")
        elif self.operation in {"replace_support", "link_verified"}:
            if self.replacement_ref is None or self.content is not None:
                raise ValueError("d8_support_requires_exact_replacement")
        elif self.content is not None or self.replacement_ref is not None:
            raise ValueError("d8_revision_unexpected_replacement")
        if self.operation == "resolve_conflict":
            if self.resolution not in {"keep_first", "keep_second", "withdraw_both",
                                       "unresolved", "different_scope"}:
                raise ValueError("d8_conflict_resolution_invalid")
        elif self.resolution is not None:
            raise ValueError("d8_revision_unexpected_resolution")
        if self.operation == "replace_support":
            if self.relation_kind not in {"parent_support", "intent_dependency"}:
                raise ValueError("d8_support_relation_invalid")
            _uuid(self.relation_owner_id, "relation_owner_id")
        elif self.relation_kind is not None or self.relation_owner_id is not None:
            raise ValueError("d8_revision_unexpected_relation")

    @classmethod
    def from_dict(cls, raw):
        value = _fields(raw, {"operation", "targets", "replacement_ref", "content",
                              "evidence_refs", "reason", "resolution", "relation_kind",
                              "relation_owner_id"})
        if not isinstance(value["targets"], list) or not isinstance(value["evidence_refs"], list):
            raise ValueError("d8_revision_arrays_required")
        return cls(**{**value, "targets": tuple(D8RevisionTarget(**_fields(t, {"root_id", "version", "ref"}))
                                               for t in value["targets"]),
                      "evidence_refs": tuple(value["evidence_refs"])})

    @classmethod
    def parse_action(cls, raw):
        if not isinstance(raw, dict) or raw.get("kind") != "conclusion_change":
            return None
        value = _fields(raw, {"kind", "operation", "targets", "replacement_ref", "content",
                              "evidence_refs", "reason", "resolution", "relation_kind",
                              "relation_owner_id"})
        return cls.from_dict({k: v for k, v in value.items() if k != "kind"})


@dataclass(frozen=True, slots=True)
class D8ChangeDecision:
    decision: str
    reason: str
    goal_supported: bool | None
    scope_explanation: str | None

    def __post_init__(self):
        if self.decision not in {"accept", "reject", "insufficient"}:
            raise ValueError("d8_change_decision_invalid")
        _bounded(self.reason, "reason", 512)
        if self.goal_supported is not None and type(self.goal_supported) is not bool:
            raise ValueError("d8_change_goal_decision_invalid")
        if self.scope_explanation is not None:
            _bounded(self.scope_explanation, "scope_explanation", 512)

    @classmethod
    def from_dict(cls, raw):
        return cls(**_fields(raw, {"decision", "reason", "goal_supported", "scope_explanation"}))


@dataclass(frozen=True, slots=True)
class D8RevisionHistoryItem(D8HistoryItem):
    logical_root_id: str = ""
    revision: int = 1
    current_status: str = "effective"
    effective_ref: str | None = None
    effective_version_id: str | None = None
    committed_version_id: str | None = None
    last_version_ref: str | None = None
    validated_by_refs: tuple[str, ...] = ()

    def __post_init__(self):
        super(D8RevisionHistoryItem, self).__post_init__()
        _uuid(self.logical_root_id, "logical_root_id")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("d8_history_revision_invalid")
        if self.current_status not in {"effective", "superseded", "withdrawn", "conflicted", "unavailable"}:
            raise ValueError("d8_history_effectiveness_invalid")
        if self.effective_ref is not None:
            _bounded(self.effective_ref, "effective_ref", 256)
        if self.effective_version_id is not None:
            _uuid(self.effective_version_id, "effective_version_id")
        if self.committed_version_id is not None:
            _uuid(self.committed_version_id, "committed_version_id")
        if self.last_version_ref is not None:
            _bounded(self.last_version_ref, "last_version_ref", 256)
        if not isinstance(self.validated_by_refs, tuple):
            raise ValueError("d8_validation_links_invalid")


@dataclass(frozen=True, slots=True)
class D8PriorChange:
    record_id: str
    roots: tuple[str, ...]
    operation: str
    decision: str
    request_reason: str
    review_reason: str

    def __post_init__(self):
        _uuid(self.record_id, "record_id")
        if not isinstance(self.roots, tuple) or not 1 <= len(self.roots) <= 2:
            raise ValueError("d8_prior_change_roots_invalid")
        for root in self.roots:
            _uuid(root, "root_id")
        _bounded(self.request_reason, "request_reason", 512)
        _bounded(self.review_reason, "review_reason", 512)


@dataclass(frozen=True, slots=True)
class D8ChangeRequest:
    model_request_id: str
    task_run_id: str
    actor_intent_id: str
    stage: str
    role_config_ref: str
    base_checkpoint_sequence: int
    history: tuple[D8HistoryItem, ...]
    candidate: D8ConclusionChange | None = None
    original_goal: str | None = None
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None
    history_context: str = ""
    purpose: str = "conclusion_revision"
    prior_changes: tuple[D8PriorChange, ...] = ()
    relation_old_ref: str | None = None

    def __post_init__(self):
        for name in ("model_request_id", "task_run_id", "actor_intent_id"):
            _uuid(getattr(self, name), name)
        if self.stage not in {"fact_proposal", "fact_review"}:
            raise ValueError("d8_change_stage_invalid")
        if self.purpose != "conclusion_revision" or not isinstance(self.history_context, str):
            raise ValueError("d8_change_purpose_invalid")
        if self.relation_old_ref is not None:
            _bounded(self.relation_old_ref, "relation_old_ref", 256)
        if self.stage == "fact_review" and not isinstance(self.candidate, D8ConclusionChange):
            raise ValueError("d8_change_review_requires_candidate")
        if self.stage == "fact_proposal" and self.candidate is not None:
            raise ValueError("d8_change_proposal_cannot_claim_candidate")
        if type(self.base_checkpoint_sequence) is not int or self.base_checkpoint_sequence < 1:
            raise ValueError("d8_change_checkpoint_invalid")
        if not self.role_config_ref.startswith("sha256:"):
            raise ValueError("d8_change_role_invalid")
        _sha(self.role_config_ref[7:], "role_config_ref")
        if not isinstance(self.history, tuple) or any(not isinstance(i, D8HistoryItem) for i in self.history):
            raise ValueError("d8_change_history_invalid")
        if not isinstance(self.prior_changes, tuple) or any(not isinstance(i, D8PriorChange) for i in self.prior_changes):
            raise ValueError("d8_prior_change_invalid")
        if self.correction_of_model_request_id is not None:
            _uuid(self.correction_of_model_request_id, "correction_request")
            _bounded(self.rejection_code, "rejection_code", 256)
        elif self.rejection_code is not None:
            raise ValueError("d8_change_correction_invalid")

    @property
    def context_sha256(self):
        return conclusion_digest(asdict(self))

    @classmethod
    def from_dict(cls, raw):
        value = dict(raw)
        value["history"] = tuple(D8HistoryItem.from_dict(i) for i in value["history"])
        value["prior_changes"] = tuple(D8PriorChange(**{**i, "roots": tuple(i["roots"])})
                                       for i in value.get("prior_changes", ()))
        if value["candidate"] is not None:
            value["candidate"] = D8ConclusionChange.from_dict(value["candidate"])
        return cls(**value)


@dataclass(frozen=True, slots=True)
class D8RevisionRecord:
    record_id: str
    task_run_id: str
    actor_intent_id: str
    sequence: int
    operation: str
    decision: str
    proposal_request_id: str
    review_request_id: str
    evidence_ref: str
    evidence_sha256: str

    def __post_init__(self):
        for name in ("record_id", "task_run_id", "actor_intent_id", "proposal_request_id",
                     "review_request_id", "evidence_ref"):
            _uuid(getattr(self, name), name)
        _sha(self.evidence_sha256, "evidence_sha256")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("d8_revision_sequence_invalid")
        if self.operation not in {"correct", "withdraw", "restore", "report_conflict",
                                  "resolve_conflict", "replace_support", "link_verified"}:
            raise ValueError("d8_revision_operation_invalid")
        if self.decision not in {"accept", "reject", "insufficient"}:
            raise ValueError("d8_revision_decision_invalid")
