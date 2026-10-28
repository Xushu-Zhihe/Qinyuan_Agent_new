"""Cache diagnostics observe final attempts without changing model inputs."""
from dataclasses import dataclass, replace
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agent_adapters import cache_probe, deepseek_roles as roles
from agent_adapters.deepseek_model import DeepSeekSettings
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.output_contracts import D8OutputRequest, output_schema
from agent_core.review_contracts import D2ReviewRequest
from agent_core.turn_modeling import digest
from scripts.analyze_run_timing import summarize_cache
from test_models import BusinessRequest, envelope


@dataclass
class IdentifiedRequest(BusinessRequest):
    model_request_id: str = ""
    task_run_id: str = ""


@pytest.mark.parametrize("stage", ["inner_plan", "review", "progress_review"])
def test_preflight_is_not_probed_and_transport_uses_exact_payload(tmp_path, monkeypatch, stage):
    path = tmp_path / "probe.jsonl"
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(path))
    if stage == "inner_plan":
        env, role = envelope()
        request = IdentifiedRequest(stage, role.role_config_ref, model_request_id=str(uuid4()), task_run_id=str(uuid4()))
        identity = request.model_request_id
    else:
        role = roles.role_config("outer_planning")
        uid = lambda: str(uuid4())
        request = D2ReviewRequest(uid(), uid(), 1, 1, "turn_start", uid(), uid(), uid(), None,
                                 1, "version1", (), 0, json.dumps({"d8_progress": {}} if stage == "progress_review" else {}))
        identity = request.review_id
    mapped, schema = output_schema(request)
    env = D8OutputRequest(request, {"version": 1, "stage": mapped, "schema": schema}, "Return JSON", None)
    preflight = roles.payload_bytes(roles.build_payload(env, role))
    # Disk JSON changes dictionary order without changing the contract meaning.
    env = replace(env, output_contract=dict(sorted(env.output_contract.items())))
    expected = roles.payload_bytes(roles.build_payload(env, role))
    assert preflight != expected
    assert not path.exists()
    records = FileD8OutputEvidence(tmp_path / "evidence")
    seen = []
    def invoke(payload, timeout, token, *, diagnostic):
        seen.append(roles.payload_bytes(payload))
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert row["request_id"] == identity
        assert row["request_sha256"] == digest(records.read("provider-request", identity))
        assert row["payload_sha256"] == hashlib.sha256(seen[-1]).hexdigest()
        diagnostic.update(transport_end="response_received", usage={"prompt_tokens": 10, "prompt_cache_hit_tokens": 0})
        return {"content": "{}", "finish_reason": "stop"}
    adapter = roles.D8DeepSeekModel(role, DeepSeekSettings(role.model, "offline-private-key"),
                                   transport=SimpleNamespace(_invoke_payload=invoke))
    adapter.bind_diagnostics(records)
    adapter.invoke(env, 30, SimpleNamespace(is_cancelled=False))
    assert seen == [expected]
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["phase"] == "provider_attempt" and row["previous_request_id"] is None
    assert "offline-private-key" not in rows[0] and "Return JSON" not in rows[0]
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(tmp_path))  # Diagnostic write failure.
    adapter.invoke(env, 30, SimpleNamespace(is_cancelled=False))
    assert seen == [expected, expected]


def cache_case(tmp_path, monkeypatch):
    path = tmp_path / "probe.jsonl"
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(path))
    task = SimpleNamespace(task_run_id=str(uuid4()))
    records, operations = {}, []
    system = 'stable output_contract={"version": 1} correction=null'
    for index, current in enumerate([system, system.replace('1}', '2}')]):
        identity = str(uuid4())
        payload = {"model": "test", "messages": [{"role": "system", "content": current}, {"role": "user", "content": "PRIVATE_EVIDENCE"}]}
        sent = dict(task_run_id=task.task_run_id, model_request_id=identity, role_config_ref="role-config",
                    stage="inner_plan", parameters={"model": "test"}, request_utf8_bytes=len(roles.payload_bytes(payload)))
        records["provider-request", identity] = sent
        records["provider-result", identity] = {"request_sha256": digest(sent), "transport": {"usage":
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 80} if index == 0 else None}}
        kinds = ["http_request", "mcp_call"] if index == 0 else ["intent_resolution"]
        records["contract", identity] = {"output_contract": {"schema": {"anyOf": [{"properties": {"kind": {"const": k}}} for k in kinds]}},
                                        "instruction": "same", "correction": None}
        operations.append(SimpleNamespace(start=SimpleNamespace(operation_id=identity, role="inner_action", started_at=100 + index * 165),
                                          completion=object() if index == 0 else None))
        cache_probe.record(payload=payload, sent=sent, role="inner_action")
    evidence = SimpleNamespace(read=lambda kind, identity: records.get((kind, identity)))
    return task, operations, evidence, path


