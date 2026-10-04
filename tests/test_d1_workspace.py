"""D1 workspace admission, conservative recovery, and saved observation replay."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.postcondition_store import T6PostconditionStore
from agent_adapters.workspace_evidence import FileD1WorkspaceRecorder
from agent_core.attachment_import import (
    AttachmentImportReadback,
    AttachmentImportReceipt,
)
from agent_core.attachment_workspace import (
    D1CommandPolicy,
    D1CopyPreparer,
    D1DigestObserver,
    fixed_digest_script,
)
from agent_core.command_contracts import (
    SandboxCommandAuthorization,
    SandboxCommandRequest,
    T6EvidenceError,
)
from agent_core.domain import IntentStatus, PlanStatus
from agent_core.intent_postconditions import (
    FileDigestObservation,
    FileSha256,
    IntentPostconditions,
)
from agent_core.runtime import TaskRunStatus
from agent_core.turn_contracts import (
    D1Authorization,
    D1BudgetExhausted,
    D1OperationRecord,
    D1RecoveryRequired,
    D1RoleConfig,
    D1RunConfig,
    D1TurnBinding,
)


class Ledger:
    def __init__(self, binding, config):
        self.binding, self.config = binding, config
        self.operations = {}

    def load_d1_run_config(self, task_run_id):
        return self.config if task_run_id == self.binding.task_run_id else None

    def load_d1_turn_binding(self, turn_id):
        return self.binding if turn_id == self.binding.turn_id else None

    def load_d1_operation(self, operation_id):
        return self.operations.get(operation_id)

    def begin_d1_operation(self, start):
        existing = self.operations.get(start.operation_id)
        if existing is not None:
            if existing.completion is None:
                raise D1RecoveryRequired("unknown")
            return existing
        if start.category == "first_digest" and any(
            item.start.category == "first_digest" for item in self.operations.values()
        ):
            raise D1BudgetExhausted("first digest")
        self.operations[start.operation_id] = D1OperationRecord(start)

    def complete_d1_operation(self, completion):
        previous = self.operations[completion.operation_id]
        result = replace(previous, completion=completion)
        self.operations[completion.operation_id] = result
        return result

    def mark_d1_operation_applied(self, operation_id, application_ref):
        if self.operations[operation_id].completion.result_kind != "completed":
            raise ValueError("unsuccessful operation cannot be applied")
        result = replace(self.operations[operation_id], application_ref=application_ref)
        self.operations[operation_id] = result
        return result


class Files:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.quiesced = []
        self.status = "present"
        self.error = None
        self.after = lambda: None
        self.delay = 0

    async def hash_file(self, task_run_id, sandbox_id, path, timeout_seconds, token):
        self.calls.append((task_run_id, sandbox_id, path, timeout_seconds))
        if self.delay:
            await asyncio.sleep(self.delay)
        self.after()
        if self.error:
            raise self.error
        return FileDigestObservation(
            self.status,
            hashlib.sha256(self.content).hexdigest()
            if self.status == "present"
            else None,
        )

    async def copy_and_readback(
        self, sandbox_id, source_path, work_path, timeout_seconds, token
    ):
        self.calls.append((sandbox_id, source_path, work_path, timeout_seconds))
        self.after()
        if self.error:
            raise self.error
        return AttachmentImportReadback(
            len(self.content), hashlib.sha256(self.content).hexdigest()
        )

    def quiesce(self, sandbox_id):
        self.quiesced.append(sandbox_id)


class Scenario:
    def __init__(self, root):
        self.clock = lambda: 1_800_000_000.0
        self.token = SimpleNamespace(is_cancelled=False)
        self.content = b"authorized attachment"
        self.intent_id = str(uuid4())
        self.binding = D1TurnBinding(
            binding_id=str(uuid4()),
            task_run_id=str(uuid4()),
            turn_id=str(uuid4()),
            intent_id=self.intent_id,
            attachment_index=0,
            import_id=str(uuid4()),
            expected_sha256=hashlib.sha256(self.content).hexdigest(),
            work_path=f"/workspace/work/intents/{self.intent_id}/sample.txt",
            preparation_ref=str(uuid4()),
            proposal_request_id=str(uuid4()),
        )
        self.config = D1RunConfig(
            D1RoleConfig("outer_planning", "fake", "fake", "outer-v1"),
            D1RoleConfig("inner_action", "fake", "fake", "inner-v1"),
            D1Authorization(str(uuid4())),
        )
        task = SimpleNamespace(
            task_run_id=self.binding.task_run_id,
            sandbox_id="sandbox",
            status=TaskRunStatus.RUNNING,
            started_at=self.clock(),
            deadline_at=self.clock() + 300,
        )
        plan = SimpleNamespace(
            plan_id="plan",
            intent_id=self.intent_id,
            status=PlanStatus.COMPLETED,
            steps=(object(),),
        )
        intent = SimpleNamespace(
            intent_id=self.intent_id,
            turn_id=self.binding.turn_id,
            status=IntentStatus.RUNNING,
        )
        self.state = SimpleNamespace(
            task_run=task,
            plans=(plan,),
            intents=(intent,),
            latest_checkpoint=SimpleNamespace(sequence=4),
        )
        self.controller = SimpleNamespace(
            control_state=self.state, cancel=self.cancel, advance_time=self.deadline
        )
        self.ledger = Ledger(self.binding, self.config)
        self.recorder = FileD1WorkspaceRecorder(root / "workspace")
        self.postconditions = T6PostconditionStore(root / "conditions")
        self.conditions = IntentPostconditions(
            self.binding.task_run_id,
            self.intent_id,
            "plan",
            (FileSha256(0, self.binding.work_path, self.binding.expected_sha256),),
        )
        self.postconditions.bind(self.conditions)
        self.files = Files(self.content)
        self.source = AttachmentImportReceipt(
            self.binding.import_id,
            self.binding.task_run_id,
            "sandbox",
            0,
            "sample.txt",
            str(root.resolve()),
            "/workspace/input/sample.txt",
            len(self.content),
            self.binding.expected_sha256,
            self.clock(),
            self.clock(),
        )
        self.imports = SimpleNamespace(
            read_receipt=lambda reference: (
                self.source if reference == self.source.import_id else None
            )
        )

    def cancel(self, _mutation_id):
        self.state.task_run.status = TaskRunStatus.CANCELLED

    def deadline(self, _mutation_id):
        self.state.task_run.status = TaskRunStatus.DEADLINE_EXCEEDED

    def observer(self, **kwargs):
        return D1DigestObserver(
            self.controller,
            self.ledger,
            self.recorder,
            self.files,
            self.postconditions,
            self.binding,
            clock=self.clock,
            **kwargs,
        )

    def preparer(self):
        return D1CopyPreparer(
            self.controller,
            self.ledger,
            self.recorder,
            self.files,
            self.imports,
            clock=self.clock,
        )

    def prepare_copy(self):
        asyncio.run(self.preparer().prepare(self.binding, self.token))
        self.files.calls.clear()

    def request(self):
        request = SandboxCommandRequest(
            str(uuid4()),
            self.binding.task_run_id,
            "plan",
            0,
            fixed_digest_script(self.binding.work_path),
            b"",
            self.binding.work_path.rsplit("/", 1)[0],
            10,
        )
        from agent_core.turn_contracts import D1OperationStart

        self.ledger.begin_d1_operation(
            D1OperationStart(
                request.request_id,
                self.binding.task_run_id,
                "inner_tool",
                self.clock(),
                4,
                "0" * 64,
                resource_ref="plan",
            )
        )
        return request


def test_copy_provenance_replays_without_second_preparation(tmp_path):
    s = Scenario(tmp_path)
    receipt = asyncio.run(s.preparer().prepare(s.binding, s.token))
    assert receipt.claim.source_path == s.source.sandbox_path
    assert receipt.claim.work_path == s.binding.work_path
    assert receipt.claim.import_id == s.source.import_id
    assert receipt.sha256 == s.source.sha256
    assert (
        s.ledger.operations[s.binding.preparation_ref].start.category
        == "copy_preparation"
    )
    assert s.preparer().read_copy(s.binding) == receipt
    assert asyncio.run(s.preparer().prepare(s.binding, s.token)) == receipt
    assert len(s.files.calls) == 1


def test_unknown_copy_does_not_redispatch(tmp_path):
    s = Scenario(tmp_path)
    s.files.error = OSError("lost copy response")
    with pytest.raises(D1RecoveryRequired):
        asyncio.run(s.preparer().prepare(s.binding, s.token))
    s.files.error = None
    with pytest.raises(D1RecoveryRequired):
        asyncio.run(s.preparer().prepare(s.binding, s.token))
    assert len(s.files.calls) == 1
    assert s.recorder.read_copy(s.binding.preparation_ref) is None


@pytest.mark.parametrize(
    "status,content",
    [
        ("present", b"authorized attachment"),
        ("present", b"different"),
        ("missing", b""),
    ],
)
def test_first_digest_is_persisted_and_replayed_after_file_change(
    tmp_path, status, content
):
    s = Scenario(tmp_path)
    s.prepare_copy()
    s.files.status, s.files.content = status, content
    observed = asyncio.run(s.observer().observe(s.conditions, s.token))
    s.files.content = b"later unrelated bytes"
    assert asyncio.run(s.observer().observe(s.conditions, s.token)) == observed
    assert s.observer().read_file(s.conditions, 0, s.binding.work_path) == observed
    assert len(s.files.calls) == 1
    category = next(
        item
        for item in s.ledger.operations.values()
        if item.start.category == "first_digest"
    )
    assert category.start.category == "first_digest"
    assert category.completion.result_kind == (
        "completed" if content == s.content and status == "present" else "not_satisfied"
    )


def test_unknown_digest_is_not_read_again(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    s.files.error = OSError("lost first read")
    assert asyncio.run(s.observer().observe(s.conditions, s.token)).status == "unknown"
    s.files.error = None
    assert asyncio.run(s.observer().observe(s.conditions, s.token)).status == "unknown"
    assert len(s.files.calls) == 1
    assert (
        next(
            item
            for item in s.ledger.operations.values()
            if item.start.category == "first_digest"
        ).completion
        is None
    )


def test_local_digest_timeout_remains_unknown_and_quiesces(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    s.files.delay = 1
    observed = asyncio.run(
        s.observer(timeout_seconds=0.01).observe(s.conditions, s.token)
    )
    assert observed.status == "unknown"
    assert s.files.quiesced == ["sandbox"]
    assert (
        next(
            item
            for item in s.ledger.operations.values()
            if item.start.category == "first_digest"
        ).completion
        is None
    )


def test_cancelled_late_digest_is_saved_without_supporting_observation(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    s.files.after = lambda: setattr(s.token, "is_cancelled", True)
    assert asyncio.run(s.observer().observe(s.conditions, s.token)).status == "unknown"
    assert s.state.task_run.status is TaskRunStatus.CANCELLED
    assert (
        next(
            item
            for item in s.ledger.operations.values()
            if item.start.category == "first_digest"
        ).completion.result_kind
        == "stale"
    )
    assert s.postconditions.read_file(s.conditions, 0, s.binding.work_path) is None


def test_declared_digest_path_mismatch_has_no_read_or_ledger_effect(tmp_path):
    s = Scenario(tmp_path)
    bad = replace(
        s.conditions, conditions=(FileSha256(0, "/workspace/work/other", "0" * 64),)
    )
    with pytest.raises(T6EvidenceError):
        asyncio.run(s.observer().observe(bad, s.token))
    assert s.files.calls == []
    assert s.ledger.operations == {}


@pytest.mark.parametrize("kind", ["d1-digest", "d1-copy"])
def test_digest_evidence_corruption_prevents_readonly_success(tmp_path, kind):
    s = Scenario(tmp_path)
    s.prepare_copy()
    asyncio.run(s.observer().observe(s.conditions, s.token))
    path = next(s.recorder.directory.glob(kind + "-*.json"))
    path.write_bytes(b"broken historical evidence")
    with pytest.raises(T6EvidenceError):
        s.observer().read_file(s.conditions, 0, s.binding.work_path)
    assert len(s.files.calls) == 1


def test_digest_requires_a_known_copy_before_first_read(tmp_path):
    s = Scenario(tmp_path)
    with pytest.raises(D1RecoveryRequired, match="d1_digest_copy_unavailable"):
        asyncio.run(s.observer().observe(s.conditions, s.token))
    assert s.files.calls == []
    assert s.ledger.operations == {}


def test_postcondition_loader_cannot_trigger_an_unmetered_digest(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    called = []
    observation = s.observer().observe_file(
        s.conditions, 0, s.binding.work_path, lambda: called.append("unmetered")
    )
    assert observation.status == "unknown"
    assert called == []
    assert s.files.calls == []


def test_cancel_before_digest_consumes_no_observation_budget(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    s.token.is_cancelled = True
    with pytest.raises(D1RecoveryRequired, match="d1_deadline_or_cancelled"):
        asyncio.run(s.observer().observe(s.conditions, s.token))
    assert s.state.task_run.status is TaskRunStatus.CANCELLED
    assert s.files.calls == []
    assert all(
        item.start.category != "first_digest" for item in s.ledger.operations.values()
    )


def test_new_plan_cannot_reset_first_observation_budget(tmp_path):
    s = Scenario(tmp_path)
    s.prepare_copy()
    asyncio.run(s.observer().observe(s.conditions, s.token))
    changed = replace(s.conditions, plan_id="new-plan")
    s.postconditions.bind(changed)
    s.state.plans = (
        SimpleNamespace(
            plan_id="new-plan",
            intent_id=s.intent_id,
            status=PlanStatus.COMPLETED,
            steps=(object(),),
        ),
    )
    with pytest.raises(D1BudgetExhausted):
        asyncio.run(s.observer().observe(changed, s.token))
    assert len(s.files.calls) == 1
    assert s.recorder.read_digest_claim(s.observer().operation_id(changed)) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("script", "echo changed"),
        ("stdin", b"untrusted"),
        ("working_directory", "/workspace/work"),
    ],
)
def test_policy_rejects_nonfixed_checks_before_generic_admission(
    tmp_path, field, value
):
    s = Scenario(tmp_path)
    s.state.plans[0].status = PlanStatus.RUNNING
    policy = D1CommandPolicy(s.binding, s.config, s.postconditions, s.ledger)
    authorization = SandboxCommandAuthorization(
        s.config.authorization.authorization_id,
        s.binding.task_run_id,
        ("sandbox.command",),
    )
    with pytest.raises(ValueError, match="d1_fixed_check_mismatch"):
        policy.authorize(
            replace(s.request(), **{field: value}),
            authorization,
            s.state,
            now=s.clock(),
            step_limit_seconds=10,
        )


def test_policy_authorizes_exact_directory_and_preserves_base_budget_checks(tmp_path):
    s = Scenario(tmp_path)
    s.state.plans[0].status = PlanStatus.RUNNING
    policy = D1CommandPolicy(s.binding, s.config, s.postconditions, s.ledger)
    authorization = SandboxCommandAuthorization(
        s.config.authorization.authorization_id,
        s.binding.task_run_id,
        ("sandbox.command",),
    )
    approved = policy.authorize(
        s.request(), authorization, s.state, now=s.clock(), step_limit_seconds=3
    )
    assert approved.effective_timeout_seconds == 3
    s.ledger.config = replace(s.config, authorization=D1Authorization(str(uuid4())))
    with pytest.raises(ValueError, match="d1_authorization_binding_mismatch"):
        policy.authorize(
            s.request(), authorization, s.state, now=s.clock(), step_limit_seconds=3
        )


def test_policy_rejects_new_request_without_persisted_budget_declaration(tmp_path):
    s = Scenario(tmp_path)
    s.state.plans[0].status = PlanStatus.RUNNING
    policy = D1CommandPolicy(s.binding, s.config, s.postconditions, s.ledger)
    authorization = SandboxCommandAuthorization(
        s.config.authorization.authorization_id,
        s.binding.task_run_id,
        ("sandbox.command",),
    )
    request = replace(s.request(), request_id=str(uuid4()))
    with pytest.raises(ValueError, match="d1_tool_dispatch_not_declared"):
        policy.authorize(
            request, authorization, s.state, now=s.clock(), step_limit_seconds=3
        )
