"""D9-2 offline transport contracts: no provider or Docker calls."""

import io
import json
import time
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import deepseek_model, deepseek_roles, task_cli, cli
from agent_adapters.simulation import SimulationModel, real_config, parse_request
from agent_adapters.run_files import immutable_read
from agent_core.handoff_contracts import D8Capacity
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.output_contracts import D8OutputRequest, D8ModelResponse, output_schema, decode_response
from test_public_simulation import request


@dataclass
class BusinessRequest:
    stage: str
    role_config_ref: str
    session_context: str = ""
    history_context: str = "current-strategic-view"
    capacity: D8Capacity = D8Capacity()


def envelope(stage="inner_plan", *, session="", correction=None):
    role = deepseek_roles.role_config("inner_action" if stage in deepseek_roles.INNER_STAGES else "outer_planning")
    business = BusinessRequest(stage, role.role_config_ref, session)
    mapped, schema = output_schema(business)
    return D8OutputRequest(business, {"version": 1, "stage": mapped, "schema": schema},
                           "Return exactly one JSON object", correction), role


def private_fixture(tmp_path):
    path = tmp_path / "private.env"
    path.write_text("QINYUAN_LLM_BACKEND=openai\nOPENAI_BASE_URL=https://api.deepseek.com\n"
                    "OPENAI_API_KEY=offline-private-key\nQINYUAN_LLM_TIMEOUT_SECONDS=180\n")
    return path


def real_request(tmp_path):
    raw = request(tmp_path)
    raw["mode"], raw["d8_config"] = "real", real_config()
    raw["dual_llm_config"]["outer_role"] = asdict(deepseek_roles.role_config("outer_planning"))
    raw["dual_llm_config"]["inner_role"] = asdict(deepseek_roles.role_config("inner_action"))
    return json.loads(json.dumps(raw))


@pytest.mark.parametrize("stage", ["tree_planning", "outer_turn", "inner_plan", "inner_size",
    "inner_sha256", "inner_complete", "inner_candidate", "fact_proposal", "fact_review",
    "intent_evaluation", "handoff_summary"])
