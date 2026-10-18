from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_adapters.deepseek_model import DeepSeekSettings
from agent_adapters.run_files import (
    immutable_read,
    immutable_write,
    parse_json,
    run_lock,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "ascii.txt"
    source.write_bytes(b"harmless ASCII attachment\n")
    value = {
        "protocol_version": 1,
        "task_request": {
            "goal": "find flag",
            "target": {
                "attachments": [
                    {"kind": "local_path", "path": source.relative_to(ROOT).as_posix()}
                ]
            },
            "deadline_seconds": 120,
        },
        "intent": {
            "description": "identify file type",
            "attachment_index": 0,
            "sandbox_path": "/workspace/input/test.txt",
        },
        "authorization": {
            "allowed_tools": ["file.inspect_file"],
            "allowed_paths": ["/workspace/input/test.txt"],
        },
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(value))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"stdout": "ASCII text\n"}))
    return tmp_path, value, request, fixture


def command(inputs, **fixture_overrides):
    root, _value, request, fixture = inputs
    fixture.write_text(json.dumps({"stdout": "ASCII text\n", **fixture_overrides}))
    return [
        sys.executable,
        str(ROOT / "agent.py"),
        "run-intent",
        "--mode",
        "simulated",
        "--request",
        str(request),
        "--fixture",
        str(fixture),
        "--state-dir",
        str(root / "runs"),
    ]


def call(args, stdin=None):
    completed = subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, (completed.stdout, completed.stderr)
    value = json.loads(lines[0])
    assert completed.returncode == value["exit_code"]
    assert not completed.stderr
    return value


def followup(inputs, result, operation, *args):
    return call(
        [
            sys.executable,
            str(ROOT / "agent.py"),
            operation,
            "--state-dir",
            str(inputs[0] / "runs"),
            "--task-run-id",
            result["task_run_id"],
            *args,
        ]
    )


def test_success_events_and_readonly_terminal_replay(inputs):
    events = inputs[0] / "events.jsonl"
    result = call(command(inputs) + ["--events", str(events)])
    assert result["exit_code"] == 0
    assert result["intent"]["status"] == "succeeded"
    assert result["intent"]["evaluation"]["outcome"] == "satisfied"
    assert len(result["intent"]["facts"]) == 1
    assert result["task_run"]["status"] == "inconclusive"
    assert result["task_run"]["reason"] == "single_intent_slice_finished"
    assert result["overall_goal"] == {"outcome": "not_evaluated"}
    assert result["cleanup"]["status"] == "succeeded"
    directory = inputs[0] / "runs" / result["task_run_id"]
    before = {
        str(p.relative_to(directory)): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }
    replay = followup(inputs, result, "replay")
    assert replay["events"] == [
        json.loads(line) for line in events.read_text().splitlines()
    ]
    assert replay["events"][-1]["details"]["reason"] == "single_intent_slice_finished"
    for operation in ("status", "resume", "cancel"):
        other = followup(inputs, result, operation)
        assert other["task_run"] == result["task_run"]
        assert other["intent"] == result["intent"]
    after = {
        str(p.relative_to(directory)): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }
    assert after == before
    assert not list((directory / "simulated-sandbox").iterdir())


def test_stdin_and_stopped_slice_is_not_intent_success(inputs):
    args = command(inputs, model_stop=True)
    args[args.index("--request") + 1] = "-"
    result = call(args, json.dumps(inputs[1]))
    assert result["exit_code"] == 0
    assert result["intent"]["status"] == "running"
    assert result["intent"]["slice_result"]["stop_reason"] == "model_stopped"
    assert result["intent"]["facts"] == []


def test_bad_authorization_has_no_run_or_side_effect(inputs):
    args = command(inputs)
    value = inputs[1]
    value["authorization"]["allowed_paths"] = ["/workspace/input/other.txt"]
    inputs[2].write_text(json.dumps(value))
    result = call(args)
    assert result["exit_code"] == 2
    assert not (inputs[0] / "runs").exists()


def test_missing_source_rejected_before_run(inputs):
    args = command(inputs)
    (ROOT / inputs[1]["task_request"]["target"]["attachments"][0]["path"]).unlink()
    assert call(args)["exit_code"] == 2
    assert not (inputs[0] / "runs").exists()


