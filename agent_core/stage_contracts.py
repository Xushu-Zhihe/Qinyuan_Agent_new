"""Stage declarations shared by planning and trusted persistence.

Source refs use the existing version-bound history identity. The trusted
boundary validates ownership, applicability and audit bindings before commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Literal, Protocol, TypeAlias

from agent_core.domain import IntentStatus
from agent_core.actions import MCPCall
from agent_core.conclusion_revision_contracts import D8ChangeDecision, D8ConclusionChange, D8RevisionRecord
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.operation_accounting import D4OperationRecord, D8HandoffBudgetPolicy
from agent_core.turn_contracts import D1RoleConfig

MAX_IN_FLIGHT_TOOLS = 2


@dataclass(frozen=True, slots=True)
class StageToolBatch:
    calls: tuple[MCPCall, ...]


def tool_calls(value):
    return value.calls if isinstance(value, StageToolBatch) else (value,) if isinstance(value, MCPCall) else ()


def tool_call_key(parent, value, call):
    """A batch item is bound to its exact unique arguments, not a fabricated model call."""
    from dataclasses import asdict
    from agent_core.turn_modeling import digest
    return parent + "/" + digest(asdict(call)) if isinstance(value, StageToolBatch) else parent

class TurnPlanningState(StrEnum):
    ACTIVE = "active"
    WAITING_CHILD = "waiting_child"
    EXHAUSTED = "exhausted"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class PlanningSession:
    """TaskRun plus scope/owner identifies the retained logical conversation."""

    scope: Literal["turn_planning", "road_execution"]
    owner_id: str


@dataclass(frozen=True, slots=True)
class TurnIntent:
    """Immutable after registration; support refs describe known premises."""

    description: str
    completion_criterion: str
    support_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoadCandidate:
    """A route for the planning session's own Turn, without generated IDs."""

    description: str
    support_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoadSnapshot:
    road_id: str
    candidate: RoadCandidate
    status: IntentStatus


@dataclass(frozen=True, slots=True)
class TurnCompletion:
    """Inner's proposed achievement, not an accepted result or a new Fact."""

    reason: str
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnAchievement:
    """Trusted record of an accepted completion; no duplicate achieved flag."""

    completion: TurnCompletion
    review_operation_id: str


@dataclass(frozen=True, slots=True)
class StageTurnSnapshot:
    """New-framework projection; the enclosing lookup owns the TaskRun scope.

    fact_refs is Turn.Fact's selection of original formal Fact versions.
    Road ownership follows containment here; storage remains normalized.
    """

    turn_id: str
    intent: TurnIntent
    planning_state: TurnPlanningState
    parent_turn_id: str | None = None
    roads: tuple[RoadSnapshot, ...] = ()
    fact_refs: tuple[str, ...] = ()
    achievement: TurnAchievement | None = None


@dataclass(frozen=True, slots=True)
class TurnPlanningContext:
    """Trusted input; history retains original status, scope and source version.

    turn is absent only during root proposal initialization. Feedback and
    pending operations remain references to their existing authoritative logs.
    """

    task_run_id: str
    goal: str
    session: PlanningSession
    turn: StageTurnSnapshot | None
    history: tuple[D8HistoryItem, ...]
    feedback_refs: tuple[str, ...] = ()
    pending_operation_ids: tuple[str, ...] = ()
    effective_support_refs: tuple[str, ...] = ()
    goal_validation: Literal["human", "external"] | None = None


@dataclass(frozen=True, slots=True)
class PlanningExhausted:
    """Scoped proposal requiring review; technical waiting is not exhaustion."""

    reason: str
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoadResolution:
    outcome: IntentStatus
    reason: str
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageFactSelection:
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageHistoryRead:
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageConclusionChange(D8ConclusionChange):
    SUPPORT_RELATIONS: ClassVar[frozenset[str]] = frozenset({"turn_premise", "road_premise"})


@dataclass(frozen=True, slots=True)
class StagePlanningDecision:
    kind: Literal["select_road", "return_to_parent", "request_final_validation", "wait_for_tools", "wait_for_review"]
    reason: str
    support_refs: tuple[str, ...]
    road_id: str | None = None
    candidate: str | None = None
    source_ref: str | None = None


TurnPlanningProposal: TypeAlias = (
    TurnIntent | tuple[RoadCandidate, ...] | TurnCompletion | PlanningExhausted | RoadResolution
    | StageFactSelection | StageHistoryRead | StageConclusionChange | StagePlanningDecision
)


@dataclass(frozen=True, slots=True)
class TurnPlanningReview:
    """Review the exact proposal; semantic edits return to the same Inner.

    For Roads, indices select semantically admissible candidates and suggest
    priority to their Inner planner. It is empty for other kinds/non-accept.
    """

    decision: Literal["accept", "revise", "reject"]
    reason: str
    support_refs: tuple[str, ...] = ()
    accepted_road_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class StageRunConfig:
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    budget: D8HandoffBudgetPolicy
    execution_policy_ref: str | None = None
    goal_validation: Literal["human", "external"] | None = None


