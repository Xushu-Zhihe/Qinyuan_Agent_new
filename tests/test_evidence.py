"""D5 ordinary query behavior over real owned control and evidence records."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_chain_operation import close_scenario
from test_chain_process_fact import _run, make_d5_scenario

from agent_adapters import cli, evidence_query
from agent_adapters.evidence_query import (
    EvidenceQueryError,
    export_static_html,
    query_d5_evidence,
    sensitive_config_from_json,
)
from agent_adapters.process_sources import D5SourceReader
from agent_adapters.run_files import immutable_write
from agent_core.domain import PlanStatus
from agent_core.process_facts import D5Source
from agent_core.redaction import D5SensitiveValue
from agent_core.sqlite_control_store import SqliteControlStore


def _finished_application_directory(
    tmp_path: Path,
    *,
    sensitive_values=(),
    summary_text=None,
):
    case = make_d5_scenario(tmp_path / "generated", sensitive_values=sensitive_values)
    if summary_text is not None:
        case[7].modify = lambda _request, candidate: {
            **candidate,
            "summary_text": summary_text,
        }
    try:
        result = _run(case)
        assert not result.recovery_required, result
        original = case[0].directory
    finally:
        close_scenario(case)
    return original, result.task_run_id


def _saved_run(tmp_path: Path, *, sensitive_values=(), summary_text=None):
    original, task_run_id = _finished_application_directory(
        tmp_path,
        sensitive_values=sensitive_values,
        summary_text=summary_text,
    )
    root = tmp_path / "state"
    directory = root / task_run_id
    shutil.copytree(original, directory)
    immutable_write(
        directory / "run.json",
        {
            "protocol_version": 2,
            "task_run_id": task_run_id,
        },
    )
    return root, task_run_id, directory


@pytest.fixture
def saved_run(tmp_path):
    return _saved_run(tmp_path)


def _query(saved_run, sensitive_config=None):
    root, task_run_id, _directory = saved_run
    return query_d5_evidence(root, task_run_id, sensitive_config=sensitive_config)


def _snapshot(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def test_query_traces_model_summary_to_execution_and_verified_proof_without_dispatch(
    saved_run,
    monkeypatch,
):
    _root, task_run_id, directory = saved_run
    before = _snapshot(directory)

    def dispatch_forbidden(*_args, **_kwargs):
        pytest.fail("evidence query dispatched an application")

    monkeypatch.setattr(cli, "supervise", dispatch_forbidden)
    result = _query(saved_run)
    assert result["exit_code"] == 0 and result["task_run_id"] == task_run_id
    assert result["redaction_configuration"]["status"] == "available"
    assert len(result["attempts"]) == len(result["process_facts"]) == 2
    assert all(item["identity"] == "model_summary" for item in result["process_facts"])
    assert all(item["status"] == "available" for item in result["process_facts"])
    assert all(item["applied"] for item in result["attempts"])
    assert result["executions"] and result["source_links"]
    assert any(item["identity"] == "verified_fact" for item in result["facts"])
    assert all(item["content_status"] == "withheld" for item in result["facts"])
    assert all(
        item["status"] == "available"
        and item["applicability"] in {"current", "historical"}
        for item in result["source_links"]
    )
    content = [item["content"] for item in result["source_links"]]
    assert any("stdout_bytes" in item and "stdout_sha256" in item for item in content)
    assert all("stdout" not in item and "stderr" not in item for item in content)
    body_fields = {
        "stdout_bytes",
        "stdout_sha256",
        "stderr_bytes",
        "stderr_sha256",
        "byte_size",
        "sha256",
    }
    assert all(set(item).issubset(body_fields) for item in content)
    assert all(
        item["evidence_body"]["identity"] == "size_sha256_only"
        for item in result["source_links"]
    )
    assert all(
        item["metadata"]["identity"] == "source_metadata"
        for item in result["source_links"]
    )
    assert all("operation_type" in item["metadata"] for item in result["source_links"])
    assert all(
        item["display_role"] == "process_fact_metadata"
        for item in result["process_facts"]
    )
    assert all(item["applicability"] == "current" for item in result["process_facts"])
    assert _snapshot(directory) == before


def test_process_fact_applicability_is_unavailable_if_any_source_is_missing(
    saved_run,
    monkeypatch,
):
    initial = _query(saved_run)
    fact = initial["process_facts"][0]
    historical_key = fact["sources"][0]["version_key"]
    source = D5Source(**fact["sources"][0]["source"])
    synthetic_object_id = str(uuid4())
    synthetic_source = replace(
        source,
        object_id=synthetic_object_id,
        content_sha256="a" * 64,
        subject_ref=source.artifact_version_id or synthetic_object_id,
    )
    original_load_fact = SqliteControlStore.load_process_fact

    def load_fact_with_an_additional_source(store, process_fact_id):
        loaded = original_load_fact(store, process_fact_id)
        if loaded is None or process_fact_id != fact["process_fact_id"]:
            return loaded
        return replace(loaded, sources=(*loaded.sources, synthetic_source))

    monkeypatch.setattr(
        SqliteControlStore, "load_process_fact", load_fact_with_an_additional_source
    )
    monkeypatch.setattr(
        evidence_query,
        "_read_fact",
        lambda _evidence, loaded, _attempt, **_kwargs: {
            "process_fact_id": loaded.process_fact_id,
            "status": "available",
        },
    )
    missing_key = synthetic_source.version_key
    original_project = D5SourceReader.project

    def project_with_mixed_source_state(reader, task_run_id, source):
        projected = original_project(reader, task_run_id, source)
        if source.version_key == historical_key:
            return {**projected, "applicability": "historical"}
        if source.version_key == missing_key:
            return {
                **projected,
                "status": "missing",
                "applicability": None,
                "content": None,
                "metadata": None,
                "evidence_body": {
                    "identity": "size_sha256_only",
                    "status": "missing",
                    "content": None,
                },
            }
        return projected

    monkeypatch.setattr(D5SourceReader, "project", project_with_mixed_source_state)
    result = _query(saved_run)
    queried = next(
        item
        for item in result["process_facts"]
        if item["process_fact_id"] == fact["process_fact_id"]
    )
    assert queried["applicability"] == "unavailable"
    assert any(item["applicability"] == "historical" for item in queried["sources"])
    assert any(item["status"] == "missing" for item in queried["sources"])


@pytest.mark.parametrize("replace_artifact", [False, True])
def test_completed_source_stays_current_until_related_plan_replaces_binding(
    saved_run,
    monkeypatch,
    replace_artifact,
):
    _root, task_run_id, directory = saved_run
    fact_row = _query(saved_run)["process_facts"][0]
    source = D5Source(**fact_row["sources"][0]["source"])
    with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
        rows = store.load_control_state_rows(task_run_id)
        assert rows is not None
        original_binding = store.load_d3_plan_binding(
            next(plan.plan_id for plan in rows.plans)
        )
        assert original_binding is not None
        plan_result = store.load_d3_plan_result(original_binding.plan_id)
        assert plan_result is not None and plan_result.proof_id is not None
        related_plan = next(
            plan for plan in rows.plans if plan.plan_id == original_binding.plan_id
        )
        latest_plan_id = str(uuid4())
        latest_plan = replace(
            related_plan, plan_id=latest_plan_id, status=PlanStatus.COMPLETED
        )
        latest_binding = replace(
            original_binding,
            plan_id=latest_plan_id,
            proposal_request_id=str(uuid4()),
            template="sha256_only",
            observation_mode="new_observation"
            if replace_artifact
            else "reuse_saved_proof",
            artifact_version_id=str(uuid4())
            if replace_artifact
            else original_binding.artifact_version_id,
            rule_binding_sha256="b" * 64
            if replace_artifact
            else original_binding.rule_binding_sha256,
            replan_review_id=str(uuid4()),
            source_proof_id=None if replace_artifact else plan_result.proof_id,
            source_fact_id=None if replace_artifact else plan_result.supporting_fact_id,
        )
        original_load_rows = SqliteControlStore.load_control_state_rows
        original_load_binding = SqliteControlStore.load_d3_plan_binding

        def rows_with_latest_plan(store, requested_task_id):
            current = original_load_rows(store, requested_task_id)
            if current is None or requested_task_id != task_run_id:
                return current
            return replace(current, plans=(*current.plans, latest_plan))

        def load_binding_with_latest(store, plan_id):
            return (
                latest_binding
                if plan_id == latest_plan_id
                else original_load_binding(store, plan_id)
            )

        monkeypatch.setattr(
            SqliteControlStore, "load_control_state_rows", rows_with_latest_plan
        )
        monkeypatch.setattr(
            SqliteControlStore, "load_d3_plan_binding", load_binding_with_latest
        )
        projected = D5SourceReader(directory, store).project(task_run_id, source)
    assert projected["status"] == "available"
    assert projected["applicability"] == (
        "historical" if replace_artifact else "current"
    )


def test_missing_summary_does_not_hide_other_evidence_or_create_a_replacement(
    saved_run,
):
    _root, _task_run_id, directory = saved_run
    first = _query(saved_run)["process_facts"][0]
    path = directory / "d5-process" / f"candidate-{first['model_request_id']}.json"
    path.unlink()
    before = _snapshot(directory)
    result = _query(saved_run)
    by_id = {item["process_fact_id"]: item for item in result["process_facts"]}
    assert by_id[first["process_fact_id"]]["status"] == "missing"
    assert sum(item["status"] == "available" for item in by_id.values()) == 1
    assert result["exit_code"] == 0 and result["executions"]
    assert not path.exists() and _snapshot(directory) == before


@pytest.mark.parametrize(
    "table,key,json_column,result_key",
    [
        ("process_facts", "process_fact_id", "metadata_json", "process_facts"),
        ("d5_attempts", "model_request_id", "attempt_json", "attempts"),
    ],
)
def test_corrupt_metadata_is_isolated_per_owned_entry(
    saved_run,
    table,
    key,
    json_column,
    result_key,
):
    _root, _task_run_id, directory = saved_run
    first = _query(saved_run)[result_key][0]
    with sqlite3.connect(directory / "control.sqlite") as connection:
        connection.execute(
            f"UPDATE {table} SET {json_column} = ? WHERE {key} = ?",
            ("{invalid json", first[key]),
        )
    result = _query(saved_run)
    rows = {item[key]: item for item in result[result_key]}
    assert rows[first[key]]["status"] == "corrupt"
    assert sum(item["status"] == "available" for item in rows.values()) == 1
    assert result["exit_code"] == 0 and result["executions"]


def test_missing_and_corrupt_source_receipts_are_separate_from_withheld(saved_run):
    _root, _task_run_id, directory = saved_run
    query = _query(saved_run)
    execution = query["executions"][0]
    path = directory / "commands" / f"finished-{execution['execution_id']}.json"
    original = path.read_bytes()
    path.unlink()
    result = _query(saved_run)
    source = next(
        item
        for item in result["executions"]
        if item["execution_id"] == execution["execution_id"]
    )["source"]
    assert source["status"] == "missing" and source["content"] is None
    assert result["exit_code"] == 0
    path.write_bytes(b"{invalid json")
    result = _query(saved_run)
    source = next(
        item
        for item in result["executions"]
        if item["execution_id"] == execution["execution_id"]
    )["source"]
    assert source["status"] == "corrupt" and source["content"] is None
    assert result["exit_code"] == 0
    path.write_bytes(original)


def test_required_sensitive_configuration_controls_free_text_only(tmp_path):
    run = _saved_run(tmp_path, sensitive_values=("configured-test-secret",))
    for config in (
        None,
        {},
        {"secret_0": D5SensitiveValue("v2", "configured-test-secret")},
    ):
        result = _query(run, config)
        assert result["redaction_configuration"]["status"] == "withheld"
        assert all(item["status"] == "withheld" for item in result["process_facts"])
        assert all(
            item["summary_text"] is None and item["claims"] is None
            for item in result["process_facts"]
        )
        assert all(
            item["metadata"]["summary"] is None for item in result["source_links"]
        )
        assert all(item["status"] == "available" for item in result["source_links"])
        assert result["exit_code"] == 0
    result = _query(run, {"secret_0": D5SensitiveValue("v1", "configured-test-secret")})
    assert all(item["status"] == "available" for item in result["process_facts"])
    assert "configured-test-secret" not in json.dumps(result)
    result = _query(
        run,
        {
            "secret_0": D5SensitiveValue("v1", "configured-test-secret"),
            "extra_secret": D5SensitiveValue("v2", "extra-test-secret"),
        },
    )
    assert all(item["status"] == "available" for item in result["process_facts"])
    assert "configured-test-secret" not in json.dumps(result)
    assert "extra-test-secret" not in json.dumps(result)


@pytest.mark.parametrize("requirements_state", ["missing", "null"])
def test_unknown_requirements_withhold_text_even_with_empty_configuration(
    saved_run,
    requirements_state,
):
    _root, task_run_id, directory = saved_run
    with sqlite3.connect(directory / "control.sqlite") as connection:
        row = connection.execute(
            "SELECT config_json FROM d3_runs WHERE task_run_id = ?",
            (task_run_id,),
        ).fetchone()
        configuration = json.loads(row[0])
        if requirements_state == "missing":
            configuration.pop("d5_sensitive_requirements")
        else:
            configuration["d5_sensitive_requirements"] = None
        connection.execute(
            "UPDATE d3_runs SET config_json = ? WHERE task_run_id = ?",
            (json.dumps(configuration), task_run_id),
        )
    result = _query(saved_run, {})
    assert result["exit_code"] == 0
    assert result["redaction_configuration"]["status"] == "withheld"
    assert all(item["status"] == "withheld" for item in result["process_facts"])
    assert all(
        item["metadata"]["summary_status"] == "withheld"
        for item in result["source_links"]
    )
    assert any(
        item["evidence_body"]["status"] == "available"
        for item in result["source_links"]
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "{invalid",
        "[]",
        '{"x":1}',
        '{"x":{"version":"v1","value":" "}}',
        '{"x":{"version":"v1","value":"one"},"x":{"version":"v1","value":"two"}}',
    ],
)
def test_invalid_sensitive_configuration_is_unavailable_without_echoing_content(raw):
    assert sensitive_config_from_json(raw) is None


def test_cli_query_succeeds_without_empty_sensitive_configuration(
    saved_run, monkeypatch, capsys
):
    root, task_run_id, _directory = saved_run
    monkeypatch.delenv("QINYUAN_AGENT_D5_SENSITIVE_CONFIG", raising=False)
    assert (
        cli.main(["evidence", "--state-dir", str(root), "--task-run-id", task_run_id])
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["redaction_configuration"]["status"] == "available"
    assert all(item["status"] == "available" for item in output["process_facts"])


@pytest.mark.parametrize("task_run_id", ["../bad", str(uuid4())])
def test_cli_invalid_or_missing_run_exits_two_without_creating_directory(
    tmp_path,
    task_run_id,
    capsys,
):
    root = tmp_path / "absent-state"
    assert (
        cli.main(["evidence", "--state-dir", str(root), "--task-run-id", task_run_id])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2
    assert not root.exists()


def test_cli_whole_database_failure_exits_two_without_raw_error_text(saved_run, capsys):
    root, task_run_id, directory = saved_run
    database = directory / "control.sqlite"
    database.write_bytes(b"not a SQLite database")
    before = _snapshot(directory)
    assert (
        cli.main(["evidence", "--state-dir", str(root), "--task-run-id", task_run_id])
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["errors"] == [{"code": "evidence_database_read_failed"}]
    assert _snapshot(directory) == before


def test_wrong_declaration_ownership_does_not_read_or_expose_other_run(tmp_path):
    root = tmp_path / "state"
    task_run_id = str(uuid4())
    directory = root / task_run_id
    directory.mkdir(parents=True)
    immutable_write(
        directory / "run.json",
        {
            "protocol_version": 2,
            "task_run_id": str(uuid4()),
            "request": "other-run-content",
        },
    )
    with pytest.raises(
        EvidenceQueryError, match="evidence_run_identity_invalid"
    ) as failure:
        query_d5_evidence(root, task_run_id, sensitive_config=None)
    assert "other-run-content" not in str(failure.value)
    assert not (directory / "control.sqlite").exists()


def test_run_directory_reads_d3_application_output_without_manifest(
    tmp_path,
    monkeypatch,
    capsys,
):
    directory, task_run_id = _finished_application_directory(tmp_path)
    assert not (directory / "run.json").exists()
    before = _snapshot(directory)
    normalized_input = directory / ".." / directory.name
    result = query_d5_evidence(
        None,
        task_run_id,
        run_dir=normalized_input,
        sensitive_config=None,
    )
    assert result["exit_code"] == 0 and result["task_run_id"] == task_run_id
    assert all(item["status"] == "available" for item in result["process_facts"])
    monkeypatch.delenv("QINYUAN_AGENT_D5_SENSITIVE_CONFIG", raising=False)
    report = tmp_path / "direct-report.html"
    assert (
        cli.main(
            [
                "evidence",
                "--run-dir",
                str(normalized_input),
                "--task-run-id",
                task_run_id,
                "--html",
                str(report),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["task_run_id"] == task_run_id
    assert len(output["process_facts"]) == 2
    assert report.exists() and not (directory / "run.json").exists()
    assert _snapshot(directory) == before


def test_run_directory_wrong_or_ambiguous_owner_exits_two_without_other_content(
    tmp_path,
    capsys,
):
    directory, task_run_id = _finished_application_directory(tmp_path)
    wrong_id = str(uuid4())
    assert (
        cli.main(
            [
                "evidence",
                "--run-dir",
                str(directory),
                "--task-run-id",
                wrong_id,
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["errors"] == [{"code": "evidence_run_ownership_invalid"}]
    assert task_run_id not in json.dumps(output)
    assert "Saved execution" not in json.dumps(output)
    with sqlite3.connect(directory / "control.sqlite") as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(task_runs)")]
        row = list(connection.execute("SELECT * FROM task_runs").fetchone())
        row[columns.index("task_run_id")] = wrong_id
        placeholders = ",".join("?" for _ in columns)
        connection.execute(f"INSERT INTO task_runs VALUES ({placeholders})", row)
    assert (
        cli.main(
            [
                "evidence",
                "--run-dir",
                str(directory),
                "--task-run-id",
                task_run_id,
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["errors"] == [{"code": "evidence_run_ownership_invalid"}]
    assert wrong_id not in json.dumps(output)
    assert "Saved execution" not in json.dumps(output)


def test_evidence_cli_rejects_missing_or_conflicting_directory_selectors(
    tmp_path,
    capsys,
):
    task_run_id = str(uuid4())
    for arguments in (
        [],
        ["--state-dir", str(tmp_path), "--run-dir", str(tmp_path)],
    ):
        assert (
            cli.main(
                [
                    "evidence",
                    "--task-run-id",
                    task_run_id,
                    *arguments,
                ]
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["errors"] == [
            {"code": "invalid_cli_arguments"}
        ]
    absent = tmp_path / "absent"
    assert (
        cli.main(
            [
                "evidence",
                "--task-run-id",
                task_run_id,
                "--run-dir",
                str(absent),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["errors"] == [
        {"code": "evidence_run_not_found"}
    ]
    assert not absent.exists()


def test_static_export_escapes_summary_and_refuses_source_or_existing_output(tmp_path):
    run = _saved_run(tmp_path, summary_text='<script>alert("evidence")</script> & text')
    root, _task_run_id, directory = run
    result = _query(run)
    output = tmp_path / "report.html"
    export_static_html(result, root, output)
    text = output.read_text(encoding="utf-8")
    assert "<script>" not in text and "&lt;script&gt;" in text and "&amp; text" in text
    assert "<iframe" not in text and "onload=" not in text
    with pytest.raises(EvidenceQueryError, match="evidence_export_exists"):
        export_static_html(result, root, output)
    with pytest.raises(
        EvidenceQueryError, match="evidence_export_must_be_outside_state_dir"
    ):
        export_static_html(result, root, directory / "report.html")
    assert not (directory / "report.html").exists()
