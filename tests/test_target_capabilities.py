"""Current TCP public contract and real scheduler wiring, with controlled transports."""
import asyncio
import copy
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4
from pathlib import Path
import pytest
from test_http import http_case, Network
from test_mcp_link import Tools
from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_adapters.multi_turn_application import D3Application
from agent_adapters.simulation import parse_request, real_config
from agent_core.target_contracts import TcpAuthorization, canonical_tcp_endpoint
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3RunConfig, d3_payload

ENDPOINT = "tcp://example.test:31337"


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_public_tcp_request_has_exact_separate_grant(http_case):
    raw = copy.deepcopy(http_case.raw)
    raw["task_request"]["target"]["endpoints"] = [{"url": ENDPOINT}]
    grant = TcpAuthorization(str(uuid4()), ENDPOINT)
    raw["authorization"] = {**asdict(grant), "attachment_indices": []}
    raw["d8_config"] = real_config(contract_version=22, http=False)
    request, config = parse_request(raw)
    assert request.task_request["target"]["endpoints"] == [{"url": ENDPOINT}]
    assert config.web_authorization is None and config.tcp_authorization == grant
    assert D3RunConfig.from_dict(json.loads(json.dumps(d3_payload(config)))) == config
    raw["authorization"]["allowed_endpoint"] = "tcp://other.test:31337"
    with pytest.raises(ValueError, match="tcp.endpoint_authorization_mismatch"):
        parse_request(raw)


@pytest.mark.parametrize("url", ["tcp://example.test", "tcp://example.test:0", "tcp://u:p@example.test:80", "tcp://example.test:80/path", "tcp://example.test:80?q=1", "http://example.test:80"])
def test_tcp_endpoint_rejects_ambiguous_or_http_shapes(url):
    with pytest.raises(ValueError):canonical_tcp_endpoint(url)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("with_attachment", [False, True])
def test_tcp_inner_executes_mcp_and_receives_result_in_same_intent(http_case, tmp_path, with_attachment):
    c = http_case
    authorization = TcpAuthorization(c.app.config.authorization.authorization_id, ENDPOINT)
    indices = (0,) if with_attachment else ()
    config = replace(c.app.config, authorization=replace(c.app.config.authorization, attachment_indices=indices),
                     web_authorization=None, http_timeout_seconds=None, tcp_authorization=authorization)
    directory = tmp_path / "tcp-run"
    directory.mkdir()
    app = D3Application(directory, config=config, sandbox=c.sandbox, import_sandbox=c.sandbox,
        workspace_sandbox=c.sandbox, command_sandbox=c.sandbox, outer_model=c.models[0], inner_model=c.models[1],
        review_model=c.models[0], fact_review_model=c.models[0], refinement_model=c.models[0], handoff_model=c.models[0],
        capacity_port=c.app.handoff.capacity_port, clock=c.clock, target_network_adapter=Network())
    file = tmp_path / "challenge.input"
    file.write_bytes(b"authorized attachment")
    task = app.start(TaskRequest.from_dict(dict(goal="Interact with the authorized TCP service.", deadline_seconds=600,
        target=dict(attachments=[dict(kind="local_path", path=file.name)] if with_attachment else [],
                    endpoints=[dict(url=ENDPOINT)]))), tmp_path)
    tools = Tools(task, app.state.task_run.sandbox_id)
    app.mcp = MCPPlanExecutor(app, tools)
    app.mcp.authorize(task)
    seen = []
    original = c.models[1].invoke
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            packet = json.loads(envelope.request.history_context)
            assert packet["tcp_target_policy"]["endpoint"] == ENDPOINT
            assert len(packet["tcp_target_policy"]["attachments"]) == len(indices)
            assert "d20_http_policy" not in packet
            seen.append(json.loads(envelope.request.session_context))
            if len(seen) > 1:c.token.is_cancelled = True
            return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "printf controlled-tcp"})
        return original(envelope, *args)
    c.models[1].invoke = inner
    try:
        result = asyncio.run(app.run(c.token))
        assert len(tools.calls) == 1, result
        assert len(seen) == 2 and seen[0]["session_id"] == seen[1]["session_id"]
        assert any(m.get("role") == "tool" and m.get("content", {}).get("mcp_tool_result", {}).get("stdout") == "test tool output"
                   for m in seen[-1]["messages"])
        assert app.state.task_run.target_network.target_url == ENDPOINT
        binding = app.store.load_d3_turn_binding(app.state.turns[0].turn_id)
        assert binding.kind == "tcp_target" and binding.endpoint == ENDPOINT
        assert not app.state.facts and not app.state.plans
        assert result.task_run_status == "cancelled"
    finally:app.store.close()


