"""Immutable, checkable Intent claims and conservative evidence evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from agent_core.domain import ExecutionStatus, IntentStatus, PlanStatus
from agent_core.runtime import TaskRunStatus
from agent_core.command_contracts import (
    T6CommandRecorderPort,
    T6EvidenceError,
    validate_working_directory,
)


def _uuid4(value: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_postcondition_task_id") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid_postcondition_task_id")


@dataclass(frozen=True, slots=True)
class OutputContains:
    step_index: int
    stream: str
    literal: str

    def __post_init__(self) -> None:
        if type(self.step_index) is not int or self.step_index < 0:
            raise ValueError("invalid_postcondition_step")
        if self.stream not in ("stdout", "stderr"):
            raise ValueError("invalid_postcondition_stream")
        if (
            not isinstance(self.literal, str)
            or not self.literal
            or len(self.literal.encode("utf-8")) > 4096
        ):
            raise ValueError("invalid_postcondition_literal")


@dataclass(frozen=True, slots=True)
class FileSha256:
    step_index: int
    path: str
    expected_sha256: str

    def __post_init__(self) -> None:
        if type(self.step_index) is not int or self.step_index < 0:
            raise ValueError("invalid_postcondition_step")
        validate_working_directory(self.path)
        if self.path == "/workspace/work":
            raise ValueError("postcondition_file_is_directory")
        if (
            not isinstance(self.expected_sha256, str)
            or len(self.expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.expected_sha256)
        ):
            raise ValueError("invalid_postcondition_digest")


Postcondition = OutputContains | FileSha256


@dataclass(frozen=True, slots=True)
class IntentPostconditions:
    task_run_id: str
    intent_id: str
    plan_id: str
    conditions: tuple[Postcondition, ...]

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id)
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("invalid_postcondition_intent")
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("invalid_postcondition_plan")
        if (
            not isinstance(self.conditions, tuple)
            or not self.conditions
            or len(self.conditions) > 32
            or any(
                not isinstance(c, (OutputContains, FileSha256)) for c in self.conditions
            )
        ):
            raise ValueError("invalid_postconditions")


class CheckStatus(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class FileDigestObservation:
    status: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"present", "missing", "unsafe", "unknown"}:
            raise ValueError("invalid_digest_status")
        if self.status == "present" and (
            self.sha256 is None
            or len(self.sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.sha256)
        ):
            raise ValueError("invalid_digest_observation")
        if self.status != "present" and self.sha256 is not None:
            raise ValueError("unexpected_digest")


class PostconditionStorePort(Protocol):
    def bind(self, binding: IntentPostconditions) -> None: ...
    def read(
        self, plan_id: str, intent_id: str | None = None
    ) -> IntentPostconditions | None: ...
    def read_file(
        self, binding: IntentPostconditions, index: int, path: str
    ) -> FileDigestObservation | None: ...
    def observe_file(
        self,
        binding: IntentPostconditions,
        index: int,
        path: str,
        loader: Callable[[], FileDigestObservation],
    ) -> FileDigestObservation: ...


class FileDigestPort(Protocol):
    def hash_file(
        self, task_run_id: str, sandbox_id: str, path: str
    ) -> FileDigestObservation: ...


class T6PostconditionVerifier:
    def __init__(
        self,
        controller,
        store: PostconditionStorePort,
        commands: T6CommandRecorderPort,
        files: FileDigestPort,
    ) -> None:
        self.controller = controller
        self.store = store
        self.commands = commands
        self.files = files

    def bind(self, binding: IntentPostconditions) -> None:
        state = self.controller.control_state
        if (
            state.task_run.task_run_id != binding.task_run_id
            or state.task_run.status is not TaskRunStatus.RUNNING
        ):
            raise ValueError("postcondition_task_not_running")
        plan = next((p for p in state.plans if p.plan_id == binding.plan_id), None)
        intent = next(
            (i for i in state.intents if i.intent_id == binding.intent_id), None
        )
        if (
            plan is None
            or intent is None
            or plan.intent_id != binding.intent_id
            or intent.status is not IntentStatus.RUNNING
            or plan.status is not PlanStatus.RUNNING
        ):
            raise ValueError("postcondition_plan_not_running")
        if any(c.step_index >= len(plan.steps) for c in binding.conditions):
            raise ValueError("postcondition_step_out_of_range")
        if any(e.plan_id == binding.plan_id for e in state.executions):
            raise ValueError("postconditions_must_precede_execution")
        self.store.bind(binding)

    def verify(self, intent_id: str) -> CheckStatus:
        state = self.controller.control_state
        plans = [plan for plan in state.plans if plan.intent_id == intent_id]
        if not plans:
            return CheckStatus.INCONCLUSIVE
        try:
            binding = self.store.read(plans[-1].plan_id, intent_id)
        except (T6EvidenceError, ValueError):
            return CheckStatus.INCONCLUSIVE
        if (
            binding is None
            or binding.task_run_id != state.task_run.task_run_id
            or binding.plan_id != plans[-1].plan_id
        ):
            return CheckStatus.INCONCLUSIVE
        plan = next((p for p in state.plans if p.plan_id == binding.plan_id), None)
        intent = next((i for i in state.intents if i.intent_id == intent_id), None)
        if (
            intent is None
            or plan is None
            or plan.intent_id != intent_id
            or plan.status is not PlanStatus.COMPLETED
            or intent.status is not IntentStatus.RUNNING
            or state.task_run.status is not TaskRunStatus.RUNNING
        ):
            return CheckStatus.INCONCLUSIVE
        return check_postconditions(
            state,
            binding,
            self.commands,
            lambda index, path: self.store.observe_file(
                binding,
                index,
                path,
                lambda: self.files.hash_file(
                    binding.task_run_id, state.task_run.sandbox_id, path
                ),
            ),
        )[0]


def check_postconditions(state, binding, commands, read_file):
    """Evaluate recorded observations; the caller owns any live file observation."""
    plan = next((p for p in state.plans if p.plan_id == binding.plan_id), None)
    if (
        binding.task_run_id != state.task_run.task_run_id
        or plan is None
        or plan.intent_id != binding.intent_id
        or plan.status is not PlanStatus.COMPLETED
        or any(c.step_index >= len(plan.steps) for c in binding.conditions)
    ):
        return CheckStatus.INCONCLUSIVE, ()
    results = []
    sources = []
    for index, condition in enumerate(binding.conditions):
        executions = [
            e
            for e in state.executions
            if e.plan_id == plan.plan_id and e.step_index == condition.step_index
        ]
        successful = [e for e in executions if e.status is ExecutionStatus.SUCCEEDED]
        if len(successful) != 1 or any(
            e.status
            not in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.TIMED_OUT,
            }
            for e in executions
        ):
            return CheckStatus.INCONCLUSIVE, ()
        execution = successful[0]
        try:
            finished = commands.read_completed(execution.execution_id)
            # A successful retry cannot erase an unknown earlier outcome.
            for previous in executions:
                if previous is execution:
                    continue
                prior = commands.read_completed(previous.execution_id)
                if (
                    prior is None
                    or prior.record.status not in {"step_timeout", "environment_fault"}
                    or prior.record.request_id != previous.execution_id
                    or prior.record.task_run_id != binding.task_run_id
                    or prior.record.sandbox_id != state.task_run.sandbox_id
                    or prior.record.plan_id != plan.plan_id
                    or prior.record.step_index != condition.step_index
                    or prior.record.command_ref != previous.command_ref
                    or prior.record.request_id != previous.output_ref
                    or not prior.record.termination_confirmed
                    or (prior.record.status == "step_timeout")
                    != (previous.status is ExecutionStatus.TIMED_OUT)
                ):
                    return CheckStatus.INCONCLUSIVE, ()
        except Exception:  # noqa: BLE001 - evidence backend errors are inconclusive
            return CheckStatus.INCONCLUSIVE, ()
        if (
            finished is None
            or finished.action is not None
            or finished.record.status != "completed"
            or finished.record.task_run_id != binding.task_run_id
            or finished.record.sandbox_id != state.task_run.sandbox_id
            or finished.record.plan_id != plan.plan_id
            or finished.record.step_index != condition.step_index
            or finished.record.command_ref != execution.command_ref
            or execution.execution_id != finished.record.request_id
            or execution.output_ref != finished.record.request_id
            or not state.task_run.started_at
            <= finished.record.started_at
            <= finished.record.finished_at
            <= state.task_run.deadline_at
        ):
            return CheckStatus.INCONCLUSIVE, ()
        sources.append(execution.execution_id)
        if isinstance(condition, OutputContains):
            stream = getattr(finished.record, condition.stream)
            if condition.literal.encode("utf-8") in stream:
                results.append(CheckStatus.SATISFIED)
            elif getattr(finished.record, condition.stream + "_truncated"):
                results.append(CheckStatus.INCONCLUSIVE)
            else:
                results.append(CheckStatus.NOT_SATISFIED)
        else:
            try:
                observed = read_file(index, condition.path)
            except Exception:  # noqa: BLE001 - unknown Sandbox digest is inconclusive
                observed = None
            if observed is None or observed.status == "unknown":
                results.append(CheckStatus.INCONCLUSIVE)
            elif observed.status == "present":
                results.append(
                    CheckStatus.SATISFIED
                    if observed.sha256 == condition.expected_sha256
                    else CheckStatus.NOT_SATISFIED
                )
            else:
                results.append(CheckStatus.NOT_SATISFIED)
    for outcome in (CheckStatus.INCONCLUSIVE, CheckStatus.NOT_SATISFIED):
        if outcome in results:
            return outcome, tuple(sources)
    return CheckStatus.SATISFIED, tuple(sources)
