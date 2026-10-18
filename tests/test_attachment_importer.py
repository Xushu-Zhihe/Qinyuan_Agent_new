from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_core.attachment_import import (
    MAX_ATTACHMENT_BYTES,
    AttachmentImportConflict,
    AttachmentImportReadback,
    AttachmentImportRecoveryRequired,
    AttachmentImportRejected,
    AttachmentImportRequest,
)
from agent_core.attachment_importer import AttachmentImporter
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import Plan, PlanStep
from agent_core.intent_goals import FileTypeIntentGoal, IntentGoalBindingError
from agent_core.modeling import IntentCandidate
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.tool_contracts import ToolAuthorization
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy


@dataclass
class Clock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Token:
    is_cancelled: bool = False


class NoExecutionEvidence:
    def command_exists(self, _ref):
        return False

    def output_exists(self, _ref):
        return False

    def lookup(self, _ref):
        return None


class FakeImportSandbox:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.lock = asyncio.Lock()
        self.active = True
        self.write_then_fail = False
        self.after_write = lambda: None

    def create(self, _task_run_id: str) -> str:
        return "fake-sandbox-1"

    def interrupt(self, _sandbox_id: str) -> None:
        self.active = False

    def destroy(self, _sandbox_id: str) -> None:
        self.active = False

    async def create_and_readback(
        self, sandbox_id, path, content, timeout_seconds, cancellation_token
    ) -> AttachmentImportReadback:
        async with self.lock:
            assert sandbox_id == "fake-sandbox-1"
            assert timeout_seconds > 0
            if not self.active or cancellation_token.is_cancelled:
                raise RuntimeError("admission closed")
            self.calls.append("create_and_readback")
            if path in self.files:
                raise FileExistsError(path)
            self.files[path] = content
            self.after_write()
            if self.write_then_fail:
                raise OSError("readback interrupted")
            return self._digest(path)

    async def readback(
        self, sandbox_id, path, timeout_seconds, cancellation_token
    ) -> AttachmentImportReadback:
        async with self.lock:
            assert sandbox_id == "fake-sandbox-1"
            assert timeout_seconds > 0
            if not self.active or cancellation_token.is_cancelled:
                raise RuntimeError("admission closed")
            self.calls.append("readback")
            return self._digest(path)

    def _digest(self, path: str) -> AttachmentImportReadback:
        content = self.files[path]
        return AttachmentImportReadback(
            len(content), hashlib.sha256(content).hexdigest()
        )