def test_exact_join_unknown_usage_offsets_and_action_changes(tmp_path, monkeypatch):
    task, operations, evidence, path = cache_case(tmp_path, monkeypatch)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"stage": "inner_plan", "request_id": "legacy-preflight"}) + "\n")
    result = summarize_cache(task, operations, evidence, path)
    first, second = result["calls"]
    assert first["prefix_status"] == "first_observed_attempt"
    assert second["prefix_status"] == "comparable"
    assert second["previous_model_request_id"] == first["model_request_id"]
    assert second["first_difference_from_contract_json_chars"] == len('{"version": ')
    assert second["since_previous_attempt_seconds"] == 165
    assert second["first_changed_message"] == 0 and len(second["messages"]) == 2
    assert not second["user_prefix_follows_identical_system"]
    assert second["prompt_tokens"] is None and not second["operation_completed"]
    assert result["stages"]["inner_plan"]["cache_usage_observations"] == 1
    assert result["stages"]["inner_plan"]["cache_hit_ratio"] == .2
    assert result["ignored_records"] == {"legacy_records": 1}
    changes = second["frozen_contract_changes"]
    assert changes["action_branches_before"] == {"http_request": 1, "mcp_call": 1}
    assert changes["action_branches_after"] == {"intent_resolution": 1}
    assert "PRIVATE_EVIDENCE" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["duplicate", "digest", "predecessor", "missing"])
def test_bad_probe_alignment_cannot_claim_valid_prefix(tmp_path, monkeypatch, damage):
    task, operations, evidence, path = cache_case(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if damage == "duplicate": rows.append(dict(rows[1]))
    elif damage == "digest": rows[1]["request_sha256"] = "wrong"
    elif damage == "predecessor": rows[1]["previous_request_id"] = "not-the-previous-call"
    else: rows.pop()
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = summarize_cache(task, operations, evidence, path)
    assert result["calls"][1]["prefix_status"] != "comparable"
    assert result["stages"]["inner_plan"]["comparable_prefixes"] == 0
    assert result["stages"]["inner_plan"]["cache_hit_ratio"] == .2


def test_probe_is_disabled_without_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("QINYUAN_CACHE_PROBE", raising=False)
    cache_probe.record(payload={}, sent={}, role="inner_action")
    assert not list(tmp_path.iterdir())


def test_segmented_messages_report_exact_prefix_without_content(tmp_path, monkeypatch):
    path = tmp_path / "segmented.jsonl"
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(path))
    messages = [{"role": "system", "content": "fixed rules"},
                {"role": "user", "content": "PRIVATE_历史证据"},
                {"role": "assistant", "content": "PRIVATE_旧决策"},
                {"role": "user", "content": "dynamic state 1"}]
    sent = dict(task_run_id=str(uuid4()), role_config_ref="role", stage="inner_plan", parameters={})
    for n in (1, 2):
        sent["model_request_id"] = str(uuid4())
        messages[-1]["content"] = f"dynamic state {n}"
        cache_probe.record(payload={"messages": messages}, sent=sent, role="inner_action")
    raw = path.read_text(encoding="utf-8")
    first, second = map(json.loads, raw.splitlines())
    assert len(second["messages"]) == 4 and second["first_changed_message"] == 3
    assert second["previous_request_id"] == first["request_id"]
    assert second["messages_prefix_utf8_bytes"] > second["messages_prefix_chars"] > 0
    assert "PRIVATE_" not in raw and "旧决策" not in raw


def test_probe_failure_is_classified_without_failing_call(tmp_path, monkeypatch):
    import qinyuan_core
    calls = []
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(tmp_path))
    monkeypatch.setattr(qinyuan_core, "get_logger", lambda _: SimpleNamespace(warning=lambda *a, **kw: calls.append(kw)))
    cache_probe.record(payload={"messages": []}, sent={"task_run_id": "run", "model_request_id": "call", "parameters": {},
        "role_config_ref": "role", "stage": "inner_plan"}, role="inner_action")
    assert calls[0]["code"] == "cache_probe_unavailable" and calls[0]["request"] == "call"


