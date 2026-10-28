"""Native attachment/HTTP adapters with real evidence and controlled IO."""

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

from test_stage_application import case
from test_http_experiments import ScriptProcess
from agent_adapters.stage_application import StageApplication
from agent_adapters.web.http_adapter import D7HttpWireResponse
from agent_adapters.web.task_result import _stage_task_result
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.contracts import TaskRequest
from agent_core.sqlite_control_store import SqliteControlStore


def test_stage_http_script_preserves_per_request_evidence_and_read_only_projection(case):
    request = case.app.state.task_run.request
    app = StageApplication(case.path / "http-task", **case.args, http_sandbox=SimpleNamespace(), goal_validation="human")
    app.start(request)
    asyncio.run(case.app.close())
    case.app = app
    case.tools.task_id = app.task_id
    sends = []
    class Transport:
        def send(self, **kwargs):
            sends.append(kwargs)
            return D7HttpWireResponse(200, (("Content-Type", "text/plain"),), b"observed")
    app.web.transport_factory = lambda _: Transport()
    app.mcp.experiments.runner_factory = ScriptProcess
    execute = app.mcp.execute
    async def execute_once(identity, owner, call, token):
        await execute(identity, owner, call, token)
        if call.tool_name == "http.run_experiment":
            report = app.mcp.experiments.result(identity)
            assert report["status"] == "completed", report["code"]
    app.mcp.execute = execute_once
    previous = case.models.inner.transform
    def experiment(request, raw):
        if request.stage == "stage_continuation":
            source = next(i.ref for i in reversed(request.context.history) if i.identity == "tool_observation_not_fact")
            return {**raw, "candidate": "observed", "source_ref": source}
        if request.stage == "stage_action" and raw["kind"] == "mcp_call":
            return {"kind": "mcp_call", "tool_name": "http.run_experiment", "arguments": {
                "script": "response=request('GET','/first')\nassert response['body']==b'observed'\nrequest('GET','/second')\nprint('observed')", "timeout_seconds": 60}}
        return previous(request, raw)
    case.models.inner.transform = experiment
    result = asyncio.run(app.run(case.token))
    assert result.turn.achievement and len(sends) == 2
    assert not case.tools.calls
    history = app.evidence.history(app.task_id)
    assert len([i for i in history if i.identity == "tool_observation_not_fact"]) == 2
    assert not app.store.blocking_d4_operations(app.task_id)
    assert len(app.store.list_d7_answer_candidates(app.task_id)) == 1
    app.mcp.reconcile()
    assert len(sends) == 2
    with SqliteControlStore.open_readonly(app.directory / "control.sqlite") as store:
        view = _stage_task_result(app.directory, dict(task_run_id=app.task_id, mode="simulated", goal_validation="human"), store, case.args["config"])
        assert len(view["tool_operations"]) == 2 and len(view["verified_facts"]) == 1


