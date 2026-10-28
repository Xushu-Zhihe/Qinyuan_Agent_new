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


def read(raw, *, limit=4096, split=None, native=False):
    class Response:
        def __init__(self): self.parts = iter(split) if split else None; self.raw = io.BytesIO(raw)
        def read1(self, size): return next(self.parts, b"") if self.parts else self.raw.read1(size)
    reports = []
    result = read_chat_stream(Response(), limit, lambda **value: reports.append(value), allow_tool_calls=native)
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


@pytest.mark.parametrize("raw,native,branch", [
    (chunk({"content": "{}"}) + b"data: [DONE]\n\n", False, "done_without_finish"),
    (b"data: private-invalid-json\n\n", False, "event_json_invalid"),
    (b"data: []\n\n", False, "event_type_invalid"),
    (b'data: {}\n\n', False, "choices_type_invalid"),
    (b'data: {"choices":[]}\n\n', False, "choices_count_invalid"),
    (b'data: {"choices":[null]}\n\n', False, "choice_type_invalid"),
    (b'data: {"choices":[{"index":1,"delta":{}}]}\n\n', False, "choice_index_invalid"),
    (b'data: {"choices":[{"delta":null}]}\n\n', False, "delta_type_invalid"),
    (chunk({"function_call": {"name": "private"}}), False, "legacy_function_call"),
    (chunk({"tool_calls": [{"index": 0}]}), False, "tool_calls_not_allowed"),
    (chunk({}, "stop") + chunk({"tool_calls": [{"index": 0}]}), True, "tool_calls_after_finish"),
    (chunk({"tool_calls": "private"}), True, "tool_calls_type_invalid"),
    (chunk({"tool_calls": [None]}), True, "tool_item_type_invalid"),
    (chunk({"tool_calls": [{"index": -1}]}), True, "tool_index_invalid"),
    (chunk({"tool_calls": [{"index": 0, "private": "private"}]}), True, "tool_fields_invalid"),
    (chunk({"tool_calls": [{"index": 0, "type": "private"}]}), True, "tool_type_invalid"),
    (chunk({"tool_calls": [{"index": 0, "id": "private-original"}]}) +
        chunk({"tool_calls": [{"index": 0, "id": "private-changed"}]}), True, "tool_id_changed"),
    (chunk({"tool_calls": [{"index": 0, "id": None}]}), True, "tool_id_type_invalid"),
    (chunk({"tool_calls": [{"index": 0, "function": None}]}), True, "tool_function_type_invalid"),
    (chunk({"tool_calls": [{"index": 0, "function": {"private-key": "private-value"}}]}), True, "tool_function_fields_invalid"),
    (chunk({"tool_calls": [{"index": 0, "function": {"name": None}}]}), True, "tool_function_field_type"),
    (chunk({}, "stop") + chunk({"content": "private-late"}), False, "text_after_finish"),
    (chunk({"reasoning_content": 42}), False, "text_type_invalid"),
    (chunk({}, ""), False, "finish_reason_invalid"),
])
def test_stream_rejection_has_safe_branch_and_event_position(raw, native, branch):
    reports = []
    with pytest.raises(ModelStreamError, match="^provider_stream_invalid$"):
        read_chat_stream(io.BytesIO(raw), 65536, lambda **value: reports.append(value), allow_tool_calls=native)
    diagnostic = reports[-1]
    detail = diagnostic["stream_error"]
    assert detail["branch"] == branch
    assert detail["event_index"] == diagnostic["sse_events"] >= 1
    assert detail["field_path"].startswith("$") and "private" not in json.dumps(diagnostic)
    assert set(detail) == {"branch", "event_index", "field_path", "observed_type"}
    assert diagnostic["done_seen"] == (branch == "done_without_finish")
    assert diagnostic["finish_seen"] == (branch in {"text_after_finish", "tool_calls_after_finish"})


