"""Planning monitoring is read-only and does not infer value from counts."""
import json
import sqlite3

import pytest

from scripts import qymonitor as monitor


def make_run(tmp_path):
    db = tmp_path / "control.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE d4_operations(operation_id TEXT, category TEXT, start_json TEXT,
                completion_json TEXT, application_ref TEXT);
            CREATE TABLE task_runs(status TEXT);
            INSERT INTO task_runs VALUES ('running');
            CREATE TABLE stage_turns(turn_id TEXT, achievement_json TEXT);
            INSERT INTO stage_turns VALUES ('turn', '{"review_operation_id":"review"}');
            CREATE TABLE stage_roads(status TEXT);
            INSERT INTO stage_roads VALUES ('pending'), ('succeeded');
            CREATE TABLE facts(status TEXT);
            INSERT INTO facts VALUES ('verified');
        """)
    (tmp_path / "stage-evidence").mkdir()
    return db


def add_call(path, identity, stage, start, end, *, usage=None, provider=True, correction=False, error=None):
    source = dict(operation_id=identity, category="planning", role="inner_action", started_at=start)
    completion = dict(finished_at=end, result_kind="completed") if end is not None and not error else None
    with sqlite3.connect(path / "control.sqlite") as con:
        con.execute("INSERT INTO d4_operations VALUES (?,?,?,?,?)", (
            identity, "planning", json.dumps(source), json.dumps(completion) if completion else None, None))
    evidence = path / "stage-evidence"
    (evidence / ("stage-request-" + identity + ".json")).write_text(json.dumps(dict(request=dict(
        stage=stage, correction_of_model_request_id="original" if correction else None))))
    if provider:
        (evidence / ("stage-provider-request-" + identity + ".json")).write_text(json.dumps(dict(stage=stage)))
        (evidence / ("stage-provider-result-" + identity + ".json")).write_text(json.dumps(dict(
            transport=dict(elapsed_seconds=end - start if end is not None else None, usage=usage), error=error)))


def test_planning_overlap_unknown_usage_and_unsettled_timeout(tmp_path):
    db = make_run(tmp_path)
    add_call(tmp_path, "plan", "stage_roads", 100, 110,
        usage=dict(prompt_tokens=100, completion_tokens=20, completion_tokens_details=dict(reasoning_tokens=15)))
    add_call(tmp_path, "review", "stage_review", 105, 115)
    add_call(tmp_path, "timeout", "stage_continuation", 115, 125, correction=True,
        error=dict(kind="timed_out", code="model_total_timeout"))
    before = {p.name: p.read_bytes() for p in (tmp_path / "stage-evidence").iterdir()}
    con = monitor.connect(db)
    try:
        report = monitor.planning_report(tmp_path, con)
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM facts")
    finally:
        con.close()
    assert report["model_interval_union_seconds"] == 25
    assert report["planning_interval_union_seconds"] == 20
    assert report["planning_share_of_observed_model_time"] == .8
    timeout = report["stages"]["stage_continuation"]
    assert timeout["unsettled"] == timeout["errors"] == timeout["corrections"] == 1
    assert timeout["tokens"]["prompt_tokens"] == dict(known_calls=0, total=None)
    assert report["stages"]["stage_roads"]["tokens"]["completion_tokens"]["total"] == 20
    assert report["outcomes"]["achieved_turns"] == 1
    assert report["outcomes"]["roads"] == dict(pending=1, succeeded=1)
    assert report["outcomes"]["overall_goal"] is None
    assert before == {p.name: p.read_bytes() for p in (tmp_path / "stage-evidence").iterdir()}


def test_missing_provider_result_is_reported_without_fabricated_duration(tmp_path, capsys):
    db = make_run(tmp_path)
    add_call(tmp_path, "pending", "stage_continuation", 100, None, provider=False)
    con = monitor.connect(db)
    try:
        report = monitor.planning_report(tmp_path, con)
    finally:
        con.close()
    group = report["stages"]["stage_continuation"]
    assert group["calls"] == group["unsettled"] == 1
    assert group["timing_known"] == 0 and group["cumulative_seconds"] is None
    assert report["planning_share_of_observed_model_time"] is None
    assert monitor.main(["planning", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stages"]["stage_continuation"]["unsettled"] == 1


def test_legacy_empty_run_is_not_interpreted_as_zero_cost_success(tmp_path):
    db = tmp_path / "control.sqlite"
    sqlite3.connect(db).close()
    con = monitor.connect(db)
    try:
        report = monitor.planning_report(tmp_path, con)
    finally:
        con.close()
    assert report["stages"] == {}
    assert report["model_interval_union_seconds"] is None
    assert not report["outcomes"]["stage_data_available"]
    assert report["outcomes"]["overall_goal"] is None
