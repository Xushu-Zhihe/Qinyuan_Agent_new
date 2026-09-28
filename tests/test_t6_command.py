"""T6 direct command admission and immutable byte evidence."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.command_recorder import (
    T6CommandRecorder,
    T6CommandRecoveryRequired,
    T6EvidenceError,
)
from agent_core.domain import PlanStatus
from agent_core.runtime import TaskRunStatus
from agent_core.command_contracts import (
    AuthorizedCommand,
    SandboxCommandAuthorization,
    SandboxCommandPolicy,
    SandboxCommandRequest,
    SandboxCommandResult,
    T6ExecutionRecord,
)


def request(**changes):
    original = SandboxCommandRequest(
        str(uuid4()),
        str(uuid4()),
        "plan-1",
        0,
        "printf 'hello'",
        b"input\x00data",
        "/workspace/work",
        120,
    )
    return replace(original, **changes)


def state(item):
    return SimpleNamespace(
        task_run=SimpleNamespace(
            task_run_id=item.task_run_id,
            status=TaskRunStatus.RUNNING,
            sandbox_id="sandbox-1",
            deadline_at=150.8,
        ),
        plans=(
            SimpleNamespace(
                plan_id=item.plan_id, status=PlanStatus.RUNNING, steps=(object(),)
            ),
        ),
    )


def test_policy_uses_smallest_budget_and_explicit_bash_grant():
    item = request()
    authorization = SandboxCommandAuthorization(
        str(uuid4()), item.task_run_id, ("sandbox.command",)
    )
    approved = SandboxCommandPolicy().authorize(
        item, authorization, state(item), now=100.1, step_limit_seconds=60
    )
    assert approved.effective_timeout_seconds == 50
    assert approved.sandbox_id == "sandbox-1"

    denied = replace(authorization, allowed_tools=("file.inspect_file",))
    with pytest.raises(ValueError, match="bash_not_authorized"):
        SandboxCommandPolicy().authorize(
            item, denied, state(item), now=100, step_limit_seconds=60
        )


def test_request_rejects_oversized_script_stdin_and_path_escape():
    with pytest.raises(ValueError, match="invalid_script"):
        request(script="x" * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="invalid_stdin"):
        request(stdin=b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="invalid_working_directory"):
        request(working_directory="/workspace/work/../input")


def test_command_receipt_replays_only_after_immutable_byte_evidence(tmp_path):
    item = request()
    auth = SandboxCommandAuthorization(
        str(uuid4()), item.task_run_id, ("sandbox.command",)
    )
    recorder = T6CommandRecorder(tmp_path)
    assert recorder.begin(item, auth, "sandbox-1") is None
    with pytest.raises(T6CommandRecoveryRequired):
        T6CommandRecorder(tmp_path).begin(item, auth, "sandbox-1")

    approved = AuthorizedCommand(item, auth.authorization_id, "sandbox-1", 30)
    command_ref = str(uuid4())
    recorder.save_command(command_ref, approved, ("bash", "-c", item.script))
    record = T6ExecutionRecord(
        item.request_id,
        item.task_run_id,
        "sandbox-1",
        item.plan_id,
        item.step_index,
        auth.authorization_id,
        command_ref,
        1.0,
        2.0,
        1,
        b"binary\x00output",
        b"stderr\xff",
        False,
        False,
        False,
        True,
        True,
        30,
        "completed",
        True,
    )
    recorder.save_record(record)
    result = SandboxCommandResult(record)
    recorder.finish(result)
    restarted = T6CommandRecorder(tmp_path)
    assert restarted.begin(item, auth, "sandbox-1") == result
    assert restarted.command_exists(command_ref)
    assert restarted.lookup(item.request_id).task_run_id == item.task_run_id
    with pytest.raises(T6EvidenceError, match="request_id_conflict"):
        restarted.begin(replace(item, script="different"), auth, "sandbox-1")