def test_workbench_freezes_tcp_grant_without_http_fields(tmp_path,monkeypatch):
    from agent_adapters.workbench import Workbench
    from agent_adapters import deepseek_roles
    from test_workbench_v3 import _challenge,_Process,_NoThread
    workbench=Workbench(tmp_path,protocol_version=3,execution_profile="d8_v22")
    challenge=workbench.create(_challenge(endpoint=ENDPOINT))
    monkeypatch.setattr(deepseek_roles,"settings_from_file",lambda *a:None)
    monkeypatch.setattr("agent_adapters.workbench_runner.subprocess.Popen",lambda *a,**k:_Process())
    monkeypatch.setattr("agent_adapters.workbench_runner.threading.Thread",_NoThread)
    run=workbench.runner.start(challenge["id"],mode="real")
    raw=json.loads((Path(run["directory"])/"request.json").read_text("utf-8"))
    request,config=parse_request(raw)
    assert config.tcp_authorization.allowed_endpoint==ENDPOINT and config.web_authorization is None
    assert "allowed_methods" not in raw["authorization"] and "http_profile" not in raw["d8_config"]


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_public_tcp_cli_runs_actual_assembly_with_controlled_ports(http_case,tmp_path,monkeypatch):
    from agent_adapters import deepseek_roles,docker_profile,simulation,task_cli
    from agent_adapters.run_files import immutable_read
    from test_models import private_fixture
    from test_http import Model,WebSandbox
    c=http_case
    seen=[]
    class PublicModel(Model):
        def __init__(self,role,settings):super().__init__(role);self.settings=settings
        def bind_runtime(self,*args):pass
        def invoke(self,envelope,*args):
            if envelope.output_contract["stage"]=="inner_plan":
                seen.append(json.loads(envelope.request.session_context))
                if len(seen)==2:c.token.is_cancelled=True
                return dict(kind="mcp_call",tool_name="shell.run_bash",arguments={"script":"printf tcp"})
            return super().invoke(envelope,*args)
    class Link(MCPPlanExecutor):
        async def open(self):
            if self.manager is None:
                self.manager=Tools(self.app.state.task_run.task_run_id,self.app.state.task_run.sandbox_id)
            await super().open()
    sandbox=WebSandbox();sandbox._runtime=object()
    monkeypatch.setattr(docker_profile,"connect",lambda *a,**k:(sandbox,sandbox))
    monkeypatch.setattr("Sandbox.target_network.T6TargetNetwork",lambda *a,**k:Network())
    monkeypatch.setattr(deepseek_roles,"D8DeepSeekModel",PublicModel)
    monkeypatch.setattr("agent_adapters.mcp_plan_executor.MCPPlanExecutor",Link)
    monkeypatch.setattr(simulation,"_Token",lambda directory:c.token)
    raw=copy.deepcopy(c.raw)
    raw["task_request"]["target"]["endpoints"]=[{"url":ENDPOINT}]
    raw["authorization"]={**asdict(TcpAuthorization(str(uuid4()),ENDPOINT)),"attachment_indices":[]}
    raw["d8_config"]=real_config(contract_version=22,http=False)
    raw["d8_config"]["intent_execution_policy"]="rolling_v1"
    monkeypatch.setattr(task_cli,"_input",lambda _:raw)
    args=SimpleNamespace(request="controlled",mode="real",state_dir=tmp_path/"public-tcp",
                         events=None,env_file=private_fixture(tmp_path),fixture=None)
    result=task_cli.dispatch_run_task(args)
    assert result["errors"]==[] and not result["recovery"]["required"],result
    assert result["task_run"]["status"]=="cancelled" and result["cleanup_status"]=="confirmed"
    assert len(seen)==2
    assert any(m.get("role")=="tool" for m in seen[-1]["messages"])
    declaration=immutable_read(args.state_dir/result["task_run_id"]/"run.json")
    assert "tcp" in declaration["capabilities"] and "http" not in declaration["capabilities"]
