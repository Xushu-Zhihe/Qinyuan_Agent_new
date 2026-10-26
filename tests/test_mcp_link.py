"""Focused MCP wiring tests: real scheduler/ledger, controlled transport, no network."""

import asyncio
import json
from contextlib import nullcontext
from threading import RLock
from types import SimpleNamespace

import pytest
from test_output import output_case
from test_chain_multi_turn import ProcessLoss
from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_adapters.sandbox_adapter import D8SandboxAdapter
from agent_core.actions import MCPCall
from agent_core.output_contracts import output_schema, decode_response
from MCPs.Mcp_client.client import MCPExecutionResult
from Sandbox.models import CommandRequest
from Sandbox.settings import SandboxSettings
from Sandbox.task_run_sandbox import TaskRunSandbox


class Tools:
    def __init__(self, task, sandbox, *, status="completed", error=None):
        self.task, self.sandbox, self.status, self.error = task, sandbox, status, error
        self.calls = []
        self.specs = [{"qualified_name": "shell.run_bash", "description": "Controlled test tool",
                       "input_schema": {"type": "object", "properties": {"script": {"type": "string"}},
                                        "required": ["script"], "additionalProperties": False},
                       "tags": ["shell"]}]

    async def start_all(self):
        pass

    async def stop_all(self):
        pass

    def list_registered_tools(self):
        return [SimpleNamespace(qualified_name=s["qualified_name"]) for s in self.specs]

    def bind_agent(self, agent_id, *, allowed_tools):
        assert agent_id == self.task
        assert set(allowed_tools) == {"shell.run_bash"}
        return self

    def list_tool_specs(self):
        return self.specs

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return MCPExecutionResult(self.task, self.sandbox, name, "bash",
                                  ("-lc", arguments["script"]), self.status,
                                  0 if self.status == "completed" else 2,
                                  "test tool output", "", False)


@pytest.fixture
def linked(output_case):
    case = output_case
    case.tools = Tools(case.task, case.app.state.task_run.sandbox_id)
    case.app.mcp = MCPPlanExecutor(case.app, case.tools)
    case.app.mcp.authorize(case.task)
    return case


def run_link(case):
    result = asyncio.run(case.app.run(case.token))
    assert not result.recovery_required, result.stop_reason
    return result


def select_then_inspect(case, count=2):
    seen = []
    def transform(envelope, raw):
        if getattr(envelope.request, "stage", None) == "inner_plan":
            packet = json.loads(envelope.request.history_context)
            assert packet["mcp_tools"][0]["qualified_name"] == "shell.run_bash"
            session = json.loads(envelope.request.session_context)
            seen.append(session)
            if len(seen) <= count:
                return {"kind": "mcp_call", "tool_name": "shell.run_bash",
                        "arguments": {"script": "printf test"}}
            case.token.is_cancelled = True
            return raw
        return raw
    case.models["inner"].transform = transform
    return seen


@pytest.mark.parametrize("output_case", [22], indirect=True)
@pytest.mark.parametrize("status", ["completed", "failed", "rejected"])
def test_inner_receives_results_and_selects_next_tool(linked, status):
    case = linked
    case.tools.status = status
    if status == "rejected":
        from Sandbox.service import SandboxPolicyError
        case.tools.error = SandboxPolicyError("not dispatched")
    seen = select_then_inspect(case)
    asyncio.run(case.app.run(case.token))
    assert len(seen) == 3
    assert len(case.tools.calls) == 2
    assert len({s["session_id"] for s in seen}) == 1
    observations = [m["content"] for m in seen[-1]["messages"] if m["role"] == "tool"]
    feedback = [o["mcp_tool_result"] for o in observations if "mcp_tool_result" in o]
    assert len(feedback) == 2
    assert all(r["status"] == status for r in feedback)
    if status == "rejected":
        assert all(r["code"] == "mcp.command_rejected" for r in feedback)
    else:
        assert all(r["stdout"] == "test tool output" for r in feedback)
    records = case.app.mcp.records()
    assert len(records) == 2 and all(r.completion is not None for r in records)
    assert all(r.start.category == "tool_dispatch" for r in records)
    assert len(case.app.mcp.records(case.app.state.intents[0].intent_id)) == 2
    assert case.app.state.facts == ()
    assert case.app.state.plans == ()


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_unknown_execution_is_not_reissued_on_resume(linked):
    case = linked
    case.tools.error = TimeoutError("not saved to ordinary diagnostics")
    select_then_inspect(case)
    first = asyncio.run(case.app.run(case.token))
    assert first.recovery_required
    assert len(case.tools.calls) == 1, first.stop_reason
    assert case.app.mcp.records()[0].completion is None
    again = asyncio.run(case.app.resume(case.task, case.token))
    assert again.recovery_required
    assert len(case.tools.calls) == 1
    assert case.app.state.facts == ()


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_persisted_result_recovery_does_not_repeat_tool(linked, monkeypatch):
    case = linked
    select_then_inspect(case, count=1)
    original = case.app.store.mark_d3_operation_applied
    def crash(identity, application_ref):
        if application_ref == case.app._id("mcp/" + identity):
            raise ProcessLoss("after tool result persisted")
        return original(identity, application_ref)
    monkeypatch.setattr(case.app.store, "mark_d3_operation_applied", crash)
    with pytest.raises(ProcessLoss):
        run_link(case)
    assert len(case.tools.calls) == 1
    monkeypatch.setattr(case.app.store, "mark_d3_operation_applied", original)
    case.app.mcp.reconcile()
    case.app.mcp.reconcile()
    assert len(case.tools.calls) == 1
    record = case.app.mcp.records()[0]
    assert record.completion is not None
    assert case.app.store.load_d3_operation(
        case.app.mcp.evidence.read("mcp-call", record.start.operation_id)["model_request_id"]
    ).application_ref == record.start.operation_id


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_registry_marker_does_not_enable_old_runs(output_case):
    link = MCPPlanExecutor(output_case.app)
    assert not link.enabled(output_case.task)


