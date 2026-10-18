from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_core.attachment_import import AttachmentImportClaim, AttachmentImportReceipt
from agent_core.contracts import TaskRequest
from agent_core.domain import ExecutionStatus, IntentStatus, PlanStatus
from agent_core.file_inspection import inspection_arguments
from agent_core.file_type_fact import FileTypeFactCandidate
from agent_core.file_type_verifier import FileTypeVerificationError, FileTypeVerifier
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.runtime import TaskRunStatus
from agent_core.tool_contracts import (
    ExecutionRecord,
    MCPCommand,
    StoredToolRequest,
    ToolAuthorization,
    ToolRequest,
    ToolResult,
)


class Proofs:
    def __init__(self) -> None:
        self.items = {}

    def read(self, proof_id):
        return self.items.get(proof_id)

    def save(self, proof):
        existing = self.items.setdefault(proof.proof_id, proof)
        if existing != proof:
            raise RuntimeError("immutable proof conflict")


class Tools:
    def __init__(self, receipt, result, command) -> None:
        self.receipt = receipt
        self.result = result
        self.command = command

    def read_request_receipt(self, request_id):
        return self.receipt if request_id == self.receipt.request.request_id else None

    def read_completed(self, request_id):
        return self.result if request_id == self.result.record.request_id else None

    def load_command(self, command_ref):
        return self.command


class Imports:
    def __init__(self, claim, receipt) -> None:
        self.claim = claim
        self.receipt = receipt

    def read_claim(self, import_id):
        return self.claim if import_id == self.claim.import_id else None

    def read_receipt(self, import_id):
        return self.receipt if import_id == self.receipt.import_id else None


class Store:
    def __init__(self, binding, state) -> None:
        self.binding = binding
        self.state = state

    def load_intent_goal(self, intent_id):
        return self.binding if intent_id == self.binding.goal.intent_id else None

    def load_control_state_rows(self, task_run_id):
        return self.state if task_run_id == self.state.task_run.task_run_id else None


def scenario():
    task_id, intent_id, execution_id, import_id = (str(uuid4()) for _ in range(4))
    plan_id, sandbox_id, command_ref = "plan-1", "sandbox-1", "command-1"
    path = "/workspace/input/sample.txt"
    request = TaskRequest.from_dict(
        {
            "goal": "find flag",
            "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]},
            "deadline_seconds": 100,
            "model_backend": "codex_cli",
        }
    )
    goal = FileTypeIntentGoal(task_id, intent_id, path)
    binding = SimpleNamespace(
        goal=goal,
        first_commit=SimpleNamespace(
            event=SimpleNamespace(details={"import_id": import_id})
        ),
    )
    task = SimpleNamespace(
        task_run_id=task_id,
        request=request,
        sandbox_id=sandbox_id,
        source_root="C:\\work",
        started_at=1.0,
        deadline_at=100.0,
        status=TaskRunStatus.RUNNING,
    )
    state = SimpleNamespace(
        task_run=task,
        intents=(SimpleNamespace(intent_id=intent_id, status=IntentStatus.RUNNING),),
        plans=(
            SimpleNamespace(
                plan_id=plan_id,
                intent_id=intent_id,
                status=PlanStatus.COMPLETED,
                steps=("inspect",),
            ),
        ),
        executions=(
            SimpleNamespace(
                execution_id=execution_id,
                plan_id=plan_id,
                step_index=0,
                command_ref=command_ref,
                output_ref=execution_id,
                status=ExecutionStatus.SUCCEEDED,
            ),
        ),
    )
    claim = AttachmentImportClaim(
        import_id,
        task_id,
        sandbox_id,
        0,
        "sample.txt",
        "C:\\work",
        path,
        4,
        "a" * 64,
        2.0,
    )
    imported = AttachmentImportReceipt(
        import_id,
        task_id,
        sandbox_id,
        0,
        "sample.txt",
        "C:\\work",
        path,
        4,
        "a" * 64,
        2.0,
        3.0,
    )
    tool_request = ToolRequest(
        execution_id, task_id, plan_id, 0, "file.inspect_file", path
    )
    authorization = ToolAuthorization("auth-1", task_id, (path,))
    receipt = StoredToolRequest(tool_request, authorization, sandbox_id)
    command = MCPCommand("python3", inspection_arguments(path), 30)
    record = ExecutionRecord(
        request_id=execution_id,
        task_run_id=task_id,
        sandbox_id=sandbox_id,
        plan_id=plan_id,
        step_index=0,
        authorization_id=authorization.authorization_id,
        execution_id=execution_id,
        command_ref=command_ref,
        output_ref=execution_id,
        started_at=4.0,
        finished_at=5.0,
        exit_code=0,
        failure_type=None,
        short_reason="",
        stdout="ASCII text\n",
        termination_confirmed=True,
    )
    store = Store(binding, state)
    tools = Tools(receipt, ToolResult(record), command)
    imports = Imports(claim, imported)
    proofs = Proofs()
    verifier = FileTypeVerifier(
        store=store, tools=tools, imports=imports, proofs=proofs, clock=lambda: 6.0
    )
    candidate = FileTypeFactCandidate(path, "ASCII text", execution_id)
    return SimpleNamespace(
        verifier=verifier,
        candidate=candidate,
        intent_id=intent_id,
        proofs=proofs,
        tools=tools,
        imports=imports,
        store=store,
    )


