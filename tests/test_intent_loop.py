from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_tool_gateway import Clock, FileRuntime, Planner, Token

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.file_tool_recorder import FileToolRecorder
from agent_adapters.file_type_proof_recorder import FileTypeProofRecorder
from agent_core.attachment_import import (
    AttachmentImportReadback,
    AttachmentImportRequest,
)
from agent_core.attachment_importer import AttachmentImporter
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import StorageError
from agent_core.controller import (
    CommittedStateReloadError,
    TaskRunController,
    TaskRunControllerRegistry,
)
from agent_core.domain import FailureType, Plan, PlanStep
from agent_core.fake_model_adapters import FakeCodexAdapter
from agent_core.file_type_verifier import FileTypeVerifier
from agent_core.intent_action_application import (
    IntentActionApplication,
    IntentActionApplicationConflict,
    IntentPlanApplication,
)
from agent_core.intent_goal_evaluator import FileTypeIntentGoalEvaluator
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.intent_loop import IntentActionRejected, IntentToolCallingLoop
from agent_core.model_invocation_audit import (
    ModelInvocationCompletion,
    ModelInvocationStart,
)
from agent_core.modeling import (
    IntentActionResponse,
    IntentCandidate,
    ModelInvocationCoordinator,
    ModelRequestType,
    PlanCandidate,
    PlanGenerationResponse,
    StopIntentAction,
)
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.tool_contracts import ToolAuthorization, ToolCallError
from agent_core.tool_gateway import ToolGateway
from agent_core.tool_policy import FileToolPolicy
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from MCPs.file_tool_adapter import FileMCPPlanner
from Sandbox.task_run_sandbox import TaskRunSandbox
from Sandbox.tool_runtime import TaskRunToolRuntime

PATH = "/workspace/input/sample.txt"


class ImportableFakeSandbox(TaskRunSandbox):
    """Fake container files, sharing the actual adapter's execution admission."""

    async def create_and_readback(
        self, sandbox_id, path, content, timeout_seconds, cancellation_token
    ):
        binding = self._binding(sandbox_id)
        with binding.execution_lock, self._admit(binding):
            assert timeout_seconds > 0 and not cancellation_token.is_cancelled
            files = self._runtime.files.setdefault(sandbox_id, {})
            if path in files:
                raise FileExistsError(path)
            files[path] = content
            return self._readback(sandbox_id, path)

    async def readback(self, sandbox_id, path, timeout_seconds, cancellation_token):
        binding = self._binding(sandbox_id)
        with binding.execution_lock, self._admit(binding):
            assert timeout_seconds > 0 and not cancellation_token.is_cancelled
            return self._readback(sandbox_id, path)

    def _readback(self, sandbox_id, path):
        content = self._runtime.files[sandbox_id][path]
        return AttachmentImportReadback(
            len(content), hashlib.sha256(content).hexdigest()
        )


class ErrorEvidence:
    def exists(self, _task_run_id, _evidence_ref):
        return False


