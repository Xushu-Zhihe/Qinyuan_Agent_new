from __future__ import annotations

import json
import multiprocessing
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters import deepseek_model
from agent_adapters.deepseek_model import (
    DeepSeekModelAdapter,
    DeepSeekSettings,
    build_payload,
)
from agent_adapters.event_export import EventExporter
from agent_core.contracts import Target
from agent_core.file_inspection import LEGACY_FILE_INSPECTION_SCRIPT
from agent_core.modeling import (
    IntentContext,
    ModelErrorKind,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
)
from agent_core.tool_contracts import MCPCommand, ToolRequest
from agent_core.tool_policy import FileToolPolicy


def request():
    return ModelRequest(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        ModelRequestType.PLAN_GENERATION,
        "find flag",
        Target.from_dict(
            {"attachments": [{"kind": "local_path", "path": "fixture.txt"}]}
        ),
        "inspect file",
        (),
        (),
        IntentContext(str(uuid4()), "identify file type"),
    )


class Token:
    def __init__(self):
        self.event = threading.Event()

    @property
    def is_cancelled(self):
        return self.event.is_set()


class Deadline:
    def remaining_seconds(self):
        return 10


class Evidence:
    def exists(self, task_id, reference):
        return False


def structured_worker(pipe, settings, payload, timeout):
    context = json.loads(payload["messages"][1]["content"])
    if context["correction_of_model_request_id"]:
        pipe.send({"payload": {"steps": [{"description": "inspect file"}]}})
    else:
        pipe.send({"payload": {"wrong": "shape"}})


def slow_model(pipe, settings, payload, timeout):
    time.sleep(30)
    pipe.send({"payload": {"steps": [{"description": "late answer"}]}})


def slow_writer(pipe, path, database, task_id, finish):
    pipe.send({"ready": True})
    pipe.send({"writing_at": time.monotonic()})
    time.sleep(30)
    Path(path).write_text("late write")


def test_correction_uses_existing_coordinator_and_json_without_tools():
    settings = DeepSeekSettings("deepseek-v4-flash", "test-key")
    r = request()
    payload = build_payload(r, settings)
    assert "tools" not in payload
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    adapter = DeepSeekModelAdapter(settings, worker=structured_worker)
    result = ModelInvocationCoordinator(adapter, Deadline(), Evidence()).invoke(
        r, Token()
    )
    assert result.candidate.steps[0].description == "inspect file"
    assert "test-key" not in repr(settings)


@pytest.mark.parametrize("cancel", [False, True])
def test_model_wait_is_killable_with_no_late_child(cancel):
    settings = DeepSeekSettings("deepseek-v4-flash", "test-key", timeout_seconds=0.8)
    adapter = DeepSeekModelAdapter(settings, worker=slow_model)
    token = Token()
    timer = threading.Timer(0.2, token.event.set)
    if cancel:
        timer.start()
    before = {p.pid for p in multiprocessing.active_children()}
    start = time.monotonic()
    result = adapter.invoke(request(), 0.8, token)
    assert time.monotonic() - start < 2
    assert result.kind is (
        ModelErrorKind.CANCELLED if cancel else ModelErrorKind.TIMED_OUT
    )
    assert {p.pid for p in multiprocessing.active_children()} == before
    if cancel:
        timer.join()


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "provider_failure"),
        (500, "provider_failure"),
        (503, "provider_failure"),
        (504, "timed_out"),
    ],
)
def test_http_errors_are_sanitized_and_not_retried(monkeypatch, status, kind):
    calls = []

    class FakeHTTP:
        sock = None

        def __init__(self, host, timeout):
            calls.append((host, timeout))
            self.sock = self

        def connect(self):
            pass

        def settimeout(self, timeout):
            assert timeout <= 10

        def request(self, method, path, **kwargs):
            assert method == "POST" and path == "/chat/completions"
            assert "tools" not in json.loads(kwargs["body"])

        def getresponse(self):
            return type("Response", (), {"status": status})()

        def close(self):
            pass

    class Pipe:
        def __init__(self):
            self.values = []

        def send(self, value):
            self.values.append(value)

        def close(self):
            pass

    monkeypatch.setattr(deepseek_model.http.client, "HTTPSConnection", FakeHTTP)
    monkeypatch.setattr(deepseek_model.signal, "signal", lambda *args: None)
    pipe = Pipe()
    settings = DeepSeekSettings("deepseek-v4-flash", "secret-value")
    deepseek_model._http_worker(pipe, settings, build_payload(request(), settings), 10)
    assert len(calls) == 1
    assert pipe.values == [
        {"connected": True},
        {"error": kind, "reason": f"deepseek_http_{status}"},
    ]
    assert "secret-value" not in json.dumps(pipe.values)


def test_event_timeout_terminates_writer_before_late_write(tmp_path):
    output = tmp_path / "events.jsonl"
    exporter = EventExporter(
        output, tmp_path / "empty.sqlite", str(uuid4()), worker=slow_writer
    )
    try:
        end = time.monotonic() + 4
        while not exporter.error and time.monotonic() < end:
            exporter.tick()
            time.sleep(0.01)
        assert exporter.error == "event_export_timeout"
        assert exporter.closed
        assert not output.exists()
    finally:
        exporter.close()


def test_connect_phase_is_bounded_even_if_dns_blocks(monkeypatch):
    monkeypatch.setattr(deepseek_model, "CONNECT_TIMEOUT_SECONDS", 0.4)
    adapter = DeepSeekModelAdapter(
        DeepSeekSettings("deepseek-v4-flash", "test-key"), worker=slow_model
    )
    started = time.monotonic()
    result = adapter.invoke(request(), 20, Token())
    assert time.monotonic() - started < 2
    assert result.kind is ModelErrorKind.TIMED_OUT
    assert result.short_reason == "model_connect_timeout"


def test_legacy_file_program_remains_evidence_only():
    policy = FileToolPolicy()
    r = ToolRequest(
        str(uuid4()),
        "task",
        "plan",
        0,
        "file.inspect_file",
        "/workspace/input/test.txt",
    )
    legacy = MCPCommand(
        "python3", ("-I", "-c", LEGACY_FILE_INSPECTION_SCRIPT, r.path), 30
    )
    assert policy.validate_recorded_command(r, legacy).allowed
    assert not policy.validate_command(r, legacy).allowed
