"""The v2 CLI application uses the same audited loop as the runner."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from uuid import uuid4

import pytest

from test_tool_gateway import Token

from agent_adapters.attachment_import_journal import T6ImportClaim, T6ImportJournal
from agent_adapters.run_files import immutable_write
from agent_adapters.task_application import TaskApplication, prepare_attachments
from agent_adapters.task_contract import TaskRunRequest, validate_task_fixture
from agent_adapters.task_result import snapshot_task_result
from agent_core.controller import SandboxCleanupError
from Sandbox.command_attachment import T6AttachmentReadback


def request():
    return {
        "protocol_version": 2,
        "task_request": {
            "goal": "identify an observation",
            "target": {"attachments": [{"kind": "local_path", "path": "sample.bin"}]},
            "deadline_seconds": 3600,
            "model_backend": "deepseek_openai",
        },
        "authorization": {"allowed_tools": ["sandbox.command"],
                          "allowed_target_urls": []},
    }


def fixture():
    return {
        "turns": [{"candidates": [{"description": "inspect the sample"}]}],
        "actions": [
            {"kind": "probe", "script": "printf observed",
             "working_directory": "/workspace/work", "timeout_seconds": 30,
             "conditions": [{"kind": "output_contains", "step_index": 0,
                             "stream": "stdout", "literal": "observed"}]},
            {"kind": "complete_intent", "fact_ids": ["$latest_verified_fact"]},
        ],
        "commands": [{
            "exit_code": 0,
            "stdout_base64": base64.b64encode(b"observed\n").decode(),
            "stderr_base64": "",
            "work_files": {"/workspace/work/result": base64.b64encode(b"abc").decode()},
        }],
    }


def declaration(tmp_path):
    raw = request()
    item = tmp_path / "sample.bin"
    item.write_bytes(b"binary sample")
    attachments = prepare_attachments(str(tmp_path.resolve()), TaskRunRequest.parse(raw))
    return {
        "protocol_version": 2,
        "task_run_id": str(uuid4()), "mode": "simulated",
        "request": raw, "source_root": str(tmp_path.resolve()),
        "attachments": [{"name": a.name, "byte_size": a.byte_size,
                         "sha256": a.sha256} for a in attachments],
        "fixture": validate_task_fixture(fixture()),
        "env_file": None, "t6_output_tokens": 8192,
        "ids": {
            "start": str(uuid4()), "cancel": str(uuid4()),
            "authorization": str(uuid4()),
            "imports": [str(uuid4())], "copies": [str(uuid4())],
        },
    }


def test_whole_task_simulation_pauses_without_claiming_goal(tmp_path):
    data = declaration(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
        assert app.state.intents[0].status.value == "succeeded"
        assert len(app.state.facts) == 1
    finally:
        app.close()
    result = snapshot_task_result(directory, data)
    assert result["stop"]["reason"] == "fixture_exhausted"
    assert result["task_run"]["status"] == "running"
    assert result["overall_goal"]["outcome"] == "not_evaluated"
    assert result["exit_code"] == 4
    assert len(result["verified_facts"]) == 1

    resumed = TaskApplication(directory, data, Token())
    try:
        resumed.controller.attach(data["task_run_id"])
        resumed.controller.cancel(data["ids"]["cancel"])
    finally:
        resumed.close()
    after = snapshot_task_result(directory, data)
    assert after["task_run"]["status"] == "cancelled"
    assert after["cleanup"]["status"] == "succeeded"
    assert after["exit_code"] == 130


def test_unverified_complete_action_is_rejected_without_replaying_work(tmp_path, monkeypatch):
    data = declaration(tmp_path)
    data["fixture"] = validate_task_fixture({
        "turns": [{"candidates": [{"description": "inspect the sample"}]}],
        "actions": [
            {"kind": "probe", "script": f"printf observation-{index}",
             "working_directory": "/workspace/work", "timeout_seconds": 30,
             "conditions": []}
            for index in range(5)
        ] + [{"kind": "complete_intent", "fact_ids": [str(uuid4())]}],
        "commands": [
            {"exit_code": 0, "stdout_base64": base64.b64encode(b"observed\n").decode(),
             "stderr_base64": ""}
            for _ in range(5)
        ],
    })
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
        intent = app.state.intents[0]
        actions = app.journal.list_for_intent(data["task_run_id"], intent.intent_id)
        assert len(actions) == 6 and actions[-1].applied_at is None
        assert len(app.state.executions) == 5
        assert all(item.status.value == "succeeded" for item in app.state.executions)
        assert not app.state.facts
        assert intent.status.value == "running"
    finally:
        app.close()
    result = snapshot_task_result(directory, data)
    assert result["stop"]["stop"] == "action_rejected"
    assert result["stop"]["reason"] == "invalid_t6_fact_reference"
    assert result["recovery"] == {"required": False, "reason": None}
    assert result["errors"] == []
    assert result["exit_code"] == 4

    resumed = TaskApplication(directory, data, Token())
    try:
        def unexpected_model(*_args, **_kwargs):
            raise AssertionError("model must not be called for a saved rejected action")

        async def unexpected_command(*_args, **_kwargs):
            raise AssertionError("command must not be replayed for a rejected action")

        monkeypatch.setattr(resumed.fixture_runtime, "invoke", unexpected_model)
        monkeypatch.setattr(resumed.fixture_runtime, "execute", unexpected_command)
        asyncio.run(resumed.execute(start=False))
        assert len(resumed.journal.list_for_intent(
            data["task_run_id"], resumed.state.intents[0].intent_id,
        )) == 6
        assert len(resumed.state.executions) == 5
        assert not resumed.state.facts
    finally:
        resumed.close()


def test_remote_cleanup_requires_both_resource_receipts(tmp_path, monkeypatch):
    data = declaration(tmp_path)
    data["request"]["task_request"]["target"] = {
        "attachments": [], "endpoints": [{"url": "tcp://example.org:31337"}],
    }
    data["request"]["authorization"]["allowed_target_urls"] = [
        "tcp://example.org:31337"
    ]
    data["attachments"] = []
    data["ids"]["imports"] = []
    data["ids"]["copies"] = []
    directory = tmp_path / "remote-run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)

    started = TaskApplication(directory, data, Token())
    try:
        asyncio.run(started.execute(start=True))
    finally:
        started.close()

    failed_cleanup = TaskApplication(directory, data, Token())
    try:
        failed_cleanup.controller.attach(data["task_run_id"])
        def fail_target_destroy(_task_id, _binding):
            raise RuntimeError("simulated network cleanup failure")

        monkeypatch.setattr(failed_cleanup.controller._target_network.network,
                            "destroy", fail_target_destroy)
        with pytest.raises(SandboxCleanupError):
            failed_cleanup.controller.cancel(data["ids"]["cancel"])
    finally:
        failed_cleanup.close()
    partial = snapshot_task_result(directory, data)
    assert partial["task_run"]["status"] == "cancelled"
    assert (directory / "cleanup.json").exists()
    assert partial["cleanup"]["status"] == "not_confirmed"
    assert partial["exit_code"] == 5

    retried = TaskApplication(directory, data, Token())
    try:
        retried.controller.attach(data["task_run_id"])
        retried.controller.cleanup_sandbox()
    finally:
        retried.close()
    complete = snapshot_task_result(directory, data)
    assert complete["cleanup"]["status"] == "succeeded"
    assert complete["cleanup"]["target_network_confirmed"] is True
    assert complete["exit_code"] == 130


def test_simulated_file_condition_uses_declared_work_snapshot(tmp_path):
    data = declaration(tmp_path)
    data["fixture"]["actions"][0]["conditions"] = [{
        "kind": "file_sha256", "step_index": 0,
        "path": "/workspace/work/result",
        "expected_sha256": hashlib.sha256(b"abc").hexdigest(),
    }]
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
        assert len(app.state.facts) == 1
        assert app.state.intents[0].status.value == "succeeded"
    finally:
        app.close()


def test_status_stops_on_damaged_model_action_evidence(tmp_path):
    data = declaration(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
    finally:
        app.close()
    action_file = next((directory / "actions").glob("model-action-*.json"))
    action_file.write_bytes(b"damaged")
    result = snapshot_task_result(directory, data)
    assert result["protocol_version"] == 2
    assert result["recovery"]["reason"] == "state_or_evidence_unreadable"
    assert result["exit_code"] == 5


def test_explicit_import_recovery_reads_sandbox_after_source_disappears(
    tmp_path, monkeypatch,
):
    data = declaration(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
        item = app._attachments()[0]
        claim = T6ImportClaim.for_attachment(
            data["ids"]["imports"][0], data["task_run_id"],
            app.state.task_run.sandbox_id, item,
        )
        journal = T6ImportJournal(directory / "input-imports.sqlite")
        assert journal.begin(claim) is False
        (tmp_path / "sample.bin").unlink()
        calls = []

        class ReadbackOnlyImporter:
            def __init__(self, _sandbox):
                pass

            def readback(self, sandbox_id, attachment, _timeout, _token):
                calls.append((sandbox_id, attachment.name))
                return T6AttachmentReadback(item.byte_size, item.sha256)

        monkeypatch.setattr(
            "agent_adapters.task_application.T6AttachmentImporter", ReadbackOnlyImporter,
        )
        data["mode"] = "real"
        app.recover_imports()
        assert journal.get(claim.import_id)[1] == "verified"
        assert calls == [(app.state.task_run.sandbox_id, item.name)]
    finally:
        app.close()


def test_status_does_not_create_missing_optional_evidence_directory(tmp_path):
    data = declaration(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    immutable_write(directory / "run.json", data)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
    finally:
        app.close()
    candidates = directory / "candidates"
    candidates.rmdir()
    result = snapshot_task_result(directory, data)
    assert result["exit_code"] == 4
    assert not candidates.exists()