class Scenario:
    def __init__(self, root, responses=(), planner=None):
        self.clock = Clock()
        self.token = Token()
        self.runtime = FileRuntime()
        self.sandbox = ImportableFakeSandbox(self.runtime)
        self.tools = FileToolRecorder(root / "tools")
        self.imports = FileAttachmentImportRecorder(root / "imports")
        self.proofs = FileTypeProofRecorder(root / "proofs")
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=self.clock)
        self.verifier = FileTypeVerifier(
            store=self.store,
            tools=self.tools,
            imports=self.imports,
            proofs=self.proofs,
            clock=self.clock,
        )
        self.repository = ControlStateRepository(self.store, self.tools, self.verifier)
        self.evaluator = FileTypeIntentGoalEvaluator(
            store=self.store,
            repository=self.repository,
            proofs=self.verifier,
        )
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.imports,
            file_type_proofs=self.verifier,
            intent_goal_evaluator=self.evaluator,
        )
        source = root / "source"
        source.mkdir()
        (source / "sample.txt").write_bytes(b"local fixture\n")
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "find flag",
                    "target": {
                        "attachments": [{"kind": "local_path", "path": "sample.txt"}],
                    },
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(source.resolve()),
        )
        self.task_id = self.state.task_run.task_run_id
        self.auth = ToolAuthorization("auth", self.task_id, (PATH,))
        self.importer = AttachmentImporter(
            controller=self.controller,
            recorder=self.imports,
            sandbox=self.sandbox,
            clock=self.clock,
        )
        self.import_request = AttachmentImportRequest(
            str(uuid4()),
            self.task_id,
            self.state.task_run.sandbox_id,
            0,
            str(source.resolve()),
            PATH,
        )
        self.adapter = FakeCodexAdapter(list(responses))
        self.coordinator = ModelInvocationCoordinator(
            self.adapter,
            self,
            ErrorEvidence(),
            audit=self.store,
            clock=self.clock,
        )
        self.planner = planner or Planner()
        self.gateway = ToolGateway(
            controller=self.controller,
            policy=FileToolPolicy(),
            planner=self.planner,
            sandbox=TaskRunToolRuntime(self.sandbox),
            recorder=self.tools,
            clock=self.clock,
        )

    @property
    def state(self):
        return self.controller.control_state

    def remaining_seconds(self):
        return self.state.task_run.deadline_at - self.clock()

    def loop(self):
        return IntentToolCallingLoop(
            controller=self.controller,
            model=self.coordinator,
            gateway=self.gateway,
            verifier=self.verifier,
            evaluator=self.evaluator,
            evidence=self.tools,
            store=self.store,
            context=self.repository,
            clock=self.clock,
        )

    async def prepare(self, *, intent_count=1):
        await self.importer.import_attachment(self.import_request, self.token)
        registration = TurnManager.prepare_turn_registration(
            state=self.state,
            mutation_id=str(uuid4()),
            turn_target="inspect attachment",
            candidates=tuple(
                IntentCandidate("identify file type") for _ in range(intent_count)
            ),
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        selected = self.controller.commit_turn_operation(
            TurnWorkingCopy(self.state, self.tools).select_next_intent(
                registration.turn_id
            ),
            str(uuid4()),
        )
        self.intent_id = selected.value
        self.controller.bind_intent_goal(
            FileTypeIntentGoal(self.task_id, self.intent_id, PATH),
            self.import_request.import_id,
            str(uuid4()),
            self.auth,
        )

    def reopen(self):
        path = self.store.path
        self.store.close()
        self.store = SqliteControlStore.open(path, clock=self.clock)
        self.verifier = FileTypeVerifier(
            store=self.store,
            tools=self.tools,
            imports=self.imports,
            proofs=self.proofs,
            clock=self.clock,
        )
        self.repository = ControlStateRepository(self.store, self.tools, self.verifier)
        self.evaluator = FileTypeIntentGoalEvaluator(
            store=self.store,
            repository=self.repository,
            proofs=self.verifier,
        )
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.imports,
            file_type_proofs=self.verifier,
            intent_goal_evaluator=self.evaluator,
        )
        self.controller.attach(self.task_id)
        self.coordinator = ModelInvocationCoordinator(
            self.adapter,
            self,
            ErrorEvidence(),
            audit=self.store,
            clock=self.clock,
        )
        self.gateway = ToolGateway(
            controller=self.controller,
            policy=FileToolPolicy(),
            planner=self.planner,
            sandbox=TaskRunToolRuntime(self.sandbox),
            recorder=self.tools,
            clock=self.clock,
        )

    def close(self):
        self.controller.cancel(str(uuid4()))
        self.controller.close()
        self.store.close()


@pytest.fixture
def root():
    runtime = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    directory = runtime / f"intent-loop-{uuid4()}"
    directory.mkdir(parents=True)
    yield directory
    assert directory.resolve().is_relative_to(runtime.resolve())
    shutil.rmtree(directory)


