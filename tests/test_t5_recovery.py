from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from test_t5_cli import followup
from test_t5_cli import inputs as cli_inputs

from agent_adapters.application import AgentApplication, Cancellation, snapshot_result
from agent_adapters.run_files import immutable_write
from agent_core.attachment_import import AttachmentImportClaim, AttachmentImportRequest
from agent_core.contracts import ModelBackend
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.model_invocation_audit import ModelInvocationStart
from agent_core.modeling import IntentCandidate, ModelRequestType
from agent_core.tool_contracts import ToolAuthorization
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy


@pytest.fixture
def inputs(tmp_path):
    return cli_inputs.__wrapped__(tmp_path)


def prepared(inputs, *, unknown_import=False):
    root, value, _, _ = inputs
    task_id = str(uuid4())
    directory = root / "runs" / task_id
    directory.mkdir(parents=True)
    declaration = {
        "protocol_version": 1,
        "task_run_id": task_id,
        "mode": "simulated",
        "request": value,
        "fixture": {"stdout": "ASCII text\n"},
        "source_root": str(Path.cwd().resolve()),
        "env_file": None,
        "ids": {
            name: str(uuid4())
            for name in (
                "start",
                "import",
                "registration",
                "select",
                "bind",
                "finish",
                "cancel",
            )
        },
    }
    immutable_write(directory / "run.json", declaration)
    app = AgentApplication(
        directory, declaration, Cancellation(threading.Event(), directory)
    )
    app.controller.start(
        app.request.task,
        ModelBackend.DEEPSEEK_OPENAI,
        app.ids["start"],
        source_root=declaration["source_root"],
        task_run_id=task_id,
    )
    source = (
        Path(declaration["source_root"])
        / value["task_request"]["target"]["attachments"][0]["path"]
    )
    if unknown_import:
        content = source.read_bytes()
        claim = AttachmentImportClaim(
            app.ids["import"],
            task_id,
            app.state.task_run.sandbox_id,
            0,
            value["task_request"]["target"]["attachments"][0]["path"],
            declaration["source_root"],
            app.request.sandbox_path,
            len(content),
            hashlib.sha256(content).hexdigest(),
            app.state.task_run.started_at,
        )
        app.imports.begin(claim)
        asyncio.run(
            app.sandbox.create_and_readback(
                app.state.task_run.sandbox_id,
                app.request.sandbox_path,
                content,
                30,
                app.token,
            )
        )
    else:
        asyncio.run(
            app.importer.import_attachment(
                AttachmentImportRequest(
                    app.ids["import"],
                    task_id,
                    app.state.task_run.sandbox_id,
                    0,
                    declaration["source_root"],
                    app.request.sandbox_path,
                ),
                app.token,
            )
        )
    return app, directory, declaration


def auth_file(inputs):
    path = inputs[0] / "authorization.json"
    path.write_text(json.dumps(inputs[1]["authorization"]))
    return str(path)


def test_resume_requires_new_auth_and_uses_same_run(inputs):
    app, directory, declaration = prepared(inputs)
    app.close()
    result = {"task_run_id": declaration["task_run_id"]}
    rejected = followup(inputs, result, "resume")
    assert rejected["exit_code"] == 2
    resumed = followup(inputs, result, "resume", "--authorization", auth_file(inputs))
    assert resumed["exit_code"] == 0
    assert resumed["task_run_id"] == declaration["task_run_id"]
    assert resumed["intent"]["status"] == "succeeded"
    assert len(list(directory.parent.iterdir())) == 1


def test_unknown_import_never_rewrites_and_requires_explicit_readback(inputs):
    app, _directory, declaration = prepared(inputs, unknown_import=True)
    app.close()
    result = {"task_run_id": declaration["task_run_id"]}
    first = followup(inputs, result, "resume", "--authorization", auth_file(inputs))
    assert first["exit_code"] == 4
    assert first["recovery"]["import_result_unknown"]
    # Source is removed: explicit recovery must only read back the container.
    source = (
        Path(declaration["source_root"])
        / inputs[1]["task_request"]["target"]["attachments"][0]["path"]
    )
    source.unlink()
    verified = followup(inputs, result, "resume", "--verify-import")
    assert not verified["recovery"]["import_result_unknown"]
    assert verified["intent"] is None
    resumed = followup(inputs, result, "resume", "--authorization", auth_file(inputs))
    assert resumed["exit_code"] == 0
    assert resumed["intent"]["status"] == "succeeded"


def test_unknown_model_resume_is_readonly_and_cancel_discards_response(inputs):
    app, directory, declaration = prepared(inputs)
    registration = TurnManager.prepare_turn_registration(
        state=app.state,
        mutation_id=app.ids["registration"],
        turn_target="inspect",
        candidates=(IntentCandidate("file type"),),
    )
    app.controller.commit_turn_registration(registration, app.ids["registration"])
    app.controller.commit_turn_operation(
        TurnWorkingCopy(app.state, app.tools).select_next_intent(
            app.state.turns[0].turn_id
        ),
        app.ids["select"],
    )
    app.controller.bind_intent_goal(
        FileTypeIntentGoal(
            app.task_id, app.state.intents[0].intent_id, app.request.sandbox_path
        ),
        app.ids["import"],
        app.ids["bind"],
        ToolAuthorization(str(uuid4()), app.task_id, (app.request.sandbox_path,)),
    )
    app.store.begin_model_invocation(
        ModelInvocationStart(
            str(uuid4()),
            app.task_id,
            app.state.intents[0].intent_id,
            ModelRequestType.PLAN_GENERATION,
            None,
            None,
            app.state.task_run.started_at,
        )
    )
    app.close()
    before = {p.name for p in directory.iterdir()}
    result = {"task_run_id": declaration["task_run_id"]}
    for _ in range(2):
        unknown = followup(
            inputs, result, "resume", "--authorization", auth_file(inputs)
        )
        assert unknown["exit_code"] == 4
        assert unknown["recovery"]["model_response_unknown"]
    assert {p.name for p in directory.iterdir()} - {
        "control.sqlite-wal",
        "control.sqlite-shm",
    } == before - {"control.sqlite-wal", "control.sqlite-shm"}
    cancelled = followup(inputs, result, "cancel")
    assert cancelled["exit_code"] == 130
    assert cancelled["cleanup"]["status"] == "succeeded"


def test_unknown_import_cancel_retains_unknown_even_after_cleanup(inputs):
    app, _directory, declaration = prepared(inputs, unknown_import=True)
    app.close()
    result = followup(inputs, {"task_run_id": declaration["task_run_id"]}, "cancel")
    assert result["exit_code"] == 4
    assert result["task_run"]["status"] == "cancelled"
    assert result["cleanup"]["status"] == "succeeded"
    assert result["recovery"]["import_result_unknown"]


def test_readonly_missing_run_does_not_create_evidence_directories(inputs):
    directory = inputs[0] / "missing"
    directory.mkdir()
    declaration = {"task_run_id": str(uuid4()), "mode": "simulated"}
    assert snapshot_result(directory, declaration)["exit_code"] == 4
    assert list(directory.iterdir()) == []
