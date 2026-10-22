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
    path.write_text("QINYUAN_LLM_BACKEND=openai\nOPENAI_BASE_URL=" + deepseek_roles.TRANSPORT["base_url"] + "\nQINYUAN_ALLOW_CUSTOM_ENDPOINT=1\n"
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
    assert "max_tokens" not in payload and payload["reasoning_effort"] == "high"
    assert "max_tokens" not in dict(role.parameters)
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


def worker_response(monkeypatch, content, finish="stop", *, extra=None, status=200, usage=None, with_diagnostic=False):
    body = json.dumps({"usage": usage, "choices": [{"message": {"content": content, "reasoning_content": "PRIVATE_THOUGHT", **(extra or {})}, "finish_reason": finish}]}).encode()
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
    diagnostic = {}
    for sent in pipe.sent:
        diagnostic.update(sent.get("diagnostic", {}))
    return (pipe.sent[-1], diagnostic) if with_diagnostic else pipe.sent[-1]


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
                def _invoke_payload(_, payload, timeout, token, *, diagnostic=None):
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
    assert settings.model == role.model
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
    def invoke(payload, timeout, token, *, diagnostic=None):
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


@pytest.mark.parametrize("confirmed", [False, True])
def test_review_timeout_requires_explicit_worker_exit_receipt(confirmed):
    from agent_core.review_contracts import D2ReviewRequest
    role = deepseek_roles.role_config("outer_planning")
    uid = lambda: str(uuid4())
    business = D2ReviewRequest(uid(), uid(), 1, 1, "turn_start", uid(), uid(), uid(), None, 1, "v1", (), 0, "{}")
    stage, schema = output_schema(business)
    env = D8OutputRequest(business, {"version": 1, "stage": stage, "schema": schema}, "Return JSON", None)
    def invoke(payload, timeout, token, *, diagnostic):
        diagnostic.update(transport_end="timeout", worker_exit_confirmed=confirmed)
        return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
    adapter = deepseek_roles.D8DeepSeekModel(role, deepseek_model.DeepSeekSettings(role.model, "offline-private-key"),
        transport=SimpleNamespace(_invoke_payload=invoke))
    adapter.bind_runtime(SimpleNamespace(is_cancelled=False), time.time() + 30)
    assert adapter.invoke(env).kind == ("provider_failure" if confirmed else "unknown")


@pytest.mark.parametrize("http_error", [False, True])
def test_provider_response_cannot_override_unconfirmed_worker_exit(http_error):
    from agent_core.structured_output import transient_provider_result
    env, role = envelope()
    def invoke(payload, timeout, token, *, diagnostic):
        diagnostic.update(transport_end="http_error" if http_error else "response_received",
                          http_status=503 if http_error else 200, worker_exit_confirmed=False)
        return (ModelError(ModelErrorKind.PROVIDER_FAILURE, "deepseek_http_503") if http_error
                else {"content": "{}", "finish_reason": "stop"})
    adapter = deepseek_roles.D8DeepSeekModel(role, deepseek_model.DeepSeekSettings(role.model, "offline-private-key"),
        transport=SimpleNamespace(_invoke_payload=invoke))
    assert adapter.invoke(env, 30, SimpleNamespace(is_cancelled=False)).kind is ModelErrorKind.TIMED_OUT
    evidence = SimpleNamespace(read=lambda *args: {"transport": {"transport_end": "http_error",
        "http_status": 503, "worker_exit_confirmed": False}})
    assert not transient_provider_result(evidence, "call")


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


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_provider_numeric_diagnostics_preserve_incomplete_content_length(monkeypatch, finish):
    result, detail = worker_response(monkeypatch, "汉字{", finish, with_diagnostic=True, usage={
        "prompt_tokens": 123, "completion_tokens": 32768, "total_tokens": 32891,
        "completion_tokens_details": {"reasoning_tokens": 32760, "private": "PRIVATE_USAGE"},
        "secret": "PRIVATE_USAGE", "prompt_cache_hit_tokens": True})
    assert detail["transport_end"] == "response_received" and detail["http_status"] == 200
    assert detail["finish_reason"] == finish and detail["content_chars"] == 3
    assert detail["content_utf8_bytes"] == 7 and detail["reasoning_chars"] == len("PRIVATE_THOUGHT")
    assert detail["usage"] == {"prompt_tokens": 123, "completion_tokens": 32768,
        "total_tokens": 32891, "completion_tokens_details": {"reasoning_tokens": 32760}}
    assert "PRIVATE" not in json.dumps(detail) and result["payload"]["content"] == "汉字{"


@pytest.mark.parametrize("failure", ["incomplete", "timeout", "http_402"])
def test_transport_end_diagnostics_distinguish_wire_and_provider_failures(monkeypatch, failure):
    class Client:
        sock = SimpleNamespace(settimeout=lambda _: None)
        def connect(self): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self):
            def read(_):
                if failure == "incomplete":raise deepseek_model.http.client.IncompleteRead(b"private partial", 50)
                raise TimeoutError("private timeout")
            return SimpleNamespace(status=402 if failure == "http_402" else 200, read=read)
        def close(self): pass
    monkeypatch.setattr(deepseek_model.http.client, "HTTPSConnection", lambda *a, **k: Client())
    pipe = Pipe()
    deepseek_roles._raw_http_worker(pipe, deepseek_model.DeepSeekSettings("deepseek-flash", "offline-private-key"), {}, 180)
    detail = {}
    for sent in pipe.sent:detail.update(sent.get("diagnostic", {}))
    assert detail["transport_end"] == {"incomplete": "incomplete_response", "timeout": "timeout", "http_402": "http_error"}[failure]
    assert detail["http_status"] == (402 if failure == "http_402" else 200)
    assert "private" not in repr(pipe.sent)


