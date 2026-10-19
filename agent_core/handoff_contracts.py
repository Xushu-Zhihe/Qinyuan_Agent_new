"""D8-2 internal handoff contracts. Summaries never acquire Fact identity."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Protocol

from agent_core.conclusion_contracts import conclusion_digest, _fields
from agent_core.operation_accounting import _sha, _text, _uuid


def handoff_json(value) -> str:
    return json.dumps(asdict(value) if hasattr(value, "__dataclass_fields__") else value,
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class D8Capacity:
    # Sizes are JSON bytes of each packet section, not tokens. The outer budget is
    # 262144 bytes (~128K-token DeepSeek window); these section caps must leave the
    # whole packet inside it instead of pausing on the first multi-turn history.
    turn_body: int = 16384
    global_body: int = 32768
    initial_history: int = 98304
    visible_history: int = 131072
    read_return: int = 16384

    def __post_init__(self):
        initial = (1024, 2048, 8192, 16384, 2048)
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) < floor
               for f, floor in zip(fields(self), initial)):
            raise ValueError("d8_capacity_below_confirmed_initial_limit")

    def increased(self, updates: dict) -> D8Capacity:
        if not updates or set(updates) - {f.name for f in fields(self)}:
            raise ValueError("d8_capacity_update_not_in_whitelist")
        if any(type(v) is not int or v < getattr(self, k) for k, v in updates.items()):
            raise ValueError("d8_capacity_cannot_decrease")
        result = replace(self, **updates)
        if result == self:
            raise ValueError("d8_capacity_update_must_increase")
        return result


@dataclass(frozen=True, slots=True)
class D8CapacityPolicy:
    """Frozen units understood by the injected capacity adapter, not token claims."""

    policy_name: str
    measurement_unit: str
    total_units: int
    output_reserve_units: int

    def __post_init__(self):
        _text(self.policy_name, "policy_name")
        _text(self.measurement_unit, "measurement_unit")
        if (type(self.total_units) is not int or type(self.output_reserve_units) is not int
                or not 0 < self.output_reserve_units < self.total_units):
            raise ValueError("d8_model_capacity_invalid")

    @property
    def policy_ref(self):
        return "sha256:" + conclusion_digest(asdict(self))


class D8CapacityPort(Protocol):
    def fits(self, request, policy: D8CapacityPolicy) -> bool: ...


@dataclass(frozen=True, slots=True)
class D8HandoffConfig:
    outer_capacity: D8CapacityPolicy
    inner_capacity: D8CapacityPolicy
    capacity: D8Capacity = D8Capacity()

    def __post_init__(self):
        if (not isinstance(self.outer_capacity, D8CapacityPolicy)
                or not isinstance(self.inner_capacity, D8CapacityPolicy)
                or not isinstance(self.capacity, D8Capacity)):
            raise ValueError("d8_handoff_configuration_invalid")

    @classmethod
    def from_dict(cls, raw):
        value = _fields(raw, {"outer_capacity", "inner_capacity", "capacity"})
        return cls(D8CapacityPolicy(**value["outer_capacity"]),
                   D8CapacityPolicy(**value["inner_capacity"]), D8Capacity(**value["capacity"]))


@dataclass(frozen=True, slots=True)
class D8HistoryItem:
    record_id: str
    task_run_id: str
    record_type: str
    turn_id: str | None
    intent_id: str | None
    artifact_version_id: str | None
    content: str
    source_refs: tuple[str, ...]
    source_version: str
    committed_sequence: int
    identity: str
    scope: str
    source_status: str
    conflict: bool = False

    def __post_init__(self):
        _text(self.record_id, "record_id")
        _uuid(self.task_run_id, "task_run_id")
        if self.record_type not in {"verified_fact", "reviewed_inference", "progress", "intent", "turn"}:
            raise ValueError("d8_history_type_invalid")
        _sha(self.source_version, "source_version")
        if (type(self.committed_sequence) is not int or self.committed_sequence < 1
                or type(self.conflict) is not bool or not isinstance(self.content, str)
                or not isinstance(self.source_refs, tuple)
                or self.source_status not in {"available", "unavailable", "withheld"}
                or self.scope not in {"current", "historical"}):
            raise ValueError("d8_history_projection_invalid")

    @property
    def ref(self):
        # Source versions are immutable; current availability is checked separately.
        return f"{self.record_type}:{self.record_id}:{self.source_version}"

    def visible(self, *, include_content=True):
        result = asdict(self)
        result["ref"] = self.ref
        if not include_content:
            result.pop("content")
        return result

    @classmethod
    def from_dict(cls, raw):
        value = dict(raw)
        value["source_refs"] = tuple(value["source_refs"])
        if "logical_root_id" in value:
            from agent_core.conclusion_revision_contracts import D8RevisionHistoryItem
            value["validated_by_refs"] = tuple(value.get("validated_by_refs", ()))
            return D8RevisionHistoryItem(**value)
        return cls(**value)


def checked_refs(raw, available: set[str]) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise ValueError("d8_refs_must_be_array")
    if len(set(raw)) != len(raw):
        raise ValueError("d8_refs_duplicate")
    if set(raw) - available:
        raise ValueError("d8_refs_not_in_frozen_input")
    return tuple(raw)


@dataclass(frozen=True, slots=True)
class D8SummaryCandidate:
    turn_body: str
    global_body: str
    ordered_refs: tuple[str, ...]
    open_questions: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def parse(cls, raw, available: set[str], capacity: D8Capacity):
        value = _fields(raw, {"turn_body", "global_body", "ordered_refs", "open_questions"})
        for name, limit in (("turn_body", capacity.turn_body), ("global_body", capacity.global_body)):
            _text(value[name], name)
            if len(value[name]) > limit:
                raise ValueError("d8_summary_body_capacity_exceeded")
        ordered = checked_refs(value["ordered_refs"], available)
        questions = value["open_questions"]
        if not isinstance(questions, list) or len(questions) > 8:
            raise ValueError("d8_open_questions_invalid")
        parsed = []
        for question in questions:
            item = _fields(question, {"text", "refs"})
            _text(item["text"], "question_text")
            if len(item["text"]) > 256:
                raise ValueError("d8_question_too_long")
            parsed.append((item["text"], checked_refs(item["refs"], available)))
        return cls(value["turn_body"], value["global_body"], ordered, tuple(parsed))


@dataclass(frozen=True, slots=True)
class D8HandoffVersion:
    record_id: str
    task_run_id: str
    turn_id: str
    turn_version: int
    global_version: int
    base_checkpoint_sequence: int
    capacity_version: int
    evidence_ref: str
    evidence_sha256: str
    source_snapshot_sha256: str
    model_request_id: str | None
    origin: str
    index_entries: tuple[tuple[str, str, str, str | None, str | None, str | None], ...] = ()

    def __post_init__(self):
        for name in ("record_id", "task_run_id", "turn_id", "evidence_ref"):
            _uuid(getattr(self, name), name)
        for name in ("evidence_sha256", "source_snapshot_sha256"):
            _sha(getattr(self, name), name)
        if any(type(getattr(self, n)) is not int or getattr(self, n) < 1 for n in
               ("turn_version", "global_version", "base_checkpoint_sequence", "capacity_version")):
            raise ValueError("d8_handoff_version_invalid")
        if self.origin not in {"model_organized", "trusted_code_fallback"}:
            raise ValueError("d8_handoff_origin_invalid")
        if self.model_request_id is not None:
            _uuid(self.model_request_id, "model_request_id")


@dataclass(frozen=True, slots=True)
class D8CapacityVersion:
    record_id: str
    task_run_id: str
    version: int
    capacity: D8Capacity

    def __post_init__(self):
        _uuid(self.record_id, "record_id")
        _uuid(self.task_run_id, "task_run_id")
        if type(self.version) is not int or self.version < 1 or not isinstance(self.capacity, D8Capacity):
            raise ValueError("d8_capacity_version_invalid")


@dataclass(frozen=True, slots=True)
class D8TreeRecord:
    record_id: str
    task_run_id: str
    turn_id: str | None
    intent_id: str | None
    kind: str
    model_request_id: str | None
    support_fact_id: str | None = None
    inner_request_id: str | None = None
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self):
        _uuid(self.record_id, "record_id")
        _uuid(self.task_run_id, "task_run_id")
        for n in ("turn_id", "intent_id", "model_request_id", "support_fact_id", "inner_request_id"):
            if getattr(self, n) is not None:
                _uuid(getattr(self, n), n)
        if self.kind not in {"registration", "succeeded", "failed", "inconclusive", "expansion_exhausted", "turn_exhausted", "selection", "evaluation_continued"}:
            raise ValueError("d8_tree_record_kind_invalid")


@dataclass(frozen=True, slots=True)
class D8HandoffRequest:
    model_request_id: str
    task_run_id: str
    turn_id: str | None
    intent_id: str | None
    stage: str
    role_config_ref: str
    base_checkpoint_sequence: int
    capacity_version: int
    capacity: D8Capacity
    goal: str
    history: tuple[D8HistoryItem, ...]
    prior_summary: str
    support_fact_id: str | None = None
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None

    def __post_init__(self):
        _uuid(self.model_request_id, "model_request_id")
        _uuid(self.task_run_id, "task_run_id")
        _text(self.role_config_ref, "role_config_ref")
        if self.stage not in {"handoff_summary", "tree_planning", "intent_evaluation"}:
            raise ValueError("d8_handoff_stage_invalid")
        if any(i.task_run_id != self.task_run_id for i in self.history):
            raise ValueError("d8_history_cross_task")

    @property
    def context_sha256(self):
        return conclusion_digest(asdict(self))

    @classmethod
    def from_dict(cls, raw):
        value = dict(raw)
        value["history"] = tuple(D8HistoryItem.from_dict(i) for i in value["history"])
        value["capacity"] = D8Capacity(**value["capacity"])
        if "inner_suggestions" in value:
            from agent_core.session_contracts import D8SuggestedPlanningRequest, D8NextIntentSuggestion
            value["inner_suggestions"] = tuple(D8NextIntentSuggestion(
                i["description"], i["intent_type"], tuple(i["dependency_refs"]),
                i["reason"], tuple(i["evidence_refs"])) for i in value["inner_suggestions"])
            return D8SuggestedPlanningRequest(**value)
        return cls(**value)


@dataclass(frozen=True, slots=True)
class D8ReadStart:
    request_id: str
    task_run_id: str
    snapshot_id: str
    snapshot_sha256: str
    checkpoint_sequence: int
    capacity_version: int
    started_at: float

    def __post_init__(self):
        for n in ("request_id", "task_run_id", "snapshot_id"):
            _uuid(getattr(self, n), n)
        _sha(self.snapshot_sha256, "snapshot_sha256")
