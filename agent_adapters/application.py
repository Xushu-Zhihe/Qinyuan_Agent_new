"""Assemble the authorized T5 single-Intent slice around the existing core."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.cli_contract import IntentRunRequest
from agent_adapters.file_tool_recorder import FileToolRecorder
from agent_adapters.file_type_proof_recorder import FileTypeProofRecorder
from agent_adapters.run_files import immutable_read, immutable_write
from agent_core.attachment_import import (
    AttachmentImportRecoveryRequired,
    AttachmentImportRejected,
    AttachmentImportRequest,
)
from agent_core.attachment_importer import AttachmentImporter
from agent_core.contracts import ModelBackend
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import (
    SandboxCleanupError,
    TaskRunController,
    TaskRunControllerRegistry,
)
from agent_core.domain import ExecutionStatus, FailureType, IntentStatus
from agent_core.file_type_verifier import FileTypeVerifier
from agent_core.intent_goal_evaluator import FileTypeIntentGoalEvaluator
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.intent_loop import IntentActionRejected, IntentToolCallingLoop
from agent_core.modeling import IntentCandidate, ModelInvocationCoordinator
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.tool_contracts import ToolAuthorization
from agent_core.tool_gateway import ToolGateway
from agent_core.tool_policy import FileToolPolicy
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from Sandbox.task_run_sandbox import TaskRunSandboxError
from Sandbox.tool_runtime import TaskRunToolRuntime


class EnvironmentUnavailable(RuntimeError):
    """No Sandbox create request has been sent."""


class Cancellation:
    def __init__(self, event, directory):
        self.event, self.directory = event, directory

    @property
    def is_cancelled(self):
        return self.event.is_set() or (self.directory / "cancel-request.json").exists()


class ErrorEvidence:
    def exists(self, task_run_id, evidence_ref):
        return False


class RecordedSandbox:
    """Record resource removal separately from the Controller's task decision."""

    def __init__(self, sandbox, directory):
        self.sandbox, self.directory = sandbox, directory

    def create(self, task_run_id):
        return self.sandbox.create(task_run_id)

    def bind(self, task_run_id, sandbox_id, *, terminal):
        return self.sandbox.bind(task_run_id, sandbox_id, terminal=terminal)

    def quiesce(self, sandbox_id):
        return self.sandbox.quiesce(sandbox_id)

    def interrupt(self, sandbox_id):
        return self.sandbox.interrupt(sandbox_id)

    def destroy(self, sandbox_id):
        try:
            self.sandbox.destroy(sandbox_id)
        except BaseException:
            immutable_write(
                self.directory / ("cleanup-attempt-" + str(uuid4()) + ".json"),
                {"sandbox_id": sandbox_id, "status": "unconfirmed", "at": time.time()},
            )
            raise
        if not (self.directory / "cleanup.json").exists():
            immutable_write(
                self.directory / "cleanup.json",
                {"sandbox_id": sandbox_id, "status": "succeeded", "at": time.time()},
            )


def evidence_components(directory, store, *, read_only=False):
    tools = FileToolRecorder(directory / "tools", read_only=read_only)
    imports = FileAttachmentImportRecorder(directory / "imports", read_only=read_only)
    proofs = FileTypeProofRecorder(directory / "proofs", read_only=read_only)
    verifier = FileTypeVerifier(
        store=store, tools=tools, imports=imports, proofs=proofs
    )
    repository = ControlStateRepository(store, tools, verifier)
    evaluator = FileTypeIntentGoalEvaluator(
        store=store, repository=repository, proofs=verifier
    )
    return tools, imports, verifier, repository, evaluator


def base_result(task_id=None, mode=None):
    return {
        "protocol_version": 1,
        "task_run_id": task_id,
        "mode": mode,
        "intent": None,
        "overall_goal": {"outcome": "not_evaluated"},
        "task_run": None,
        "cleanup": {"status": "not_confirmed"},
        "errors": [],
        "exit_code": 4,
    }


