"""Explicit D8-4 conversations and progress judgments; no provider chat state."""

import json
from dataclasses import asdict, dataclass, replace

from agent_core.conclusion_contracts import D8ModelRequest, _sha, _uuid
from agent_core.dual_llm_contracts import D3ReviewOutput, parse_d3_review_output
from agent_core.handoff_contracts import checked_refs, D8HandoffRequest
from agent_core.conclusion_revision_contracts import D8ChangeRequest


@dataclass(frozen=True, slots=True)
class D8DecisionRecord:
    record_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    ordinal: int
    root_request_id: str
    model_request_id: str
    evidence_sha256: str
    disposition: str = "applied"

    def __post_init__(self):
        for name in ("record_id", "task_run_id", "turn_id", "intent_id",
                     "root_request_id", "model_request_id"):
            _uuid(getattr(self, name), name)
        _sha(self.evidence_sha256, "evidence_sha256")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("d8_decision_ordinal_invalid")
        if self.disposition not in {"applied", "review_requested"}:
            raise ValueError("d8_decision_disposition_invalid")


@dataclass(frozen=True, slots=True)
class D8SessionConclusionRequest(D8ModelRequest):
    session_id: str = ""
    session_context: str = ""


@dataclass(frozen=True, slots=True)
class D8SessionChangeRequest(D8ChangeRequest):
    session_id: str = ""
    session_context: str = ""


def restricted_progress_request(request):
    return json.loads(request.context_summary).get("d8_progress", {}).get("mode") == "restricted_repair"


@dataclass(frozen=True, slots=True)
class D8ProgressJudgment:
    classification: str
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D8ProgressReviewOutput(D3ReviewOutput):
    progress: D8ProgressJudgment


def parse_progress_review(raw, request):
    if "progress_policy" in json.loads(request.context_summary):
        from agent_core.progress_policy import parse_bound
        return parse_bound(raw, request)
    return _parse_progress_review_legacy(raw, request)


def _parse_progress_review_legacy(raw, request):
    if isinstance(raw, D8ProgressReviewOutput):
        raw = asdict(raw)
    if not isinstance(raw, dict) or set(raw) != {
            "decision", "rationale", "source_ids", "constraints", "progress"}:
        raise ValueError("d8_progress_fields_invalid")
    p = raw["progress"]
    if not isinstance(p, dict) or set(p) != {"classification", "reason", "evidence_refs"}:
        raise ValueError("d8_progress_judgment_invalid")
    allowed = {"meaningful": {"continue_current_intent", "replan_current_intent"},
               "repetitive": {"replan_current_intent", "switch_sibling_intent", "abandon_current_intent"},
               "inconclusive": {"pause_current_intent"}}
    if json.loads(request.context_summary).get("d8_progress", {}).get("intent_execution_policy") == "rolling_v1":
        allowed["inconclusive"] = {"continue_current_intent", "pause_current_intent"}
    if restricted_progress_request(request):
        allowed = {"meaningful": {"continue_current_intent"},
                   "repetitive": {"pause_current_intent"}, "inconclusive": {"pause_current_intent"}}
        if raw["constraints"]:
            raise ValueError("d8_repair_progress_cannot_change_constraints")
    if p["classification"] not in allowed or raw["decision"] not in allowed[p["classification"]]:
        raise ValueError("d8_progress_decision_mismatch")
    unbounded = json.loads(request.context_summary).get('progress_policy', {}).get('version') == 2
    if (not isinstance(p["reason"], str) or not p["reason"].strip()
            or ("\x00" in p["reason"] if unbounded else len(p["reason"]) > 512)):
        raise ValueError("d8_progress_reason_invalid")
    refs = checked_refs(p["evidence_refs"], set(request.source_versions))
    if not refs or len(refs) > 8:
        raise ValueError("d8_progress_sources_required")
    summary = json.loads(request.context_summary)
    summary.pop("d8_progress")
    base_request = replace(request, context_summary=json.dumps(summary))
    base = {k: v for k, v in raw.items() if k != "progress"}
    if base["decision"] in {"pause_current_intent", "abandon_current_intent"}:
        base["decision"] = "continue_current_intent"
    parsed = parse_d3_review_output(base, base_request)
    if raw["decision"] == "pause_current_intent" and parsed.constraints:
        raise ValueError("d8_progress_pause_cannot_add_constraints")
    return D8ProgressReviewOutput(raw["decision"], parsed.rationale, parsed.source_ids,
                                  parsed.constraints,
                                  D8ProgressJudgment(p["classification"], p["reason"], refs))


@dataclass(frozen=True, slots=True)
class D8NextIntentSuggestion:
    description: str
    intent_type: str | None
    dependency_refs: tuple[str, ...]
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D8SuggestedPlanningRequest(D8HandoffRequest):
    suggestion_request_id: str | None = None
    inner_suggestions: tuple[D8NextIntentSuggestion, ...] = ()
    inner_suggestions_identity: str = "inner_candidates_require_outer_review_not_registered"


def parse_next_intents(raw, visible, *, maximum=2):
    if not isinstance(raw, list) or maximum is not None and len(raw) > maximum:
        raise ValueError("d8_next_intents_array_required")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
                "description", "intent_type", "dependency_refs", "reason", "evidence_refs"}:
            raise ValueError("d8_next_intent_fields_invalid")
        if (not isinstance(item["description"], str) or not item["description"].strip()
                or item["intent_type"] is not None and not isinstance(item["intent_type"], str)
                or not isinstance(item["reason"], str) or not item["reason"].strip()
                or len(item["reason"]) > 512):
            raise ValueError("d8_next_intent_text_invalid")
        from agent_core.modeling import IntentCandidate
        IntentCandidate(item["description"], item["intent_type"])
        refs = checked_refs(item["evidence_refs"], visible)
        deps = checked_refs(item["dependency_refs"], visible)
        if not refs or len(refs) > 8:
            raise ValueError("d8_next_intent_sources_required")
        result.append(D8NextIntentSuggestion(item["description"], item["intent_type"], deps,
                                             item["reason"], refs))
    return tuple(result)
