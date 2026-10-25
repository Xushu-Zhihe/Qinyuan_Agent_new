"""Read-only timing includes failed tails; no event-gap or retry-count guesses."""
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.turn_modeling import digest
from scripts.analyze_run_timing import summarize


def sample(tmp_path):
    task = NS(task_run_id=str(uuid4()), started_at=100.0, finished_at=None)
    evidence = FileD8OutputEvidence(tmp_path / "output")
    operations = []
    for role, stage, begin, finish, reason in (
        ("outer_planning", "progress_review", 100, 110, "stop"),
        ("inner_action", "inner_plan", 105, 140, "length")):
        identity = str(uuid4())
        start = NS(operation_id=identity, role=role, role_config_ref="role:" + role, started_at=begin)
        operations.append(NS(start=start, completion=NS(finished_at=finish)))
        sent = dict(task_run_id=task.task_run_id, model_request_id=identity,
                    role_config_ref=start.role_config_ref, stage=stage)
        evidence.save("provider-request", identity, sent)
        evidence.save("provider-result", identity, dict(model_request_id=identity, request_sha256=digest(sent),
            transport=dict(finish_reason=reason, transport_end="response_received", usage=dict(
                prompt_tokens=100, completion_tokens=20, completion_tokens_details=dict(reasoning_tokens=15)))))
        evidence.save("contract", identity, {**sent, "correction_of_model_request_id": operations[0].start.operation_id if stage == "inner_plan" else None})
    return task, operations, evidence


def test_failed_tail_roles_corrections_and_overlapping_intervals(tmp_path):
    task, operations, evidence = sample(tmp_path)
    report = summarize(task, operations, evidence)
    assert report["observed_wall_seconds"] == 40 and report["wall_is_lower_bound"]
    assert report["roles"]["inner_action"]["seconds"] == 35
    assert report["stages"]["inner_plan"]["finish_reasons"] == {"length": 1}
    assert report["stages"]["inner_plan"]["corrections"] == 1
    assert sum(g["seconds"] for g in report["roles"].values()) == 45
    assert report['model_interval_sum_seconds'] == 45
    assert report['model_interval_union_seconds'] == 40
    assert sum(g["completion_tokens"] for g in report["roles"].values()) == 40
    assert sum(g["reasoning_tokens"] for g in report["roles"].values()) == 30
    task.finished_at = 150
    assert summarize(task, operations, evidence)["observed_wall_seconds"] == 50
    assert not summarize(task, operations, evidence)["wall_is_lower_bound"]


def test_unknown_call_is_reported_without_zero_duration_or_replay(tmp_path):
    task, operations, evidence = sample(tmp_path)
    identity = str(uuid4())
    operations.append(NS(start=NS(operation_id=identity, started_at=145, role="inner_action",
        role_config_ref="role:inner_action"), completion=None))
    result = summarize(task, operations, evidence)
    assert result["unfinished_model_operations"] == [identity]
    assert result["missing_provider_requests"] == [identity]
    assert result["calls"][-1]["seconds"] is None
    assert result["observed_wall_seconds"] == 45


def test_timing_rejects_result_bound_to_another_request(tmp_path, monkeypatch):
    task, operations, evidence = sample(tmp_path)
    read = evidence.read
    monkeypatch.setattr(evidence, "read", lambda kind, key: {**read(kind, key), "request_sha256": "0" * 64}
                        if kind == "provider-result" else read(kind, key))
    with pytest.raises(ValueError, match="provider_result_binding"):
        summarize(task, operations, evidence)


def test_activity_counts_durable_reads_and_separates_fetch_from_search(tmp_path):
    from agent_adapters.handoff_evidence import FileD8HandoffEvidence
    from scripts.analyze_run_timing import summarize_activity
    output = FileD8OutputEvidence(tmp_path/'output')
    handoff = FileD8HandoffEvidence(tmp_path/'handoff')
    sessions = FileD8HandoffEvidence(tmp_path/'sessions')
    task = NS(task_run_id=str(uuid4()))
    identity, actor, model = (str(uuid4()) for _ in range(3))
    output.save('mcp-call', identity, dict(task_run_id=task.task_run_id,
        call=dict(tool_name='web.fetch'),intent_id=actor,model_request_id=model))
    op = NS(start=NS(operation_id=identity, category='tool_dispatch',
        resource_ref='mcp/'+actor+'/'+model),completion=NS(result_kind='completed'))
    read_id, snapshot_id, cursor_id = (str(uuid4()) for _ in range(3))
    snapshot = dict(read_input=dict(cursor=None))
    handoff.save('read-snapshot',snapshot_id,snapshot)
    handoff.save('read-snapshot',cursor_id,dict(read_input=dict(cursor=str(uuid4()))))
    block = dict(task_run_id=task.task_run_id,scope='intent',checkpoint_sequence=9,
                 intent_id=actor,kind='visible_history')
    handoff.save('pause',str(uuid4()),block)
    handoff.save('pause',str(uuid4()),{**block,'next_action':'continue_other_feasible_intents'})
    handoff.save('pause',str(uuid4()),dict(identity='technical_suspension_not_business_verdict'))
    store = NS(list_d8_decisions=lambda _: [],list_d4_operations=lambda _: [op],
        list_d8_reads=lambda _: [(read_id,dict(snapshot_id=snapshot_id,snapshot_sha256=digest(snapshot)),dict(sha256='x'))])
    report = summarize_activity(task,store,output,sessions,handoff)
    assert report['tools'] == {'web.fetch:completed':1}
    assert report['history_reads'] == {'completed':1,'initial':1}
    assert report['distinct_capacity_blocks'] == 1
    assert report['applied_inner_decisions'] == {}