def snapshot_result(directory: Path, declaration: dict):
    """No runtime attachment, migration, model call or Docker access."""
    task_id = declaration["task_run_id"]
    result = base_result(task_id, declaration["mode"])
    cleanup = directory / "cleanup.json"
    if cleanup.exists():
        result["cleanup"] = immutable_read(cleanup)
    database = directory / "control.sqlite"
    if not database.exists():
        failed = directory / "start-failure.json"
        if failed.exists():
            result.update(
                exit_code=3,
                cleanup={"status": "not_required"},
                errors=[immutable_read(failed)],
            )
            return result
        result["errors"].append({"code": "start_outcome_unknown"})
        return result
    with SqliteControlStore.open_readonly(database) as store:
        tools, imports, _verifier, repository, evaluator = evidence_components(
            directory, store, read_only=True
        )
        state = repository.load_control_state(task_id)
        if state is None:
            failed = directory / "start-failure.json"
            if failed.exists():
                result.update(
                    exit_code=3,
                    cleanup={"status": "not_required"},
                    errors=[immutable_read(failed)],
                )
                return result
            result["errors"].append({"code": "start_outcome_unknown"})
            return result
        task = state.task_run
        result["task_run"] = {
            "status": task.status.value,
            "reason": task.failure_reason,
            "sandbox_id": task.sandbox_id,
            "deadline_at": task.deadline_at,
            "finished_at": task.finished_at,
        }
        model_unknown = False
        tool_unknown = False
        if state.intents:
            intent = state.intents[0]
            binding = store.load_intent_goal(intent.intent_id)
            evaluation = (
                asdict(evaluator.evaluate(task_id, intent.intent_id))
                if binding
                else None
            )
            saved = directory / "slice-result.json"
            if not saved.exists():
                saved = directory / "recovery-result.json"
            loop = immutable_read(saved) if saved.exists() else None
            result["intent"] = {
                "intent_id": intent.intent_id,
                "status": intent.status.value,
                "evaluation": evaluation,
                "slice_result": loop,
                "facts": [asdict(f) for f in state.facts],
            }
            records = store.list_model_invocations(task_id, intent.intent_id)
            model_unknown = any(r.completion is None for r in records)
            for record in records:
                application = store.load_action_application(
                    record.start.model_request_id
                )
                if application:
                    for request_id in (
                        application.tool_request_id,
                        application.retry_request_id,
                    ):
                        if (
                            request_id
                            and tools.read_request_receipt(request_id)
                            and tools.read_completed(request_id) is None
                        ):
                            tool_unknown = True
        tool_unknown = tool_unknown or any(
            e.status in {ExecutionStatus.RUNNING, ExecutionStatus.PENDING}
            for e in state.executions
        )
        for execution in state.executions:
            receipt = tools.read_completed(execution.execution_id)
            if (
                receipt is None
                or not receipt.record.termination_confirmed
                or receipt.record.failure_type is FailureType.OUTCOME_UNKNOWN
            ):
                tool_unknown = True
        claim = imports.read_claim(declaration["ids"]["import"])
        import_unknown = bool(claim and imports.read_receipt(claim.import_id) is None)
        side_effect_unknown = tool_unknown or import_unknown
        unknown = model_unknown or side_effect_unknown
        result["recovery"] = {
            "model_response_unknown": model_unknown,
            "external_result_unknown": side_effect_unknown,
            "tool_result_unknown": tool_unknown,
            "import_result_unknown": import_unknown,
        }
        terminal = task.status in TERMINAL_STATUSES
        clean = result["cleanup"]["status"] == "succeeded"
        if terminal and not clean:
            result["exit_code"] = 5
        elif (
            terminal
            and task.status is TaskRunStatus.CANCELLED
            and clean
            and not side_effect_unknown
        ):
            # Only model responses may be abandoned by a confirmed cancellation.
            result["exit_code"] = 130
        elif side_effect_unknown:
            result["exit_code"] = 4
        elif terminal and task.status is TaskRunStatus.DEADLINE_EXCEEDED and clean:
            result["exit_code"] = 3
        elif unknown or not terminal:
            result["exit_code"] = 4
        elif task.status is TaskRunStatus.FAILED:
            result["exit_code"] = 3
        else:
            result["exit_code"] = 0
        if side_effect_unknown:
            result["errors"].append({"code": "external_result_unknown"})
        if model_unknown:
            result["errors"].append(
                {
                    "code": "model_response_abandoned"
                    if terminal
                    else "model_response_unknown"
                }
            )
        if result["intent"] and result["intent"]["slice_result"]:
            reason = result["intent"]["slice_result"]["stop_reason"]
            if reason in {"model_error", "tool_failed", "action_rejected"}:
                result["errors"].append({"code": reason})
                if result["exit_code"] == 0:
                    result["exit_code"] = 3
    return result


