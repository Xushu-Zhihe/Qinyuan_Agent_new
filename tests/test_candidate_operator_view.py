"""Explicit, terminal-only viewing of protected D7 candidate bodies."""

from __future__ import annotations

import hashlib
import io
import json
from uuid import uuid4

import pytest

from agent_adapters.web import candidate_operator_view as operator_view
from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_adapters.web.candidate_operator_view import (
    OPERATOR_CANDIDATE_VIEW_ADAPTER_ID,
    D7CandidateOperatorViewer,
    D7CandidateOperatorViewError,
)


class _TerminalInput(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class _TerminalOutput:
    encoding = "utf-8"

    def __init__(self, *, tty: bool = True) -> None:
        self.buffer = io.BytesIO()
        self.text = io.StringIO()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        return 1

    def write(self, value: str) -> int:
        return self.text.write(value)

    def flush(self) -> None:
        self.text.flush()


class _ReadPort:
    def __init__(self, service, adapter_id: str) -> None:
        self.service = service
        self.adapter_id = adapter_id

    def read(self, candidate_id: str) -> str:
        self.service.calls.append(("read", candidate_id))
        return self.service.body


class _Service(D7CandidateEvidenceService):
    def __init__(self, task_run_id: str, candidate_id: str, body: str) -> None:
        self.task_run_id = task_run_id
        self.candidate_id = candidate_id
        self.body = body
        self.calls = []
        self.binding = {
            "task_run_id": task_run_id,
            "candidate_id": candidate_id,
            "execution_id": str(uuid4()),
            "evidence_version": "b" * 64,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "byte_size": len(body.encode("utf-8")),
            "created_at": 12.0,
            "body_status": "withheld",
            "body_ref": f"d7-candidate:{candidate_id}:{'b' * 64}",
            "body": None,
        }

    def query(self, candidate_id: str) -> dict[str, object]:
        self.calls.append(("query", candidate_id))
        return self.binding

    def read_port(self, adapter_id: str) -> _ReadPort:
        self.calls.append(("port", adapter_id))
        return _ReadPort(self, adapter_id)


@pytest.fixture
def saved_candidate(monkeypatch):
    monkeypatch.setattr(operator_view, "IS_WINDOWS", False)
    candidate_id = str(uuid4())
    value = " 旗標：D7 Ω\nline two"
    service = _Service(str(uuid4()), candidate_id, value)
    return service, candidate_id, value


def test_operator_view_confirms_exact_id_then_writes_exact_body_and_audits(
    saved_candidate,
):
    service, candidate_id, value = saved_candidate
    stdin = _TerminalInput(candidate_id + "\n")
    stdout = _TerminalOutput()

    result = D7CandidateOperatorViewer(service).view(
        candidate_id,
        evidence_version=service.binding["evidence_version"],
        stdin=stdin,
        stdout=stdout,
    )

    assert stdout.text.getvalue() == (
        "Protected candidate binding\n"
        f"TaskRun: {service.task_run_id}\n"
        f"Candidate: {candidate_id}\n"
        f"Execution: {service.binding['execution_id']}\n"
        f"Evidence version: {service.binding['evidence_version']}\n"
        f"Body SHA-256: {hashlib.sha256(value.encode('utf-8')).hexdigest()}\n"
        f"UTF-8 bytes: {len(value.encode('utf-8'))}\n"
        "Enter the exact candidate ID to display its body: "
    )
    assert stdout.buffer.getvalue() == value.encode("utf-8")
    assert result.to_dict() == {
        "task_run_id": service.task_run_id,
        "candidate_id": candidate_id,
        "evidence_version": service.binding["evidence_version"],
        "status": "viewed",
    }
    assert value not in json.dumps(result.to_dict(), ensure_ascii=False)
    assert service.calls == [
        ("query", candidate_id),
        ("port", OPERATOR_CANDIDATE_VIEW_ADAPTER_ID),
        ("read", candidate_id),
    ]


@pytest.mark.parametrize("stream_name", ["stdin", "stdout"])
def test_noninteractive_stream_is_rejected_before_query_or_decryption(
    saved_candidate, monkeypatch, stream_name
):
    service, candidate_id, _value = saved_candidate
    query_calls = []
    monkeypatch.setattr(
        service,
        "query",
        lambda *_args: query_calls.append("query"),
    )
    stdin = _TerminalInput(candidate_id + "\n")
    stdout = _TerminalOutput(tty=stream_name != "stdout")
    if stream_name == "stdin":
        stdin.isatty = lambda: False

    with pytest.raises(
        D7CandidateOperatorViewError,
        match="^d7_candidate_view_requires_terminal$",
    ):
        D7CandidateOperatorViewer(service).view(
            candidate_id,
            evidence_version=service.binding["evidence_version"],
            stdin=stdin,
            stdout=stdout,
        )

    assert query_calls == []
    assert stdout.buffer.getvalue() == b""


def test_windows_pipe_or_redirection_is_rejected_before_query(
    saved_candidate, monkeypatch
):
    service, candidate_id, _value = saved_candidate
    query_calls = []
    monkeypatch.setattr(
        service,
        "query",
        lambda *_args: query_calls.append("query"),
    )
    monkeypatch.setattr(operator_view, "IS_WINDOWS", True)
    checks = []

    def console_handle(stream):
        checks.append(stream)
        return len(checks) == 1

    monkeypatch.setattr(
        operator_view,
        "_windows_console_handle",
        console_handle,
    )
    stdout = _TerminalOutput()

    with pytest.raises(
        D7CandidateOperatorViewError,
        match="^d7_candidate_view_requires_terminal$",
    ):
        D7CandidateOperatorViewer(service).view(
            candidate_id,
            evidence_version=service.binding["evidence_version"],
            stdin=_TerminalInput(candidate_id + "\n"),
            stdout=stdout,
        )

    assert len(checks) == 2
    assert query_calls == []
    assert stdout.text.getvalue() == ""


def test_wrong_confirmation_never_reads_or_decrypts_candidate(
    saved_candidate, monkeypatch
):
    service, candidate_id, _value = saved_candidate
    stdout = _TerminalOutput()

    with pytest.raises(
        D7CandidateOperatorViewError,
        match="^d7_candidate_view_confirmation_rejected$",
    ):
        D7CandidateOperatorViewer(service).view(
            candidate_id,
            evidence_version=service.binding["evidence_version"],
            stdin=_TerminalInput(candidate_id.upper() + "\n"),
            stdout=stdout,
        )

    assert stdout.buffer.getvalue() == b""
    assert service.calls == [("query", candidate_id)]
