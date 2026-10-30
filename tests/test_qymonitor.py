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


def test_review_overlap_source_adoption_and_partial_cost_coverage(tmp_path):
    db = make_run(tmp_path)
    with sqlite3.connect(db) as con:
        con.executescript("""
            ALTER TABLE facts ADD COLUMN fact_id TEXT;
            ALTER TABLE facts ADD COLUMN source_ref TEXT;
            ALTER TABLE facts ADD COLUMN verification_ref TEXT;
            DELETE FROM facts;
            INSERT INTO facts VALUES ('verified','f1','tool-a','stage:fact-review');
            CREATE TABLE stage_review_queue(proposal_operation_id TEXT,state TEXT,review_operation_id TEXT);
            INSERT INTO stage_review_queue VALUES ('a','settled','review-correction'),('b','settled','review-correction');
            CREATE TABLE stage_branches(branch_id TEXT);
            INSERT INTO stage_branches VALUES ('one'),('two');
            CREATE TABLE control_mutations(mutation_id TEXT,committed_at REAL);
            INSERT INTO control_mutations VALUES ('review-commit',125.5);
        """)
        con.execute("UPDATE stage_turns SET achievement_json=?", (json.dumps(dict(completion=dict(
            support_refs=["verified_fact:f1:v1"]), review_operation_id="closure")),))
    for identity, stage, begin, end in [("a", "stage_action", 90, 92), ("b", "stage_action", 92, 94),
            ("review", "stage_review", 100, 120), ("review-correction", "stage_review", 120, 125),
            ("fact-proposal", "stage_action", 128, 129), ("fact-review", "stage_review", 130, 136),
            ("unused", "stage_action", 105, 115), ("cancelled", "stage_action", 106, 116),
            ("unfinished", "stage_action", 140, None)]:
        add_call(tmp_path, identity, stage, begin, end, correction=identity == "review-correction",
            usage=dict(prompt_tokens=10, completion_tokens=4, completion_tokens_details=dict(reasoning_tokens=3)) if identity == "unused" else None)
    evidence = tmp_path / "stage-evidence"
    for identity in ("review", "review-correction"):
        path = evidence / f"stage-request-{identity}.json"
        value = json.loads(path.read_text())
        value["request"].update(proposal_operation_id="a", execution_context=dict(stage_runtime=dict(review_members=["a", "b"])))
        path.write_text(json.dumps(value))
    path = evidence / "stage-request-fact-review.json"
    value = json.loads(path.read_text())
    value["request"].update(proposal_operation_id="fact-proposal", proposal=dict(support_refs=["progress:tool-a:v1", "progress:tool-c:v1"]))
    path.write_text(json.dumps(value))
    (evidence / "stage-result-cancelled.json").write_text(json.dumps(dict(outcome="superseded")))
    for identity in ("fence", "tool-b"):
        (evidence / f"stage-tool-dispatch-stop-{identity}.json").write_text(json.dumps(dict(outcome="not_dispatched")))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE d4_operations SET application_ref='stage-discarded/review-correction' WHERE operation_id='unused'")
        con.execute("UPDATE d4_operations SET application_ref='review-commit' WHERE operation_id='review-correction'")
        for identity, begin, end in [("tool-a",105,112), ("tool-b",115,118), ("tool-c",119,128), ("tool-d",126,129)]:
            con.execute("INSERT INTO d4_operations VALUES (?,?,?,?,NULL)", (identity, "tool_dispatch",
                json.dumps(dict(category="tool_dispatch", started_at=begin)), json.dumps(dict(finished_at=end, result_kind="completed"))))
    before = {p.name: p.read_bytes() for p in evidence.iterdir()}
    with monitor.connect(db) as con:
        report = monitor.planning_report(tmp_path, con)["reviews"]
        assert con.total_changes == 0
    assert before == {p.name: p.read_bytes() for p in evidence.iterdir()}
    assert report["overlap"] == dict(review_interval_union_seconds=31, review_with_inner_seconds=11, review_with_tools_seconds=16)
    assert report["queue"][0]["attempt_ids"] == ["review", "review-correction"]
    assert report["queue"][0]["proposal_ready_to_first_review_seconds"] == 8
    assert report["queue"][0]["settled_at"] == 125.5 and report["branch_count"] == 2
    assert report["exploration"]["tools_overlapping_review"] == ["tool-a", "tool-b", "tool-c"]
    assert report["exploration"]["observations_completed_during_review"] == ["tool-a", "tool-b"]
    assert report["exploration"]["observations_referenced_by_stored_facts"] == ["tool-a"]
    assert report["exploration"]["observations_without_recorded_fact_reference"] == ["tool-b"]
    assert report["exploration"]["fact_ids_referenced_by_achievements"] == ["f1"]
    assert report["unadopted_generation_ids"] == ["cancelled", "unused"]
    assert report["cost"]["unadopted_generations"]["calls"] == 2
    assert report["cost"]["unadopted_generations"]["tokens"]["prompt_tokens"] == dict(total=10, known_calls=1)
    assert report["cost"]["unadopted_generations"]["tokens"]["completion_tokens"]["total"] == 4
    assert report["cost"]["corrections"]["tokens"]["prompt_tokens"]["total"] is None
    assert report["monetary_cost"] is None and report["inconsistent_fence_ids"] == ["tool-b"]
