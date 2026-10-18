from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.file_tool_recorder import EvidenceStorageError, FileToolRecorder
from agent_core.tool_contracts import (
    ExecutionRecord,
    ToolAuthorization,
    ToolRequest,
    ToolResult,
)


@pytest.fixture
def recorder_directory() -> Path:
    runtime = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    directory = runtime / f"file-tool-receipt-{uuid4()}"
    yield directory
    if directory.exists():
        assert directory.resolve().is_relative_to(runtime.resolve())
        shutil.rmtree(directory)


def test_read_only_receipt_and_completion_check_inner_fingerprint(
    recorder_directory: Path,
) -> None:
    recorder = FileToolRecorder(recorder_directory)
    request_id = str(uuid4())
    request = ToolRequest(
        request_id,
        "task-1",
        "plan-1",
        0,
        "file.inspect_file",
        "/workspace/input/sample.txt",
    )
    authorization = ToolAuthorization(
        "auth-1", "task-1", ("/workspace/input/sample.txt",)
    )
    assert recorder.begin(request, authorization, "sandbox-1") is None
    stored = recorder.read_request_receipt(request_id)
    assert stored.request == request
    assert stored.authorization == authorization
    assert stored.sandbox_id == "sandbox-1"
    assert recorder.read_completed(request_id) is None

    record = ExecutionRecord(
        request_id,
        "task-1",
        "sandbox-1",
        "plan-1",
        0,
        "auth-1",
        request_id,
        "command-1",
        request_id,
        1.0,
        2.0,
        0,
        None,
        "",
        stdout="ASCII text\n",
        termination_confirmed=True,
    )
    recorder.save_record(record)
    recorder.finish(ToolResult(record))
    assert recorder.read_completed(request_id).record == record

    path = recorder_directory / f"receipt-{request_id}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["fingerprint"] = "0" * 64
    import hashlib

    payload = json.dumps(
        envelope["payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    envelope["sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(EvidenceStorageError, match="invalid request receipt"):
        recorder.read_request_receipt(request_id)
