"""D9-1 public D8 assembly with real local persistence and no external calls."""

import io
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import task_cli, cli
from agent_adapters.d8_simulation import parse_request, simulation_config, SimulationModel
from agent_adapters.run_files import immutable_read
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_adapters.workbench import Workbench, WorkbenchError
from agent_core.operation_accounting import D8HandoffBudgetPolicy
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_contracts import D1RoleConfig
from test_d7_workbench_v3 import _challenge, _wait_for_cli


def request(tmp_path, *, count=1, mode="simulated"):
    for index in range(count):
        (tmp_path / f"input{index}.txt").write_bytes(b"D9 local observation")
    return json.loads(json.dumps({
        "protocol_version": 3, "request_id": str(uuid4()), "execution_profile": "d8_v6",
        "mode": mode, "d8_config": simulation_config(),
        "task_request": {"goal": "Observe the authorized copy", "deadline_seconds": 300,
                         "target": {"source_root": str(tmp_path), "endpoints": [],
                                    "attachments": [{"kind": "local_path", "path": f"input{i}.txt"}
                                                    for i in range(count)]}},
        "authorization": {"authorization_id": str(uuid4()), "attachment_indices": list(range(count)),
                          "allowed_tool": "d3_fixed_readonly", "network_allowed": False},
        "dual_llm_config": {
            "outer_role": asdict(D1RoleConfig("outer_planning", "fixture", "sim-outer", "sim-outer-v1")),
            "inner_role": asdict(D1RoleConfig("inner_action", "fixture", "sim-inner", "sim-inner-v1")),
            "d4_budget": asdict(D8HandoffBudgetPolicy()),
        },
    }))


def start(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode=raw["mode"], state_dir=tmp_path / "state", events=None)
    return task_cli.dispatch_run_task(args), args


@pytest.mark.parametrize("count", [1, 2])
def test_public_d8_core_records_and_idempotent_cancel(tmp_path, monkeypatch, count):
    raw = request(tmp_path, count=count)
    result, args = start(tmp_path, monkeypatch, raw)
    assert result["errors"] == [], result
    assert result["execution_profile"] == "d8_v6"
    assert result["contract_version"] == 6
    assert result["goal_evaluation"] == "not_evaluated"
    assert result["answer_candidates"] == []
    assert len(result["verified_facts"]) == 1, result
    assert not result["recovery"]["required"], result
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
        assert store.load_d3_run_config(result["task_run_id"]).contract_version == 6
        assert store.list_d8_conclusions(result["task_run_id"])[0].outcome == "verified_fact"
        assert store.list_d8_handoffs(result["task_run_id"])
        assert store.list_d8_decisions(result["task_run_id"])
        assert store.list_d8_tree_records(result["task_run_id"])
    assert list((directory / "d8-output").rglob("*.json"))
    before = (directory / "control.sqlite").read_bytes()
    monkeypatch.setattr(SimulationModel, "invoke", lambda *a: pytest.fail("replay called model"))
    replay = task_cli.dispatch_run_task(args)
    assert replay["task_run_id"] == result["task_run_id"]
    assert (directory / "control.sqlite").read_bytes() == before
    assert snapshot_v3_task_result(directory, declaration)["contract_version"] == 6
    args.command, args.task_run_id = "cancel", result["task_run_id"]
    cancelled = cli.dispatch(args)
    assert cancelled["task_run"]["status"] == "cancelled", cancelled
    assert cancelled["cleanup_status"] == "confirmed", cancelled
    assert cli.dispatch(args)["cleanup_status"] == "confirmed"


def test_zero_input_is_unverified_and_cleaned(tmp_path, monkeypatch):
    result, _ = start(tmp_path, monkeypatch, request(tmp_path, count=0))
    assert result["task_run"]["status"] == "inconclusive", result
    assert result["goal_evaluation"] == "not_evaluated"
    assert result["cleanup_status"] == "confirmed"
    assert result["run_exit_code"] == 4


def test_real_rejects_fake_configuration(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="invalid_d8_configuration"):
        start(tmp_path, monkeypatch, request(tmp_path, mode="real"))
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("field", ["mode", "d8_config", "dual_llm_config"])
def test_d8_missing_configuration_never_falls_back(tmp_path, field):
    raw = request(tmp_path)
    del raw[field]
    with pytest.raises(ValueError):
        parse_request(raw)


