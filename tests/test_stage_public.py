"""Public-stage assembly/projection boundaries, without a provider or Docker."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from test_stage_application import case
from agent_adapters.stage_application import StageApplication
from agent_adapters.stage_evidence import StageEvidence
from agent_adapters.web.task_result import _stage_task_result
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_storage import StageRecoveryRequired


def test_public_projection_uses_actual_stage_road_and_current_fact_identities(case):
    result = asyncio.run(case.app.run(case.token))
    declaration = dict(task_run_id=case.app.task_id, mode="simulated", protocol_version=3)
    before = {p.relative_to(case.path) for p in case.path.rglob('*')}
    with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
        view = _stage_task_result(case.path, declaration, store, case.args["config"])
        assert store._connection.total_changes == 0
    assert {p.relative_to(case.path) for p in case.path.rglob('*')} == before
    assert view["turns"][0]["intent"]["description"] == result.turn.intent.description
    assert view["turns"][0]["roads"][0]["road_id"] == result.turn.roads[0].road_id
    assert "intents" not in view["turns"][0]
    assert view["verified_facts"][0]["road_id"] == result.turn.roads[0].road_id
    assert view["verified_facts"][0]["current_status"] == "effective"
    assert view["goal_evaluation"] == "not_evaluated" and view["task_run"]["status"] == "running"
    assert len(view["tool_operations"]) == 1 and not view["plans"] and not view["executions"]
    assert view["task_run"]["goal"] == case.app.state.task_run.request.goal
    by_id = {r.model_request_id: r for r in (*case.models.inner.calls, *case.models.outer.calls)}
    for review in view["retrospective"]:
        request = by_id[review["operation_id"]]
        session = request.context.session
        if session.scope == "road_execution":
            assert review["road_id"] == session.owner_id
            assert review["turn_id"] == request.context.turn.turn_id
        else:
            assert review["turn_id"] == session.owner_id and review["road_id"] is None


def test_public_projection_rejects_changed_frozen_policy(case):
    asyncio.run(case.app.run(case.token))
    expected = replace(case.args["config"], authorization=replace(case.args["config"].authorization, attachment_indices=(0,)))
    with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
        with pytest.raises(ValueError, match="execution_configuration_changed"):
            _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"), store, expected)


def test_stage_read_only_evidence_does_not_create_missing_directory(case):
    missing = case.path / "missing-read-only-evidence"
    evidence = StageEvidence(missing, case.app.store, create=False)
    assert evidence.read("request", str(uuid4())) is None
    assert not missing.exists()


def test_entry_supplied_task_identity_is_preserved(case):
    identity = str(uuid4())
    app = StageApplication(case.path / "public-entry", **case.args)
    try:
        actual = app.start(case.app.state.task_run.request, task_run_id=identity)
        assert actual == identity and app.state.task_run.task_run_id == identity
        assert app.store.load_stage_run_config(identity) is not None
    finally:
        asyncio.run(app.close())


def test_unknown_task_restore_has_classified_policy_error(case):
    with pytest.raises(StageRecoveryRequired, match="task_run_not_found"):
        case.app.bind(str(uuid4()))


def test_model_format_stop_is_classified_without_exposing_original_text(case):
    from agent_core.stage_storage import StageStateError
    case.models.inner.transform = lambda request, raw: dict(unexpected="controlled private invalid output")
    with pytest.raises(StageStateError, match="model_output_unusable"):
        asyncio.run(case.app.run(case.token))
    with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
        view = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"), store, case.args["config"])
    assert len(view["model_failures"]) == 1
    assert view["model_failures"][0]["category"] == "input"
    assert view["model_failures"][0]["phase"] == "stage_intent"
    assert view["recovery"]["reason"] == "stage.model_output_unusable"
    assert "controlled private" not in str(view)


def test_stream_failure_projection_retains_safe_branch_without_correction(case, monkeypatch):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.stage_storage import StageStateError
    detail = dict(branch="tool_id_changed", event_index=3,
        field_path="$.choices[0].delta.tool_calls[0].id", observed_type="str")
    calls = []
    def invoke(envelope, timeout, token):
        identity = envelope.request.model_request_id
        calls.append(identity)
        case.app.evidence.save("provider-result", identity, {"transport": {
            "stream_error": detail, "finish_seen": False, "done_seen": False}})
        return ModelError(ModelErrorKind.PROVIDER_FAILURE, "provider_stream_invalid")
    monkeypatch.setattr(case.models.inner, "invoke", invoke)
    with pytest.raises(StageStateError, match="model_output_unusable"):
        asyncio.run(case.app.run(case.token))
    view = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"),
        case.app.store, case.args["config"])
    assert len(calls) == 1 and not case.tools.calls
    failure = view["model_failures"][0]
    assert failure["category"] == "dependency" and failure["stream_error"] == detail
    assert failure["finish_seen"] is False and failure["done_seen"] is False


def test_operator_commands_accept_actual_stage_scope_without_old_configuration(case):
    from agent_core import operator_controls
    now = case.now.value
    assert case.app.store.load_d3_run_config(case.app.task_id) is None
    operator_controls.submit(case.app.store, case.app.task_id, str(uuid4()), "configure",
        dict(question_seconds=3600, overall_deadline_at=now+7200, platform_deadline_at=None))
    operator_controls.submit(case.app.store, case.app.task_id, str(uuid4()), "pause", {})
    with pytest.raises(operator_controls.OperatorPaused):
        case.app._check_dispatch()
    operator_controls.submit(case.app.store, case.app.task_id, str(uuid4()), "resume", {})
    case.app._check_dispatch()


def test_stage_policy_rejection_keeps_fixed_cli_code_without_raw_message(monkeypatch, capsys):
    import json
    from agent_adapters import cli
    from agent_core.stage_storage import StageRecoveryRequired
    from qinyuan_core import Category
    def refused(args):
        raise StageRecoveryRequired("stage.human_confirmation_closed", category=Category.POLICY)
    monkeypatch.setattr(cli, "dispatch", refused)
    result = cli.main(["status", "--state-dir", "unused", "--task-run-id", str(uuid4())])
    output = json.loads(capsys.readouterr().out)
    assert result == 2 and output["errors"] == [dict(code="stage.human_confirmation_closed", category="policy", phase="stage_control")]


@pytest.mark.parametrize("close_failure", [False, True])
def test_public_primary_failure_still_runs_export_app_and_resource_cleanup(tmp_path, monkeypatch, close_failure):
    from types import SimpleNamespace
    from agent_adapters import stage_public
    directory = tmp_path / "state" / "task"
    directory.mkdir(parents=True)
    closed, owners = [], []
    original = RuntimeError("primary controlled failure")
    async def close():
        closed.append("application")
        owners.append(asyncio.current_task())
        if close_failure: raise ValueError("secondary application failure")
    def export():
        closed.append("export")
        owners.append(asyncio.current_task())
        raise ValueError("secondary export failure")
    def request(declaration):
        owners.append(asyncio.current_task())
        raise original
    app = SimpleNamespace(close=close)
    resource = SimpleNamespace(close=lambda: closed.append("resources"))
    monkeypatch.setattr(stage_public, "build_application", lambda *args: (app, resource))
    monkeypatch.setattr(stage_public, "_d3_request", request)
    monkeypatch.setattr(stage_public, "_Token", lambda directory: SimpleNamespace(is_cancelled=False))
    with pytest.raises(RuntimeError) as caught:
        stage_public.run_stage_task(directory, {"task_run_id": str(uuid4())}, finalize_export=export)
    assert caught.value is original and closed == ["export", "application", "resources"]
    assert owners[0] is owners[1] is owners[2]
    assert "stage.public_cleanup_failed:export" in original.__notes__
    if close_failure: assert "stage.public_cleanup_failed:application" in original.__notes__
