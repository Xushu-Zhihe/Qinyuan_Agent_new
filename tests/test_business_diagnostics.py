"""Contract7 planning diagnostics over real local core state, no API calls."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import task_cli, cli
from agent_adapters.simulation import SimulationModel, parse_request, real_config
from agent_adapters.deepseek_roles import role_config, build_payload
from agent_adapters.run_files import immutable_read
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_adapters.web.task_result import snapshot_v3_task_result
from agent_core.business_diagnostics import BusinessDiagnostics, planning_issues
from agent_core.handoff_contracts import D8HandoffRequest
from agent_core.output_contracts import D8OutputRequest, output_schema
from agent_core.sqlite_control_store import SqliteControlStore, CURRENT_SCHEMA_VERSION
from test_public_simulation import request


def version7_request(tmp_path):
    raw = request(tmp_path)
    raw["execution_profile"] = "d8_v7"
    return raw


def bad_candidate(raw, failure):
    raw = json.loads(json.dumps(raw))
    if failure == "target": raw["turn_target"] = "PRIVATE_REJECTED_VALUE"
    if failure == "rule": raw["completion_rule"]["rule_id"] = "PRIVATE_REJECTED_VALUE"
    if failure == "index": raw["completion_rule"]["parameters"]["attachment_index"] = 2
    if failure == "authorization": raw["completion_rule"]["parameters"]["attachment_index"] = 1
    if failure == "intent": raw["intents"][0]["description"] = " "
    if failure == "intent_type": raw["intents"][0]["intent_type"] = " "
    if failure == "dependency": raw["intents"][0]["dependency_refs"] = ["PRIVATE_REJECTED_VALUE"]
    if failure == "duplicate": raw["intents"][0]["dependency_refs"] = ["PRIVATE_REJECTED_VALUE"] * 2
    if failure == "exhaustion_reason": raw = {"kind": "exhausted", "reason": " ", "evidence_refs": []}
    if failure == "exhaustion_refs": raw = {"kind": "exhausted", "reason": "No further approach", "evidence_refs": ["PRIVATE_REJECTED_VALUE"]}
    return raw


def run_case(tmp_path, monkeypatch, failure="target", *, reject_twice=False, raw=None):
    raw = raw or version7_request(tmp_path)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    original = SimulationModel.invoke
    seen = []
    def invoke(model, envelope, *args):
        seen.append(envelope)
        value = original(model, envelope, *args)
        if (envelope.output_contract["stage"] == "tree_planning" and value.get("kind") == "candidates"
                and (envelope.correction is None or reject_twice)):
            return bad_candidate(value, failure)
        return value
    monkeypatch.setattr(SimulationModel, "invoke", invoke)
    args = SimpleNamespace(request="unused", mode=raw["mode"], state_dir=tmp_path / "state", events=None)
    result = task_cli.dispatch_run_task(args)
    return result, args, seen


@pytest.mark.parametrize("failure,code", [
    ("target", "planning_target_mismatch"), ("rule", "planning_rule_unregistered"),
    ("index", "planning_attachment_invalid"), ("authorization", "planning_attachment_unauthorized"),
    ("intent", "planning_intent_invalid"), ("intent_type", "planning_intent_type_invalid"),
    ("dependency", "planning_dependency_unknown"), ("duplicate", "planning_dependency_duplicate"),
    ("exhaustion_reason", "planning_exhaustion_reason_invalid"), ("exhaustion_refs", "planning_evidence_refs_invalid"),
])
def test_rejection_is_safe_bound_corrected_once_and_applied(tmp_path, monkeypatch, failure, code):
    result, args, seen = run_case(tmp_path, monkeypatch, failure)
    assert result["errors"] == [], result
    assert len(result["verified_facts"]) == 1, result
    assert result["execution_profile"] == "d8_v7" and result["contract_version"] == 7
    diagnostic = result["business_diagnostics"][0]
    assert diagnostic["diagnostic_version"] == 1
    assert diagnostic["issues"][0]["code"] == code
    correction = next(e for e in seen if e.correction is not None)
    assert correction.correction["reason"] == "output_business_invalid"
    assert correction.correction["issues"] == diagnostic["issues"]
    assert correction.correction["business_diagnostic_sha256"] == diagnostic["diagnostic_sha256"]
    assert "PRIVATE_REJECTED_VALUE" not in json.dumps(asdict(correction))
    directory = args.state_dir / result["task_run_id"]
    with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
        assert store._schema_version == CURRENT_SCHEMA_VERSION
        assert store.load_d3_run_config(result["task_run_id"]).contract_version == 7
    for path in (directory / "d8-output").glob("*business-diagnostic*.json"):
        text = path.read_text()
        assert "PRIVATE_REJECTED_VALUE" not in text
        assert '"raw"' not in text


def test_second_business_rejection_stops_with_two_diagnostics(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch, reject_twice=True)
    assert len(seen) == 2
    assert len(result["business_diagnostics"]) == 2
    assert result["verified_facts"] == [] and result["recovery"]["required"]
    args.command, args.task_run_id = "status", result["task_run_id"]
    assert cli.dispatch(args)["business_diagnostics"] == result["business_diagnostics"]
    assert len(seen) == 2


def test_old6_still_uses_generic_correction_and_has_no_new_evidence(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch, raw=request(tmp_path))
    assert result["contract_version"] == 6 and "business_diagnostics" not in result
    correction = next(e for e in seen if e.correction is not None)
    assert correction.correction["reason"] == "proposal_rejected" and correction.correction["issues"] == []
    directory = args.state_dir / result["task_run_id"]
    assert not list((directory / "d8-output").glob("*business-diagnostic*.json"))


@pytest.mark.parametrize("damage", ["missing", "wrong_owner", "wrong_checkpoint", "unsafe_path", "unknown_code", "too_many", "wrong_result_hash", "wrong_root", "wrong_role", "wrong_contract", "missing_request"])
def test_diagnostic_damage_blocks_public_result_and_cancel_without_model(tmp_path, monkeypatch, damage):
    result, args, seen = run_case(tmp_path, monkeypatch, reject_twice=True)
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    target = result["business_diagnostics"][0]["model_request_id"]
    original = FileD8OutputEvidence.read
    original_handoff = FileD8HandoffEvidence.read
    if damage == "missing_request":
        def handoff_read(evidence, kind, identity):
            return None if kind == "request" and identity == target else original_handoff(evidence, kind, identity)
        monkeypatch.setattr(FileD8HandoffEvidence, "read", handoff_read)
    def read(evidence, kind, identity):
        value = original(evidence, kind, identity)
        if kind != "business-diagnostic" or identity != target or value is None:
            return value
        if damage == "missing": return None
        if damage == "wrong_owner": value["task_run_id"] = str(uuid4())
        if damage == "wrong_checkpoint": value["base_checkpoint_sequence"] += 1
        if damage == "unsafe_path": value["issues"][0]["path"] = "$/PRIVATE_REJECTED_VALUE"
        if damage == "unknown_code": value["issues"][0]["code"] = "PRIVATE_REJECTED_VALUE"
        if damage == "too_many": value["issues"] *= 9
        if damage == "wrong_result_hash": value["finished_at"] += 0.001
        if damage == "wrong_root": value["root_request_id"] = str(uuid4())
        if damage == "wrong_role": value["role_config_ref"] = "sha256:" + "0" * 64
        if damage == "wrong_contract": value["contract_sha256"] = "0" * 64
        return value
    monkeypatch.setattr(FileD8OutputEvidence, "read", read)
    projected = snapshot_v3_task_result(directory, declaration)
    assert projected["recovery"]["required"] and projected["verified_facts"] == []
    # 投影会附带白名单化的 safe_failure_details（phase / exception_types / error_codes），
    # 这里只断言「损坏证据必须退化为 state_read_failed」，并确认没有泄漏被篡改的原始值。
    assert [error["code"] for error in projected["errors"]] == ["state_read_failed"]
    assert "PRIVATE_REJECTED_VALUE" not in str(projected)
    before = len(seen)
    args.command, args.task_run_id = "cancel", result["task_run_id"]
    cancelled = cli.dispatch(args)
    assert cancelled["recovery"]["required"] and len(seen) == before


def test_known_diagnostic_recovers_missing_result_by_original_id_without_model(tmp_path, monkeypatch):
    original = FileD8HandoffEvidence.save
    lost = None
    def save(evidence, kind, identity, value):
        nonlocal lost
        if kind == "result" and value.get("business_diagnostic_sha256") and lost is None:
            lost = identity
            raise SystemExit("offline interrupted after durable diagnostic")
        return original(evidence, kind, identity, value)
    monkeypatch.setattr(FileD8HandoffEvidence, "save", save)
    with pytest.raises(SystemExit):
        run_case(tmp_path, monkeypatch)
    monkeypatch.setattr(FileD8HandoffEvidence, "save", original)
    directory = next((tmp_path / "state").glob("*/run.json")).parent
    before = immutable_read(directory / "run.json")
    monkeypatch.setattr(SimulationModel, "invoke", lambda *a: pytest.fail("cancel recovery dispatched model"))
    args = SimpleNamespace(command="cancel", task_run_id=before["task_run_id"], state_dir=directory.parent,
                           events=None)
    cancelled = cli.dispatch(args)
    assert cancelled["task_run"]["status"] == "cancelled", (cancelled["errors"], cancelled["recovery"], cancelled["task_run"])
    assert cancelled["cleanup_status"] == "confirmed"
    recovered = FileD8HandoffEvidence(directory / "d8-handoff", create=False).read("result", lost)
    assert recovered["outcome"] == "invalid" and recovered["business_diagnostic_sha256"]


def test_new_profile_real_requires_v2_roles_and_model_payload_contains_guidance(tmp_path):
    raw = version7_request(tmp_path)
    raw["mode"], raw["d8_config"] = "real", real_config()
    for key, role in [("outer_role", "outer_planning"), ("inner_role", "inner_action")]:
        raw["dual_llm_config"][key] = asdict(role_config(role, version=2))
    raw = json.loads(json.dumps(raw))
    _, config = parse_request(raw)
    assert config.contract_version == 7
    raw["dual_llm_config"]["outer_role"] = json.loads(json.dumps(asdict(role_config("outer_planning"))))
    with pytest.raises(ValueError, match="role_configuration"):
        parse_request(raw)


@pytest.mark.parametrize("order", ["format_then_business", "business_then_format", "unknown"])
def test_format_business_share_one_correction_and_unknown_never_reasks(tmp_path, monkeypatch, order):
    from agent_core.modeling import ModelError, ModelErrorKind
    original = SimulationModel.invoke
    raw = version7_request(tmp_path)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    seen = []
    def invoke(model, env, *args):
        seen.append(env)
        if order == "unknown": return ModelError(ModelErrorKind.TIMED_OUT, "fixed_timeout")
        base = original(model, env, *args)
        is_format = (len(seen) == 1) == (order == "format_then_business")
        return '{"kind":"candidates","kind":"exhausted"}' if is_format else bad_candidate(base, "target")
    monkeypatch.setattr(SimulationModel, "invoke", invoke)
    args = SimpleNamespace(request="unused", mode="simulated", state_dir=tmp_path / "state", events=None)
    result = task_cli.dispatch_run_task(args)
    assert len(seen) == (1 if order == "unknown" else 2)
    assert len(result["business_diagnostics"]) == (0 if order == "unknown" else 1)
    before = len(seen)
    args.command, args.task_run_id = "resume", result["task_run_id"]
    cli.dispatch(args)
    assert len(seen) == before


def test_real_v2_payload_offline_drives_new_core_and_business_correction(tmp_path, monkeypatch):
    from test_models import install_controlled_transport, private_fixture
    raw = version7_request(tmp_path)
    raw["mode"], raw["d8_config"] = "real", real_config()
    for key, role in [("outer_role", "outer_planning"), ("inner_role", "inner_action")]:
        raw["dual_llm_config"][key] = asdict(role_config(role, version=2))
    raw = json.loads(json.dumps(raw))
    def alter(env, number):
        if env.output_contract["stage"] == "tree_planning" and env.correction is None and env.request.turn_id is None:
            value = SimulationModel(role_config("outer_planning", version=2)).invoke(env, 180, None)
            return {"content": json.dumps(bad_candidate(value, "target")), "finish_reason": "stop"}
    captured = install_controlled_transport(monkeypatch, alter=alter)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state", events=None,
                           env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    assert result["contract_version"] == 7 and result["sandbox_backend"] == "local_simulated"
    assert len(result["verified_facts"]) == 1, result
    assert result["business_diagnostics"][0]["issues"][0]["code"] == "planning_target_mismatch"
    assert all("Safe business diagnostics" in payload["messages"][0]["content"] for _, payload in captured)


def test_new_workbench_child_uses7_and_preserves_cancel(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile="d8_v7")
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "sample.txt", io.BytesIO(b"fixture"), 7)
    workbench.runner.start(challenge["id"], mode="simulated")
    run = _wait_for_cli(workbench, challenge["id"])
    assert run["result"]["contract_version"] == 7 and run["result"]["execution_profile"] == "d8_v7"
    assert run["result"]["business_diagnostics"] == []
    workbench.runner.cancel(challenge["id"])
    cancelled = _wait_for_cli(workbench, challenge["id"])
    assert cancelled["result"]["cleanup_status"] == "confirmed"


def test_frozen_unavailable_reference_and_issue_limit_have_no_values(tmp_path, monkeypatch):
    from agent_core.handoff_contracts import D8HistoryItem
    result, args, _ = run_case(tmp_path, monkeypatch)
    directory = args.state_dir / result["task_run_id"]
    evidence = FileD8HandoffEvidence(directory / "d8-handoff", create=False)
    root = result["business_diagnostics"][0]["model_request_id"]
    req = D8HandoffRequest.from_dict(evidence.read("request", root))
    record = D8HistoryItem(str(uuid4()), req.task_run_id, "verified_fact", None, None, None,
                          "SAFE", (), "0" * 64, 1, "verified_result_fact", "historical", "unavailable")
    req = replace(req, history=(record,))
    valid = {"kind": "candidates", "turn_target": req.goal,
             "completion_rule": {"rule_id": "attachment_copy_sha256", "version": 1, "parameters": {"attachment_index": 0}},
             "intents": [{"description": "Observe", "intent_type": None, "dependency_refs": [record.ref]}]}
    config = parse_request(immutable_read(directory / "run.json")["request"])[1]
    issues = planning_issues(req, valid, config.authorization, lambda _: True)
    assert issues == [{"code": "planning_dependency_unavailable", "path": "$/intents/0/dependency_refs",
                       "expected": "available_reliable_refs", "actual": "unavailable"}]
    invalid = bad_candidate(valid, "target")
    invalid["completion_rule"]["rule_id"] = "PRIVATE_REJECTED_VALUE"
    invalid["intents"] = [{"description": " ", "intent_type": " ", "dependency_refs": ["PRIVATE_REJECTED_VALUE"]}] * 2
    issues = planning_issues(req, invalid, config.authorization, lambda _: False)
    assert len(issues) == 8 and "PRIVATE_REJECTED_VALUE" not in json.dumps(issues)


def test_corrective_model_thread_only_reads_frozen_files(tmp_path, monkeypatch):
    import threading
    owner = threading.get_ident()
    original = SqliteControlStore.load_d4_operation
    def load(store, identity):
        assert threading.get_ident() == owner, "model thread accessed SQLite"
        return original(store, identity)
    monkeypatch.setattr(SqliteControlStore, "load_d4_operation", load)
    result, _, _ = run_case(tmp_path, monkeypatch)
    assert len(result["verified_facts"]) == 1