def test_stage_schema_rule_and_role_binding(stage):
    env, role = envelope(stage)
    payload = deepseek_roles.build_payload(env, role)
    assert env.output_contract["schema"] == json.loads(payload["messages"][0]["content"].split("output_contract=")[1].split(" correction=")[0])["schema"]
    assert deepseek_roles.RULES[stage] in payload["messages"][0]["content"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["max_tokens"] == 16384 and payload["reasoning_effort"] == "high"
    assert "tools" not in payload and "temperature" not in payload and "top_p" not in payload
    with pytest.raises(ValueError, match="role_binding"):
        deepseek_roles.build_payload(env, deepseek_roles.role_config("outer_planning" if role.role == "inner_action" else "inner_action"))


def test_safe_session_is_carried_once_without_provider_tool_messages():
    session = {"session_id": "same-intent", "messages": [
        {"role": "assistant", "content": {"one": "HISTORICAL_UNIQUE"}, "identity": "historical_model_result_not_verified"},
        {"role": "tool", "content": {"observation": "EXECUTION_UNIQUE"}, "identity": "historical_execution_projection_not_current_approval"}],
        "current_history": {}, "session_scope": "intent"}
    env, role = envelope(session=json.dumps(session), correction={"reason": "proposal_rejected"})
    payload = deepseek_roles.build_payload(env, role)
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    body = json.loads(payload["messages"][1]["content"])
    assert body["session_context"] == session
    assert payload["messages"][1]["content"].count("HISTORICAL_UNIQUE") == 1
    assert '"reason": "proposal_rejected"' in payload["messages"][0]["content"]
    outer, outer_role = envelope("tree_planning")
    assert len(deepseek_roles.build_payload(outer, outer_role)["messages"]) == 2


@pytest.mark.parametrize("stage", ["conclusion_repair", "change_review", "refinement", "review", "progress_review"])
def test_additional_stages_map_typed_business_requests(stage):
    from agent_core.conclusion_revision_contracts import D8ChangeRequest, D8ConclusionChange, D8RevisionTarget
    from agent_core.process_facts import D5Attempt, D5Source, D5RefinementRequest
    from agent_core.review_contracts import D2ReviewRequest
    uid = lambda: str(uuid4())
    role = deepseek_roles.role_config("inner_action" if stage == "conclusion_repair" else "outer_planning")
    if stage in {"conclusion_repair", "change_review"}:
        change = None if stage == "conclusion_repair" else D8ConclusionChange(
            "withdraw", (D8RevisionTarget(uid(), 1, "old-ref"),), None, None, (), "Withdraw stale inference", None, None, None)
        business = D8ChangeRequest(uid(), uid(), uid(), "fact_proposal" if change is None else "fact_review",
                                  role.role_config_ref, 1, (), candidate=change)
    elif stage == "refinement":
        source_id = uid()
        source = D5Source("execution", source_id, "0" * 64, source_id, "current_execution")
        attempt = D5Attempt(uid(), uid(), uid(), 1, 1.0, role.role_config_ref, (source,), "0" * 64)
        business = D5RefinementRequest(attempt, (asdict(source),))
    else:
        business = D2ReviewRequest(uid(), uid(), 1, 1, "turn_start", uid(), uid(), uid(), None,
                                  1, "version1", (), 0, json.dumps({"d8_progress": {}} if stage == "progress_review" else {}))
    mapped, schema = output_schema(business)
    assert mapped == stage
    env = D8OutputRequest(business, {"version": 1, "stage": stage, "schema": schema}, "Return JSON", None)
    payload = deepseek_roles.build_payload(env, role)
    assert deepseek_roles.RULES[stage] in payload["messages"][0]["content"]
    assert json.loads(payload["messages"][1]["content"])


def test_capacity_counts_actual_payload_with_output_and_fixed_margin():
    env, role = envelope()
    capacity = deepseek_roles.EstimatedCapacity(deepseek_roles.role_config("outer_planning"), role)
    assert capacity.fits(env, deepseek_roles.CAPACITY_POLICY)
    oversized = replace(env, request=replace(env.request, history_context="大" * 90000))
    assert not capacity.fits(oversized, deepseek_roles.CAPACITY_POLICY)
    with pytest.raises(ValueError, match="capacity_policy"):
        capacity.fits(env, replace(deepseek_roles.CAPACITY_POLICY, measurement_unit="tokens"))


class Pipe:
    def __init__(self):
        self.sent = []
    def send(self, value):
        self.sent.append(value)
    def close(self):
        pass


def worker_response(monkeypatch, content, finish="stop", *, extra=None, status=200):
    body = json.dumps({"choices": [{"message": {"content": content, "reasoning_content": "PRIVATE_THOUGHT", **(extra or {})}, "finish_reason": finish}]}).encode()
    class Client:
        sock = SimpleNamespace(settimeout=lambda _: None)
        def connect(self): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return SimpleNamespace(status=status, read=lambda _: body)
        def close(self): pass
    monkeypatch.setattr(deepseek_model.http.client, "HTTPSConnection", lambda *a, **k: Client())
    pipe = Pipe()
    deepseek_roles._raw_http_worker(pipe, deepseek_model.DeepSeekSettings("deepseek-flash", "offline-private-key"), {}, 180)
    assert "PRIVATE_THOUGHT" not in repr(pipe.sent)
    return pipe.sent[-1]


@pytest.mark.parametrize("content,finish,issue", [
    ('{"a":1,"a":2}', "stop", "json_duplicate_key"),
    ('```json\n{}\n```', "stop", "json_parse_failed"),
    ('{"a":', "length", "output_truncated"),
    ('{}', "content_filter", "completion_invalid"),
    ('', "stop", "json_parse_failed"),
])
def test_raw_content_and_finish_reach_d8(monkeypatch, content, finish, issue):
    result = worker_response(monkeypatch, content, finish)
    assert result["payload"] == {"content": content, "finish_reason": finish}
    _, issues, terminal, _ = decode_response(D8ModelResponse(**result["payload"]), {"type": "object"})
    assert issues[0].code == issue
    if finish != "stop": assert terminal == issue


@pytest.mark.parametrize("extra,status,error", [({"tool_calls": [{}]}, 200, "validation_code"),
    ({"refusal": "raw private refusal"}, 200, "error"), ({}, 401, "error")])
def test_provider_rejections_are_safe(monkeypatch, extra, status, error):
    result = worker_response(monkeypatch, "{}", extra=extra, status=status)
    assert error in result and "raw private" not in repr(result)


def test_provider_envelope_cap_and_d8_content_cap_stay_separate(monkeypatch):
    oversized_envelope = worker_response(monkeypatch, "x" * deepseek_roles.TRANSPORT["max_response_bytes"])
    assert oversized_envelope["error"] == "invalid_response"
    result = worker_response(monkeypatch, "x" * 65537)
    assert "payload" in result
    _, _, terminal, units = decode_response(D8ModelResponse(**result["payload"]), {"type": "object"})
    assert terminal == "output_size_exceeded" and units["utf8_bytes"] == 65537


def install_controlled_transport(monkeypatch, *, alter=None):
    captured = []
    original = deepseek_roles.D8DeepSeekModel
    class ControlledModel(original):
        def __init__(self, role, settings):
            class Transport:
                def _invoke_payload(_, payload, timeout, token):
                    captured.append((self.current, payload))
                    if alter:
                        changed = alter(self.current, len(captured))
                        if changed is not None:
                            return changed
                    raw = SimulationModel(role).invoke(self.current, timeout, token)
                    return {"content": json.dumps(raw), "finish_reason": "stop"}
            super().__init__(role, settings, transport=Transport())
        def invoke(self, env, timeout=None, token=None):
            self.current = env
            return super().invoke(env, timeout, token)
    monkeypatch.setattr(deepseek_roles, "D8DeepSeekModel", ControlledModel)
    return captured


def run_real_offline(tmp_path, monkeypatch, *, alter=None):
    captured = install_controlled_transport(monkeypatch, alter=alter)
    raw = real_request(tmp_path)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state",
                           events=None, env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    return result, args, captured


def test_real_public_core_offline_result_replay_and_cancel_without_key(tmp_path, monkeypatch):
    result, args, captured = run_real_offline(tmp_path, monkeypatch)
    assert result["errors"] == [], result
    assert len(result["verified_facts"]) == 1, (result["task_run"], result["recovery"], [e.output_contract["stage"] for e, _ in captured])
    assert result["mode"] == "real" and result["sandbox_backend"] == "local_simulated"
    assert result["goal_evaluation"] == "not_evaluated" and result["answer_candidates"] == []
    stages = {env.output_contract["stage"] for env, _ in captured}
    assert {"tree_planning", "inner_plan", "inner_size", "inner_sha256", "fact_proposal", "fact_review", "inner_complete", "intent_evaluation", "handoff_summary", "review"} <= stages
    inner = [json.loads(p["messages"][1]["content"]) for e, p in captured if e.output_contract["stage"] in deepseek_roles.INNER_STAGES]
    assert len({c["session_id"] for c in inner}) == 1
    assert inner[-1]["session_context"]["messages"]
    count = len(captured)
    args.env_file.unlink()
    monkeypatch.setattr(SimulationModel, "invoke", lambda *args: pytest.fail("real lifecycle invoked a simulated model"))
    assert task_cli.dispatch_run_task(args)["task_run_id"] == result["task_run_id"]
    args.command, args.task_run_id = "cancel", result["task_run_id"]
    cancelled = cli.dispatch(args)
    assert cancelled["task_run"]["status"] == "cancelled", cancelled
    assert cancelled["cleanup_status"] == "confirmed"
    assert len(captured) == count


def test_once_format_correction_uses_full_schema_and_same_session(tmp_path, monkeypatch):
    sent = False
    def alter(env, number):
        nonlocal sent
        if env.output_contract["stage"] == "inner_plan" and not sent:
            sent = True
            return {"content": '{"template":"x","template":"y"}', "finish_reason": "stop"}
    result, _, captured = run_real_offline(tmp_path, monkeypatch, alter=alter)
    assert len(result["verified_facts"]) == 1, result
    plans = [e for e, _ in captured if e.output_contract["stage"] == "inner_plan"]
    assert len(plans) == 2 and plans[1].correction["reason"] == "output_format_invalid"
    assert plans[0].request.session_id == plans[1].request.session_id


@pytest.mark.parametrize("failure", ["length", "timeout", "disconnect"])
def test_unknown_and_truncation_never_reask_on_resume(tmp_path, monkeypatch, failure):
    def alter(env, number):
        if env.output_contract["stage"] == "fact_review":
            if failure == "length": return {"content": '{"decision":', "finish_reason": "length"}
            if failure == "disconnect": return ModelError(ModelErrorKind.PROVIDER_FAILURE, "http_transport_failure")
            return ModelError(ModelErrorKind.TIMED_OUT, "http_timeout")
    result, args, captured = run_real_offline(tmp_path, monkeypatch, alter=alter)
    assert any(e.output_contract["stage"] == "fact_review" for e, _ in captured)
    count = len(captured)
    args.command, args.task_run_id = "resume", result["task_run_id"]
    resumed = cli.dispatch(args)
    assert len(captured) == count, resumed
    assert not resumed["verified_facts"]


@pytest.mark.parametrize("field", ["sandbox_backend", "env_file", "model_transport"])
def test_real_configuration_tampering_is_read_only_recovery(tmp_path, monkeypatch, field):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    result, args, captured = run_real_offline(tmp_path, monkeypatch)
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    if field == "model_transport": declaration["request"]["d8_config"][field]["timeout_seconds"] = 179
    else: declaration[field] = "changed"
    failed = snapshot_v3_task_result(directory, declaration)
    assert failed["recovery"]["required"] and failed["verified_facts"] == []


def test_review_timeout_is_unknown_and_not_a_format_retry(tmp_path, monkeypatch):
    def alter(env, number):
        if env.output_contract["stage"] == "review":
            return ModelError(ModelErrorKind.TIMED_OUT, "http_timeout")
    result, args, captured = run_real_offline(tmp_path, monkeypatch, alter=alter)
    assert [e.output_contract["stage"] for e, _ in captured] == ["tree_planning", "review"]
    args.command, args.task_run_id = "resume", result["task_run_id"]
    cli.dispatch(args)
    assert len(captured) == 2


def test_env_credentials_do_not_override_frozen_roles_or_leak(tmp_path, monkeypatch):
    path = private_fixture(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "unexpected-env-key")
    role = deepseek_roles.role_config("inner_action")
    settings = deepseek_roles.settings_from_file(role, path)
    assert settings.api_key == "offline-private-key"
    assert "offline-private-key" not in repr(settings)
    assert settings.model == "deepseek-flash"
    with pytest.raises(ValueError, match="role_configuration"):
        deepseek_roles.settings_from_file(replace(role, model="other"), path)
    with pytest.raises(ValueError, match="invalid_d8_configuration"):
        raw = real_request(tmp_path)
        del raw["d8_config"]["model_transport"]
        parse_request(raw)


@pytest.mark.parametrize("failure", [ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED,
                                      ModelErrorKind.PROVIDER_FAILURE, ModelErrorKind.AUTHENTICATION_FAILED,
                                      ModelErrorKind.REFUSED])
def test_single_argument_review_has_runtime_timeout_cancel_and_failure_mapping(tmp_path, failure):
    from agent_core.review_contracts import D2ReviewRequest, D2ModelFailure
    role = deepseek_roles.role_config("outer_planning")
    uid = lambda: str(uuid4())
    business = D2ReviewRequest(uid(), uid(), 1, 1, "turn_start", uid(), uid(), uid(), None, 1, "v1", (), 0, "{}")
    stage, schema = output_schema(business)
    env = D8OutputRequest(business, {"version": 1, "stage": stage, "schema": schema}, "Return JSON", None)
    calls = []
    def invoke(payload, timeout, token):
        calls.append((timeout, token))
        return ModelError(failure, "http_transport_failure" if failure == ModelErrorKind.PROVIDER_FAILURE else "fixed_safe_reason")
    adapter = deepseek_roles.D8DeepSeekModel(role, deepseek_roles.settings_from_file(role, private_fixture(tmp_path)),
                                      transport=SimpleNamespace(_invoke_payload=invoke))
    with pytest.raises(ValueError, match="runtime_binding"):
        adapter.invoke(env)
    token = SimpleNamespace(is_cancelled=False)
    adapter.bind_runtime(token, time.time() + 30)
    response = adapter.invoke(env)
    assert len(calls) == 1 and 0 < calls[0][0] <= 30 and calls[0][1] is token
    assert isinstance(response, D2ModelFailure)
    assert response.kind == ("unknown" if failure in {ModelErrorKind.TIMED_OUT, ModelErrorKind.CANCELLED, ModelErrorKind.PROVIDER_FAILURE} else failure.value)


def test_workbench_real_emits_frozen_roles_and_keeps_credentials_private(tmp_path, monkeypatch):
    from unittest.mock import patch
    from agent_adapters.workbench import Workbench
    from test_workbench_real import _Process, _NoThread
    from test_workbench_v3 import _challenge
    env_file = private_fixture(tmp_path)
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", env_file)
    workbench = Workbench(tmp_path / "workbench", protocol_version=3, execution_profile="d8_v6")
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"fixture"), 7)
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=_Process()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", _NoThread):
        run = workbench.runner.start(challenge["id"], mode="real")
    command = popen.call_args.args[0]
    assert command[command.index("--mode") + 1] == "real"
    from pathlib import Path
    text = (Path(run["directory"]) / "request.json").read_text()
    raw = json.loads(text)
    assert raw["d8_config"]["sandbox_backend"] == "local_simulated"
    assert raw["task_request"]["target"]["endpoints"] == []
    assert "offline-private-key" not in text
    assert parse_request(raw)[1].contract_version == 6
