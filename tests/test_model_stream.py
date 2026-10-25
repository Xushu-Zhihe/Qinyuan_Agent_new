"""Offline SSE framing, semantic timeouts, cleanup and configuration contracts."""
import io
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import deepseek_model, deepseek_roles
from agent_adapters.deepseek_model import DeepSeekModelAdapter, DeepSeekSettings
from agent_adapters.model_stream import ModelStreamError, read_chat_stream
from agent_adapters.output_evidence import FileD8OutputEvidence, safe_model_error_code
from agent_core.modeling import ModelErrorKind, model_call_timeout


def chunk(delta=None, finish=None, **extra):
    return b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta or {},
        "finish_reason": finish}], **extra}, ensure_ascii=False).encode() + b"\r\n\r\n"


def read(raw, *, limit=4096, split=None):
    class Response:
        def __init__(self): self.parts = iter(split) if split else None; self.raw = io.BytesIO(raw)
        def read1(self, size): return next(self.parts, b"") if self.parts else self.raw.read1(size)
    reports = []
    result = read_chat_stream(Response(), limit, lambda **value: reports.append(value))
    return result, reports


def test_split_utf8_reasoning_and_usage_are_observed_without_retaining_thought():
    secret_thought = "PRIVATE-THOUGHT-汉字"
    raw = (b": keep-alive\r\n\r\n" + chunk({"role": "assistant"}) +
        chunk({"reasoning_content": secret_thought}) + chunk({"content": '{"答案":'}) +
        chunk({"content": '1}'}, "stop") +
        b'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":9}}\n\n' + b"data: [DONE]\n\n")
    (data, reasoning), reports = read(raw, split=[raw[i:i+3] for i in range(0, len(raw), 3)])
    assert data["choices"][0]["message"] == {"content": '{"答案":1}'}
    assert data["usage"]["prompt_tokens"] == 42 and reasoning == len(secret_thought)
    assert reports[-1]["stream_end"] == "done" and reports[-1]["keepalive_lines"] == 1
    assert reports[-1]["reasoning_utf8_bytes"] == len(secret_thought.encode())
    assert secret_thought not in repr((data, reports))


@pytest.mark.parametrize("tail", [b"", b"data: [DONE]\n\n"])
def test_complete_looking_json_without_finish_is_not_a_decision(tail):
    with pytest.raises(ModelStreamError): read(chunk({"content": "{}"}) + tail)


def test_finish_without_done_is_incomplete_and_length_is_preserved():
    with pytest.raises(ModelStreamError, match="http_transport_failure"):
        read(chunk({"content": "{}"}, "stop"))
    (data, count), _ = read(chunk({"reasoning_content": "thinking"}, "length") + b"data: [DONE]\n\n")
    assert data["choices"][0]["finish_reason"] == "length" and count == 8
    assert data["choices"][0]["message"]["content"] == ""


@pytest.mark.parametrize("raw", [b"data: bad-json\n\n", b'data: {"choices":[]}\n\n',
    chunk({"tool_calls": [{"function": {"name": "forbidden"}}]}),
    chunk({"content": 123}), chunk({}, "stop") + chunk({"content": "late"})])
def test_malformed_or_native_tool_stream_is_rejected(raw):
    with pytest.raises(ModelStreamError, match="provider_stream_invalid"): read(raw)


def test_multiline_sse_and_protocol_overhead_do_not_consume_text_allowance():
    raw = (b'data: {"choices": [\n' +
           b'data: {"delta": {"content": "{}"}, "finish_reason": "stop"}]}\n\n' +
           b"data: [DONE]\n\n")
    (data, _), reports = read(raw, limit=128)
    assert data["choices"][0]["message"]["content"] == "{}"
    assert reports[-1]["response_utf8_bytes"] > 2
    with pytest.raises(ModelStreamError, match="response_rejected"):
        read(chunk({"reasoning_content": "x" * 200}), limit=128)


