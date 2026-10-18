"""Security regressions across the D5 model and persistence boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from test_chain_process_fact import _reopen, close_scenario, make_d5_scenario

from agent_adapters.event_export import read_events
from agent_adapters.multi_turn_application import D3Application
from agent_core.contracts import TaskRequest, TaskRequestError
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.redaction import D5Redactor, D5SensitiveValue


def _stored_text(directory) -> str:
    return "\n".join(
        path.read_bytes().decode("utf-8", errors="replace")
        for path in directory.rglob("*")
        if path.is_file()
    )


def test_multiline_quoted_credentials_are_fully_redacted() -> None:
    redactor = D5Redactor(())

    for key in ("password", "token", "api_key"):
        cleaned = redactor.redact(f'{key}="first-line\nsecond-line"')
        assert "first-line" not in cleaned
        assert "second-line" not in cleaned
        assert cleaned.endswith('"')


@pytest.mark.parametrize("quote", ('"', "'"))
def test_escaped_quoted_credentials_leave_no_secret_tail(quote) -> None:
    redactor = D5Redactor(())
    raw = f"password={quote}prefix\\{quote}remaining-credential\nlast-line{quote}"

    assert redactor.contains_sensitive(raw)
    cleaned = redactor.redact(raw)
    assert cleaned == f"password={quote}[REDACTED]{quote}"
    assert not redactor.contains_sensitive(cleaned)


@pytest.mark.parametrize("field", ("goal", "attachment", "endpoint", "source_root"))
def test_sensitive_task_request_is_rejected_before_task_run_or_sandbox_creation(
    tmp_path, field
) -> None:
    secret = "preflight-sensitive-value"
    case = make_d5_scenario(tmp_path / "fixture", sensitive_values=(secret,))
    original = case[0]
    app = D3Application(
        tmp_path / "preflight-run",
        config=original.config,
        sandbox=case[1],
        import_sandbox=case[1],
        workspace_sandbox=case[1],
        command_sandbox=case[1],
        outer_model=case[2],
        inner_model=case[3],
        review_model=case[4],
        clock=case[5],
        refinement_model=case[7],
        sensitive_config={"secret_0": D5SensitiveValue("v1", secret)},
    )
    source_root = case[0].state.task_run.source_root
    assert source_root is not None
    sandbox_creates = []
    create = case[1].create
    case[1].create = lambda task_run_id: (
        sandbox_creates.append(task_run_id),
        create(task_run_id),
    )[1]

    request_value = {
        "goal": secret if field == "goal" else "verify local copy",
        "target": {
            "endpoints": (
                [{"url": f"https://{secret}.example"}] if field == "endpoint" else []
            ),
            "attachments": [
                {
                    "kind": "local_path",
                    "path": secret if field == "attachment" else "first.txt",
                }
            ],
        },
    }
    request = TaskRequest.from_dict(request_value)
    request_before = request.to_dict()
    if field == "source_root":
        source_root = str(tmp_path / secret / "workspace")
        Path(source_root).mkdir(parents=True)

    try:
        with pytest.raises(
            TaskRequestError, match="^d5_sensitive_task_request_rejected$"
        ):
            app.start(request, Path(source_root))

        assert not sandbox_creates
        with sqlite3.connect(app.directory / "control.sqlite") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
                == 0
            )
        assert secret not in (app.directory / "control.sqlite").read_bytes().decode(
            "utf-8", errors="ignore"
        )
        assert request.to_dict() == request_before
        assert (
            not case[2].calls
            and not case[3].calls
            and not case[4].calls
            and not case[7].calls
        )
    finally:
        app.store.close()
        close_scenario(case)


def test_every_d5_model_input_path_receives_redacted_text(tmp_path) -> None:
    secret = "model-input-private-value"
    case = make_d5_scenario(tmp_path, sensitive_values=(secret,))
    try:
        result = asyncio.run(case[0].run(case[6]))
        assert not result.recovery_required, result

        assert all((case[2].calls, case[3].calls, case[4].calls, case[7].calls))
        requests = [
            *case[2].calls,
            *case[3].calls,
            *case[4].calls,
            *case[7].calls,
        ]
        for request in requests:
            serialized = json.dumps(asdict(request), ensure_ascii=False, default=str)
            assert secret not in serialized
    finally:
        close_scenario(case)


@pytest.mark.parametrize("field", ("summary_text", "claim_key", "claim_value"))
def test_sensitive_candidate_is_rejected_whole_before_any_candidate_is_saved(
    tmp_path, field
) -> None:
    secret = "configured-candidate-secret"
    case = make_d5_scenario(tmp_path, sensitive_values=(secret,))
    original = case[7].modify

    def inject_sensitive_text(request, candidate):
        value = {**candidate, "claims": [dict(item) for item in candidate["claims"]]}
        if field == "summary_text":
            value[field] = secret
        elif field == "claim_key":
            value["claims"][0][field] = secret
        else:
            value["claims"][0][field] = secret
        return original(request, value)

    case[7].modify = inject_sensitive_text
    try:
        result = asyncio.run(case[0].run(case[6]))
        assert not result.recovery_required, result
        attempts = case[0].store.list_d5_attempts(result.task_run_id)
        assert attempts
        assert not case[0].store.list_process_facts(result.task_run_id)
        for attempt in attempts:
            saved_result = case[0].refinement.evidence.read_result(
                attempt.model_request_id
            )
            assert saved_result is not None
            assert saved_result["outcome"] == "invalid"
            assert saved_result["reason"] == "candidate_rejected"
            assert (
                case[0].refinement.evidence.read_candidate(attempt.model_request_id)
                is None
            )
        assert secret not in _stored_text(case[0].directory)
    finally:
        close_scenario(case)


def test_d5_event_projection_redacts_or_withholds_without_mutating_receipts(
    tmp_path, monkeypatch
) -> None:
    secret = "event-private-value"
    case = make_d5_scenario(tmp_path, sensitive_values=(secret,))
    try:
        result = asyncio.run(case[0].run(case[6]))
        assert not result.recovery_required, result
        database = case[0].directory / "control.sqlite"
        event_id = str(uuid4())
        details = {
            "operation": "start_task_run",
            "entity_ids": [event_id],
            "result": event_id,
            "path": f"C:/private/{secret}/file.txt",
            f"field-{secret}": "free-form-value",
        }
        with sqlite3.connect(database) as connection:
            sequence = connection.execute(
                "SELECT sequence FROM runtime_events WHERE task_run_id = ? "
                "ORDER BY sequence LIMIT 1",
                (result.task_run_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE runtime_events SET details_json = ? "
                "WHERE task_run_id = ? AND sequence = ?",
                (json.dumps(details), result.task_run_id, sequence),
            )
            raw_before = connection.execute(
                "SELECT details_json FROM runtime_events "
                "WHERE task_run_id = ? AND sequence = ?",
                (result.task_run_id, sequence),
            ).fetchone()[0]

        monkeypatch.setenv(
            "QINYUAN_AGENT_D5_SENSITIVE_CONFIG",
            json.dumps({"secret_0": {"version": "v1", "value": secret}}),
        )
        available = read_events(database, result.task_run_id)
        projected = next(item for item in available if item["sequence"] == sequence)
        assert projected["protocol_version"] == 2
        assert projected["redaction_status"] == "available"
        assert secret not in json.dumps(projected)
        assert projected["details"]["path"] == "C:/private/[REDACTED]/file.txt"
        assert f"field-{secret}" not in projected["details"]

        withheld = read_events(database, result.task_run_id, sensitive_config={})
        projected = next(item for item in withheld if item["sequence"] == sequence)
        assert projected["redaction_status"] == "withheld"
        assert projected["redaction_reason"] == "sensitive_configuration_unavailable"
        assert projected["details"]["operation"] == "start_task_run"
        assert projected["details"]["entity_ids"] == [event_id]
        assert projected["details"]["result"] == event_id
        assert projected["details"]["path"] == "[WITHHELD]"
        assert f"field-{secret}" not in projected["details"]
        assert projected["details"]["withheld_fields"] == 1

        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT details_json FROM runtime_events "
                    "WHERE task_run_id = ? AND sequence = ?",
                    (result.task_run_id, sequence),
                ).fetchone()[0]
                == raw_before
            )

        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT config_json FROM d3_runs WHERE task_run_id = ?",
                (result.task_run_id,),
            ).fetchone()
            config = json.loads(row[0])
            config.pop("d5_enabled")
            config.pop("d5_sensitive_requirements")
            connection.execute(
                "UPDATE d3_runs SET config_json = ? WHERE task_run_id = ?",
                (json.dumps(config), result.task_run_id),
            )
        legacy = read_events(database, result.task_run_id)
        projected = next(item for item in legacy if item["sequence"] == sequence)
        assert projected["protocol_version"] == 1
        assert "redaction_status" not in projected
        assert projected["details"] == details
    finally:
        close_scenario(case)


def test_legacy_event_database_without_d3_declaration_keeps_v1_projection(
    tmp_path,
) -> None:
    database = tmp_path / "legacy.sqlite"
    task_run_id = str(uuid4())
    details = {"operation": "finish_task_run", "legacy_text": "unchanged"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE checkpoints (task_run_id TEXT, watermark_event_sequence INTEGER)"
        )
        connection.execute(
            "CREATE TABLE runtime_events (task_run_id TEXT, sequence INTEGER, type TEXT, "
            "timestamp REAL, status TEXT, details_json TEXT)"
        )
        connection.execute("INSERT INTO checkpoints VALUES (?, ?)", (task_run_id, 1))
        connection.execute(
            "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?)",
            (task_run_id, 1, "task_run_finished", 1.5, "partial", json.dumps(details)),
        )

    result = read_events(database, task_run_id)
    assert result == [
        {
            "protocol_version": 1,
            "task_run_id": task_run_id,
            "sequence": 1,
            "type": "task_run_finished",
            "timestamp": 1.5,
            "status": "partial",
            "details": details,
        }
    ]


@pytest.mark.parametrize(
    "configuration",
    (
        None,
        {},
        {"secret_0": D5SensitiveValue("v2", "restored-secret")},
        {"secret_0": D5SensitiveValue("v1", " ")},
    ),
    ids=("missing", "empty-map", "wrong-version", "empty-value"),
)
def test_resume_with_incomplete_sensitive_configuration_dispatches_no_models(
    tmp_path, configuration
) -> None:
    case = make_d5_scenario(tmp_path, sensitive_values=("required-secret",))
    try:
        app = _reopen(case, sensitive_config=configuration)
        task_run_id = app.state.task_run.task_run_id
        usage = app.store.d4_budget_usage(task_run_id)
        checkpoint = app.state.latest_checkpoint.sequence
        result = asyncio.run(app.resume(task_run_id, case[6]))

        assert result.recovery_required
        assert result.stop_reason == "d5_redaction_configuration_unavailable"
        assert asyncio.run(app.run(case[6])).stop_reason == result.stop_reason
        assert app.store.d4_budget_usage(task_run_id) == usage
        assert app.state.latest_checkpoint.sequence == checkpoint
        assert not case[2].calls
        assert not case[3].calls
        assert not case[4].calls
        assert not case[7].calls
    finally:
        close_scenario(case)


@pytest.mark.parametrize(
    ("failure_mode", "expected_outcome", "expected_reason"),
    (
        ("typed", "failed", "model_failure"),
        ("exception", "response_unknown", "not_applied_response_unknown"),
    ),
)
def test_provider_failure_persists_only_fixed_classification(
    tmp_path, failure_mode, expected_outcome, expected_reason
) -> None:
    secret = "provider-error-secret"
    case = make_d5_scenario(tmp_path, sensitive_values=(secret,))
    model = case[7]

    def fail(request, timeout_seconds, token):
        model.calls.append(request)
        if failure_mode == "typed":
            return ModelError(ModelErrorKind.REFUSED, f"provider returned {secret}")
        raise RuntimeError(f"provider crashed with {secret}")

    model.invoke = fail
    try:
        result = asyncio.run(case[0].run(case[6]))
        assert not result.recovery_required, result
        attempts = case[0].store.list_d5_attempts(result.task_run_id)
        assert attempts
        for attempt in attempts:
            saved_result = case[0].refinement.evidence.read_result(
                attempt.model_request_id
            )
            assert saved_result is not None
            assert saved_result["outcome"] == expected_outcome
            assert saved_result["reason"] == expected_reason
            assert (
                case[0].refinement.evidence.read_candidate(attempt.model_request_id)
                is None
            )
        assert secret not in _stored_text(case[0].directory)
        assert secret not in result.stop_reason
    finally:
        close_scenario(case)