def test_application_claims_are_immutable_and_do_not_advance_checkpoint(root):
    async def run():
        s = Scenario(root)
        try:
            await s.prepare()
            call = ModelInvocationStart(
                str(uuid4()),
                s.task_id,
                s.intent_id,
                ModelRequestType.PLAN_GENERATION,
                None,
                None,
                s.clock(),
            )
            s.store.begin_model_invocation(call)
            steps = (PlanStep("inspect"),)
            s.store.complete_model_invocation(
                ModelInvocationCompletion(
                    call.model_request_id,
                    s.clock(),
                    "plan",
                    PlanGenerationResponse(PlanCandidate(steps)),
                )
            )
            claim = IntentPlanApplication(
                call.model_request_id,
                s.task_id,
                s.intent_id,
                steps,
                str(uuid4()),
                str(uuid4()),
                str(uuid4()),
            )
            checkpoint = s.state.latest_checkpoint
            s.store.save_plan_application(claim)
            s.store.save_plan_application(claim)
            assert s.store.load_plan_application(call.model_request_id) == claim
            assert (
                s.store.load_control_state_rows(s.task_id).latest_checkpoint
                == checkpoint
            )
            with pytest.raises(IntentActionApplicationConflict):
                s.store.save_plan_application(replace(claim, plan_id=str(uuid4())))
            s.controller.commit_turn_operation(
                TurnWorkingCopy(s.state, s.tools).register_plan(
                    Plan(claim.plan_id, s.intent_id, steps)
                ),
                claim.register_mutation_id,
            )
            s.controller.commit_turn_operation(
                TurnWorkingCopy(s.state, s.tools).start_plan(claim.plan_id),
                claim.start_mutation_id,
            )
            action_call = ModelInvocationStart(
                str(uuid4()),
                s.task_id,
                s.intent_id,
                ModelRequestType.INTENT_ACTION,
                claim.plan_id,
                None,
                s.clock(),
            )
            s.store.begin_model_invocation(action_call)
            action = StopIntentAction("stop")
            s.store.complete_model_invocation(
                ModelInvocationCompletion(
                    action_call.model_request_id,
                    s.clock(),
                    "intent_action",
                    IntentActionResponse(action),
                )
            )
            action_claim = IntentActionApplication(
                action_call.model_request_id,
                s.task_id,
                s.intent_id,
                claim.plan_id,
                action,
            )
            s.store.save_action_application(action_claim)
            with SqliteControlStore.open(s.store.path, clock=s.clock) as reopened:
                assert (
                    reopened.load_action_application(action_call.model_request_id)
                    == action_claim
                )
            with pytest.raises(IntentActionApplicationConflict):
                s.store.save_action_application(
                    replace(action_claim, action=StopIntentAction("other"))
                )
        finally:
            s.close()

    asyncio.run(run())


def plan_response(steps=1):
    return {
        "event": "completed",
        "payload": {"steps": [{"description": "inspect"}] * steps},
    }


def action_response(**action):
    return {"event": "completed", "payload": {"action": action}}


def candidate_response(request, *_):
    observation = request.action_context.latest_tool_observation
    assert observation is not None
    return action_response(
        kind="file_type_candidate",
        path=PATH,
        observed_type="ASCII text",
        execution_id=observation.execution_id,
    )


def successful_responses():
    return [
        plan_response(),
        action_response(kind="inspect_file", path=PATH),
        candidate_response,
    ]


def test_real_mcp_loop_commits_fact_and_only_completes_intent(root):
    async def run():
        s = Scenario(root, successful_responses(), FileMCPPlanner())
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "intent_satisfied"
            assert result.evaluation.outcome == "satisfied"
            assert len(result.verified_fact_ids) == 1
            assert s.state.facts[0].verification_ref is not None
            assert s.state.intents[0].status.value == "succeeded"
            assert s.state.plans[0].status.value == "completed"
            assert s.state.turns[0].status.value == "running"
            assert s.state.task_run.status.value == "running"
            assert s.state.task_run.confirmation_deadline is None
            assert len(s.runtime.execute_calls) == 1
            checkpoint = s.state.latest_checkpoint
            events = list(s.runtime.client.events)
            again = await s.loop().run(s.intent_id, s.auth, s.token)
            assert again == result
            assert s.state.latest_checkpoint == checkpoint
            assert s.runtime.client.events == events
            assert len(s.adapter.invocations) == 3
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize("stdout", ["ASCII text\n\n", "", "Python source\n"])
def test_unsupported_output_does_not_create_fact_and_replay_does_not_call_model(
    root, stdout
):
    async def run():
        s = Scenario(root, successful_responses())
        s.runtime.stdout = stdout
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "candidate_rejected"
            assert result.evaluation.outcome == "not_satisfied"
            assert s.state.facts == ()
            checkpoint = s.state.latest_checkpoint
            assert await s.loop().run(s.intent_id, s.auth, s.token) == result
            assert s.state.latest_checkpoint == checkpoint
            assert len(s.runtime.execute_calls) == 1
            assert len(s.adapter.invocations) == 3
            assert s.state.intents[0].status.value == "running"
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("responses", "code", "dispatches"),
    [
        ([plan_response(2)], "unsupported_plan_steps", 0),
        (
            [
                plan_response(),
                action_response(kind="inspect_file", path="/workspace/input/other.txt"),
            ],
            "path_out_of_scope",
            0,
        ),
        (
            [
                plan_response(),
                action_response(kind="inspect_file", path=PATH),
                action_response(kind="inspect_file", path=PATH),
            ],
            "repeated_inspection",
            1,
        ),
    ],
)
def test_rejected_actions_remain_distinct_and_replayable(
    root, responses, code, dispatches
):
    async def run():
        s = Scenario(root, responses)
        try:
            await s.prepare()
            with pytest.raises(IntentActionRejected) as rejection:
                await s.loop().run(s.intent_id, s.auth, s.token)
            assert rejection.value.code == code
            before = s.state.latest_checkpoint
            calls = len(s.adapter.invocations)
            with pytest.raises(IntentActionRejected) as replay:
                await s.loop().run(s.intent_id, s.auth, s.token)
            assert replay.value.code == code
            assert s.state.latest_checkpoint == before
            assert len(s.adapter.invocations) == calls
            assert len(s.runtime.execute_calls) == dispatches
            assert s.state.intents[0].status.value == "running"
            assert s.state.facts == ()
        finally:
            s.close()

    asyncio.run(run())


