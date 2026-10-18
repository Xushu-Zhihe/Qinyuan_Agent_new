from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_core.attachment_import import AttachmentImportClaim, AttachmentImportReceipt
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import (
    Execution,
    ExecutionEvidence,
    FactStatus,
    IntentStatus,
    Plan,
    PlanStep,
    PlanValidationStatus,
)
from agent_core.control_store import (
    IdempotencyConflictError,
    PlanValidationRecordDraft,
    StorageError,
)
from agent_core.file_type_fact import FileTypeFactCandidate
from agent_core.file_type_proof import FileTypeVerificationProof
from agent_core.file_type_verifier import FileTypeProofEvidence, FileTypeVerificationError
from agent_core.fake_model_adapters import FakeCodexAdapter
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.intent_goal_evaluator import (
    FileTypeIntentGoalEvaluator,
    IntentGoalEvaluation,
    IntentGoalEvaluationError,
)
from agent_core.modeling import IntentCandidate
from agent_core.modeling import (
    FileTypeCandidateAction,
    InspectFileAction,
    IntentActionContext,
    IntentActionResponse,
    IntentContext,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
    PlanCandidate,
    PlanGenerationResponse,
    StopIntentAction,
)
from agent_core.model_invocation_audit import (
    ModelInvocationBudgetExhausted,
    ModelInvocationCompletion,
    ModelInvocationConflict,
    ModelInvocationRecoveryRequired,
    ModelInvocationStart,
)
from agent_core.runtime import TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.tool_contracts import ToolAuthorization
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Sandbox:
    def create(self, _task_run_id):
        return "fake-sandbox-1"

    def interrupt(self, _sandbox_id):
        pass

    def destroy(self, _sandbox_id):
        pass


class Evidence:
    def command_exists(self, _command_ref):
        return True

    def output_exists(self, _output_ref):
        return True

    def lookup(self, _execution_id):
        return ExecutionEvidence("unused", None, Clock.now)


class Deadline:
    def remaining_seconds(self):
        return 60.0


class ModelErrorEvidence:
    def exists(self, _task_run_id, _evidence_ref):
        return False


class Token:
    is_cancelled = False


class Proofs:
    def __init__(self):
        self.proof = None
        self.calls = 0

    def lookup_file_type_proof(self, execution_id, proof_id):
        self.calls += 1
        assert self.proof is not None
        assert self.proof.execution_id == execution_id
        assert self.proof.proof_id == proof_id
        return FileTypeProofEvidence(
            self.proof,
            ExecutionEvidence(
                self.proof.task_run_id, self.proof.local_path, self.proof.verified_at
            ),
        )


@pytest.fixture
def root() -> Path:
    runtime = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    directory = runtime / f"file-type-commit-{uuid4()}"
    directory.mkdir()
    yield directory
    assert directory.resolve().is_relative_to(runtime.resolve())
    shutil.rmtree(directory)


