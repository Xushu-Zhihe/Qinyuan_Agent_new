"""Trusted, immutable goals for the first file-type Intent slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_core.file_inspection import validate_inspection_path


GOAL_BINDING_ERROR_CODES = frozenset(
    {
        "intent_not_running",
        "task_run_mismatch",
        "goal_path_not_in_task_scope",
        "goal_path_not_authorized",
        "goal_conflict",
        "mutation_conflict",
    }
)


class IntentGoalBindingError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in GOAL_BINDING_ERROR_CODES:
            raise ValueError("unsupported Intent goal binding error code")
        self.code = code
        super().__init__(code)


def _uuid4(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4")


def _canonical_uuid(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")


@dataclass(frozen=True, slots=True)
class FileTypeIntentGoal:
    task_run_id: str
    intent_id: str
    path: str
    goal_type: Literal["file_type"] = "file_type"

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _canonical_uuid(self.intent_id, "intent_id")
        if self.goal_type != "file_type":
            raise ValueError("only file_type Intent goals are supported")
        validate_inspection_path(self.path)
