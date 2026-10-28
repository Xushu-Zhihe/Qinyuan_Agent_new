"""Native attachment/HTTP adapters with real evidence and controlled IO."""

import asyncio
import hashlib
import json
from dataclasses import replace
from uuid import uuid4
from types import SimpleNamespace
import pytest

from test_stage_application import case
from test_http_experiments import ScriptProcess
from agent_adapters.stage_application import StageApplication
from agent_adapters.web.http_adapter import D7HttpWireResponse
from agent_adapters.web.task_result import _stage_task_result
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.contracts import TaskRequest
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_storage import StageStateError


def test_zero_request_failure_is_visible_to_next_inner_without_becoming_a_fact(case):
    from agent_adapters.deepseek_roles import _stage_input
    request = case.app.state.task_run.request
    app = StageApplication(case.path / 'failed-http', **case.args, http_sandbox=SimpleNamespace())
    app.start(request)
    asyncio.run(case.app.close())
    case.app, case.tools.task_id = app, app.task_id
    def unexpected_send(**kwargs): raise AssertionError('zero-request bridge must never dispatch HTTP')
    app.web.transport_factory = lambda _: SimpleNamespace(send=unexpected_send)
    class FailedBridge:
        exec_id = 'controlled-broken-bridge'
        def __init__(self, *args): pass
        def receive(self): raise ValueError('PRIVATE transport detail')
        def close(self): pass
    app.mcp.experiments.runner_factory = FailedBridge
    actions = []
    def decision(req, raw):
        if req.stage == 'stage_action':
            actions.append(req)
            if len(actions) == 1:
                assert 'authorized-target-channel' in next(t for t in req.tools if t['qualified_name'] == 'http.run_experiment')['tags']
                return dict(kind='mcp_call', tool_name='http.run_experiment', arguments=dict(script="print('controlled')", timeout_seconds=60))
            data = _stage_input(req)
            assert not data['history'] or all('tool_feedback' not in str(i) for i in data['history'])
            result = data['current']['tool_feedback'][0]['result']
            assert result['requests'] == [] and result['failure']['phase'] == 'bridge_ready'
            assert result['failure']['error_type'] == 'ValueError' and 'PRIVATE' not in json.dumps(result)
            assert not req.context.history  # No fabricated HTTP observation or formal Fact.
            return dict(kind='road_resolution', outcome='failed', reason='local bridge failed, target untested', support_refs=[])
        if req.stage == 'stage_completion':
            return dict(kind='incomplete', reason='target untested', support_refs=[])
        return raw
    case.models.inner.transform = decision
    asyncio.run(app.run(case.token))
    assert len(actions) == 2 and not any(i.record_type == 'verified_fact' for i in app.evidence.history(app.task_id))
    assert not any(op.start.category == 'tool_dispatch' for op in app.store.list_d4_operations(app.task_id))


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


@pytest.mark.parametrize('batched', [False, True])
def test_http_experiment_overlaps_public_search_and_keeps_each_request_bound(case, batched):
    import threading
    first_http, release_http = threading.Event(), threading.Event()
    model_entered, release_model, second_http = threading.Event(), threading.Event(), threading.Event()
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
            else:
                second_http.set()
            return D7HttpWireResponse(200, (), b"observed")
    app.web.transport_factory = lambda _: Transport()
    app.mcp.experiments.runner_factory = ScriptProcess
    def decision(req, raw):
        if req.stage == "stage_action":
            actions.append(req)
            if len(actions) == 1:
                experiment = dict(tool_name="http.run_experiment", arguments=dict(
                    script="request('GET','/first')\nrequest('GET','/second')", timeout_seconds=60))
                return (dict(kind='mcp_calls', calls=[experiment, dict(tool_name='web.search', arguments={'query': 'public knowledge gap'})])
                    if batched else dict(kind='mcp_call', **experiment))
            if batched and len(actions) == 2:
                model_entered.set()
                assert release_model.wait(5)
                return dict(kind='wait_for_tools', reason='the target experiment is still running')
            if len(actions) == 2:
                return dict(kind="mcp_call", tool_name="web.search", arguments={"query": "public knowledge gap"})
            return dict(kind="road_resolution", outcome="failed", reason="controlled end", support_refs=[])
        if req.stage == "stage_completion":
            return dict(kind="incomplete", reason="no formal success claimed", support_refs=[])
        return raw
    case.models.inner.transform = decision
    async def exercise():
        pending = asyncio.create_task(app.run(case.token))
        try:
            assert await asyncio.to_thread(first_http.wait, 5)
            if not batched:
                await asyncio.sleep(.06)
                assert len(actions) == 1 and not public_started.is_set()
                release_http.set()
            await asyncio.wait_for(public_started.wait(), 5)
            public_release.set()
            if batched:
                assert await asyncio.to_thread(model_entered.wait, 5)
                release_http.set()
                assert await asyncio.to_thread(second_http.wait, 5)
                from agent_core.stage_storage import StageRecoveryRequired
                # The ordinary state gate stays strict while Inner generates.
                with pytest.raises(StageRecoveryRequired, match="stage.operation_outcome_unknown"):
                    app._check_dispatch()
                resource = "mcp/" + actions[0].context.session.owner_id + "/experiment/" + actions[0].model_request_id + "/1"
                app._check_dispatch(tool_resource=resource)
                with pytest.raises(StageRecoveryRequired):
                    app._check_dispatch(tool_resource=resource.replace(actions[0].model_request_id, str(uuid4())))
                owned = set(app.models.live_operations)
                app.models.live_operations.clear()
                with pytest.raises(StageRecoveryRequired):
                    app._check_dispatch(tool_resource=resource)
                app.models.live_operations.update(owned)
                release_model.set()
            await asyncio.wait_for(pending, 5)
        finally:
            release_http.set(); public_release.set(); release_model.set()
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


