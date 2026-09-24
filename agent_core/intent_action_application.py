"""Durable identities for applying one audited Intent action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_core.domain import PlanStep
from agent_core.modeling import (
    FileTypeCandidateAction,
    InspectFileAction,
    IntentAction,
    StopIntentAction,
)


class IntentActionApplicationConflict(RuntimeError):
    """An audited action was already assigned different side-effect identities."""


def _uuid4(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4")


@dataclass(frozen=True, slots=True)
class IntentPlanApplication:
    model_request_id: str
    task_run_id: str
    intent_id: str
    steps: tuple[PlanStep, ...]
    plan_id: str
    register_mutation_id: str
    start_mutation_id: str

    def __post_init__(self) -> None:
        for name in (
            "model_request_id",
            "task_run_id",
            "plan_id",
            "register_mutation_id",
            "start_mutation_id",
        ):
            _uuid4(getattr(self, name), name)
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("intent_id is required")
        if (
            not isinstance(self.steps, tuple)
            or len(self.steps) != 1
            or not isinstance(self.steps[0], PlanStep)
        ):
            raise ValueError("T4 Plan must contain exactly one step")
        if self.register_mutation_id == self.start_mutation_id:
            raise ValueError("Plan mutation IDs must be distinct")


@dataclass(frozen=True, slots=True)
class IntentActionApplication:
    model_request_id: str
    task_run_id: str
    intent_id: str
    plan_id: str
    action: IntentAction
    tool_request_id: str | None = None
    retry_request_id: str | None = None
    complete_plan_mutation_id: str | None = None
    proof_id: str | None = None
    fact_id: str | None = None
    commit_fact_mutation_id: str | None = None
    complete_intent_mutation_id: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id, "model_request_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("intent_id is required")
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("plan_id is required")
        ids = (
            self.tool_request_id,
            self.retry_request_id,
            self.complete_plan_mutation_id,
            self.proof_id,
            self.fact_id,
            self.commit_fact_mutation_id,
            self.complete_intent_mutation_id,
        )
        for name, value in zip(
            (
                "tool_request_id",
                "retry_request_id",
                "complete_plan_mutation_id",
                "proof_id",
                "fact_id",
                "commit_fact_mutation_id",
                "complete_intent_mutation_id",
            ),
            ids,
            strict=True,
        ):
            if value is not None:
                _uuid4(value, name)
        present_ids = [value for value in ids if value is not None]
        if len(set(present_ids)) != len(present_ids):
            raise ValueError("side-effect IDs must be distinct")
        if isinstance(self.action, InspectFileAction):
            if any(value is None for value in ids[:3]) or any(
                value is not None for value in ids[3:]
            ):
                raise ValueError("inspect_file requires only tool and Plan IDs")
        elif isinstance(self.action, FileTypeCandidateAction):
            if any(value is not None for value in ids[:3]) or any(
                value is None for value in ids[3:]
            ):
                raise ValueError("file_type_candidate requires only proof and Fact IDs")
        elif isinstance(self.action, StopIntentAction):
            if any(value is not None for value in ids):
                raise ValueError("stop must not allocate side-effect IDs")
        else:
            raise TypeError("unsupported Intent action")


class IntentApplicationStorePort(Protocol):
    def save_plan_application(self, claim: IntentPlanApplication) -> None: ...

    def load_plan_application(
        self, model_request_id: str
    ) -> IntentPlanApplication | None: ...

    def save_action_application(self, claim: IntentActionApplication) -> None: ...

    def load_action_application(
        self, model_request_id: str
    ) -> IntentActionApplication | None: ...
