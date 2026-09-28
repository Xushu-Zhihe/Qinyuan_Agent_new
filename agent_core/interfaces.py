"""Confirmed internal ports; no application, tool or result schema is inferred.

Existing domain types and ports are reused. The new protocols describe only
the confirmed slices documented in interface.md, not a complete agent runtime.
All methods are declarations; no business logic or dependency wiring lives here.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ModelContextSnapshot, RecoveryResult
from agent_core.control_store import (
    CommittedOperation,
    ControlCommitResult,
    ControlMutation,
    ControlStateSnapshot,
    ControlStore,
    TaskRunSnapshot,
    TurnRegistration,
    TurnOperation,
)
from agent_core.domain import ExecutionEvidencePort, PlanValidationEvidencePort
from agent_core.modeling import (
    CancellationToken,
    DeadlinePort,
    IntentCandidate,
    ModelAdapter,
    ModelError,
    ModelErrorEvidencePort,
    ModelRequest,
    ModelResponse,
)
from agent_core.runtime import SandboxPort, TaskRunSandboxPort, TaskRunStatus
from agent_core.supervisor_contracts import IntentRunnerPort
from agent_core.intent_loop import IntentLoopResult
from agent_core.tool_contracts import (
    ScopedToolGatewayPort, ToolPolicyPort, ToolCommandPlannerPort,
    ToolSandboxPort, ToolRecorderPort,
    ToolAuthorization,
)


class ClockPort(Protocol):
    """UTC Unix seconds for the persistent control plane."""

    def now_utc(self) -> float: ...


class TaskRunLifecyclePort(Protocol):
    """Persistent controller subset; requires store/repository/registry wiring.

    UUID4 mutation IDs are mandatory here. The legacy in-memory controller's
    optional arguments and return values are outside this contract.
    """

    def start(
        self,
        request: TaskRequest,
        effective_model_backend: ModelBackend,
        mutation_id: str,
        *,
        source_root: str | None = None,
        task_run_id: str | None = None,
    ) -> CommittedOperation: ...

    def attach(self, task_run_id: str) -> ControlStateSnapshot: ...

    def status(self) -> TaskRunStatus: ...

    def advance_time(self, mutation_id: str) -> CommittedOperation | None: ...

    def commit_turn_registration(
        self, registration: TurnRegistration, mutation_id: str
    ) -> CommittedOperation: ...

    def commit_turn_operation(
        self, operation: TurnOperation, mutation_id: str
    ) -> CommittedOperation: ...

    def mark_candidate_found(self, mutation_id: str) -> CommittedOperation: ...

    def cancel(self, mutation_id: str) -> CommittedOperation: ...

    def conclude(
        self, status: TaskRunStatus, mutation_id: str, *, reason: str | None = None
    ) -> CommittedOperation: ...

    def recover_task_run(self, mutation_id: str) -> RecoveryResult: ...

    def reload_state(self) -> ControlStateSnapshot: ...

    def cleanup_sandbox(self) -> None: ...

    def close(self) -> None: ...


class TurnRegistrationPort(Protocol):
    """Prepare the confirmed pending Turn/Intent registration without writing."""

    @staticmethod
    def prepare_turn_registration(
        *,
        state: ControlStateSnapshot,
        mutation_id: str,
        turn_target: str,
        candidates: tuple[IntentCandidate, ...],
    ) -> TurnRegistration: ...


class ModelReasonerPort(Protocol):
    """Provider-neutral candidate generation; the loop owns action application."""

    def invoke(
        self, request: ModelRequest, cancellation_token: CancellationToken
    ) -> ModelResponse | ModelError: ...


class ContextBuilderPort(Protocol):
    """Confirmed bounded Fact/failure context query, without prompt assembly."""

    def query_model_context(self, task_run_id: str) -> ModelContextSnapshot: ...


class ControlStatePort(Protocol):
    """Read committed snapshots; recovery belongs to TaskRunLifecyclePort."""

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None: ...

    def load_control_state(self, task_run_id: str) -> ControlStateSnapshot | None: ...


class AgentApplicationPort(Protocol):
    """Application entry boundary; CLI and Python API shapes remain pending."""


class IntentToolCallingLoopPort(Protocol):
    async def run(
        self, intent_id: str, authorization: ToolAuthorization,
        cancellation_token: CancellationToken,
    ) -> IntentLoopResult: ...


class RunSupervisorPort(Protocol):
    """Owns the T6 TaskRun loop and depth-first stop semantics."""

    async def run(self, cancellation_token: CancellationToken | None = None) -> object: ...


class StagePlannerPort(Protocol):
    """Stage planning boundary; Stage fields and transitions are not frozen."""


class ProcedureSelectorPort(Protocol):
    """Procedure lookup boundary; matching and version rules remain pending."""


class ProcedureRunnerPort(Protocol):
    """Procedure execution boundary; failure routing remains pending."""


class ToolGatewayPort(ScopedToolGatewayPort, Protocol):
    """Confirmed T3 asynchronous request, authorization and cancellation."""


class PolicyPort(ToolPolicyPort, Protocol):
    """Authorize input and verify the exact returned command before dispatch."""


class MCPManagerPort(ToolCommandPlannerPort, Protocol):
    """Bounded command planning; no Sandbox execution or hidden retries."""


class SandboxRuntimePort(ToolSandboxPort, Protocol):
    """TaskRun-owned T3 execution; lifecycle remains on SandboxPort."""


class ExecutionRecorderPort(ToolRecorderPort, Protocol):
    """Independent bounded evidence and persistent request receipts."""


class FactVerifierPort(Protocol):
    """Candidate Fact verification boundary; postcondition rules remain pending."""


class AtomicCommitterPort(Protocol):
    """Typed control-plane commit boundary."""

    def commit(self, mutation: ControlMutation) -> ControlCommitResult: ...


class GoalEvaluatorPort(Protocol):
    """Goal evaluation boundary; completion evidence and output remain pending."""


class EventPort(Protocol):
    """Event publication boundary; subscription and replay rules remain pending."""


__all__ = [
    "IntentToolCallingLoopPort",
    "CancellationToken",
    "AgentApplicationPort",
    "AtomicCommitterPort",
    "ClockPort",
    "ContextBuilderPort",
    "ControlStatePort",
    "EventPort",
    "ExecutionRecorderPort",
    "FactVerifierPort",
    "GoalEvaluatorPort",
    "MCPManagerPort",
    "ControlStore",
    "DeadlinePort",
    "ExecutionEvidencePort",
    "ModelAdapter",
    "ModelErrorEvidencePort",
    "ModelReasonerPort",
    "PolicyPort",
    "ProcedureRunnerPort",
    "ProcedureSelectorPort",
    "RunSupervisorPort",
    "IntentRunnerPort",
    "SandboxRuntimePort",
    "StagePlannerPort",
    "PlanValidationEvidencePort",
    "SandboxPort",
    "TaskRunSandboxPort",
    "TaskRunLifecyclePort",
    "ToolGatewayPort",
    "TurnRegistrationPort",
]