@pytest.mark.parametrize("source_error", ["none", "corrected", "repeated"])
def test_stage_attachment_import_is_task_scoped_and_reused_without_copying_again(case, source_error):
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
            if source_error != "none" and (not request.correction_of_model_request_id or source_error == "repeated"):
                sources += [i.ref for i in request.context.history if i.identity == "imported_attachment_not_fact"]
            return {"kind": "fact_candidate", "content": "observed", "reason": "own tool result", "support_refs": sources, "inferred": False}
        return {"kind": "road_resolution", "outcome": "succeeded", "reason": "own result obtained", "support_refs": facts}
    case.models.inner.transform = own_observation
    if source_error == "repeated":
        with pytest.raises(StageStateError, match="stage.model_output_unusable"):
            asyncio.run(app.run(case.token))
        assert len(case.tools.calls) == 1
        assert not any(i.record_type == "verified_fact" for i in app.evidence.history(app.task_id))
        assert not app.store.blocking_d4_operations(app.task_id)
        return
    asyncio.run(app.run(case.token))
    if source_error == "corrected":
        corrections = [e for e in case.models.inner.envelopes if e.request.correction_of_model_request_id]
        assert len(corrections) == 1
        assert corrections[0].correction["reason"] == "output_business_invalid"
        assert corrections[0].correction["issues"][0]["code"] == "stage.fact_source_ineligible"
        bad_id = corrections[0].request.correction_of_model_request_id
        assert not any(r.proposal_operation_id == bad_id for r in case.models.outer.calls)
        assert len(case.tools.calls) == 1
    assert len(copies) == 1
    root_request = next(r for r in case.models.inner.calls if r.stage == "stage_intent")
    assert any(i.identity == "imported_attachment_not_fact" for i in root_request.context.history)
    original = app.evidence.read("imports", app.task_id)
    asyncio.run(app.run(case.token))
    assert len(copies) == 1 and app.evidence.read("imports", app.task_id) == original


def test_http_stage_integrity_error_is_not_downgraded_to_script_failure(case):
    from agent_core.stage_storage import StageStateError
    from agent_adapters.output_evidence import safe_failure_details
    from qinyuan_core import Category
    request = case.app.state.task_run.request
    app = StageApplication(case.path / "domain-error", **case.args, http_sandbox=SimpleNamespace())
    app.start(request)
    asyncio.run(case.app.close())
    case.app, case.tools.task_id = app, app.task_id
    original = StageStateError("stage.evidence_corrupt", code="stage.evidence_corrupt")
    original.category = Category.UNKNOWN
    class Bridge:
        exec_id = "controlled-domain-failure"
        def __init__(self, *args): pass
        def receive(self): raise original
        def close(self): pass
    app.mcp.experiments.runner_factory = Bridge
    app.web.transport_factory = lambda _: SimpleNamespace(send=lambda **kwargs: pytest.fail("HTTP must not dispatch"))
    def decision(req, raw):
        if req.stage == "stage_action":
            return dict(kind="mcp_call", tool_name="http.run_experiment", arguments=dict(script="print('test')", timeout_seconds=60))
        return raw
    case.models.inner.transform = decision
    with pytest.raises(StageStateError) as caught:
        asyncio.run(app.run(case.token))
    assert caught.value is original
    details = safe_failure_details(caught.value, include_chain=True)
    assert "StageStateError" in details["exception_types"] and "stage.evidence_corrupt" in details["error_codes"]
    root = next(r for r in case.models.inner.calls if r.stage == "stage_action")
    assert app.mcp.experiments.result(root.model_request_id) is None
    assert not app.evidence.history(app.task_id)


def test_http_unknown_keeps_worker_diagnostic_and_original_road_operation(case):
    from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
    from agent_adapters.web.http_adapter import HttpCapabilityError
    from Sandbox.task_run_sandbox import TaskRunSandboxError
    from agent_adapters.output_evidence import safe_failure_details
    request = case.app.state.task_run.request
    app = StageApplication(case.path / "http-diagnostic", **case.args, http_sandbox=SimpleNamespace())
    app.start(request)
    asyncio.run(case.app.close())
    case.app, case.tools.task_id = app, app.task_id
    diagnostic = dict(phase="exec_inspect", branch="worker_exit_nonzero", exec_id="controlled-exec", exit_code=1,
        worker_phase="response_headers", worker_code="http.worker.response_headers_failed", error_type="RemoteDisconnected")
    class Transport:
        def send(self, **kwargs):
            failure = TaskRunSandboxError("http_dispatch", "outcome_unknown", app.task_id)
            failure.http_diagnostic = diagnostic
            raise HttpCapabilityError("outcome_unknown") from failure
    app.web.transport_factory = lambda _: Transport()
    app.mcp.experiments.runner_factory = ScriptProcess
    def decision(req, raw):
        if req.stage == "stage_action":
            return dict(kind="mcp_call", tool_name="http.run_experiment", arguments=dict(script="request('GET','/')", timeout_seconds=60))
        return raw
    case.models.inner.transform = decision
    with pytest.raises(ExecutionRecoveryRequired) as caught: asyncio.run(app.run(case.token))
    detail = safe_failure_details(caught.value, include_chain=True)
    assert detail["http_diagnostic"] == diagnostic
    context = detail["operation_context"]
    op = app.store.load_d4_operation(context["operation_id"])
    assert op.completion is None and op.start.resource_ref == context["resource_ref"]
    assert "/" + context["road_id"] + "/experiment/" in context["resource_ref"]
    saved = app.evidence.read("experiment-failure", context["operation_id"])
    assert saved["http_diagnostic"] == diagnostic
    assert not app.evidence.history(app.task_id)