class Scenario:
    def __init__(self, root: Path, local_path: str = "sample.txt") -> None:
        self.clock = Clock()
        self.token = Token()
        self.sandbox = FakeImportSandbox()
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=self.clock)
        self.recorder = FileAttachmentImportRecorder(root / "imports")
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=ControlStateRepository(self.store, NoExecutionEvidence()),
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.recorder,
        )
        self.source = root / "source"
        self.source.mkdir()
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "find a flag",
                    "target": {
                        "attachments": [{"kind": "local_path", "path": local_path}]
                    },
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(self.source.resolve(strict=True)),
        )
        self.request = AttachmentImportRequest(
            str(uuid4()),
            self.controller.control_state.task_run.task_run_id,
            self.controller.control_state.task_run.sandbox_id,
            0,
            str(self.source.resolve(strict=True)),
            "/workspace/input/sample.txt",
        )
        self.importer = AttachmentImporter(
            controller=self.controller,
            recorder=self.recorder,
            sandbox=self.sandbox,
            clock=self.clock,
        )

    def close(self) -> None:
        self.controller.cancel(str(uuid4()))
        self.controller.close()
        self.store.close()

    def select_intent(self) -> str:
        registration = TurnManager.prepare_turn_registration(
            state=self.controller.control_state,
            mutation_id=str(uuid4()),
            turn_target="inspect attachment",
            candidates=(IntentCandidate("identify file type"),),
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        operation = TurnWorkingCopy(
            self.controller.control_state, NoExecutionEvidence()
        ).select_next_intent(registration.turn_id)
        selected = self.controller.commit_turn_operation(operation, str(uuid4()))
        assert selected.value is not None
        return selected.value


def test_import_persists_verified_mapping_and_replays_without_source(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"local attachment\n")
        receipt = asyncio.run(
            scenario.importer.import_attachment(scenario.request, scenario.token)
        )
        assert (
            receipt.task_run_id
            == scenario.controller.control_state.task_run.task_run_id
        )
        assert receipt.sha256 == hashlib.sha256(b"local attachment\n").hexdigest()
        assert scenario.sandbox.files[receipt.sandbox_path] == b"local attachment\n"
        assert scenario.recorder.read_receipt(receipt.import_id) == receipt

        (scenario.source / "sample.txt").unlink()
        assert (
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
            == receipt
        )
        assert scenario.sandbox.calls == ["create_and_readback"]
    finally:
        scenario.close()


def test_unknown_write_requires_read_only_recovery(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        scenario.sandbox.write_then_fail = True
        with pytest.raises(AttachmentImportRecoveryRequired):
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert scenario.recorder.read_claim(scenario.request.import_id) is not None
        assert scenario.recorder.read_receipt(scenario.request.import_id) is None
        with pytest.raises(AttachmentImportRecoveryRequired):
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert scenario.sandbox.calls == ["create_and_readback"]

        completed = asyncio.run(
            scenario.importer.recover_import(scenario.request.import_id, scenario.token)
        )
        assert completed.sha256 == hashlib.sha256(b"original").hexdigest()
        assert scenario.sandbox.calls == ["create_and_readback", "readback"]
    finally:
        scenario.close()


def test_recovery_does_not_accept_different_sandbox_content(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        scenario.sandbox.write_then_fail = True
        with pytest.raises(AttachmentImportRecoveryRequired):
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        scenario.sandbox.files[scenario.request.sandbox_path] = b"different"
        with pytest.raises(AttachmentImportRecoveryRequired):
            asyncio.run(
                scenario.importer.recover_import(
                    scenario.request.import_id, scenario.token
                )
            )
        assert scenario.recorder.read_receipt(scenario.request.import_id) is None
    finally:
        scenario.close()


def test_preclaim_path_and_size_rejections_do_not_touch_sandbox(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    traversal = tmp_path / "traversal"
    traversal.mkdir()
    scenario = Scenario(traversal, "../outside.txt")
    try:
        with pytest.raises(AttachmentImportRejected) as rejected:
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert rejected.value.code == "source_path_rejected"
        assert scenario.recorder.read_claim(scenario.request.import_id) is None
        assert scenario.sandbox.calls == []
    finally:
        scenario.close()

    large = tmp_path / "large"
    large.mkdir()
    scenario = Scenario(large)
    try:
        (scenario.source / "sample.txt").write_bytes(b"a" * (MAX_ATTACHMENT_BYTES + 1))
        with pytest.raises(AttachmentImportRejected) as rejected:
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert rejected.value.code == "attachment_too_large"
        assert scenario.recorder.read_claim(scenario.request.import_id) is None
        assert scenario.sandbox.calls == []
    finally:
        scenario.close()


def test_conflicting_import_id_and_claimed_path_stay_immutable(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        asyncio.run(
            scenario.importer.import_attachment(scenario.request, scenario.token)
        )
        with pytest.raises(AttachmentImportConflict):
            asyncio.run(
                scenario.importer.import_attachment(
                    replace(
                        scenario.request, sandbox_path="/workspace/input/other.txt"
                    ),
                    scenario.token,
                )
            )
        with pytest.raises(AttachmentImportConflict):
            asyncio.run(
                scenario.importer.import_attachment(
                    replace(scenario.request, import_id=str(uuid4())), scenario.token
                )
            )
        assert scenario.sandbox.calls == ["create_and_readback"]
    finally:
        scenario.close()


def test_missing_recovery_claim_and_cancelled_preclaim(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        with pytest.raises(AttachmentImportRejected) as missing:
            asyncio.run(scenario.importer.recover_import(str(uuid4()), scenario.token))
        assert missing.value.code == "import_not_found"
        scenario.token.is_cancelled = True
        with pytest.raises(AttachmentImportRejected) as cancelled:
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert cancelled.value.code == "deadline_or_cancelled"
        assert scenario.recorder.read_claim(scenario.request.import_id) is None
    finally:
        scenario.close()


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_interrupt_after_claim_never_signs_success(tmp_path, interrupt: str) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        if interrupt == "cancel":
            scenario.sandbox.after_write = lambda: setattr(
                scenario.token, "is_cancelled", True
            )
        else:
            scenario.sandbox.after_write = lambda: setattr(
                scenario.clock,
                "now",
                scenario.controller.control_state.task_run.deadline_at,
            )
        with pytest.raises(AttachmentImportRecoveryRequired):
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert scenario.recorder.read_claim(scenario.request.import_id) is not None
        assert scenario.recorder.read_receipt(scenario.request.import_id) is None
    finally:
        scenario.close()


def test_symlink_source_is_rejected_before_claim(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        target = scenario.source / "real.txt"
        target.write_bytes(b"original")
        link = scenario.source / "sample.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("this Windows environment cannot create a symlink")
        with pytest.raises(AttachmentImportRejected) as rejected:
            asyncio.run(
                scenario.importer.import_attachment(scenario.request, scenario.token)
            )
        assert rejected.value.code == "source_path_rejected"
        assert scenario.recorder.read_claim(scenario.request.import_id) is None
        assert scenario.sandbox.calls == []
    finally:
        scenario.close()


def test_goal_binding_uses_import_receipt_and_commits_once(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        receipt = asyncio.run(
            scenario.importer.import_attachment(scenario.request, scenario.token)
        )
        intent_id = scenario.select_intent()
        goal = FileTypeIntentGoal(
            scenario.request.task_run_id, intent_id, receipt.sandbox_path
        )
        authorization = ToolAuthorization(
            "test-auth", goal.task_run_id, (goal.path,)
        )
        mutation_id = str(uuid4())
        before = scenario.controller.control_state.latest_checkpoint.sequence
        created = scenario.controller.bind_intent_goal(
            goal, receipt.import_id, mutation_id, authorization
        )
        assert created.disposition == "created"
        assert created.first_commit.event.details["import_id"] == receipt.import_id
        assert scenario.controller.control_state.latest_checkpoint.sequence == before + 1
        assert scenario.store.load_intent_goal(intent_id).goal == goal

        changed_auth = ToolAuthorization(
            "no-access", goal.task_run_id, ("/workspace/input/other.txt",)
        )
        replayed = scenario.controller.bind_intent_goal(
            goal, str(uuid4()), mutation_id, changed_auth
        )
        assert replayed.disposition == "replayed"
        assert replayed.first_commit.event.details["import_id"] == receipt.import_id
        assert scenario.controller.control_state.latest_checkpoint.sequence == before + 1
        with pytest.raises(IntentGoalBindingError) as mutation_conflict:
            scenario.controller.bind_intent_goal(
                replace(goal, path="/workspace/input/other.txt"),
                receipt.import_id,
                mutation_id,
                authorization,
            )
        assert mutation_conflict.value.code == "mutation_conflict"

        with pytest.raises(IntentGoalBindingError) as rejected:
            scenario.controller.bind_intent_goal(
                goal, receipt.import_id, str(uuid4()), changed_auth
            )
        assert rejected.value.code == "goal_path_not_authorized"
        existing = scenario.controller.bind_intent_goal(
            goal, receipt.import_id, str(uuid4()), authorization
        )
        assert existing.disposition == "existing"
        assert scenario.controller.control_state.latest_checkpoint.sequence == before + 1

        plan_operation = TurnWorkingCopy(
            scenario.controller.control_state, NoExecutionEvidence()
        ).register_plan(Plan("inspect-plan", intent_id, (PlanStep("inspect"),)))
        scenario.controller.commit_turn_operation(plan_operation, str(uuid4()))
        with_plan = scenario.controller.control_state.latest_checkpoint.sequence
        assert scenario.controller.bind_intent_goal(
            goal, receipt.import_id, str(uuid4()), authorization
        ).disposition == "existing"
        assert scenario.controller.control_state.latest_checkpoint.sequence == with_plan
        with pytest.raises(IntentGoalBindingError) as conflict:
            scenario.controller.bind_intent_goal(
                replace(goal, path="/workspace/input/other.txt"),
                receipt.import_id,
                str(uuid4()),
                authorization,
            )
        assert conflict.value.code == "goal_conflict"
    finally:
        scenario.close()


def test_goal_binding_rejects_unmapped_path_and_terminal_new_mutation(tmp_path) -> None:
    scenario = Scenario(tmp_path)
    try:
        (scenario.source / "sample.txt").write_bytes(b"original")
        receipt = asyncio.run(
            scenario.importer.import_attachment(scenario.request, scenario.token)
        )
        intent_id = scenario.select_intent()
        goal = FileTypeIntentGoal(
            scenario.request.task_run_id, intent_id, receipt.sandbox_path
        )
        authorization = ToolAuthorization(
            "test-auth", goal.task_run_id, (goal.path,)
        )
        with pytest.raises(IntentGoalBindingError) as unmapped:
            scenario.controller.bind_intent_goal(
                goal, str(uuid4()), str(uuid4()), authorization
            )
        assert unmapped.value.code == "goal_path_not_in_task_scope"
        mutation_id = str(uuid4())
        created = scenario.controller.bind_intent_goal(
            goal, receipt.import_id, mutation_id, authorization
        )
        assert created.disposition == "created"
        scenario.controller.cancel(str(uuid4()))
        assert scenario.controller.bind_intent_goal(
            goal, receipt.import_id, mutation_id, authorization
        ).disposition == "replayed"
        with pytest.raises(IntentGoalBindingError) as terminal:
            scenario.controller.bind_intent_goal(
                goal, receipt.import_id, str(uuid4()), authorization
            )
        assert terminal.value.code == "intent_not_running"
    finally:
        scenario.close()
