from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agent_core.contracts import TaskRequest


CONFIRMATION_WINDOW_SECONDS = 180


class InvalidTransition(RuntimeError):
    """Raised when a TaskRun operation is not valid in its current state."""


class TaskRunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACHIEVED = "achieved"
    ASSUMED_ACHIEVED = "assumed_achieved"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"


TERMINAL_STATUSES = frozenset(
    {
        TaskRunStatus.ACHIEVED,
        TaskRunStatus.ASSUMED_ACHIEVED,
        TaskRunStatus.PARTIAL,
        TaskRunStatus.INCONCLUSIVE,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
        TaskRunStatus.DEADLINE_EXCEEDED,
    }
)


class SandboxPort(Protocol):
    """The only Sandbox lifecycle operations needed by this core round."""

    def create(self, task_run_id: str) -> str:
        """Create one TaskRun-owned Sandbox and return its identifier."""

    def interrupt(self, sandbox_id: str) -> None:
        """Interrupt in-flight work before a deadline-driven teardown."""

    def destroy(self, sandbox_id: str) -> None:
        """Destroy the Sandbox after the TaskRun reaches a terminal state."""


@runtime_checkable
class TaskRunSandboxPort(SandboxPort, Protocol):
    """T2 lifecycle extension; legacy in-memory sandboxes need not implement it."""

    def bind(self, task_run_id: str, sandbox_id: str, *, terminal: bool) -> None:
        """Verify an active resource, or bind a terminal resource for cleanup only."""

    def quiesce(self, sandbox_id: str) -> None:
        """Close execution admission before starting terminal cleanup."""


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: str
    task_run_id: str
    timestamp: float
    status: TaskRunStatus
    details: Mapping[str, Any] = field(default_factory=dict)
    sequence: int | None = None


@dataclass(slots=True)
class TaskRun:
    task_run_id: str
    request: TaskRequest
    started_at: float
    deadline_at: float
    status: TaskRunStatus = TaskRunStatus.RUNNING
    sandbox_id: str | None = None
    confirmation_deadline: float | None = None
    finished_at: float | None = None
    failure_reason: str | None = None

    @classmethod
    def create(
        cls,
        request: TaskRequest,
        *,
        started_at: float,
        task_run_id: str | None = None,
    ) -> "TaskRun":
        return cls(
            task_run_id=task_run_id or str(uuid.uuid4()),
            request=request,
            started_at=started_at,
            deadline_at=started_at + request.deadline_seconds,
        )


