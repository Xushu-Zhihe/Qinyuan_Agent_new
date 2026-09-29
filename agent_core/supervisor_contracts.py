"""Narrow contracts for the T6-4 scheduling slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from agent_core.intent_generation import IntentGenerationResult
from agent_core.modeling import CancellationToken

IntentRunOutcome = Literal[
    "succeeded",
    "target_mismatch",
    "target_mismatch_committed",
    "step_timeout",
    "inconclusive",
    "recovery_required",
    "model_error",
    "action_rejected",
]


@dataclass(frozen=True, slots=True)
class IntentRunResult:
    """One structured classification returned by an Intent runner.

    ``recovery_required`` is deliberately non-terminal: it preserves a
    running Execution/Intent/Turn until an explicit recovery operation settles
    the unknown side effect.
    """

    task_run_id: str
    intent_id: str
    outcome: IntentRunOutcome
    plan_id: str | None = None
    evidence_ref: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_run_id, str) or not self.task_run_id.strip():
            raise ValueError("task_run_id must be non-empty")
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        if self.outcome not in {
            "succeeded",
            "target_mismatch",
            "target_mismatch_committed",
            "step_timeout",
            "inconclusive",
            "recovery_required",
            "model_error",
            "action_rejected",
        }:
            raise ValueError("unsupported Intent runner outcome")
        if self.outcome in {"target_mismatch", "target_mismatch_committed", "step_timeout"}:
            if not isinstance(self.plan_id, str) or not self.plan_id.strip():
                raise ValueError(f"{self.outcome} requires plan_id")
            if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
                raise ValueError(f"{self.outcome} requires evidence_ref")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be text")
        if len(self.reason) > 256:
            raise ValueError("reason must be at most 256 Unicode code points")


class IntentRunnerPort(Protocol):
    """Execute one already-selected running Intent."""

    async def run(self, intent_id: str) -> IntentRunResult: ...


class TurnGeneratorPort(Protocol):
    """Create the next ordered Turn from committed state and context."""

    async def generate_next(
        self, task_run_id: str, turn_target: str, token: CancellationToken
    ) -> IntentGenerationResult: ...


__all__ = ["IntentRunOutcome", "IntentRunResult", "IntentRunnerPort", "TurnGeneratorPort"]