def test_task_run_native_argv_keeps_owner_and_admission():
    calls, checked = [], []
    sandbox = TaskRunSandbox.__new__(TaskRunSandbox)
    binding = SimpleNamespace(task_run_id="owner", accepting=True, execution_lock=RLock())
    sandbox._binding = lambda identity: binding if identity == "container" else None
    sandbox._lock = RLock()
    sandbox._settings = SandboxSettings()
    sandbox._verify_active = lambda task, identity: checked.append((task, identity))
    sandbox._admit = lambda owner: nullcontext()
    def execute(identity, request, *, admission):
        with admission():
            calls.append((identity, request.argv))
            return "result"
    sandbox._runtime = SimpleNamespace(execute=execute)
    bridge = D8SandboxAdapter(sandbox, "owner", "container")
    assert bridge.execute("owner", CommandRequest("curl", ("--version",), 5)) == "result"
    assert calls == [("container", ["curl", "--version"])]
    assert checked == [("owner", "container")]
    with pytest.raises(ValueError):
        sandbox.execute("container", CommandRequest("curl", (), 5))
    from Sandbox.service import SandboxNotFoundError, SandboxPolicyError
    with pytest.raises(SandboxNotFoundError):
        bridge.execute("other-task", CommandRequest("curl"))
    with pytest.raises(SandboxPolicyError):
        bridge.execute("owner", CommandRequest("not-registered"))
    assert len(calls) == 1


def test_actual_mcp_manager_uses_bound_task_run_container():
    """Real stdio discovery/call; only the container execution is a test double."""
    from pathlib import Path
    from dataclasses import replace
    from MCPs.Mcp_client.client import MCPManager, MCPRegistryConfig, load_registry
    from Sandbox.models import CommandResult
    captured = []
    class OwnedSandbox:
        _settings = SimpleNamespace(maximum_timeout_seconds=60)
        def execute_mcp(self, sandbox_id, request):
            captured.append((sandbox_id, request))
            return CommandResult(sandbox_id, 0, "ASCII text", "")
        def interrupt(self, sandbox_id):
            raise AssertionError("this successful call must not interrupt")
    async def exercise():
        registry = load_registry(MCPPlanExecutor.registry_path())
        server = registry.servers["file"]
        # Servers import the same project packages, independent of server cwd.
        server = replace(server, env={**server.env, "PYTHONPATH": str(Path.cwd())})
        bridge = D8SandboxAdapter(OwnedSandbox(), "task-owner", "same-container")
        manager = MCPManager(MCPRegistryConfig({"file": server}), bridge, required_servers=("file",))
        try:
            await manager.start_all()
            client = manager.bind_agent("task-owner", allowed_tools=("file.inspect_file",))
            specs = client.list_tool_specs()
            assert specs[0]["input_schema"]["properties"]["path"]
            result = await client.call_tool("file.inspect_file",
                                           {"path": "/workspace/input/sample.txt", "timeout_seconds": 5})
            assert result.status == "completed"
            assert result.agent_id == "task-owner" and result.sandbox_id == "same-container"
            assert result.stdout == "ASCII text"
        finally:
            await manager.stop_all()
    asyncio.run(exercise())
    assert len(captured) == 1
    assert captured[0][0] == "same-container"
    assert captured[0][1].tool == "python3"