def test_http_experiment_overlaps_public_search_and_keeps_each_request_bound(case):
    import threading
    first_http, release_http = threading.Event(), threading.Event()
    public_started, public_release = asyncio.Event(), asyncio.Event()
    request = case.app.state.task_run.request
    app = StageApplication(case.path / "http-overlap", **case.args, http_sandbox=SimpleNamespace())
    async def public(call, timeout, token, target):
        public_started.set()
        await public_release.wait()
        return dict(status="completed", ok=True, identity="public_reference_not_target_observation", content="reference")
    app.mcp.public_web = SimpleNamespace(binding={"backend": "controlled"}, validate=lambda call: None, call=public)
    app.start(request)
    asyncio.run(case.app.close())
    case.app, case.tools.task_id = app, app.task_id
    sends, actions = [], []
    class Transport:
        def send(self, **kwargs):
            sends.append(kwargs)
            if len(sends) == 1:
                first_http.set()
                assert release_http.wait(5)
            return D7HttpWireResponse(200, (), b"observed")
    app.web.transport_factory = lambda _: Transport()
    app.mcp.experiments.runner_factory = ScriptProcess
    def decision(req, raw):
        if req.stage == "stage_action":
            actions.append(req)
            if len(actions) == 1:
                return dict(kind="mcp_call", tool_name="http.run_experiment", arguments=dict(
                    script="request('GET','/first')\nrequest('GET','/second')", timeout_seconds=60))
            if len(actions) == 2:
                assert {t["qualified_name"] for t in req.tools} == {"web.search", "web.fetch"}
                return dict(kind="mcp_call", tool_name="web.search", arguments={"query": "public knowledge gap"})
            if len(actions) == 3:
                return dict(kind="wait_for_tools", reason="the target experiment is still running")
            return dict(kind="road_resolution", outcome="failed", reason="controlled end", support_refs=[])
        if req.stage == "stage_completion":
            return dict(kind="incomplete", reason="no formal success claimed", support_refs=[])
        return raw
    case.models.inner.transform = decision
    async def exercise():
        pending = asyncio.create_task(app.run(case.token))
        try:
            await asyncio.wait_for(public_started.wait(), 5)
            assert await asyncio.to_thread(first_http.wait, 5)
            public_release.set()
            while len(actions) < 3: await asyncio.sleep(0.01)
            release_http.set()
            await asyncio.wait_for(pending, 5)
        finally:
            release_http.set(); public_release.set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 12))
    assert len(sends) == 2 and not case.tools.calls
    report = app.mcp.experiments.result(actions[0].model_request_id)
    assert report["status"] == "completed" and len(report["requests"]) == 2
    records = app.mcp.experiments.records(actions[0].model_request_id)
    assert all(op.completion is not None for op in records)
    assert any(op.start.base_checkpoint_sequence > actions[0].base_checkpoint_sequence for op in records)
    app.mcp.reconcile()
    assert len(sends) == 2 and not app.store.blocking_d4_operations(app.task_id)


def test_stage_attachment_import_is_task_scoped_and_reused_without_copying_again(case):
    source = case.path / "source"
    source.mkdir()
    (source / "sample.bin").write_bytes(b"owned original attachment")
    config = replace(case.args["config"], authorization=replace(case.args["config"].authorization, attachment_indices=(0,)),
        web_authorization=None, http_timeout_seconds=None)
    copies = []
    class ImportSandbox:
        async def create_and_readback(self, sandbox_id, path, content, timeout_seconds, cancellation_token):
            copies.append((sandbox_id, path, content))
            return AttachmentImportReadback(len(content), hashlib.sha256(content).hexdigest())
        async def readback(self, sandbox_id, path, timeout_seconds, cancellation_token):
            content = copies[0][2]
            return AttachmentImportReadback(len(content), hashlib.sha256(content).hexdigest())
    args = {**case.args, "config": config, "import_sandbox": ImportSandbox(), "target_network_adapter": None}
    app = StageApplication(case.path / "attachment-task", **args)
    request = TaskRequest.from_dict({"goal": "inspect the attachment", "deadline_seconds": 3600,
        "target": {"attachments": [{"kind": "local_path", "path": "sample.bin"}]}})
    app.start(request, source_root=str(source.resolve()))
    asyncio.run(case.app.close())
    case.app = app
    case.tools.task_id = app.task_id
    previous = case.models.inner.transform
    def own_observation(request, raw):
        if request.stage != "stage_action":
            return previous(request, raw)
        owner = request.context.session.owner_id
        sources = [i.ref for i in request.context.history if i.identity == "tool_observation_not_fact" and i.intent_id == owner]
        facts = [i.ref for i in request.context.history if i.record_type == "verified_fact" and i.intent_id == owner]
        if not sources:
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        if not facts:
            return {"kind": "fact_candidate", "content": "observed", "reason": "own tool result", "support_refs": sources, "inferred": False}
        return {"kind": "road_resolution", "outcome": "succeeded", "reason": "own result obtained", "support_refs": facts}
    case.models.inner.transform = own_observation
    asyncio.run(app.run(case.token))
    assert len(copies) == 1
    root_request = next(r for r in case.models.inner.calls if r.stage == "stage_intent")
    assert any(i.identity == "imported_attachment_not_fact" for i in root_request.context.history)
    original = app.evidence.read("imports", app.task_id)
    asyncio.run(app.run(case.token))
    assert len(copies) == 1 and app.evidence.read("imports", app.task_id) == original
