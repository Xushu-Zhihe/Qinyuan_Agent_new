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
    kind: Literal["select_road", "return_to_parent", "request_final_validation", "wait_for_tools"]
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

    For accepted Road candidates, accepted_road_indices explicitly selects
    their order. It is empty for other proposal kinds and non-accept verdicts.
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
class StageFactApplication:
    fact_id: str
    review_operation_id: str


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
class StageStateChange:
    """Atomic command; the enclosing ControlMutation owns run and mutation IDs."""

    turns: tuple[StageTurnSnapshot, ...] = ()
    sessions: tuple[PlanningSession, ...] = ()
    reviews: tuple[StageReviewBinding, ...] = ()
    config: StageRunConfig | None = None
    facts: tuple[StageFactApplication, ...] = ()
    applications: tuple[str, ...] = ()
    revisions: tuple[D8RevisionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class StageModelEvidence:
    session: PlanningSession
    context_sha256: str
    evidence_sha256: str
    result: TurnPlanningProposal | TurnPlanningReview | StageFactProposal | MCPCall | StageToolBatch | StageIncomplete | StageStrategyAdvice | StageSessionSummary | D8ChangeDecision
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
