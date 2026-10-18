"""CLI boundary tests for protected D7 candidate viewing."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli

def test_candidate_read_parser_requires_exact_selector_and_version():
    args = cli.parser().parse_args(
        [
            "candidate-read",
            "--state-dir",
            "state",
            "--task-run-id",
            str(uuid4()),
            "--candidate-id",
            str(uuid4()),
            "--evidence-version",
            "a" * 64,
        ]
    )
    assert args.command == "candidate-read"
    assert args.run_dir is None

    with pytest.raises(ValueError, match="invalid_cli_arguments"):
        cli.parser().parse_args(
            [
                "candidate-read",
                "--state-dir",
                "state",
                "--run-dir",
                "run",
                "--task-run-id",
                str(uuid4()),
                "--candidate-id",
                str(uuid4()),
                "--evidence-version",
                "a" * 64,
            ]
        )


def test_candidate_read_rejects_non_v3_before_service_use(tmp_path, monkeypatch):
    task_run_id = str(uuid4())
    run_dir = tmp_path / task_run_id
    run_dir.mkdir()
    (run_dir / "run.json").write_bytes(b"not-used")
    monkeypatch.setattr(cli, "immutable_read", lambda _path: {
        "task_run_id": task_run_id,
        "protocol_version": 2,
    })
    service_created = []

    class Service:
        def __init__(self, *args, **kwargs):
            service_created.append((args, kwargs))

    import agent_adapters.web.candidate_evidence as evidence

    monkeypatch.setattr(evidence, "D7CandidateEvidenceService", Service)
    args = SimpleNamespace(
        command="candidate-read",
        run_dir=str(run_dir),
        state_dir=None,
        task_run_id=task_run_id,
        candidate_id=str(uuid4()),
        evidence_version="a" * 64,
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True)
    with pytest.raises(ValueError, match="d7_candidate_run_declaration_invalid"):
        cli.dispatch(args)
    assert service_created == []


def test_candidate_read_passes_version_and_keeps_body_out_of_result(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    candidate_id = str(uuid4())
    version = "b" * 64
    run_dir = tmp_path / task_run_id
    run_dir.mkdir()
    (run_dir / "run.json").write_bytes(b"immutable")
    # Candidate reads require an owned control ledger before opening evidence.
    (run_dir / "control.sqlite").write_bytes(b"placeholder")
    monkeypatch.setattr(cli, "immutable_read", lambda _path: {
        "task_run_id": task_run_id,
        "protocol_version": 3,
    })

    class Ledger:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def list_task_run_refs(self):
            return (task_run_id,)

        def load_task_run(self, _task_run_id):
            return object()

    import agent_core.sqlite_control_store as sqlite_store

    monkeypatch.setattr(sqlite_store.SqliteControlStore, "open_readonly", lambda _path: Ledger())

    class Service:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Viewer:
        def __init__(self, service):
            self.service = service

        def view(self, candidate, *, evidence_version, stdin, stdout):
            assert candidate == candidate_id
            assert evidence_version == version
            assert stdin is cli.sys.stdin
            assert stdout is cli.sys.stderr
            return SimpleNamespace(
                to_dict=lambda: {
                    "task_run_id": task_run_id,
                    "candidate_id": candidate_id,
                    "evidence_version": version,
                    "status": "viewed",
                }
            )

    import agent_adapters.web.candidate_evidence as evidence
    import agent_adapters.web.candidate_operator_view as operator_view

    monkeypatch.setattr(evidence, "D7CandidateEvidenceService", Service)
    monkeypatch.setattr(operator_view, "D7CandidateOperatorViewer", Viewer)
    # Replace the process streams instead of patching ``sys.stderr.encoding``;
    # the Windows stream exposes encoding as a read-only property.
    class _Terminal:
        encoding = "utf-8"

        def __init__(self):
            self.buffer = self

        def isatty(self):
            return True

        def write(self, value):
            return len(value)

        def flush(self):
            return None

        def fileno(self):
            return 1

        def readline(self, _size=-1):
            return candidate_id + "\n"

    terminal = _Terminal()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
    ))
    args = SimpleNamespace(
        command="candidate-read",
        run_dir=str(run_dir),
        state_dir=None,
        task_run_id=task_run_id,
        candidate_id=candidate_id,
        evidence_version=version,
    )
    result = cli.dispatch(args)
    assert result["exit_code"] == 0
    assert "body" not in result