def test_model_stop_is_replayed_without_completing_intent(root):
    async def run():
        s = Scenario(
            root,
            [
                plan_response(),
                action_response(kind="stop", short_reason="no further action"),
            ],
        )
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "model_stopped"
            assert result.evaluation.outcome == "not_satisfied"
            assert s.state.intents[0].status.value == "running"
            assert await s.loop().run(s.intent_id, s.auth, s.token) == result
            assert len(s.adapter.invocations) == 2
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["tool", "planning"])
def test_environment_fault_has_one_explicit_retry(root, stage):
    async def run():
        planner = Planner()
        s = Scenario(root, successful_responses(), planner)
        if stage == "tool":
            s.runtime.result_code = 42
        else:
            planner.error = ToolCallError(
                FailureType.ENVIRONMENT_FAULT, "mcp unavailable"
            )
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "tool_failed"
            assert len(s.runtime.execute_calls) == (2 if stage == "tool" else 0)
            assert len(planner.calls) == (1 if stage == "tool" else 2)
            if stage == "tool":
                assert len({e.command_ref for e in s.state.executions}) == 1
            assert len(s.adapter.invocations) == 2
            assert await s.loop().run(s.intent_id, s.auth, s.token) == result
            assert len(s.runtime.execute_calls) == (2 if stage == "tool" else 0)
        finally:
            s.close()

    asyncio.run(run())


def test_retry_can_succeed_without_another_model_decision(root):
    async def run():
        planner = Planner()
        s = Scenario(root, successful_responses(), planner)
        s.runtime.result_code = 42
        # The first returned result changes only after its persisted failure.
        real_call = s.gateway.call

        async def call(request, authorization, token):
            result = await real_call(request, authorization, token)
            s.runtime.result_code = 0
            return result

        s.gateway.call = call
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "intent_satisfied"
            assert len(result.execution_ids) == 2
            assert len(s.adapter.invocations) == 3
            assert len(s.runtime.execute_calls) == 2
            assert len(planner.calls) == 1
        finally:
            s.close()

    asyncio.run(run())


def test_unknown_tool_outcome_never_retries(root):
    async def run():
        s = Scenario(root, successful_responses())
        s.runtime.confirmed = False
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "recovery_required"
            assert result.evaluation.outcome == "inconclusive"
            assert await s.loop().run(s.intent_id, s.auth, s.token) == result
            assert len(s.runtime.execute_calls) == 1
            assert len(s.adapter.invocations) == 2
        finally:
            s.close()

    asyncio.run(run())


