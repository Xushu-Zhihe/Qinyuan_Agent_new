"""D7 request receipt status CLI selection contract."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli
from agent_adapters.v7.request_staging import D7RequestStager
from agent_adapters.run_files import immutable_write, run_path


class _NoopProtection:
    def restrict_directory(self, _path):
        pass

    def verify_directory(self, _path):
        pass


@pytest.fixture(autouse=True)
def _consistent_test_protection(monkeypatch):
    # These tests isolate the public receipt/status contract.  Both writer and
    # reader must use the same protection boundary; the platform ACL contract
    # is verified separately rather than weakening the production reader.
    monkeypatch.setattr(
        "agent_adapters.d7_request_staging.WindowsEvidenceProtection",
        _NoopProtection,
    )


def _staged_request(tmp_path, *, task_run_id=None, rejected=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_bytes(b"private evidence body")
    state_dir = tmp_path / "state"
    request_id = str(uuid4())
    stager = D7RequestStager(
        state_dir, clock=lambda: 21.0, protection=_NoopProtection()
    )
    stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "private problem statement"},
    )
    if task_run_id is not None:
        stager.record_outcome(
            request_id, state="task_run_created", task_run_id=task_run_id
        )
    elif rejected:
        stager.record_outcome(
            request_id,
            state="capability_not_supported",
            reason="attachment_count_unsupported",
        )
    return state_dir, request_id, stager


@pytest.mark.parametrize(
    ("selector", "value"),
    [("--task-run-id", "task-id"), ("--request-id", "request-id")],
)
def test_status_requires_one_supported_id_selector(selector, value):
    args = cli.parser().parse_args(
        ["status", selector, value, "--state-dir", "state"]
    )

    assert args.command == "status"
    assert args.task_run_id == (value if selector == "--task-run-id" else None)
    assert args.request_id == (value if selector == "--request-id" else None)
    assert args.state_dir == "state"


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--state-dir", "state"],
        [
            "status",
            "--task-run-id",
            "task-id",
            "--request-id",
            "request-id",
            "--state-dir",
            "state",
        ],
    ],
)
def test_status_rejects_missing_or_ambiguous_id_selector(argv):
    with pytest.raises(ValueError, match="invalid_cli_arguments"):
        cli.parser().parse_args(argv)


def test_request_id_status_returns_safe_projection_and_operation_exit(tmp_path):
    state_dir, request_id, _stager = _staged_request(tmp_path, rejected=True)
    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result["protocol_version"] == 3
    assert result["request_id"] == request_id
    assert result["exit_code"] == 0
    assert result["request_receipt"]["request_exit_code"] == 2
    assert result["request_receipt"]["state"] == "capability_not_supported"
    assert result["request_receipt"]["attachments"] == [
        {
            "index": 0,
            "name": "evidence.txt",
            "byte_size": len(b"private evidence body"),
            "sha256": result["request_receipt"]["attachments"][0]["sha256"],
        }
    ]
    assert "run_exit_code" not in result
    serialized = cli.encode(result).decode("ascii")
    assert "request_sha256" not in serialized
    assert "request_identity_sha256" not in serialized
    assert "private problem statement" not in serialized
    assert "private evidence body" not in serialized
    assert str(tmp_path / "source") not in serialized


def test_missing_request_receipt_is_read_only_and_reports_query_exit(tmp_path):
    state_dir = tmp_path / "missing-state"
    request_id = str(uuid4())

    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result == {
        "protocol_version": 3,
        "request_id": request_id,
        "errors": [{"code": "request_receipt_not_found"}],
        "exit_code": 2,
    }
    assert not state_dir.exists()


def test_invalid_request_id_is_rejected_before_opening_storage(monkeypatch, tmp_path):
    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("invalid request id reached storage")

    monkeypatch.setattr(D7RequestStager, "open_existing", unexpected_open)
    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id="not-a-uuid", state_dir=str(tmp_path / "state")
        )
    )

    assert result["errors"] == [{"code": "request_id_invalid"}]
    assert result["exit_code"] == 2
    assert not (tmp_path / "state").exists()


def test_request_status_reads_linked_v3_result_without_changing_run(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    state_dir, request_id, _stager = _staged_request(
        tmp_path, task_run_id=task_run_id
    )
    directory = run_path(state_dir, task_run_id)
    directory.mkdir(parents=True)
    declaration = {
        "protocol_version": 3,
        "task_run_id": task_run_id,
        "request_id": request_id,
    }
    immutable_write(directory / "run.json", declaration)
    before = (directory / "run.json").read_bytes()
    monkeypatch.setattr(
        cli,
        "_snapshot_v3",
        lambda _directory, _declaration: {
            "protocol_version": 3,
            "task_run_id": task_run_id,
            "task_run": {"status": "awaiting_external_feedback"},
            "goal_evaluation": "not_evaluated",
            "overall_goal_summary": {"schema_version": 1, "outcome": "pending"},
            "cleanup": {"status": "not_confirmed"},
            "cleanup_status": "unconfirmed",
            "recovery": {"required": False},
            "recovery_status": "not_required",
            "errors": [],
        },
    )

    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result["exit_code"] == 0
    assert result["run_result"] == {
        "task_run_id": task_run_id,
        "run_status": "awaiting_external_feedback",
        "goal_evaluation": "not_evaluated",
        "overall_goal_summary": {"schema_version": 1, "outcome": "pending"},
        "cleanup_status": "unconfirmed",
        "recovery_status": "not_required",
    }
    assert "run_exit_code" not in result
    assert (directory / "run.json").read_bytes() == before


def test_request_status_nests_only_terminal_run_exit_code(tmp_path, monkeypatch):
    task_run_id = str(uuid4())
    state_dir, request_id, _stager = _staged_request(
        tmp_path, task_run_id=task_run_id
    )
    directory = run_path(state_dir, task_run_id)
    directory.mkdir(parents=True)
    immutable_write(
        directory / "run.json",
        {
            "protocol_version": 3,
            "task_run_id": task_run_id,
            "request_id": request_id,
        },
    )
    monkeypatch.setattr(
        cli,
        "_snapshot_v3",
        lambda *_: {
            "task_run_id": task_run_id,
            "task_run": {"status": "achieved"},
            "goal_evaluation": "accepted",
            "overall_goal_summary": {
                "schema_version": 1,
                "outcome": "accepted",
                "candidate_count": 1,
                "private_body": "must not be projected",
            },
            "cleanup_status": "confirmed",
            "recovery_status": "not_required",
            "run_exit_code": 0,
            "errors": [],
        },
    )

    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result["exit_code"] == 0
    assert "run_exit_code" not in result
    assert result["run_result"] == {
        "task_run_id": task_run_id,
        "run_status": "achieved",
        "goal_evaluation": "accepted",
        "overall_goal_summary": {
            "schema_version": 1,
            "outcome": "accepted",
            "candidate_count": 1,
        },
        "cleanup_status": "confirmed",
        "recovery_status": "not_required",
        "run_exit_code": 0,
    }


@pytest.mark.parametrize("failure", ["missing_run", "corrupt_snapshot"])
def test_unavailable_linked_run_preserves_receipt_and_returns_operation_five(
    failure, tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    state_dir, request_id, _stager = _staged_request(
        tmp_path, task_run_id=task_run_id
    )
    if failure == "corrupt_snapshot":
        directory = run_path(state_dir, task_run_id)
        directory.mkdir(parents=True)
        immutable_write(
            directory / "run.json",
            {
                "protocol_version": 3,
                "task_run_id": task_run_id,
                "request_id": request_id,
            },
        )
        monkeypatch.setattr(
            cli,
            "_snapshot_v3",
            lambda *_: {
                "task_run_id": task_run_id,
                "run_exit_code": 5,
                "errors": [{"code": "state_read_failed"}],
            },
        )

    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result["protocol_version"] == 3
    assert result["request_receipt"]["task_run_id"] == task_run_id
    assert result["errors"] == [{"code": "run_result_unavailable"}]
    assert result["exit_code"] == 5
    assert "run_exit_code" not in result


def test_request_status_rejects_run_bound_to_another_request(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    state_dir, request_id, _stager = _staged_request(
        tmp_path, task_run_id=task_run_id
    )
    directory = run_path(state_dir, task_run_id)
    directory.mkdir(parents=True)
    immutable_write(
        directory / "run.json",
        {
            "protocol_version": 3,
            "task_run_id": task_run_id,
            "request_id": str(uuid4()),
        },
    )
    monkeypatch.setattr(
        cli,
        "_snapshot_v3",
        lambda *_: pytest.fail("misbound run must be rejected before snapshot"),
    )

    result = cli.dispatch(
        SimpleNamespace(
            command="status", request_id=request_id, state_dir=str(state_dir)
        )
    )

    assert result["protocol_version"] == 3
    assert result["request_id"] == request_id
    assert result["request_receipt"]["task_run_id"] == task_run_id
    assert result["errors"] == [{"code": "run_result_unavailable"}]
    assert result["exit_code"] == 5
    assert "run_exit_code" not in result