def test_native_tool_argument_fragments_and_finish_are_preserved():
    raw = (chunk({"tool_calls": [{"index": 0, "id": "call_local", "type": "function",
        "function": {"name": "submit_stage_result", "arguments": '{"result":'}}]}) +
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"value":"汉字"}}'}}]}, "tool_calls") +
        b"data: [DONE]\n\n")
    (data, _), reports = read(raw, split=[raw[i:i+3] for i in range(0, len(raw), 3)], native=True)
    call = data["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_local" and call["function"]["name"] == "submit_stage_result"
    assert json.loads(call["function"]["arguments"]) == {"result": {"value": "汉字"}}
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert reports[-1]["content_chars"] > 0


@pytest.mark.parametrize("index,id_value", [(-1, "call_other"), (True, "call_other"), (0, "changed")])
def test_native_stream_rejects_invalid_index_or_changed_identity(index, id_value):
    raw = chunk({"tool_calls": [{"index": 0, "id": "original", "function": {"name": "submit_stage_result"}}]})
    raw += chunk({"tool_calls": [{"index": index, "id": id_value, "function": {"arguments": "{}"}}]}, "tool_calls")
    with pytest.raises(ModelStreamError): read(raw, native=True)


def test_interleaved_native_calls_are_received_without_selecting_a_decision():
    raw = (chunk({"tool_calls": [
        {"index": 1, "id": "second", "function": {"name": "submit_stage_result", "arguments": '{"result":'}},
        {"index": 0, "id": "first", "function": {"name": "submit_stage_result", "arguments": '{"result":'}}]}) +
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"一"}'}},
                              {"index": 1, "function": {"arguments": '"二"}'}}]}, "tool_calls",
              usage={"prompt_tokens": 42, "completion_tokens": 12}) + b"data: [DONE]\n\n")
    (data, _), reports = read(raw, native=True, split=[raw[i:i+3] for i in range(0, len(raw), 3)])
    calls = data["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["first", "second"]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [{"result": "一"}, {"result": "二"}]
    assert data["usage"]["completion_tokens"] == 12
    assert reports[-1]["finish_seen"] and reports[-1]["done_seen"] and reports[-1]["stream_error"] is None
    with pytest.raises(ModelStreamError, match="http_transport_failure"):
        read(raw.removesuffix(b"data: [DONE]\n\n"), native=True)


def test_native_call_collection_and_index_sequence_remain_bounded():
    raw = b"".join(chunk({"tool_calls": [{"index": i}]}) for i in range(100))
    with pytest.raises(ModelStreamError, match="response_rejected"):
        read(raw, native=True, limit=512)
    reports = []
    raw = chunk({"tool_calls": [{"index": 1}]}, "tool_calls") + b"data: [DONE]\n\n"
    with pytest.raises(ModelStreamError, match="provider_stream_invalid"):
        read_chat_stream(io.BytesIO(raw), 4096, lambda **v: reports.append(v), allow_tool_calls=True)
    assert reports[-1]["stream_error"]["branch"] == "tool_index_sequence_invalid"
    assert reports[-1]["finish_seen"] and reports[-1]["done_seen"]


def test_changed_identity_in_second_call_has_its_actual_field_path():
    reports = []
    raw = (chunk({"tool_calls": [{"index": 0, "id": "first"}, {"index": 1, "id": "second"}]}) +
        chunk({"tool_calls": [{"index": 0}, {"index": 1, "id": "changed"}]}))
    with pytest.raises(ModelStreamError, match="provider_stream_invalid"):
        read_chat_stream(io.BytesIO(raw), 4096, lambda **v: reports.append(v), allow_tool_calls=True)
    assert reports[-1]["stream_error"]["branch"] == "tool_id_changed"
    assert reports[-1]["stream_error"]["field_path"] == "$.choices[0].delta.tool_calls[1].id"


def test_native_arguments_remain_bounded_and_incomplete_stream_is_not_valid():
    raw = chunk({"tool_calls": [{"index": 0, "function": {"arguments": "x" * 4097}}]})
    with pytest.raises(ModelStreamError, match="response_rejected"): read(raw, native=True)
    raw = chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, "tool_calls")
    with pytest.raises(ModelStreamError, match="http_transport_failure"): read(raw, native=True)


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
    raw = (chunk({"tool_calls": [
        {"index": i, "id": f"call_{i}", "type": "function",
         "function": {"name": "submit_stage_result", "arguments": '{"result":{}}'}} for i in range(2)]}) +
        chunk({}, "tool_calls", usage={"prompt_tokens": 42, "completion_tokens": 12}) + b"data: [DONE]\n\n")
    pipe = Pipe(); deepseek_roles._raw_http_worker(pipe, settings, {"stream": True, "tools": [{}]}, 5)
    assert len(pipe.messages[-1]["payload"]["tool_calls"]) == 2
    assert pipe.messages[-1]["payload"]["finish_reason"] == "tool_calls"
    assert any(m.get("diagnostic", {}).get("done_seen") for m in pipe.messages)
    assert any(m.get("diagnostic", {}).get("usage", {}).get("completion_tokens") == 12
               for m in pipe.messages if m.get("diagnostic", {}).get("usage"))
    raw = (chunk({"reasoning_content": "private thought"}) +
        chunk({"tool_calls": [{"index": 0, "function": {"name": None}}]}) +
        chunk({}, "tool_calls") + b"data: [DONE]\n\n")
    pipe = Pipe(); deepseek_roles._raw_http_worker(pipe, settings, {"stream": True, "tools": [{}]}, 5)
    assert pipe.messages[-1] == {"error": "provider_failure", "reason": "provider_stream_invalid"}
    diagnostic = {}
    for message in pipe.messages:
        diagnostic.update(message.get("diagnostic", {}))
    assert diagnostic["stream_error"] == {"branch": "tool_function_field_type", "event_index": 2,
        "field_path": "$.choices[0].delta.tool_calls[0].function.name", "observed_type": "null"}
    assert not diagnostic["finish_seen"] and not diagnostic["done_seen"]
    assert "private thought" not in repr(pipe.messages)


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
