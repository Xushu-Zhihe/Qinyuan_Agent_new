"""Intent tree context management for D22.

Explicit active_intent_frontier construction for tree planning context injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_core.domain import IntentStatus

if TYPE_CHECKING:
    from agent_core.domain import Intent, Turn


@dataclass(frozen=True)
class IntentFrontierEntry:
    """Single entry in the active Intent frontier.

    Represents one pending or running Intent that the tree planning model
    should consider when proposing new candidate Intents.
    """

    intent_id: str  # UUID of Intent
    turn_id: str  # Parent Turn UUID
    description: str  # Goal text
    status: str  # IntentStatus value
    parent_intent_id: str | None  # If this Intent spawned from another
    depth: int  # Tree depth (0 = root Intent from Turn)
    has_running_plan: bool  # True if Intent has Plan in RUNNING status
    plan_count: int  # Total Plans under this Intent
    fact_count: int  # Total Facts sourced from this Intent's Executions


@dataclass(frozen=True)
class IntentTreeFrontier:
    """Active Intent frontier for tree planning context.

    Explicit listing of all pending/running Intents that the model should
    consider when proposing new candidate Intents.

    Design principle: Lenient toward input (model can see all candidates),
    strict about output boundaries (only validated Intents make it here).
    """

    entries: tuple[IntentFrontierEntry, ...]  # Sorted by creation order
    total_pending: int  # Count of IntentStatus.PENDING
    total_running: int  # Count of IntentStatus.RUNNING
    max_depth: int  # Maximum tree depth
    task_run_id: str


def build_intent_frontier(
    intents: list[Intent],
    turns: list[Turn],
    plans: list,  # List of Plan domain objects
    facts: list,  # List of Fact domain objects
    task_run_id: str,
) -> IntentTreeFrontier:
    """
    Build active Intent frontier from current state.

    Only includes Intents with status PENDING or RUNNING (excludes COMPLETED,
    FAILED, CANCELLED). This is the "active frontier" the model uses to decide
    where to explore next in the Intent tree.

    Args:
        intents: All Intents from state
        turns: All Turns from state (for depth calculation)
        plans: All Plans from state (for activity tracking)
        facts: All Facts from state (for progress tracking)
        task_run_id: Current TaskRun UUID

    Returns:
        IntentTreeFrontier with active entries

    Design:
        - Lenient: includes all pending/running Intents (no filtering by age)
        - Strict: only validated Intents from state (no candidates)
        - Explicit: model receives structured frontier, not inferred
    """
    # Index Plans and Facts by Intent
    plans_by_intent: dict[str, list] = {}
    for plan in plans:
        plans_by_intent.setdefault(plan.intent_id, []).append(plan)

    facts_by_intent: dict[str, int] = {}
    for fact in facts:
        # Fact.source_ref points to Execution, need to traverse Plan
        for plan in plans:
            for execution in getattr(plan, "executions", []):
                if execution.execution_id == fact.source_ref:
                    facts_by_intent[plan.intent_id] = facts_by_intent.get(plan.intent_id, 0) + 1

    # Build parent chain for depth calculation
    intent_parents = {i.intent_id: i.parent_intent_id for i in intents}

    def compute_depth(intent_id: str) -> int:
        """Compute tree depth (0 = root Intent from Turn)."""
        depth = 0
        current = intent_id
        while True:
            parent = intent_parents.get(current)
            if parent is None:
                break
            depth += 1
            current = parent
            if depth > 100:  # Safety limit
                break
        return depth

    # Filter to active frontier (pending/running only)
    active_intents = [
        i for i in intents
        if i.status in {IntentStatus.PENDING, IntentStatus.RUNNING}
    ]

    # Build frontier entries
    entries = []
    for intent in active_intents:
        plans_for_intent = plans_by_intent.get(intent.intent_id, [])
        has_running = any(p.status.value == "running" for p in plans_for_intent)
        plan_count = len(plans_for_intent)
        fact_count = facts_by_intent.get(intent.intent_id, 0)
        depth = compute_depth(intent.intent_id)

        entries.append(
            IntentFrontierEntry(
                intent_id=intent.intent_id,
                turn_id=intent.turn_id,
                description=intent.description,
                status=intent.status.value,
                parent_intent_id=intent.parent_intent_id,
                depth=depth,
                has_running_plan=has_running,
                plan_count=plan_count,
                fact_count=fact_count,
            )
        )

    # Sort by creation order (Intent UUID order, which is stable)
    entries.sort(key=lambda e: e.intent_id)

    # Compute statistics
    total_pending = sum(1 for e in entries if e.status == "pending")
    total_running = sum(1 for e in entries if e.status == "running")
    max_depth = max((e.depth for e in entries), default=0)

    return IntentTreeFrontier(
        entries=tuple(entries),
        total_pending=total_pending,
        total_running=total_running,
        max_depth=max_depth,
        task_run_id=task_run_id,
    )


def format_frontier_for_context(frontier: IntentTreeFrontier) -> str:
    """
    Format frontier as JSON for model context injection.

    Returns compact JSON representation suitable for tree planning context.
    """
    import json

    data = {
        "task_run_id": frontier.task_run_id,
        "total_pending": frontier.total_pending,
        "total_running": frontier.total_running,
        "max_depth": frontier.max_depth,
        "active_intents": [
            {
                "intent_id": e.intent_id,
                "turn_id": e.turn_id,
                "description": e.description,
                "status": e.status,
                "parent_intent_id": e.parent_intent_id,
                "depth": e.depth,
                "has_running_plan": e.has_running_plan,
                "plan_count": e.plan_count,
                "fact_count": e.fact_count,
            }
            for e in frontier.entries
        ],
    }
    return json.dumps(data, indent=2)