def test_real_worker_stream_success_and_key_split_redaction(monkeypatch):
    raw = chunk({"reasoning_content": "never persist this thought"}) + chunk({"content": "{}"}, "stop") + b"data: [DONE]\n\n"
    calls = []
    class Client:
        def __init__(self, *args, **kw): self.sock = SimpleNamespace(settimeout=lambda v: None)
        def connect(self): pass
        def request(self, *args, **kw): calls.append(json.loads(kw["body"]))
        def getresponse(self): return SimpleNamespace(status=200, read1=io.BytesIO(raw).read1)
        def close(self): pass
    monkeypatch.setattr(deepseek_model.http.client, "HTTPSConnection", Client)
    class Pipe:
        def __init__(self): self.messages = []
        def send(self, item): self.messages.append(item)
        def close(self): pass
    pipe = Pipe()
    settings = DeepSeekSettings("offline", "private-key")
    deepseek_roles._raw_http_worker(pipe, settings, {"stream": True}, 5)
    assert pipe.messages[-1] == {"payload": {"content": "{}", "finish_reason": "stop"}}
    assert "never persist this thought" not in repr(pipe.messages)
    assert any(m.get("diagnostic", {}).get("reasoning_chars") == 26 for m in pipe.messages)
    raw = chunk({"content": "private-"}) + chunk({"content": "key"}, "stop") + b"data: [DONE]\n\n"
    pipe = Pipe(); deepseek_roles._raw_http_worker(pipe, settings, {"stream": True}, 5)
    assert pipe.messages[-1] == {"error": "invalid_response", "reason": "response_rejected"}
    assert not any("payload" in m for m in pipe.messages)


def progress_worker(connection, settings, payload, timeout):
    connection.send({"connected": True})
    case = payload["case"]
    count = 0
    if case == "idle":
        connection.send({"diagnostic": {"reasoning_chars": 1, "response_utf8_bytes": 20}})
    while True:
        count += 1
        if case == "progress":
            connection.send({"diagnostic": {"reasoning_chars": count, "response_utf8_bytes": count * 20}})
            if count == 6:
                connection.send({"payload": {"content": "{}", "finish_reason": "stop"}})
                return
        else:
            connection.send({"diagnostic": {"response_utf8_bytes": 20 + count, "keepalive_lines": count}})
        time.sleep(.04)


def unconnected_worker(connection, settings, payload, timeout):
    time.sleep(30)


@pytest.mark.parametrize("case,code", [("first", "model_first_output_timeout"), ("idle", "model_idle_timeout"),
    ("total", "model_total_timeout"), ("connect", "model_connect_timeout"), ("progress", None)])
def test_semantic_timers_ignore_heartbeat_and_confirm_worker_exit(case, code):
    settings = DeepSeekSettings("offline", "private-key", timeout_seconds=4,
        first_output_timeout_seconds=.18 if case == "first" else 3,
        idle_timeout_seconds=.14, connect_timeout_seconds=.9 if case == "connect" else 2)
    adapter = DeepSeekModelAdapter(settings, worker=unconnected_worker if case == "connect" else progress_worker)
    detail = {}
    result = adapter._invoke_payload({"stream": True, "case": case}, .9 if case == "total" else 4,
        SimpleNamespace(is_cancelled=False), diagnostic=detail)
    assert detail["worker_exit_confirmed"] is True and detail["elapsed_seconds"] < 5
    if code:
        assert result.kind is ModelErrorKind.TIMED_OUT and result.short_reason == code
        assert safe_model_error_code(code) == code and detail["transport_end"] == "timeout"
    else: assert result == {"content": "{}", "finish_reason": "stop"}
    if case == "first": assert "first_output_seconds" not in detail and detail["keepalive_lines"] > 1
    if case in {"idle", "progress"}: assert detail["first_output_seconds"] <= detail["last_output_seconds"]


def test_shared_policy_unwraps_and_respects_configuration_and_task_deadline():
    settings = DeepSeekSettings("offline", "private-key")
    assert (settings.timeout_seconds, settings.first_output_timeout_seconds, settings.idle_timeout_seconds) == (600, 180, 60)
    for limit in (23, 180, 600, 900):
        adapter = SimpleNamespace(adapter=SimpleNamespace(adapter=SimpleNamespace(settings=replace(settings, timeout_seconds=limit))))
        assert model_call_timeout(adapter, 500) == min(limit, 500)
    assert model_call_timeout(object(), 700) == 600


@pytest.mark.parametrize("field,value", [("timeout_seconds", 0), ("idle_timeout_seconds", float("nan")),
    ("first_output_timeout_seconds", float("inf")), ("connect_timeout_seconds", True)])
def test_invalid_timeout_configuration_rejected(field, value):
    with pytest.raises(ValueError, match="invalid_model_timeout"): DeepSeekSettings("offline", "key", **{field: value})