@pytest.mark.parametrize("failure", ["length", "timeout", "http_402"])
def test_call_diagnostics_are_durable_without_debug_trace_or_provider_text(tmp_path, monkeypatch, failure):
    from agent_adapters.output_evidence import FileD8OutputEvidence
    from agent_core.turn_modeling import digest
    monkeypatch.delenv("QINYUAN_DEBUG_TRACE", raising=False)
    role = deepseek_roles.role_config("outer_planning")
    uid = lambda: str(uuid4())
    from agent_core.review_contracts import D2ReviewRequest
    business = D2ReviewRequest(uid(), uid(), 1, 1, "turn_start", uid(), uid(), uid(), None, 1, "v1", (), 0, "{}")
    stage, schema = output_schema(business)
    env = D8OutputRequest(business, {"version": 1, "stage": stage, "schema": schema}, "Return JSON", None)
    def invoke(payload, timeout, token, *, diagnostic):
        diagnostic.update(http_status=402 if failure == "http_402" else 200,
                          transport_end="response_received" if failure == "length" else "timeout" if failure == "timeout" else "http_error")
        if failure == "length":
            diagnostic.update(deepseek_model._response_diagnostic({"usage": {
                "completion_tokens": 32768, "completion_tokens_details": {"reasoning_tokens": 32760}}},
                {"reasoning_content": "PRIVATE_THOUGHT"}, "length", "PRIVATE_RESPONSE"))
            return {"content": "PRIVATE_RESPONSE", "finish_reason": "length"}
        return ModelError(ModelErrorKind.TIMED_OUT if failure == "timeout" else ModelErrorKind.PROVIDER_FAILURE,
                          "http_timeout" if failure == "timeout" else "deepseek_http_402")
    adapter = deepseek_roles.D8DeepSeekModel(role, deepseek_roles.settings_from_file(role, private_fixture(tmp_path)),
        transport=SimpleNamespace(_invoke_payload=invoke))
    directory = tmp_path / "d8-output"
    adapter.bind_diagnostics(FileD8OutputEvidence(directory))
    adapter.invoke(env, 90, SimpleNamespace(is_cancelled=False))
    persisted = FileD8OutputEvidence(directory, create=False)
    sent = persisted.read("provider-request", business.review_id)
    result = persisted.read("provider-result", business.review_id)
    assert sent["model_request_id"] == business.review_id and sent["task_run_id"] == business.task_run_id
    assert sent["parameters"] == {"model": role.model, "response_format": {"type": "json_object"},
        "stream": False, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert sent["timeout_seconds"] == 90 and result["request_sha256"] == digest(sent)
    if failure == "length":
        assert result["transport"]["usage"]["completion_tokens"] == 32768
        assert result["transport"]["finish_reason"] == "length" and result["transport"]["content_chars"] == 16
    text = json.dumps([sent, result])
    assert "PRIVATE" not in text and "offline-private-key" not in text and "messages" not in text
    assert len(list(directory.glob("*.json"))) == 2


def test_real_adapter_diagnostics_are_connected_to_public_assembly(tmp_path, monkeypatch):
    from agent_adapters.output_evidence import FileD8OutputEvidence
    result, args, captured = run_real_offline(tmp_path, monkeypatch)
    records = FileD8OutputEvidence(args.state_dir / result["task_run_id"] / "d8-output", create=False)
    for env, payload in captured:
        request = getattr(env.request, "attempt", env.request)
        identity = getattr(request, "model_request_id", None) or getattr(request, "review_id", None)
        assert records.read("provider-request", identity)["parameters"]["model"] == payload["model"]
        assert records.read("provider-result", identity)["model_request_id"] == identity


def test_missing_output_contract_identifies_stage_without_read_retry(tmp_path, monkeypatch):
    from agent_adapters.web import task_result as projection
    def alter(env, number):
        if env.output_contract["stage"] == "fact_review":
            return {"content": '{"decision":', "finish_reason": "length"}
    result, args, captured = run_real_offline(tmp_path, monkeypatch, alter=alter)
    directory = args.state_dir / result["task_run_id"]
    request = next(e.request for e, _ in captured if e.output_contract["stage"] == "fact_review")
    (directory / "d8-output" / ("d8-handoff-contract-" + request.model_request_id + ".json")).unlink()
    monkeypatch.setattr(projection.time, "sleep", lambda _: pytest.fail("corrupt evidence must not be retried"))
    view = projection.snapshot_v3_task_result(directory, immutable_read(directory / "run.json"))
    error = view["errors"][0]
    assert error["code"] == "state_read_failed" and error["read_stage"] == "output_diagnostics"
    assert "d8_output_diagnostic_binding_changed" in error["error_codes"]
    assert error["exception_chain"][0]["source_location"]["module"] == "agent_core.structured_output"
    assert not view["verified_facts"]



def diagnostic_pipe_worker(connection, settings, payload, timeout):
    """Actual child/pipe lifecycle, without HTTP or model invocation."""
    connection.send({"connected": True})
    connection.send({"diagnostic": {"http_status": 200, "transport_end": "response_received",
        "finish_reason": "length", "content_chars": 3, "usage": {"completion_tokens": 32768}}})
    if payload["case"] == "result":
        connection.send({"payload": {"content": "{}[", "finish_reason": "length"}})
    elif payload["case"] == "error":
        connection.send({"error": "invalid_response", "reason": "provider_content_invalid"})
    connection.close()


@pytest.mark.parametrize("case", ["result", "error", "lost"])
def test_parent_transport_retains_metadata_on_result_error_and_lost_worker(case):
    adapter = deepseek_model.DeepSeekModelAdapter(
        deepseek_model.DeepSeekSettings("deepseek-flash", "offline-private-key"), worker=diagnostic_pipe_worker)
    detail = {}
    result = adapter._invoke_payload({"case": case}, 15, SimpleNamespace(is_cancelled=False), diagnostic=detail)
    assert detail["usage"]["completion_tokens"] == 32768 and detail["finish_reason"] == "length"
    if case == "result":assert result["finish_reason"] == "length"
    else:assert isinstance(result, ModelError)
    assert detail["transport_end"] == ("worker_lost" if case == "lost" else "response_received")
    assert detail["worker_exit_confirmed"] is True


def stalled_model_worker(connection, settings, payload, timeout):
    connection.send({"connected": True})
    connection.send({"diagnostic": {"http_status": 200}})
    time.sleep(30)


def test_model_timeout_confirms_child_exit_without_http():
    adapter = deepseek_model.DeepSeekModelAdapter(
        deepseek_model.DeepSeekSettings("offline", "offline-private-key"), worker=stalled_model_worker)
    detail = {}
    result = adapter._invoke_payload({}, 1.5, SimpleNamespace(is_cancelled=False), diagnostic=detail)
    assert result.kind is ModelErrorKind.TIMED_OUT
    assert detail["transport_end"] == "timeout" and detail["worker_exit_confirmed"] is True


@pytest.mark.parametrize("cooperative", [True, False])
def test_outer_watchdog_waits_for_adapter_or_keeps_result_unknown(cooperative):
    import asyncio
    import threading
    from agent_core.dual_llm_modeling import D3AuditedModel
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    release = threading.Event()
    seen = []
    def invoke(request, timeout, token):
        while not token.is_cancelled:
            time.sleep(.001)
        seen.append(token.is_timed_out)
        if not cooperative:release.wait(5)
        return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
    model = SimpleNamespace(adapter=SimpleNamespace(invoke=invoke), output_boundary=None, clock=time.time,
        controller=SimpleNamespace(control_state=SimpleNamespace(task_run=SimpleNamespace(
            status=SimpleNamespace(value="running"), deadline_at=time.time()+60))))
    req = SimpleNamespace(task_run_id="run", model_request_id="call", stage="inner_plan")
    try:
        call = D3AuditedModel._invoke(model, req, .02, SimpleNamespace(is_cancelled=False))
        if cooperative:
            assert asyncio.run(call).kind is ModelErrorKind.TIMED_OUT
        else:
            with pytest.raises(D3RecoveryRequired, match="d3_model_call_outcome_unknown"):
                asyncio.run(call)
        assert seen == [True]
    finally:
        release.set()


def test_user_cancel_waits_for_model_adapter_acknowledgement():
    import asyncio
    from agent_core.dual_llm_modeling import D3AuditedModel
    token = SimpleNamespace(is_cancelled=False)
    def invoke(request, timeout, invocation):
        token.is_cancelled = True
        while not invocation.is_cancelled:time.sleep(.001)
        time.sleep(.02)  # Transport cleanup is still in flight.
        assert not invocation.is_timed_out
        return ModelError(ModelErrorKind.CANCELLED, "model_cancelled")
    model = SimpleNamespace(adapter=SimpleNamespace(invoke=invoke), output_boundary=None, clock=time.time,
        controller=SimpleNamespace(control_state=SimpleNamespace(task_run=SimpleNamespace(
            status=SimpleNamespace(value="running"), deadline_at=time.time()+60))))
    req = SimpleNamespace(task_run_id="run", model_request_id="call", stage="inner_plan")
    assert asyncio.run(D3AuditedModel._invoke(model, req, 30, token)).kind is ModelErrorKind.CANCELLED


def test_shared_schema_preserves_all_enum_constraints_without_mutating_source():
    # Same reference list occurs in several real output branches.
    choices = {"type": "string", "enum": [str(uuid4()) for _ in range(30)], "maxLength": 80}
    schema = {"anyOf": [{"properties": {"a": choices}}, {"properties": {"b": choices}}]}
    before = json.dumps(schema)
    shared = deepseek_roles._shared_schema(schema)
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return expand(shared["$defs"][value["$ref"].split("/")[-1]])
            return {k: expand(v) for k, v in value.items() if k != "$defs"}
        return [expand(v) for v in value] if isinstance(value, list) else value
    assert expand(shared) == schema
    assert len(json.dumps(shared)) < len(before)
    assert json.dumps(schema) == before


def test_shared_history_retains_tools_evidence_and_unequal_sections():
    history = {"items": [{"ref": "fact:1", "content": "observation" * 100}],
               "mcp_tools": [{"name": "tool", "parameters": "x" * 200}],
               "scope": "new"}
    current = {**history, "scope": "old", "session_only": "preserve"}
    context = {"history_context": json.dumps(history), "session_context": {"current_history": current}}
    deepseek_roles._shared_history(context)
    projected = context["session_context"]["current_history"]
    assert context["history_context"] == history
    restored = {k: history[v["$ref"].split("/")[-1]] if isinstance(v, dict) and set(v) == {"$ref"} else v
                for k, v in projected.items()}
    assert restored == {**history, "scope": "old", "session_only": "preserve"}
    assert projected["items"] == {"$ref": "#/history_context/items"}
    assert projected["scope"] == "old"


@pytest.mark.parametrize("version, marker, valid", [(22, "shared_refs_v1", True),
    (21, "shared_refs_v1", False), (22, "unknown", False)])
def test_shared_presentation_marker_never_weakens_schema_binding(version, marker, valid):
    from agent_core.output_contracts import matches_output_contract
    schema = {"type": "string", "enum": ["original"]}
    contract = {"version": 1, "stage": "inner_plan", "schema": schema, "input_projection": marker}
    assert matches_output_contract(contract, "inner_plan", schema, version) is valid
    assert not matches_output_contract(contract, "inner_plan", {"type": "string"}, version)