def test_unknown_tool_result_is_never_reexecuted(inputs):
    result = call(command(inputs, exit_code=43))
    assert result["exit_code"] == 4
    assert result["recovery"]["tool_result_unknown"]
    assert result["intent"]["slice_result"]["stop_reason"] == "recovery_required"
    directory = inputs[0] / "runs" / result["task_run_id"]
    before = {p.name: p.read_bytes() for p in (directory / "tools").glob("*.json")}
    for _ in range(2):
        again = followup(inputs, result, "resume")
        assert again["exit_code"] == 4
    assert {
        p.name: p.read_bytes() for p in (directory / "tools").glob("*.json")
    } == before
    cancelled = followup(inputs, result, "cancel")
    assert cancelled["exit_code"] == 4
    assert cancelled["cleanup"]["status"] == "succeeded"


def test_deadline_committed_and_cleanup_confirmed(inputs):
    inputs[1]["task_request"]["deadline_seconds"] = 1
    inputs[2].write_text(json.dumps(inputs[1]))
    result = call(command(inputs, model_delay_seconds=30))
    assert result["exit_code"] == 3
    assert result["task_run"]["status"] == "deadline_exceeded"
    assert result["cleanup"]["status"] == "succeeded"


def test_export_failure_preserves_committed_result(inputs):
    events = inputs[0] / "occupied.jsonl"
    events.write_text("keep")
    result = call(command(inputs) + ["--events", str(events)])
    assert result["exit_code"] == 3
    assert result["intent"]["status"] == "succeeded"
    assert result["cleanup"]["status"] == "succeeded"
    assert events.read_text() == "keep"
    assert followup(inputs, result, "replay")["events"]


def test_cleanup_failure_preserves_terminal_and_readonly_resume(inputs):
    result = call(command(inputs, cleanup_failure=True, model_stop=True))
    assert result["exit_code"] == 5
    assert result["task_run"]["status"] == "inconclusive"
    directory = inputs[0] / "runs" / result["task_run_id"]
    attempts = list(directory.glob("cleanup-attempt-*"))
    assert len(attempts) == 1
    again = followup(inputs, result, "resume")
    assert again["exit_code"] == 5
    assert list(directory.glob("cleanup-attempt-*")) == attempts
    followup(inputs, result, "resume", "--cleanup-only")
    assert len(list(directory.glob("cleanup-attempt-*"))) == 2


def wait_for_model(root, process):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        for directory in (root / "runs").glob("*") if (root / "runs").exists() else ():
            database = directory / "control.sqlite"
            if database.exists():
                try:
                    with sqlite3.connect(database) as connection:
                        if connection.execute(
                            "SELECT COUNT(*) FROM model_invocations"
                        ).fetchone()[0]:
                            return directory
                except sqlite3.OperationalError:
                    pass
        assert process.poll() is None
        time.sleep(0.025)
    pytest.fail("model invocation not started")


@pytest.mark.parametrize("cleanup_delay", [0, 20])
def test_ctrl_c_process_exit_under_five_seconds(inputs, cleanup_delay):
    args = command(inputs, model_delay_seconds=60, cleanup_delay_seconds=cleanup_delay)
    process = subprocess.Popen(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        directory = wait_for_model(inputs[0], process)
        start = time.monotonic()
        process.send_signal(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        )
        stdout, stderr = process.communicate(timeout=5)
        assert time.monotonic() - start < 5
        result = json.loads(stdout)
        assert result["exit_code"] == (5 if cleanup_delay else 130), result
        assert result["task_run"]["status"] == "cancelled"
        assert not result["intent"]["facts"]
        assert not stderr
        with sqlite3.connect(directory / "control.sqlite") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_external_cancel_and_writer_lock(inputs):
    process = subprocess.Popen(
        command(inputs, model_delay_seconds=60),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        directory = wait_for_model(inputs[0], process)
        with pytest.raises(ValueError, match="task_run_busy"), run_lock(directory):
            pytest.fail("second writer acquired lock")
        result = followup(inputs, {"task_run_id": directory.name}, "cancel")
        assert result["exit_code"] == 130
        stdout, stderr = process.communicate(timeout=5)
        assert json.loads(stdout)["exit_code"] == 130
        assert not stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://proxy.example",
        "https://api.deepseek.com.evil",
        "https://user@api.deepseek.com",
        "https://api.deepseek.com/?x=1",
    ],
)
def test_deepseek_rejects_nonofficial_configuration(url):
    with pytest.raises(ValueError):
        DeepSeekSettings("deepseek-v4-flash", "secret", url)


def test_strict_json_and_immutable_declarations(tmp_path):
    for data in (b'{"a":1,"a":2}', b'{"a":NaN}'):
        with pytest.raises(ValueError):
            parse_json(data)
    path = tmp_path / "run.json"
    immutable_write(path, {"a": 1})
    immutable_write(path, {"a": 1})
    with pytest.raises(ValueError):
        immutable_write(path, {"a": 2})
    assert immutable_read(path) == {"a": 1}