def test_unsupported_attachments_keep_rejection_receipt(tmp_path, monkeypatch):
    result, args = start(tmp_path, monkeypatch, request(tmp_path, count=3))
    assert result["errors"] == [{"code": "capability_not_supported"}]
    assert result["task_run_id"] is None
    assert result["request_receipt"]["state"] == "capability_not_supported"
    assert not list(args.state_dir.glob("*/control.sqlite"))


def test_workbench_requires_mode_before_spawning(tmp_path, monkeypatch):
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile="d8_v6")
    challenge = workbench.create(_challenge())
    monkeypatch.setattr("agent_adapters.workbench_runner.subprocess.Popen",
                        lambda *a, **k: pytest.fail("rejected mode spawned a process"))
    with pytest.raises(WorkbenchError, match="mode_required"):
        workbench.runner.start(challenge["id"])
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", tmp_path / "missing.env")
    with pytest.raises(WorkbenchError, match="d8_model_configuration_unavailable"):
        workbench.runner.start(challenge["id"], mode="real")
    assert not list(workbench.root.glob("runs/*"))


def test_workbench_child_process_uses_d8_and_can_cancel(tmp_path):
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile="d8_v6")
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"fixture"), 7)
    workbench.runner.start(challenge["id"], mode="simulated")
    run = _wait_for_cli(workbench, challenge["id"])
    assert run["status"] == "paused", run["result"]
    assert run["result"]["contract_version"] == 6, run["result"]
    assert run["result"]["verified_facts"], run["result"]
    assert run["result"]["answer_candidates"] == []
    workbench.runner.cancel(challenge["id"])
    cancelled = _wait_for_cli(workbench, challenge["id"])
    assert cancelled["status"] == "cancelled", cancelled["result"]
    assert cancelled["result"]["cleanup_status"] == "confirmed"


@pytest.mark.parametrize(("extra", "protocol", "profile", "mode"), [
    ([], 3, "d8_v20", None),
    (["--execution-profile", "d8_v7"], 3, "d8_v7", None),
    (["--execution-profile", "d8_v6"], 3, "d8_v6", None),
    (["--execution-profile", "d7", "--d7-mode", "simulated"], 3, "d7", "simulated"),
    (["--protocol-version", "2"], 2, "d7", "simulated"),
])
def test_launcher_defaults_and_explicit_legacy(tmp_path, monkeypatch, extra, protocol, profile, mode):
    from agent_adapters import workbench as module
    captured = []

    class Server:
        server_port = 0
        def __init__(self, address, workbench):
            captured.append(workbench)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.delenv("QINYUAN_WORKBENCH_PROTOCOL_VERSION", raising=False)
    monkeypatch.setattr(module, "WorkbenchServer", Server)
    module.main(["--data-dir", str(tmp_path), "--port", "0", *extra])
    assert captured[0].protocol_version == protocol
    assert captured[0].execution_profile == profile
    assert captured[0].d7_mode == mode


@pytest.mark.parametrize("damage", ["missing_request", "missing_result", "wrong_binding"])
def test_handoff_review_damage_blocks_cancel_without_new_model(tmp_path, monkeypatch, damage):
    from agent_adapters.handoff_evidence import FileD8HandoffEvidence
    raw = request(tmp_path)
    result, args = start(tmp_path, monkeypatch, raw)
    directory = args.state_dir / result["task_run_id"]
    with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
        target = next(op.start.operation_id for op in store.list_d4_operations(result["task_run_id"])
                      if op.start.category == "review" and op.start.resource_ref != op.start.operation_id)
    original = FileD8HandoffEvidence.read

    def read(evidence, kind, identity):
        value = original(evidence, kind, identity)
        if identity == target:
            if (damage == "missing_request" and kind == "request"
                    or damage == "missing_result" and kind == "result"):
                return None
            if damage == "wrong_binding" and kind == "request":
                return {**value, "base_checkpoint_sequence": value["base_checkpoint_sequence"] + 1}
        return value

    monkeypatch.setattr(FileD8HandoffEvidence, "read", read)
    monkeypatch.setattr(SimulationModel, "invoke", lambda *a: pytest.fail("damaged review was redispatched"))
    args.command, args.task_run_id = "cancel", result["task_run_id"]
    stopped = cli.dispatch(args)
    assert stopped["recovery"]["required"], stopped
    assert stopped["task_run"]["status"] == "running"
    assert stopped["cleanup_status"] != "confirmed"
    assert not (directory / "cleanup.json").exists()