@pytest.mark.parametrize("boundary", ["task", "parameters", "restart"])
def test_comparison_groups_do_not_mix_runs_models_or_restarts(tmp_path, monkeypatch, boundary):
    task, operations, evidence, path = cache_case(tmp_path, monkeypatch)
    sent = dict(evidence.read("provider-request", operations[-1].start.operation_id))
    sent["model_request_id"] = str(uuid4())
    if boundary == "task": sent["task_run_id"] = str(uuid4())
    elif boundary == "parameters": sent["parameters"] = {"model": "another-model"}
    else: cache_probe._PREVIOUS.clear()  # Simulate a new process; no false cross-process prefix.
    payload = {"model": "test", "messages": [{"role": "system", "content": "new"}, {"role": "user", "content": "new"}]}
    sent["request_utf8_bytes"] = len(roles.payload_bytes(payload))
    cache_probe.record(payload=payload, sent=sent, role="inner_action")
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["previous_request_id"] is None and row["system_prefix_chars"] is None


def test_stage_probe_separates_sessions_and_observes_runtime_and_tool_changes(tmp_path, monkeypatch):
    path = tmp_path / "stage-probe.jsonl"
    monkeypatch.setenv("QINYUAN_CACHE_PROBE", str(path))
    from agent_core.stage_contracts import PlanningSession
    task = str(uuid4())
    session = PlanningSession("road_execution", str(uuid4()))
    payload = {"messages": [{"role": "system", "content": "fixed role"},
        {"role": "user", "content": "private-source-body"}, {"role": "system", "content": "current reference A"},
        {"role": "user", "content": "current data"}], "tools": [{"name": "stable-tool"}]}
    sent = {"task_run_id": task, "model_request_id": str(uuid4()), "role_config_ref": "role",
        "stage": "stage_action", "parameters": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}}
    for n in range(3):
        sent["model_request_id"] = str(uuid4())
        payload["messages"][2]["content"] = "current reference " + str(n)
        cache_probe.record(payload=payload, sent=sent, role="inner_action", session=session if n < 2 else
            PlanningSession("road_execution", str(uuid4())))
    raw = path.read_text(encoding="utf-8")
    first, second, third = map(json.loads, raw.splitlines())
    assert second["previous_request_id"] == first["request_id"] and second["tools_unchanged"] is True
    assert second["system_prefix_chars"] == len("fixed role") and second["first_changed_message"] == 2
    assert second["runtime_system_prefix_chars"] == len("current reference ")
    assert third["previous_request_id"] is None and third["tools_unchanged"] is None
    assert "private-source-body" not in raw and "current reference" not in raw


def test_cache_groups_are_weighted_and_only_use_applied_intent_ownership(tmp_path, monkeypatch):
    task, operations, evidence, path = cache_case(tmp_path, monkeypatch)
    # Distinct token weights must not become an average of per-call percentages.
    first, second = (op.start.operation_id for op in operations)
    evidence.read("provider-result", first)["transport"]["usage"] = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}
    evidence.read("provider-result", second)["transport"]["usage"] = {"prompt_tokens": 900, "prompt_cache_hit_tokens": 0}
    decisions = [SimpleNamespace(model_request_id=first, intent_id="applied-intent")]
    report = summarize_cache(task, operations, evidence, path, decisions=decisions)
    assert report["roles"]["inner_action"]["cache_hit_ratio"] == .08
    assert report["stages"]["inner_plan"]["cache_hit_ratio"] == .08
    assert report["intents"]["applied-intent"]["cache_hit_ratio"] == .8
    assert report["unattributed_inner_attempts"] == 1
    assert report["calls"][1]["intent_id"] is None
    evidence.read("provider-result", second)["transport"]["usage"] = None
    missing = summarize_cache(task, operations, evidence, path, decisions=decisions)
    assert missing["roles"]["inner_action"]["cache_usage_observations"] == 1
    assert missing["roles"]["inner_action"]["cache_hit_ratio"] == .8
    assert missing["calls"][1]["prompt_tokens"] is None