def test_verified_proof_rechecks_sources_and_reuses_orphan_on_retry() -> None:
    s = scenario()
    proof_id = str(uuid4())
    proof = s.verifier.verify_candidate(
        intent_id=s.intent_id, candidate=s.candidate, proof_id=proof_id
    )
    evidence = s.verifier.lookup_file_type_proof(s.candidate.execution_id, proof_id)
    assert evidence.proof == proof
    assert evidence.execution.target_ref == "sample.txt"
    assert (
        s.verifier.verify_candidate(
            intent_id=s.intent_id, candidate=s.candidate, proof_id=proof_id
        )
        == proof
    )


def test_candidate_mismatch_does_not_write_proof() -> None:
    s = scenario()
    with pytest.raises(FileTypeVerificationError) as error:
        s.verifier.verify_candidate(
            intent_id=s.intent_id,
            candidate=replace(s.candidate, observed_type="Python source"),
            proof_id=str(uuid4()),
        )
    assert error.value.code == "candidate_rejected"
    assert s.proofs.items == {}


def test_mismatched_command_or_missing_import_is_unavailable_evidence() -> None:
    s = scenario()
    s.tools.command = MCPCommand("python3", ("-c", "print('wrong')"), 30)
    with pytest.raises(FileTypeVerificationError) as error:
        s.verifier.verify_candidate(
            intent_id=s.intent_id, candidate=s.candidate, proof_id=str(uuid4())
        )
    assert error.value.code == "evidence_unavailable"
    assert s.proofs.items == {}

    s = scenario()
    s.imports.receipt = None
    with pytest.raises(FileTypeVerificationError) as error:
        s.verifier.verify_candidate(
            intent_id=s.intent_id, candidate=s.candidate, proof_id=str(uuid4())
        )
    assert error.value.code == "evidence_unavailable"


def test_cross_intent_execution_and_non_success_are_state_conflicts() -> None:
    s = scenario()
    s.store.state.plans = (
        SimpleNamespace(
            plan_id="plan-1",
            intent_id="another-intent",
            status=PlanStatus.COMPLETED,
            steps=("inspect",),
        ),
    )
    with pytest.raises(FileTypeVerificationError) as error:
        s.verifier.verify_candidate(
            intent_id=s.intent_id, candidate=s.candidate, proof_id=str(uuid4())
        )
    assert error.value.code == "state_conflict"