@dataclass(frozen=True, slots=True)
class StageFactProposal:
    content: str
    reason: str
    support_refs: tuple[str, ...]
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class StageFactBatch:
    candidates: tuple[StageFactProposal, ...]

    @property
    def support_refs(self):
        return tuple(dict.fromkeys(ref for value in self.candidates for ref in value.support_refs))

    @property
    def reason(self):
        return "Independent conclusion candidates"


@dataclass(frozen=True, slots=True)
class StageFactBatchReview:
    decisions: tuple[TurnPlanningReview, ...]

    @property
    def decision(self):
        return "accept" if any(item.decision == "accept" for item in self.decisions) else "reject"

    @property
    def reason(self):
        return "Per-candidate review decisions"

    @property
    def support_refs(self):
        return tuple(dict.fromkeys(ref for item in self.decisions for ref in item.support_refs))


def fact_candidates(value):
    return (tuple(enumerate(value.candidates)) if isinstance(value, StageFactBatch)
            else ((None, value),) if isinstance(value, StageFactProposal) else ())


def reviewed_fact(value, review, index):
    """Exact proposal order is the per-claim identity within a batch."""
    if isinstance(value, StageFactBatch):
        if (type(index) is not int or not 0 <= index < len(value.candidates)
                or not isinstance(review, StageFactBatchReview) or len(review.decisions) != len(value.candidates)):
            raise ValueError("stage.fact_batch_binding_invalid")
        return value.candidates[index], review.decisions[index]
    if index is not None or not isinstance(value, StageFactProposal) or not isinstance(review, TurnPlanningReview):
        raise ValueError("stage.fact_binding_invalid")
    return value, review


def fact_verification_ref(review_id, index=None):
    return "stage:" + review_id + ("" if index is None else ":" + str(index))


def fact_review_parts(reference):
    parts = reference.removeprefix("stage:").split(":")
    index = None if len(parts) == 1 else int(parts[1])
    if (len(parts) not in {1, 2} or index is not None and index < 0
            or reference != fact_verification_ref(parts[0], index)):
        raise ValueError("stage.fact_binding_invalid")
    return parts[0], index


@dataclass(frozen=True, slots=True)
class StageFactApplication:
    fact_id: str
    review_operation_id: str
    candidate_index: int | None = None


@dataclass(frozen=True, slots=True)
class StageIncomplete:
    reason: str
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageStrategyAdvice:
    decision: Literal["continue_current_intent", "request_intent_resolution"]
    reason: str
    support_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageSessionSummary:
    content: str


@dataclass(frozen=True, slots=True)
class StageReviewBinding:
    proposal_operation_id: str
    review_operation_id: str


@dataclass(frozen=True, slots=True)
class StageQueuedReview:
    """A queue entry is not acceptance or a formal knowledge record."""

    proposal_operation_id: str
    state: Literal["pending", "blocked", "settled", "superseded"]
    review_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class StageBranchFork:
    proposal_operation_id: str
    review_operation_id: str | None = None
    target_session: PlanningSession | None = None


@dataclass(frozen=True, slots=True)
class StageSessionBranch:
    branch_id: str
    proposal_operation_id: str
    review_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class StageStateChange:
    """Atomic command; the enclosing ControlMutation owns run and mutation IDs."""

    turns: tuple[StageTurnSnapshot, ...] = ()
    sessions: tuple[PlanningSession, ...] = ()
    reviews: tuple[StageReviewBinding, ...] = ()
    config: StageRunConfig | None = None
    facts: tuple[StageFactApplication, ...] = ()
    applications: tuple[str, ...] = ()
    revisions: tuple[D8RevisionRecord, ...] = ()
    review_submissions: tuple[str, ...] = ()
    blocked_reviews: tuple[StageReviewBinding, ...] = ()
    branches: tuple[StageBranchFork, ...] = ()
    planning_submissions: tuple[str, ...] = ()
    discarded_decisions: tuple[StageReviewBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class StageModelEvidence:
    session: PlanningSession
    context_sha256: str
    evidence_sha256: str
    result: TurnPlanningProposal | TurnPlanningReview | StageFactProposal | StageFactBatch | StageFactBatchReview | MCPCall | StageToolBatch | StageIncomplete | StageStrategyAdvice | StageSessionSummary | D8ChangeDecision
    proposal_operation_id: str | None = None
    proposal_sha256: str | None = None


class StageEvidencePort(Protocol):
    """Trusted IO verifies original bytes and frozen request metadata.

    This is supplied by existing evidence/history adapters, never model output.
    """

    def read_history(
        self, task_run_id: str, refs: tuple[str, ...],
    ) -> tuple[D8HistoryItem, ...]: ...

    def read_model_result(self, operation: D4OperationRecord) -> StageModelEvidence: ...

    def read_history_result(self, operation: D4OperationRecord) -> tuple[D8HistoryItem, ...]: ...

    def request(self, identity: str): ...

    def read(self, kind: str, identity: str) -> dict | None: ...

    def correction_depth(self, request) -> int: ...

    def transport_failure(self, request, settled: dict | None = None) -> dict | None: ...

    def review_sources(self, request) -> tuple[tuple[str, StageModelEvidence], ...]: ...

    def review_rejections(self, review_id: str) -> tuple[str, ...]: ...

    def review_interruptions(self, review_id: str) -> tuple[str, ...]: ...