def test_verified_fact_commits_once_without_finishing_task(root: Path) -> None:
    clock = Clock()
    source = root / "source"
    source.mkdir()
    imports = FileAttachmentImportRecorder(root / "imports")
    evidence = Evidence()
    proofs = Proofs()
    with SqliteControlStore.open(root / "control.sqlite", clock=clock) as store:
        repository = ControlStateRepository(store, evidence, proofs)
        evaluator = FileTypeIntentGoalEvaluator(
            store=store, repository=repository, proofs=proofs
        )
        controller = TaskRunController(
            Sandbox(),
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
            attachment_imports=imports,
            file_type_proofs=proofs,
            intent_goal_evaluator=evaluator,
        )
        controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "find flag",
                    "target": {
                        "attachments": [{"kind": "local_path", "path": "sample.txt"}]
                    },
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(source.resolve(strict=True)),
        )
        state = controller.control_state
        task_id = state.task_run.task_run_id
        registration = TurnManager.prepare_turn_registration(
            state=state,
            mutation_id=str(uuid4()),
            turn_target="inspect attachment",
            candidates=(IntentCandidate("identify file type"),),
        )
        controller.commit_turn_registration(registration, str(uuid4()))

        def commit(operation):
            return controller.commit_turn_operation(operation, str(uuid4()))

        work = lambda: TurnWorkingCopy(controller.control_state, evidence)
        intent_id = commit(work().select_next_intent(registration.turn_id)).value
        assert intent_id is not None
        import_id = str(uuid4())
        path = "/workspace/input/sample.txt"
        claim = AttachmentImportClaim(
            import_id,
            task_id,
            state.task_run.sandbox_id,
            0,
            "sample.txt",
            str(source.resolve(strict=True)),
            path,
            4,
            "a" * 64,
            clock.now,
        )
        imports.begin(claim)
        imports.save_receipt(
            AttachmentImportReceipt(
                claim.import_id,
                claim.task_run_id,
                claim.sandbox_id,
                claim.attachment_index,
                claim.local_path,
                claim.source_root,
                claim.sandbox_path,
                claim.byte_size,
                claim.sha256,
                claim.claimed_at,
                clock.now,
            )
        )
        goal = FileTypeIntentGoal(task_id, intent_id, path)
        controller.bind_intent_goal(
            goal,
            import_id,
            str(uuid4()),
            ToolAuthorization("auth-1", task_id, (path,)),
        )
        assert evaluator.evaluate(task_id, intent_id).outcome == "not_satisfied"

        checkpoint_before_audit = controller.control_state.latest_checkpoint.sequence
        plan_call = ModelInvocationStart(
            str(uuid4()), task_id, intent_id, ModelRequestType.PLAN_GENERATION,
            None, None, clock.now,
        )
        assert store.begin_model_invocation(plan_call) is None
        assert store.begin_model_invocation(plan_call).completion is None
        with pytest.raises(ModelInvocationConflict):
            store.begin_model_invocation(
                ModelInvocationStart(
                    plan_call.model_request_id, task_id, intent_id,
                    ModelRequestType.PLAN_GENERATION, None, None, clock.now + 1,
                )
            )
        plan_completion = ModelInvocationCompletion(
            plan_call.model_request_id, clock.now, "plan",
            PlanGenerationResponse(PlanCandidate((PlanStep("inspect"),))),
        )
        assert store.complete_model_invocation(plan_completion).completion == plan_completion
        assert store.complete_model_invocation(plan_completion).completion == plan_completion
        assert controller.control_state.latest_checkpoint.sequence == checkpoint_before_audit

        plan_id = "plan-1"
        commit(work().register_plan(Plan(plan_id, intent_id, (PlanStep("inspect"),))))
        commit(work().start_plan(plan_id))
        adapter = FakeCodexAdapter(
            [{
                "event": "completed",
                "payload": {
                    "action": {"kind": "inspect_file", "path": path}
                },
            }]
        )
        coordinator = ModelInvocationCoordinator(
            adapter, Deadline(), ModelErrorEvidence(), audit=store, clock=clock
        )
        action_request = ModelRequest(
            str(uuid4()), task_id, registration.turn_id,
            ModelRequestType.INTENT_ACTION, "find flag",
            controller.control_state.task_run.request.target, "inspect attachment",
            (), (), IntentContext(intent_id, "identify file type"),
            action_context=IntentActionContext(intent_id, plan_id, 0, path),
        )
        expected_action = IntentActionResponse(InspectFileAction(path))
        assert coordinator.invoke(action_request, Token()) == expected_action
        assert coordinator.invoke(action_request, Token()) == expected_action
        assert len(adapter.invocations) == 1
        action_call = store.list_model_invocations(task_id, intent_id)[1].start
        action_completion = store.list_model_invocations(task_id, intent_id)[1].completion
        assert store.list_model_invocations(task_id, intent_id) == (
            store.begin_model_invocation(plan_call),
            store.begin_model_invocation(action_call),
        )
        assert action_completion is not None
        with pytest.raises(ModelInvocationConflict):
            store.complete_model_invocation(
                ModelInvocationCompletion(
                    action_call.model_request_id, clock.now + 1,
                    "intent_action", IntentActionResponse(InspectFileAction(path)),
                )
            )
        pending = ModelInvocationStart(
            str(uuid4()), task_id, intent_id, ModelRequestType.INTENT_ACTION,
            plan_id, None, clock.now,
        )
        assert store.begin_model_invocation(pending) is None
        with pytest.raises(ModelInvocationRecoveryRequired):
            store.begin_model_invocation(
                ModelInvocationStart(
                    str(uuid4()), task_id, intent_id,
                    ModelRequestType.INTENT_ACTION, plan_id, None, clock.now,
                )
            )
        store.complete_model_invocation(
            ModelInvocationCompletion(
                pending.model_request_id, clock.now, "intent_action",
                IntentActionResponse(StopIntentAction("test")),
            )
        )
        for _ in range(5):
            call = ModelInvocationStart(
                str(uuid4()), task_id, intent_id,
                ModelRequestType.INTENT_ACTION, plan_id, None, clock.now,
            )
            assert store.begin_model_invocation(call) is None
            store.complete_model_invocation(
                ModelInvocationCompletion(
                    call.model_request_id, clock.now, "intent_action",
                    IntentActionResponse(StopIntentAction("test")),
                )
            )
        execution_id = str(uuid4())
        validation = PlanValidationRecordDraft(
            str(uuid4()),
            plan_id,
            task_id,
            0,
            1,
            PlanValidationStatus.SUCCEEDED,
            None,
            "command authorized",
            "command-1",
        )
        commit(work().append_plan_validation_record(validation))
        commit(
            work().create_execution(
                Execution(execution_id, plan_id, 0, "command-1"),
                plan_validation_id=validation.plan_validation_id,
            )
        )
        commit(work().start_execution(execution_id))
        assert evaluator.evaluate(task_id, intent_id).outcome == "inconclusive"
        commit(work().complete_execution(execution_id=execution_id, output_ref=execution_id))
        commit(work().complete_plan(plan_id))

        candidate_call = ModelInvocationStart(
            str(uuid4()), task_id, intent_id, ModelRequestType.INTENT_ACTION,
            plan_id, None, clock.now,
        )
        assert store.begin_model_invocation(candidate_call) is None
        store.complete_model_invocation(
            ModelInvocationCompletion(
                candidate_call.model_request_id, clock.now, "intent_action",
                IntentActionResponse(
                    FileTypeCandidateAction(path, "ASCII text", execution_id)
                ),
            )
        )
        with pytest.raises(ModelInvocationBudgetExhausted):
            store.begin_model_invocation(
                ModelInvocationStart(
                    str(uuid4()), task_id, intent_id,
                    ModelRequestType.INTENT_ACTION, plan_id, None, clock.now,
                )
            )

        candidate = FileTypeFactCandidate(path, "ASCII text", execution_id)
        proof_id, fact_id, mutation_id = (str(uuid4()) for _ in range(3))
        proofs.proof = FileTypeVerificationProof(
            proof_id,
            task_id,
            intent_id,
            execution_id,
            import_id,
            path,
            "sample.txt",
            "ASCII text",
            clock.now,
        )
        first = controller.commit_verified_file_type_fact(
            intent_id, candidate, proof_id, fact_id, mutation_id
        )
        assert first.value == fact_id
        assert first.commit.replayed is False
        assert controller.control_state.facts[0].status is FactStatus.VERIFIED
        assert controller.control_state.facts[0].verification_ref == proof_id
        assert controller.control_state.task_run.status is TaskRunStatus.RUNNING
        evaluation = evaluator.evaluate(task_id, intent_id)
        assert evaluation.outcome == "satisfied"
        assert evaluation.verified_fact_ids == (fact_id,)
        calls = proofs.calls
        replay = controller.commit_verified_file_type_fact(
            intent_id, candidate, proof_id, fact_id, mutation_id
        )
        assert replay.commit.replayed is True
        assert proofs.calls == calls
        with pytest.raises(FileTypeVerificationError) as rejected:
            controller.commit_verified_file_type_fact(
                intent_id,
                FileTypeFactCandidate(path, "Python source", execution_id),
                proof_id,
                str(uuid4()),
                str(uuid4()),
            )
        assert rejected.value.code == "evidence_unavailable"
        assert len(controller.control_state.facts) == 1

        completion_id = str(uuid4())
        committed_proof = proofs.proof
        proofs.proof = None
        with pytest.raises(StorageError):
            controller.complete_satisfied_intent(intent_id, evaluation, completion_id)
        assert controller.control_state.intents[0].status is IntentStatus.RUNNING
        proofs.proof = committed_proof
        completed = controller.complete_satisfied_intent(
            intent_id, evaluation, completion_id
        )
        assert completed.value == "succeeded"
        assert controller.control_state.intents[0].status is IntentStatus.SUCCEEDED
        assert controller.control_state.task_run.status is TaskRunStatus.RUNNING
        assert controller.control_state.turns[0].status.value == "running"
        calls = proofs.calls
        assert controller.complete_satisfied_intent(
            intent_id, evaluation, completion_id
        ).commit.replayed is True
        assert proofs.calls == calls
        with pytest.raises(IdempotencyConflictError):
            controller.complete_satisfied_intent(
                intent_id,
                IntentGoalEvaluation(
                    task_id, intent_id, "satisfied", (str(uuid4()),),
                    "verified_file_type_fact",
                ),
                completion_id,
            )
        with pytest.raises(IntentGoalEvaluationError) as conflict:
            controller.complete_satisfied_intent(
                intent_id,
                IntentGoalEvaluation(
                    task_id, intent_id, "satisfied", (str(uuid4()),),
                    "verified_file_type_fact",
                ),
                str(uuid4()),
            )
        assert conflict.value.code == "state_conflict"

        proofs.proof = None
        with pytest.raises(StorageError):
            evaluator.evaluate(task_id, intent_id)