@pytest.mark.parametrize("output_case", [22], indirect=True)
@pytest.mark.live
def test_inner_mcp_native_argv_in_real_task_run(output_case):
    """Controlled InnerLLM, real MCP stdio and one real owned Docker container."""
    from dataclasses import replace
    from pathlib import Path
    from agent_adapters import docker_profile
    from agent_adapters.multi_turn_application import D3Application
    from MCPs.Mcp_client.client import MCPManager, MCPRegistryConfig, load_registry
    from agent_core.contracts import TaskRequest
    from agent_core.runtime import TERMINAL_STATUSES
    case = output_case
    case.app.store.close()
    directory = case.directory.parent / "real-run"
    directory.mkdir()
    resources, workspace = docker_profile.connect(docker_profile.sandbox_config(), directory)
    app = D3Application(
        directory, config=case.app.config, sandbox=resources, import_sandbox=workspace,
        workspace_sandbox=workspace, command_sandbox=workspace,
        outer_model=case.models["outer"], inner_model=case.models["inner"],
        review_model=case.models["review"], fact_review_model=case.models["fact"],
        refinement_model=case.models["refinement"], handoff_model=case.models["handoff"],
        capacity_port=case.capacity, clock=case.clock)
    case.app = app
    task = None
    try:
        task = app.start(TaskRequest.from_dict({"goal": "Inspect local MCP wiring",
            "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}),
            directory.parent / "input")
        app.handoff.increase_capacity({"initial_history": 32768, "visible_history": 131072})
        sid = app.state.task_run.sandbox_id
        server = load_registry(MCPPlanExecutor.registry_path()).servers["shell"]
        server = replace(server, env={**server.env, "PYTHONPATH": str(Path.cwd())})
        manager = MCPManager(MCPRegistryConfig({"shell": server}),
                             D8SandboxAdapter(resources, task, sid), required_servers=("shell",))
        app.mcp = MCPPlanExecutor(app, manager)
        app.mcp.authorize(task)
        seen = []
        def choose(envelope, raw):
            if getattr(envelope.request, "stage", None) == "inner_plan":
                seen.append(json.loads(envelope.request.session_context))
                return {"kind": "mcp_call", "tool_name": "shell.run_bash",
                        "arguments": {"script": "printf mcp-link", "timeout_seconds": 5}}
            return raw
        case.models["inner"].transform = choose
        invoke = app.mcp.execute
        async def invoke_and_stop(*args):
            await invoke(*args)
            if len(app.mcp.records()) == 2:
                case.token.is_cancelled = True
        app.mcp.execute = invoke_and_stop
        async def exercise():
            try:
                return await app.run(case.token)
            finally:
                await app.mcp.close()
        result = asyncio.run(exercise())
        assert len(app.mcp.records()) == 2, result.stop_reason
        assert len(seen) == 2
        feedback = [m["content"]["mcp_tool_result"] for m in seen[-1]["messages"]
                    if m["role"] == "tool" and "mcp_tool_result" in m["content"]]
        assert feedback[0]["stdout"] == "mcp-link" and feedback[0]["status"] == "completed"
        assert feedback[0]["command"][0] == "bash"
        assert app.state.facts == () and app.state.plans == ()
        assert app.state.task_run.status in TERMINAL_STATUSES, result.stop_reason
        assert resources.inspect_presence(task, sid) == "absent"
    finally:
        if task is not None and app.state.task_run.status in TERMINAL_STATUSES:
            app.close()
        else:
            app.store.close()
        resources.close()


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_tool_schema_and_review_restrict_before_dispatch(linked, monkeypatch):
    case = linked
    def inspect(envelope, raw):
        if envelope.request.stage != "inner_plan":
            return raw
        call = {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf test"}}
        _, schema = output_schema(envelope.request, contract_version=22)
        assert decode_response(call, schema)[1] == []
        assert decode_response({**call, "tool_name": "unavailable.tool"}, schema)[1]
        assert decode_response({**call, "arguments": {"script": 42}}, schema)[1]
        from dataclasses import replace
        from agent_adapters.deepseek_roles import build_payload, role_config
        role = role_config("inner_action", version=17)
        request = replace(envelope.request, role_config_ref=role.role_config_ref)
        payload = build_payload(replace(envelope, request=request), role)
        assert "one mcp_call" in payload["messages"][0]["content"]
        assert "mcp_tools" in payload["messages"][1]["content"]
        monkeypatch.setattr(case.app.validator, "action_limits", lambda *a: (("complete_intent",), 5))
        return call
    case.models["inner"].transform = inspect
    result = asyncio.run(case.app.run(case.token))
    assert result.recovery_required and "mcp.execution_blocked_by_review" in result.stop_reason
    assert case.tools.calls == [] and case.app.mcp.records() == ()


def test_public_real22_wires_mcp_and_closes_same_loop(tmp_path, monkeypatch):
    """Actual public entry, controlled model/MCP/Docker ports; no external calls."""
    from dataclasses import asdict
    from agent_adapters import deepseek_roles, docker_profile, simulation, task_cli
    from agent_adapters.web.simulated_application import _LocalDigestSandbox
    from agent_adapters.run_files import immutable_read
    from test_docker import new_request
    from test_models import private_fixture
    links, seen, failures = [], [], []
    original_run = simulation.run_simulated_task
    def traced_run(*args, **kwargs):
        try:
            return original_run(*args, **kwargs)
        except BaseException as exc:
            failures.append(exc)
            raise
    monkeypatch.setattr(simulation, "run_simulated_task", traced_run)
    token = SimpleNamespace(is_cancelled=False)
    class Link(MCPPlanExecutor):
        async def open(self):
            loop[:] = [asyncio.get_running_loop()]
            if self.manager is None:
                task = self.app.state.task_run
                self.manager = Tools(task.task_run_id, task.sandbox_id)
                links.append(self)
            await super().open()
        async def close(self):
            if loop:
                assert asyncio.get_running_loop() is loop[0]
            await super().close()
    loop = []
    class Model(simulation.SimulationModel):
        def __init__(self, role, settings):
            super().__init__(role)
            self.settings = settings
        def bind_runtime(self, *args):
            pass
        def invoke(self, envelope, *args):
            if getattr(envelope.request, "stage", None) == "inner_plan":
                seen.append(json.loads(envelope.request.session_context))
                if len(seen) == 2:
                    token.is_cancelled = True
                return {"kind": "mcp_call", "tool_name": "shell.run_bash",
                        "arguments": {"script": "printf test"}}
            return super().invoke(envelope, *args)
    monkeypatch.setattr("agent_adapters.mcp_plan_executor.MCPPlanExecutor", Link)
    monkeypatch.setattr(deepseek_roles, "D8DeepSeekModel", Model)
    monkeypatch.setattr(simulation, "_Token", lambda directory: token)
    def connect(configuration, directory, *, wait_for_ready=False):
        assert wait_for_ready
        sandbox = _LocalDigestSandbox(directory)
        return sandbox, sandbox
    monkeypatch.setattr(docker_profile, "connect", connect)
    raw = new_request(tmp_path, real=True)
    raw["execution_profile"], raw["d8_config"] = "d8_v22", simulation.real_config(contract_version=22)
    for name, role in (("outer", "outer_planning"), ("inner", "inner_action")):
        raw["dual_llm_config"][name + "_role"] = asdict(deepseek_roles.role_config(role, version=17))
    raw = json.loads(json.dumps(raw))
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state",
                           events=None, env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    if failures:
        raise failures[0]
    assert result["errors"] == [] and not result["recovery"]["required"], json.dumps(result, ensure_ascii=True)
    assert result["task_run"]["status"] == "cancelled" and result["cleanup_status"] == "confirmed"
    assert result["verified_facts"] == []
    assert len(links) == 1 and links[0].client is None
    assert len(links[0].manager.calls) == 1 and len(seen) == 2
    assert any("mcp_tool_result" in m["content"] for m in seen[-1]["messages"] if m["role"] == "tool")
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    assert declaration["contract_version"] == 22


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_mcp_standard_schema_accepts_optional_arguments_and_checks_constraints(linked):
    case = linked
    case.tools.specs[0]["input_schema"] = {
        "type": "object", "properties": {"timeout_seconds": {
            "type": "integer", "default": 15, "minimum": 1, "maximum": 60}}}
    checked = []
    def inspect(envelope, raw):
        if getattr(envelope.request, "stage", None) == "inner_plan":
            _, schema = output_schema(envelope.request, contract_version=22)
            call = {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {}}
            assert decode_response(call, schema)[1] == []
            assert decode_response({**call, "arguments": {"timeout_seconds": 0}}, schema)[1]
            assert decode_response({**call, "arguments": {"timeout_seconds": "15"}}, schema)[1]
            checked.append(True)
            case.token.is_cancelled = True
            return call
        return raw
    case.models["inner"].transform = inspect
    asyncio.run(case.app.run(case.token))
    assert checked == [True] and case.tools.calls == []


@pytest.mark.parametrize("output_case", [22], indirect=True)
def test_mcp_decisions_trigger_existing_four_step_progress_review(linked):
    case = linked
    seen = select_then_inspect(case, count=5)
    asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 5 and len(seen) == 6
    intent = case.app.state.intents[0].intent_id
    reviews = case.app.sessions.reviews(intent)
    assert len(reviews) == 1
    assert len(case.app.mcp.records(intent)) == 5
    assert case.app.state.facts == ()


@pytest.mark.parametrize("output_case", [22], indirect=True)
@pytest.mark.parametrize("failure", [None,"known","unknown","budget"])
def test_download_preparation_uses_original_ledger_and_feedback(output_case,monkeypatch,failure):
    from Sandbox.tool_download import ToolDownload,ToolDownloadError,ToolDownloadUnknown
    c=output_case
    class DownloadTools(Tools):
        def bind_agent(self,agent_id,*,allowed_tools):return self
        async def call_tool(self,name,arguments):
            self.calls.append((name,arguments))
            return MCPExecutionResult(self.task,self.sandbox,name,"python3",(),"completed",0,"installed portable tool","",False)
    tools=DownloadTools(c.task,c.app.state.task_run.sandbox_id)
    tools.specs=[{"qualified_name":"download.install_tool","description":"Controlled download",
        "input_schema":{"type":"object","properties":{"name":{"type":"string"},"url":{"type":"string"},"sha256":{"type":"string"}},
                        "required":["name","url","sha256"],"additionalProperties":False},"tags":["download"]}]
    sandbox=SimpleNamespace(_settings=SimpleNamespace(maximum_timeout_seconds=300),_runtime=SimpleNamespace(client=object()))
    tools.sandbox_service=D8SandboxAdapter(sandbox,c.task,tools.sandbox)
    c.app.mcp=MCPPlanExecutor(c.app,tools);c.app.mcp.authorize(c.task)
    preparations=[]
    elapsed=[0.0]
    if failure=="budget":
        monkeypatch.setattr("agent_adapters.mcp_plan_executor.monotonic",lambda:elapsed[0])
    async def prepare(self,arguments,timeout):
        preparations.append(self.operation_id)
        if failure=="budget":elapsed[0]=timeout+1
        if failure=="known":raise ToolDownloadError("download.http_404")
        if failure=="unknown":raise ToolDownloadUnknown("download.worker_stop_unconfirmed")
        result={"wheelhouse":"/tools/wheels/"+self.operation_id,"wheel_hashes":{}}
        self.evidence.save("mcp-download",self.operation_id,{"state":"prepared","prepared":result})
        return result
    monkeypatch.setattr(ToolDownload,"run",prepare)
    seen=[]
    def select(envelope,raw):
        if getattr(envelope.request,"stage",None)=="inner_plan":
            session=json.loads(envelope.request.session_context);seen.append(session)
            if len(seen)>(2 if failure is None else 1):c.token.is_cancelled=True
            return {"kind":"mcp_call","tool_name":"download.install_tool",
                    "arguments":{"name":"sevenzip","url":"https://source.test/portable","sha256":"0"*64}}
        return raw
    c.models["inner"].transform=select
    result=asyncio.run(c.app.run(c.token))
    assert len(preparations)==1
    assert not c.app.state.facts
    if failure=="unknown":
        assert result.recovery_required and result.stop_reason=="download.worker_stop_unconfirmed"
        assert not tools.calls and c.app.mcp.records()[0].completion is None
    else:
        feedback=[m["content"]["mcp_tool_result"] for m in seen[-1]["messages"]
                  if m.get("role")=="tool" and "mcp_tool_result" in m.get("content",{})]
        assert feedback
        assert all(o["status"]==("failed" if failure else "completed") for o in feedback)
        if failure:
            expected="download.stopped_before_install" if failure=="budget" else "download.http_404"
            assert feedback[-1]["code"]==expected and not tools.calls
        else:
            assert len(tools.calls)==2
            assert c.app.output_boundary.evidence.read("mcp-download",c.app.mcp.records()[-1].start.operation_id)["reused_from"]
        assert all(o.completion is not None for o in c.app.mcp.records())