def test_role_env_configuration_and_frozen_policy(tmp_path):
    env = tmp_path / "offline.env"
    env.write_text("QINYUAN_LLM_BACKEND=openai\nOPENAI_BASE_URL=" + deepseek_roles.TRANSPORT["base_url"] +
        "\nQINYUAN_ALLOW_CUSTOM_ENDPOINT=1\nOPENAI_API_KEY=private-key\n"
        "QINYUAN_LLM_TIMEOUT_SECONDS=750\nQINYUAN_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS=200\n"
        "QINYUAN_LLM_IDLE_TIMEOUT_SECONDS=75\nQINYUAN_LLM_CONNECT_TIMEOUT_SECONDS=12\n")
    role = deepseek_roles.role_config("outer_planning")
    settings = deepseek_roles.settings_from_file(role, env)
    assert deepseek_roles.timeout_policy(settings) == {"connect_seconds": 12, "total_seconds": 750,
        "first_output_seconds": 200, "idle_seconds": 75}
    evidence = FileD8OutputEvidence(tmp_path / "evidence")
    identity = str(uuid4()); model = deepseek_roles.D8DeepSeekModel(role, settings)
    deepseek_roles.freeze_timeout_policy(evidence, identity, model)
    deepseek_roles.freeze_timeout_policy(evidence, identity, model)
    model.settings = replace(settings, timeout_seconds=600)
    with pytest.raises(ValueError, match="d8_model_timeout_policy_changed"):
        deepseek_roles.freeze_timeout_policy(evidence, identity, model)
    assert "private-key" not in repr(evidence.read("model-timeouts", identity))


def test_cancellation_during_reasoning_confirms_exit_without_a_partial_result():
    detail = {}
    class Token:
        @property
        def is_cancelled(self): return (detail.get("reasoning_chars") or 0) >= 2
    adapter = DeepSeekModelAdapter(DeepSeekSettings("offline", "private-key"), worker=progress_worker)
    result = adapter._invoke_payload({"stream": True, "case": "progress"}, 4, Token(), diagnostic=detail)
    assert result.kind is ModelErrorKind.CANCELLED and detail["transport_end"] == "cancelled"
    assert detail["worker_exit_confirmed"] is True and detail["reasoning_chars"] >= 2


def test_incomplete_stream_retains_observed_finish_and_numeric_usage_only():
    reports = []
    raw = chunk({"content": "{}"}, "stop", usage={"completion_tokens": 7, "private": "NEVER-STORE"})
    with pytest.raises(ModelStreamError):
        read_chat_stream(io.BytesIO(raw), 4096, lambda **v: reports.append(v))
    assert reports[-1]["finish_reason"] == "stop" and reports[-1]["usage"] == {"completion_tokens": 7}
    assert "NEVER-STORE" not in repr(reports)


@pytest.mark.parametrize("remaining,expected", [(500, None), (90, "model_total_timeout")])
def test_virtual_long_generation_exceeds_old_180_but_not_task_deadline(monkeypatch, remaining, expected):
    now = [0.0]
    scheduled = [(1, {"connected": True})] + [(n, {"diagnostic": {"reasoning_chars": n}})
        for n in (20, 70, 120, 170, 220)] + [(221, {"payload": {"content": "{}", "finish_reason": "stop"}})]
    class Pipe:
        def poll(self, delay): now[0] += 1; return bool(scheduled and scheduled[0][0] <= now[0])
        def recv(self): return scheduled.pop(0)[1]
        def close(self): pass
    class Process:
        pid = 1
        alive = True
        def start(self): pass
        def is_alive(self): return self.alive
        def terminate(self): self.alive = False
        def join(self, timeout): pass
        def kill(self): self.alive = False
        def close(self): pass
    monkeypatch.setattr(deepseek_model.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deepseek_model.multiprocessing, "get_context", lambda mode: SimpleNamespace(
        Pipe=lambda **kw: (Pipe(), Pipe()), Process=lambda **kw: Process()))
    detail = {}
    result = DeepSeekModelAdapter(DeepSeekSettings("offline", "private-key"))._invoke_payload(
        {"stream": True}, remaining, SimpleNamespace(is_cancelled=False), diagnostic=detail)
    assert detail["worker_exit_confirmed"] is True
    if expected:
        assert result.short_reason == expected and detail["elapsed_seconds"] == remaining
    else:
        assert result["content"] == "{}" and detail["elapsed_seconds"] == 221


@pytest.mark.parametrize("raw", [b"data:\n" * 200, b":" + b"x" * 200 + b"\n"])
def test_empty_data_lines_and_terminated_large_lines_remain_bounded(raw):
    with pytest.raises(ModelStreamError, match="response_rejected"): read(raw, limit=128)