def test_receipt_without_finished_marker_requires_recovery(root):
    async def run():
        s = Scenario(root, successful_responses())
        real_finish = s.tools.finish

        def interrupted_finish(result):
            raise StorageError("injected interruption before finish receipt")

        s.tools.finish = interrupted_finish
        try:
            await s.prepare()
            with pytest.raises(StorageError):
                await s.loop().run(s.intent_id, s.auth, s.token)
            s.tools.finish = real_finish
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "recovery_required"
            assert len(s.runtime.execute_calls) == 1
            assert len(s.adapter.invocations) == 2
            assert s.state.facts == ()
        finally:
            s.tools.finish = real_finish
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "operation",
    [
        "register_plan",
        "start_plan",
        "complete_plan",
        "commit_verified_file_type_fact",
        "complete_satisfied_intent",
    ],
)
def test_commit_then_reload_failure_resumes_with_original_ids(root, operation):
    async def run():
        s = Scenario(root, successful_responses())
        try:
            await s.prepare()
            original_reload = s.controller._reload_after_commit
            interrupted = False

            def reload(commit=None):
                nonlocal interrupted
                if (
                    not interrupted
                    and commit
                    and commit.event.details.get("operation") == operation
                ):
                    interrupted = True
                    s.controller._cache_invalid = True
                    raise CommittedStateReloadError("injected reload failure", commit)
                return original_reload(commit)

            s.controller._reload_after_commit = reload
            with pytest.raises(CommittedStateReloadError):
                await s.loop().run(s.intent_id, s.auth, s.token)
            assert interrupted
            s.controller._reload_after_commit = original_reload
            s.controller.reload_state()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "intent_satisfied"
            assert len(s.state.facts) == 1
            assert len(s.state.plans) == 1
            assert len(s.runtime.execute_calls) == 1
            assert len(s.adapter.invocations) == 3
        finally:
            s.close()

    asyncio.run(run())


def test_orphan_proof_is_reused_after_precommit_failure(root):
    async def run():
        s = Scenario(root, successful_responses())
        try:
            await s.prepare()
            original = s.controller.commit_verified_file_type_fact

            def fail(*args, **kwargs):
                raise StorageError("injected precommit error")

            s.controller.commit_verified_file_type_fact = fail
            with pytest.raises(StorageError):
                await s.loop().run(s.intent_id, s.auth, s.token)
            records = s.store.list_model_invocations(s.task_id, s.intent_id)
            claim = s.store.load_action_application(records[-1].start.model_request_id)
            proof = s.proofs.read(claim.proof_id)
            assert proof is not None and s.state.facts == ()
            s.controller.commit_verified_file_type_fact = original
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.verified_fact_ids == (claim.fact_id,)
            assert s.state.facts[0].verification_ref == proof.proof_id
            assert len(s.adapter.invocations) == 3
            assert len(s.runtime.execute_calls) == 1
        finally:
            s.close()

    asyncio.run(run())


def test_historical_completed_call_without_declaration_is_not_applied(root):
    async def run():
        s = Scenario(root, successful_responses())
        try:
            await s.prepare()
            request = s.loop()._request(
                s.intent_id, PATH, ModelRequestType.PLAN_GENERATION, str(uuid4())
            )
            await s.coordinator.invoke_async(request, s.token)
            checkpoint = s.state.latest_checkpoint
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "recovery_required"
            assert s.state.plans == ()
            assert s.state.latest_checkpoint == checkpoint
            assert len(s.adapter.invocations) == 1
            assert s.runtime.execute_calls == []
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_late_model_result_cannot_apply_actions(root, interruption):
    async def run():
        started, release = threading.Event(), threading.Event()

        def blocked(*_):
            started.set()
            assert release.wait(5)
            return plan_response()

        s = Scenario(root, [blocked])
        try:
            await s.prepare()
            running = asyncio.create_task(s.loop().run(s.intent_id, s.auth, s.token))
            assert await asyncio.to_thread(started.wait, 2)
            if interruption == "cancel":
                s.token.is_cancelled = True
            else:
                s.clock.now = s.state.task_run.deadline_at
            result = await asyncio.wait_for(running, 2)
            assert result.stop_reason == "interrupted"
            assert s.state.task_run.status.value == (
                "cancelled" if interruption == "cancel" else "deadline_exceeded"
            )
            release.set()
            await asyncio.sleep(0.05)
            records = s.store.list_model_invocations(s.task_id, s.intent_id)
            assert len(records) == 1 and records[0].completion is None
            assert s.state.plans == () and s.state.facts == ()
            assert s.runtime.execute_calls == []
        finally:
            release.set()
            s.close()

    asyncio.run(run())


