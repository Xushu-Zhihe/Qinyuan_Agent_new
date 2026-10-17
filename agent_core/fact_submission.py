"""Fact Submission Point Logic for D22.

Determines WHEN Facts are submitted from inner layer candidates to outer layer review.
Replaces "every Plan creates exactly one Fact" with lenient submission triggers.

Submission Points (in order of strictness):
1. Intent completion handoff (mandatory)
2. Inner layer explicit request (via submission_reason in output)
3. Periodic review checkpoint (configurable interval)

Design Principle: Lenient toward input, strict about output boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.control_store import D4ControlStore, UpsertFact


class SubmissionTrigger(Enum):
    """Why a Fact submission was triggered."""

    INTENT_COMPLETION = "intent_completion"  # Intent reached terminal state
    INNER_REQUEST = "inner_request"  # Inner layer explicitly requested submission
    PERIODIC_REVIEW = "periodic_review"  # Time or step threshold reached
    BUDGET_EXHAUSTED = "budget_exhausted"  # MCP budget exhausted, force review


@dataclass(frozen=True)
class FactSubmissionConfig:
    """Configuration for Fact submission timing.

    Lenient configuration:
    - Allow inner layer to control submission frequency
    - Don't force submission after every Plan
    - Support batching multiple candidates before review

    Strict boundaries:
    - Mandatory submission at Intent completion
    - Maximum accumulation limit (prevent unbounded growth)
    - Periodic checkpoint regardless of inner layer preference
    """

    # Mandatory submission triggers
    force_on_intent_completion: bool = True
    force_on_budget_exhausted: bool = True

    # Periodic review triggers
    periodic_review_enabled: bool = True
    periodic_review_step_interval: int = 5  # Submit every N Plan steps
    periodic_review_time_interval: float = 300.0  # Submit every N seconds

    # Accumulation limits
    max_candidates_before_review: int = 10  # Force review if this many candidates pending
    max_seconds_before_review: float = 600.0  # Force review after this long


class FactSubmissionManager:
    """Manages when Facts are submitted from inner to outer layer.

    Tracks pending Fact candidates per Intent and determines when to trigger
    outer layer review based on submission triggers.
    """

    def __init__(
        self,
        control_store: D4ControlStore,
        config: FactSubmissionConfig | None = None,
    ) -> None:
        self.control_store = control_store
        self.config = config or FactSubmissionConfig()

        # Track submission state per Intent
        self._intent_submission_state: dict[str, _IntentSubmissionState] = {}

    def should_submit_now(
        self,
        intent_id: str,
        *,
        plan_step_completed: bool = False,
        inner_requested_submission: bool = False,
        intent_completing: bool = False,
        budget_exhausted: bool = False,
        current_time: float,
        pending_candidate_count: int,
    ) -> tuple[bool, SubmissionTrigger | None]:
        """
        Determine if Fact candidates should be submitted for review now.

        Lenient toward inner layer:
        - Honors explicit submission requests
        - Allows accumulation of multiple candidates
        - No forced submission after every Plan

        Strict about boundaries:
        - Mandatory submission at Intent completion
        - Mandatory submission when budget exhausted
        - Periodic checkpoint to prevent unbounded growth

        Args:
            intent_id: Current Intent UUID
            plan_step_completed: True if a Plan just completed
            inner_requested_submission: True if inner layer set submission_reason
            intent_completing: True if Intent reached terminal state
            budget_exhausted: True if MCP budget exhausted
            current_time: Current timestamp
            pending_candidate_count: Number of Fact candidates awaiting review

        Returns:
            (should_submit, trigger) tuple
            - should_submit: True if submission should happen now
            - trigger: SubmissionTrigger enum value if should_submit=True, else None
        """
        # Mandatory: Intent completion
        if intent_completing and self.config.force_on_intent_completion:
            return True, SubmissionTrigger.INTENT_COMPLETION

        # Mandatory: Budget exhausted (force review before recovery)
        if budget_exhausted and self.config.force_on_budget_exhausted:
            return True, SubmissionTrigger.BUDGET_EXHAUSTED

        # Honor inner layer explicit request
        if inner_requested_submission:
            return True, SubmissionTrigger.INNER_REQUEST

        # Get or create submission state
        state = self._intent_submission_state.get(intent_id)
        if state is None:
            state = _IntentSubmissionState(
                intent_id=intent_id,
                steps_since_last_submission=0,
                last_submission_time=current_time,
            )
            self._intent_submission_state[intent_id] = state

        # Periodic review: step interval
        if self.config.periodic_review_enabled and plan_step_completed:
            state.steps_since_last_submission += 1
            if state.steps_since_last_submission >= self.config.periodic_review_step_interval:
                return True, SubmissionTrigger.PERIODIC_REVIEW

        # Periodic review: time interval
        if self.config.periodic_review_enabled:
            time_since_last = current_time - state.last_submission_time
            if time_since_last >= self.config.periodic_review_time_interval:
                return True, SubmissionTrigger.PERIODIC_REVIEW

        # Accumulation limits: candidate count
        if pending_candidate_count >= self.config.max_candidates_before_review:
            return True, SubmissionTrigger.PERIODIC_REVIEW

        # Accumulation limits: time
        time_since_last = current_time - state.last_submission_time
        if time_since_last >= self.config.max_seconds_before_review:
            return True, SubmissionTrigger.PERIODIC_REVIEW

        # No trigger - allow inner layer to continue accumulating
        return False, None

    def record_submission(
        self,
        intent_id: str,
        trigger: SubmissionTrigger,
        current_time: float,
        submitted_count: int,
    ) -> None:
        """Record that a Fact submission just occurred."""
        state = self._intent_submission_state.get(intent_id)
        if state is None:
            state = _IntentSubmissionState(
                intent_id=intent_id,
                steps_since_last_submission=0,
                last_submission_time=current_time,
            )
        else:
            state = _IntentSubmissionState(
                intent_id=intent_id,
                steps_since_last_submission=0,  # Reset counter
                last_submission_time=current_time,
            )
        self._intent_submission_state[intent_id] = state


@dataclass(frozen=True)
class _IntentSubmissionState:
    """Internal submission tracking per Intent."""

    intent_id: str
    steps_since_last_submission: int
    last_submission_time: float