class AgentApplication:
    """One process owns one store/controller and one local authorization."""

    def __init__(self, directory, declaration, token, *, settings=None):
        self.directory, self.declaration, self.token = directory, declaration, token
        self.request = IntentRunRequest.parse(declaration["request"])
        self.ids = declaration["ids"]
        self.task_id = declaration["task_run_id"]
        self.store = SqliteControlStore.open(directory / "control.sqlite")
        self.tools, self.imports, self.verifier, self.repository, self.evaluator = (
            evidence_components(directory, self.store)
        )
        if declaration["mode"] == "real":
            from Sandbox.attachment_runtime import ImportableTaskRunSandbox
            from Sandbox.settings import SandboxSettings

            try:
                self.sandbox = ImportableTaskRunSandbox.connect(SandboxSettings())
            except Exception as exc:
                self.store.close()
                raise EnvironmentUnavailable(
                    "docker_unavailable_before_create"
                ) from exc
            self.settings = settings
        else:
            from agent_adapters.simulated_runtime import SimulatedSandbox

            self.sandbox = SimulatedSandbox(directory, declaration["fixture"])
        self.controller = TaskRunController(
            RecordedSandbox(self.sandbox, directory),
            clock=time.time,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.imports,
            file_type_proofs=self.verifier,
            intent_goal_evaluator=self.evaluator,
        )
        self.importer = AttachmentImporter(
            controller=self.controller, recorder=self.imports, sandbox=self.sandbox
        )

    @property
    def state(self):
        return self.controller.control_state

    def remaining_seconds(self):
        return self.state.task_run.deadline_at - time.time()

    def interrupt_if_needed(self):
        if self.state.task_run.status in TERMINAL_STATUSES:
            return True
        if self.remaining_seconds() <= 0:
            self.controller.advance_time(str(uuid4()))
            return True
        if self.token.is_cancelled:
            self.controller.cancel(self.ids["cancel"])
            return True
        return False

    async def execute(self, *, start, authorization, already_attached=False):
        if start:
            # Marker precedes the possible Docker create; never recreate on resume.
            immutable_write(
                self.directory / "start-attempt.json", {"task_run_id": self.task_id}
            )
            self.controller.start(
                self.request.task,
                ModelBackend.DEEPSEEK_OPENAI,
                self.ids["start"],
                source_root=self.declaration["source_root"],
                task_run_id=self.task_id,
            )
        elif not already_attached:
            self.controller.attach(self.task_id)
        if self.interrupt_if_needed():
            return
        await self.importer.import_attachment(
            AttachmentImportRequest(
                self.ids["import"],
                self.task_id,
                self.state.task_run.sandbox_id,
                0,
                self.declaration["source_root"],
                self.request.sandbox_path,
            ),
            self.token,
        )
        if self.interrupt_if_needed():
            return
        if not self.state.turns:
            registration = TurnManager.prepare_turn_registration(
                state=self.state,
                mutation_id=self.ids["registration"],
                turn_target=self.request.description,
                candidates=(IntentCandidate(self.request.description),),
            )
            self.controller.commit_turn_registration(
                registration, self.ids["registration"]
            )
        if self.interrupt_if_needed():
            return
        if self.state.intents[0].status is IntentStatus.PENDING:
            self.controller.commit_turn_operation(
                TurnWorkingCopy(self.state, self.tools).select_next_intent(
                    self.state.turns[0].turn_id
                ),
                self.ids["select"],
            )
        if self.interrupt_if_needed():
            return
        intent_id = self.state.intents[0].intent_id
        auth = ToolAuthorization(
            str(uuid4()),
            self.task_id,
            tuple(authorization["allowed_paths"]),
            tuple(authorization["allowed_tools"]),
        )
        if self.store.load_intent_goal(intent_id) is None:
            self.controller.bind_intent_goal(
                FileTypeIntentGoal(self.task_id, intent_id, self.request.sandbox_path),
                self.ids["import"],
                self.ids["bind"],
                auth,
            )
        if self.interrupt_if_needed():
            return
        if self.declaration["mode"] == "real":
            from agent_adapters.deepseek_model import DeepSeekModelAdapter

            adapter = DeepSeekModelAdapter(self.settings)
        else:
            from agent_adapters.simulated_runtime import SimulatedModel

            adapter = SimulatedModel(
                self.declaration["fixture"], self.request.sandbox_path
            )
        from MCPs.file_tool_adapter import FileMCPPlanner

        coordinator = ModelInvocationCoordinator(
            adapter, self, ErrorEvidence(), audit=self.store
        )
        gateway = ToolGateway(
            controller=self.controller,
            policy=FileToolPolicy(),
            planner=FileMCPPlanner(),
            sandbox=TaskRunToolRuntime(self.sandbox),
            recorder=self.tools,
            clock=time.time,
        )
        loop = IntentToolCallingLoop(
            controller=self.controller,
            model=coordinator,
            gateway=gateway,
            verifier=self.verifier,
            evaluator=self.evaluator,
            evidence=self.tools,
            store=self.store,
            context=self.repository,
        )
        saved = self.directory / "slice-result.json"
        if saved.exists():
            outcome = immutable_read(saved)
        else:
            try:
                outcome = asdict(await loop.run(intent_id, auth, self.token))
            except IntentActionRejected as exc:
                outcome = {
                    "task_run_id": self.task_id,
                    "intent_id": intent_id,
                    "stop_reason": "action_rejected",
                    "error": exc.code,
                    "evaluation": asdict(
                        self.evaluator.evaluate(self.task_id, intent_id)
                    ),
                }
            if outcome["stop_reason"] != "recovery_required":
                immutable_write(saved, outcome)
            else:
                immutable_write(self.directory / "recovery-result.json", outcome)
        if self.interrupt_if_needed() or outcome["stop_reason"] == "recovery_required":
            return
        # Audit/evidence completeness, not Fact count, gates slice closure.
        if any(
            r.completion is None
            for r in self.store.list_model_invocations(self.task_id, intent_id)
        ):
            return
        for execution in self.state.executions:
            completed = self.tools.read_completed(execution.execution_id)
            if completed is None or not completed.record.termination_confirmed:
                return
        self.controller.conclude(
            TaskRunStatus.INCONCLUSIVE,
            self.ids["finish"],
            reason="single_intent_slice_finished",
        )

    def close(self):
        try:
            self.sandbox.close()
        finally:
            self.store.close()


def safe_error(exc):
    if isinstance(exc, EnvironmentUnavailable):
        return {
            "code": "docker_unavailable",
            "phase": "connect",
            "outcome": "not_started",
        }
    if isinstance(exc, TaskRunSandboxError):
        return {
            "code": "sandbox_error",
            "phase": exc.phase,
            "outcome": exc.outcome,
            "task_run_id": exc.task_run_id,
            "sandbox_ids": list(exc.sandbox_ids),
        }
    if isinstance(exc, AttachmentImportRejected):
        return {"code": "attachment_rejected", "reason": exc.code}
    if isinstance(exc, AttachmentImportRecoveryRequired):
        return {"code": "attachment_recovery_required"}
    if isinstance(exc, SandboxCleanupError):
        return {"code": "sandbox_cleanup_unconfirmed"}
    # Raw SDK exceptions can contain request bodies, credentials or local paths.
    return {"code": "operation_outcome_unknown", "category": type(exc).__name__[:80]}