def test_schema_5_migration_preserves_model_audit_and_rolls_back_on_failure(
    root, monkeypatch
):
    import agent_core.sqlite_control_store as module

    async def run():
        s = Scenario(root)
        try:
            await s.prepare()
            call = ModelInvocationStart(
                str(uuid4()),
                s.task_id,
                s.intent_id,
                ModelRequestType.PLAN_GENERATION,
                None,
                None,
                s.clock(),
            )
            s.store.begin_model_invocation(call)
            with closing(
                sqlite3.connect(s.store.path, isolation_level=None)
            ) as connection:
                connection.execute("DROP TABLE intent_action_applications")
                connection.execute("DROP TABLE intent_plan_applications")
                connection.execute("DROP TABLE t6_action_resource_ids")
                connection.execute("DROP TABLE t6_action_invocations")
                connection.execute("DROP TABLE turn_model_invocations")
                connection.execute("DELETE FROM schema_versions WHERE version >= 6")
                connection.execute("ALTER TABLE task_runs DROP COLUMN target_network_json")
            with monkeypatch.context() as patch:
                patch.setattr(
                    module, "MIGRATION_6", module.MIGRATION_6 + "INVALID SQL;"
                )
                with pytest.raises(StorageError):
                    SqliteControlStore.open(s.store.path, clock=s.clock)
            with closing(sqlite3.connect(s.store.path)) as connection:
                assert (
                    connection.execute(
                        "SELECT MAX(version) FROM schema_versions"
                    ).fetchone()[0]
                    == 5
                )
                assert (
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'intent_action_applications'"
                    ).fetchall()
                    == []
                )
            with SqliteControlStore.open(s.store.path, clock=s.clock) as reopened:
                records = reopened.list_model_invocations(s.task_id, s.intent_id)
                assert len(records) == 1 and records[0].start == call
                assert (
                    reopened.load_control_state_rows(s.task_id).latest_checkpoint
                    == s.state.latest_checkpoint
                )
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["policy", "parameter", "model_error", "bad_structure"]
)
def test_definitive_failures_do_not_retry_model_or_complete_task(root, failure):
    async def run():
        responses = successful_responses()
        if failure == "model_error":
            responses[0] = {
                "event": "error",
                "code": "authentication_failed",
                "message": "not configured",
            }
        if failure == "bad_structure":
            responses = [
                plan_response(),
                action_response(kind="inspect_file"),
                action_response(kind="inspect_file"),
            ]
        s = Scenario(root, responses)
        try:
            await s.prepare()
            if failure == "policy":
                s.auth = replace(s.auth, allowed_paths=("/workspace/input/other.txt",))
            if failure == "parameter":
                s.runtime.result_code = 40
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            expected = (
                "tool_failed" if failure in {"policy", "parameter"} else "model_error"
            )
            assert result.stop_reason == expected
            assert s.state.task_run.status.value == "running"
            assert s.state.facts == ()
            count = len(s.adapter.invocations)
            assert await s.loop().run(s.intent_id, s.auth, s.token) == result
            assert len(s.adapter.invocations) == count
            assert len(s.runtime.execute_calls) == (1 if failure == "parameter" else 0)
        finally:
            s.close()

    asyncio.run(run())


def test_one_structural_correction_is_audited(root):
    async def run():
        responses = successful_responses()
        responses.insert(1, action_response(kind="inspect_file"))
        s = Scenario(root, responses)
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "intent_satisfied"
            calls = s.store.list_model_invocations(s.task_id, s.intent_id)
            assert len(calls) == 4
            assert calls[1].completion.result_kind == "invalid_structure"
            assert (
                calls[2].start.correction_of_model_request_id
                == calls[1].start.model_request_id
            )
            assert len(s.runtime.execute_calls) == 1
        finally:
            s.close()

    asyncio.run(run())


def test_cross_intent_execution_reference_is_state_conflict(root):
    async def run():
        responses = successful_responses()
        responses[-1] = action_response(
            kind="file_type_candidate",
            path=PATH,
            observed_type="ASCII text",
            execution_id=str(uuid4()),
        )
        s = Scenario(root, responses)
        try:
            await s.prepare()
            from agent_core.file_type_verifier import FileTypeVerificationError

            with pytest.raises(FileTypeVerificationError) as issue:
                await s.loop().run(s.intent_id, s.auth, s.token)
            assert issue.value.code == "state_conflict"
            assert s.state.facts == ()
        finally:
            s.close()

    asyncio.run(run())