def test_unknown_fact_review_is_read_only_and_never_redispatched(tmp_path, monkeypatch):
    class ProcessLoss(BaseException):
        pass

    original = SimulationModel.invoke
    def lose(model, envelope, *args):
        if envelope.output_contract["stage"] == "fact_review":
            raise ProcessLoss()
        return original(model, envelope, *args)

    raw = request(tmp_path)
    monkeypatch.setattr(SimulationModel, "invoke", lose)
    with pytest.raises(ProcessLoss):
        start(tmp_path, monkeypatch, raw)
    directory = next((tmp_path / "state").glob("*/control.sqlite")).parent
    declaration = immutable_read(directory / "run.json")
    before = (directory / "control.sqlite").read_bytes()
    view = snapshot_v3_task_result(directory, declaration)
    assert view["recovery"]["required"]
    assert (directory / "control.sqlite").read_bytes() == before
    monkeypatch.setattr(SimulationModel, "invoke", lambda *a: pytest.fail("unknown model was redispatched"))
    args = SimpleNamespace(command="resume", task_run_id=declaration["task_run_id"],
                           state_dir=tmp_path / "state", events=None)
    resumed = cli.dispatch(args)
    assert resumed["recovery"]["required"]
    assert resumed["exit_code"] == 5
    assert not resumed["verified_facts"]


def test_capacity_insufficient_is_not_automatically_increased(tmp_path, monkeypatch):
    raw = request(tmp_path)
    for name in ("outer_capacity", "inner_capacity"):
        raw["d8_config"]["handoff_config"][name]["total_units"] = 8193
    monkeypatch.setattr(SimulationModel, "invoke", lambda *a: pytest.fail("capacity failure called model"))
    result, args = start(tmp_path, monkeypatch, raw)
    assert result["recovery"]["required"]
    assert not result["verified_facts"]
    assert result["budget"]["category_counts"]["planning"] == 0
    assert result["budget"]["category_counts"]["action"] == 0
    assert result["budget"]["total_used"] == result["budget"]["category_counts"]["attachment_import"] == 1
    assert immutable_read(args.state_dir / result["task_run_id"] / "run.json")["request"] == raw


def test_mode_mismatch_is_rejected_before_staging(tmp_path, monkeypatch):
    raw = request(tmp_path)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state", events=None)
    with pytest.raises(ValueError, match="d8_mode_mismatch"):
        task_cli.dispatch_run_task(args)
    assert not args.state_dir.exists()


def test_http_mode_contract_and_page_control(tmp_path):
    import threading
    import urllib.request
    from urllib.error import HTTPError
    from agent_adapters.workbench import WorkbenchServer
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile="d8_v6")
    challenge = workbench.create(_challenge())
    with WorkbenchServer(("127.0.0.1", 0), workbench) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(origin + "/api/health") as response:
                health = json.load(response)["capabilities"]
            assert health["contract_version"] == 6
            assert health["mode_selection_required"] and health["real_supported"]
            assert health["real_sandbox_backend"] == "local_simulated"
            with urllib.request.urlopen(origin + "/") as response:
                page = response.read().decode("utf-8")
            assert 'id="run-mode"' in page and '<option value="">' in page
            for body, code in [({}, "mode_required")]:
                call = urllib.request.Request(origin + f"/api/challenges/{challenge['id']}/run",
                                              data=json.dumps(body).encode(),
                                              headers={"Content-Type": "application/json"})
                with pytest.raises(HTTPError) as raised:
                    urllib.request.urlopen(call)
                assert raised.value.code == 409
                assert code in raised.value.read().decode()
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("change", ["profile_missing", "contract", "mode", "capacity", "budget", "config_missing"])
def test_new_declaration_mismatch_never_reads_as_legacy(tmp_path, monkeypatch, change):
    result, args = start(tmp_path, monkeypatch, request(tmp_path))
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    if change == "profile_missing":
        del declaration["execution_profile"]
    elif change == "contract":
        declaration["contract_version"] = 5
    elif change == "mode":
        declaration["mode"] = declaration["request"]["mode"] = "real"
    elif change == "capacity":
        declaration["request"]["d8_config"]["handoff_config"]["capacity"]["initial_history"] += 1
    elif change == "budget":
        declaration["d4_budget_policy"]["total"] = 48
    else:
        import sqlite3
        with sqlite3.connect(directory / "control.sqlite") as connection:
            connection.execute("DELETE FROM d3_runs WHERE task_run_id=?", (result["task_run_id"],))
    before = (directory / "control.sqlite").read_bytes()
    view = snapshot_v3_task_result(directory, declaration)
    assert view["recovery"]["required"]
    assert view["task_run"] is None and not view["verified_facts"]
    assert (directory / "control.sqlite").read_bytes() == before