class TaskRunController:
    """Own one TaskRun and its one Sandbox lifecycle."""

    def __init__(
        self,
        sandbox: SandboxPort,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sandbox = sandbox
        self._clock = clock
        self._task_run: TaskRun | None = None
        self._events: list[RuntimeEvent] = []
        self._sandbox_destroyed = False

    @property
    def task_run(self) -> TaskRun:
        if self._task_run is None:
            raise InvalidTransition("TaskRun has not been started")
        return self._task_run

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    def start(self, request: TaskRequest) -> TaskRun:
        if self._task_run is not None:
            raise InvalidTransition("TaskRunController can only start once")

        started_at = self._clock()
        task_run = TaskRun.create(request, started_at=started_at)
        sandbox_id = self._sandbox.create(task_run.task_run_id)
        task_run.sandbox_id = sandbox_id
        self._task_run = task_run
        self._events.append(
            RuntimeEvent(
                type="task_run_started",
                task_run_id=task_run.task_run_id,
                timestamp=started_at,
                status=task_run.status,
            )
        )
        return task_run

    def poll(self) -> TaskRunStatus:
        self._enforce_time_limits(self._clock())
        return self.task_run.status

    def mark_candidate_found(self) -> TaskRunStatus:
        now = self._clock()
        self._enforce_time_limits(now)
        task_run = self.task_run
        if task_run.status != TaskRunStatus.RUNNING:
            raise InvalidTransition(
                "a completion candidate can only be reported while running"
            )
        task_run.status = TaskRunStatus.AWAITING_CONFIRMATION
        task_run.confirmation_deadline = now + CONFIRMATION_WINDOW_SECONDS
        self._events.append(
            RuntimeEvent(
                type="completion_confirmation_requested",
                task_run_id=task_run.task_run_id,
                timestamp=now,
                status=task_run.status,
            )
        )
        return task_run.status

    def handle_control_message(self, message: Mapping[str, Any]) -> TaskRunStatus:
        """Handle the confirmed external completion message shape."""

        now = self._clock()
        self._enforce_time_limits(now)
        if not isinstance(message, Mapping):
            raise InvalidTransition("control message must be an object")
        if message.get("type") != "completion":
            raise InvalidTransition("unsupported control message type")
        if message.get("task_run_id") != self.task_run.task_run_id:
            raise InvalidTransition("control message task_run_id does not match")
        if message.get("status") != "achieved":
            raise InvalidTransition("completion message status must be achieved")
        if self.task_run.status != TaskRunStatus.AWAITING_CONFIRMATION:
            raise InvalidTransition(
                "completion can only be confirmed during the confirmation period"
            )
        self._finish(TaskRunStatus.ACHIEVED, now=now)
        return self.task_run.status

    def model_failed(self, reason: str | None = None) -> TaskRunStatus:
        now = self._clock()
        self._enforce_time_limits(now)
        if self.task_run.status not in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_CONFIRMATION,
        }:
            raise InvalidTransition("model failure cannot change a terminal TaskRun")
        self.task_run.failure_reason = reason
        self._finish(TaskRunStatus.FAILED, now=now)
        return self.task_run.status

    def cancel(self) -> TaskRunStatus:
        now = self._clock()
        self._enforce_time_limits(now)
        if self.task_run.status not in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_CONFIRMATION,
        }:
            raise InvalidTransition("cancel cannot change a terminal TaskRun")
        self._finish(TaskRunStatus.CANCELLED, now=now)
        return self.task_run.status

    def conclude(self, status: TaskRunStatus) -> TaskRunStatus:
        """Finish a non-achieved TaskRun after the upper flow ends its Turns."""

        if status not in {TaskRunStatus.PARTIAL, TaskRunStatus.INCONCLUSIVE}:
            raise InvalidTransition("conclude only accepts partial or inconclusive")
        now = self._clock()
        self._enforce_time_limits(now)
        if self.task_run.status in TERMINAL_STATUSES:
            raise InvalidTransition("cannot conclude a terminal TaskRun")
        self._finish(status, now=now)
        return self.task_run.status

    def _enforce_time_limits(self, now: float) -> None:
        task_run = self.task_run
        if task_run.status in TERMINAL_STATUSES:
            return

        # Deadline has precedence over the confirmation grace period.
        if now >= task_run.deadline_at:
            self._finish(TaskRunStatus.DEADLINE_EXCEEDED, now=now, interrupt=True)
            return

        if (
            task_run.status == TaskRunStatus.AWAITING_CONFIRMATION
            and task_run.confirmation_deadline is not None
            and now >= task_run.confirmation_deadline
        ):
            self._finish(TaskRunStatus.ASSUMED_ACHIEVED, now=now)

    def _finish(
        self,
        status: TaskRunStatus,
        *,
        now: float,
        interrupt: bool = False,
    ) -> None:
        task_run = self.task_run
        if task_run.status in TERMINAL_STATUSES:
            return
        task_run.status = status
        task_run.finished_at = now

        if interrupt and task_run.sandbox_id is not None:
            self._sandbox.interrupt(task_run.sandbox_id)
        self._destroy_sandbox()
        self._events.append(
            RuntimeEvent(
                type="task_run_finished",
                task_run_id=task_run.task_run_id,
                timestamp=now,
                status=status,
            )
        )

    def _destroy_sandbox(self) -> None:
        task_run = self.task_run
        if self._sandbox_destroyed or task_run.sandbox_id is None:
            return
        self._sandbox.destroy(task_run.sandbox_id)
        self._sandbox_destroyed = True