def test_cancel_wins_over_replaying_a_rejected_action(root):
    async def run():
        s = Scenario(root, [plan_response(2)])
        try:
            await s.prepare()
            with pytest.raises(IntentActionRejected):
                await s.loop().run(s.intent_id, s.auth, s.token)
            s.token.is_cancelled = True
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "interrupted"
            assert s.state.task_run.status.value == "cancelled"
            assert len(s.adapter.invocations) == 1
        finally:
            s.close()

    asyncio.run(run())


def test_observation_is_bounded_but_verifier_reads_complete_evidence(root):
    async def run():
        def candidate(request, *_):
            observation = request.action_context.latest_tool_observation
            assert len(observation.stdout) + len(observation.stderr) == 4096
            assert observation.context_clipped and not observation.stdout_truncated
            return candidate_response(request)

        s = Scenario(
            root,
            [
                plan_response(),
                action_response(kind="inspect_file", path=PATH),
                candidate,
            ],
        )
        s.runtime.stdout = "x" * 5000
        try:
            await s.prepare()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "candidate_rejected"
            assert s.state.facts == ()
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_interruption_during_tool_wait_preserves_unknown_and_never_redispatches(
    root, interruption
):
    async def run():
        s = Scenario(root, successful_responses())
        s.runtime.wait = True
        try:
            await s.prepare()
            running = asyncio.create_task(s.loop().run(s.intent_id, s.auth, s.token))
            assert await asyncio.to_thread(s.runtime.waiting.wait, 2)
            if interruption == "cancel":
                s.token.is_cancelled = True
            else:
                s.clock.now = s.state.task_run.deadline_at
            result = await asyncio.wait_for(running, 3)
            assert result.stop_reason in {"interrupted", "recovery_required"}
            assert s.state.task_run.status.value == (
                "cancelled" if interruption == "cancel" else "deadline_exceeded"
            )
            assert s.state.facts == ()
            s.runtime.release_wait.set()
            await s.loop().run(s.intent_id, s.auth, s.token)
            assert len(s.runtime.execute_calls) == 1
        finally:
            s.runtime.release_wait.set()
            s.close()

    asyncio.run(run())


def test_reopening_after_tool_commit_uses_persisted_action_and_budget(root):
    async def run():
        s = Scenario(root, successful_responses())
        try:
            await s.prepare()
            original = s.store.save_action_application
            calls = 0

            def save(claim):
                nonlocal calls
                calls += 1
                original(claim)
                if calls == 2:
                    raise StorageError(
                        "injected process interruption after candidate declaration"
                    )

            s.store.save_action_application = save
            with pytest.raises(StorageError):
                await s.loop().run(s.intent_id, s.auth, s.token)
            previous = s.store.list_model_invocations(s.task_id, s.intent_id)
            s.reopen()
            result = await s.loop().run(s.intent_id, s.auth, s.token)
            assert result.stop_reason == "intent_satisfied"
            assert s.store.list_model_invocations(s.task_id, s.intent_id) == previous
            assert len(s.adapter.invocations) == 3
            assert len(s.runtime.execute_calls) == 1
        finally:
            s.close()

    asyncio.run(run())


def test_multiple_intents_have_separate_inspection_and_model_counts(root):
    async def run():
        s = Scenario(root, successful_responses() + successful_responses())
        try:
            await s.prepare(intent_count=2)
            first_intent = s.intent_id
            first = await s.loop().run(first_intent, s.auth, s.token)
            second_id = s.controller.commit_turn_operation(
                TurnWorkingCopy(s.state, s.tools).select_next_intent(
                    s.state.turns[0].turn_id
                ),
                str(uuid4()),
            ).value
            s.controller.bind_intent_goal(
                FileTypeIntentGoal(s.task_id, second_id, PATH),
                s.import_request.import_id,
                str(uuid4()),
                s.auth,
            )
            second = await s.loop().run(second_id, s.auth, s.token)
            assert first.stop_reason == second.stop_reason == "intent_satisfied"
            assert set(first.execution_ids).isdisjoint(second.execution_ids)
            assert set(first.verified_fact_ids).isdisjoint(second.verified_fact_ids)
            assert len(s.store.list_model_invocations(s.task_id, first_intent)) == 3
            assert len(s.store.list_model_invocations(s.task_id, second_id)) == 3
            assert len(s.runtime.execute_calls) == 2
            assert len({call[0] for call in s.runtime.execute_calls}) == 1
            assert s.state.task_run.status.value == "running"
        finally:
            s.close()

    asyncio.run(run())
