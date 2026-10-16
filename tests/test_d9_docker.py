"""D9-3 offline contracts; real Docker/API acceptance is recorded separately."""

import asyncio
import hashlib
import json
import socket
import threading
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4, uuid5, NAMESPACE_URL

import pytest

from agent_adapters import d8_docker, d8_model, d8_simulation, task_cli
from agent_adapters.d8_simulation import SimulationModel, parse_request, real_config
from agent_adapters.run_files import immutable_read
from agent_adapters.v7.simulated_application import _LocalDigestSandbox
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_core.artifact_workspace import artifact_path, D3StopClaim
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.sqlite_control_store import SqliteControlStore
from Sandbox.d8_workspace import D8DockerWorkspace, WORKSPACE_SCRIPT
from Sandbox.task_run_sandbox import TaskRunSandboxError
from test_d9_public import request, start
from test_d9_models import private_fixture
from test_d8_output import output_case


def new_request(tmp_path, *, real=False):
    raw = request(tmp_path)
    raw["execution_profile"] = "d8_v18"
    if real:
        raw["mode"], raw["d8_config"] = "real", real_config(contract_version=18)
        for name, role in (("outer", "outer_planning"), ("inner", "inner_action")):
            raw["dual_llm_config"][name + "_role"] = asdict(d8_model.role_config(role, version=13))
    return json.loads(json.dumps(raw))


def test_new18_and_old17_have_separate_frozen_backends(tmp_path):
    raw = new_request(tmp_path, real=True)
    _, config = parse_request(raw)
    assert config.contract_version == 18
    policy = raw["d8_config"]["sandbox_config"]
    assert policy["image_id"] == d8_docker.IMAGE_ID
    spec = policy["spec"]
    assert spec["network_mode"] == "none" and spec["read_only"] is True
    assert spec["mem_limit"] == spec["memswap_limit"] == "512m"
    assert spec["nano_cpus"] == 500_000_000 and spec["pids_limit"] == 128
    assert not {"volumes", "ports", "privileged"} & set(spec)
    raw["execution_profile"], raw["d8_config"] = "d8_v17", real_config()
    for name, role in (("outer", "outer_planning"), ("inner", "inner_action")):
        raw["dual_llm_config"][name + "_role"] = asdict(d8_model.role_config(role, version=12))
    assert parse_request(json.loads(json.dumps(raw)))[1].contract_version == 17
    assert raw["d8_config"]["sandbox_backend"] == "local_simulated"


@pytest.mark.parametrize("field,value", [("network_mode", "bridge"), ("read_only", False),
                                          ("mem_limit", "1g"), ("pids_limit", 0),
                                          ("volumes", {"/": {"bind": "/host"}})])
def test_changed_resource_policy_rejected_before_connection(tmp_path, monkeypatch, field, value):
    raw = new_request(tmp_path, real=True)
    raw["d8_config"]["sandbox_config"]["spec"][field] = value
    monkeypatch.setattr(d8_docker, "connect", lambda *a: pytest.fail("connected before validation"))
    with pytest.raises(ValueError, match="configuration_mismatch"):
        parse_request(raw)


def test_new18_public_simulation_uses_all_existing_gates(tmp_path, monkeypatch):
    result, args = start(tmp_path, monkeypatch, new_request(tmp_path))
    assert result["errors"] == [] and not result["recovery"]["required"], result
    assert result["contract_version"] == 18 and len(result["verified_facts"]) == 1
    assert result["goal_evaluation"] == "not_evaluated"
    with SqliteControlStore.open_readonly(args.state_dir / result["task_run_id"] / "control.sqlite") as store:
        assert store.list_d8_handoffs(result["task_run_id"])
        assert store.list_d8_decisions(result["task_run_id"])


def test_real18_public_assembly_cancel_without_model_key_and_status_without_docker(tmp_path, monkeypatch):
    # Explicit boundary substitutes prove wiring, not Docker/API behavior.
    class Model(SimulationModel):
        def __init__(self, role, settings):
            super().__init__(role)
        def bind_runtime(self, *args):
            pass
    connections = []
    def connect(configuration, directory):
        connections.append(configuration)
        sandbox = _LocalDigestSandbox(directory)
        return sandbox, sandbox
    monkeypatch.setattr(d8_model, "D8DeepSeekModel", Model)
    monkeypatch.setattr(d8_docker, "connect", connect)
    raw = new_request(tmp_path, real=True)
    env_file = private_fixture(tmp_path)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state",
                           events=None, env_file=env_file, fixture=None)
    result = task_cli.dispatch_run_task(args)
    assert not result["errors"] and not result["recovery"]["required"], result
    assert result["sandbox_backend"] == "docker" and result["contract_version"] == 18
    assert len(result["verified_facts"]) == 1 and len(connections) == 1
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    assert immutable_read(directory / "d8-runtime.json")["sandbox_backend"] == "docker"
    env_file.unlink()
    monkeypatch.setattr(d8_model, "D8DeepSeekModel", lambda *a: pytest.fail("cancel invoked model"))
    cancelled = d8_simulation.run_simulated_task(directory, declaration, operation="cancel")
    assert cancelled["cleanup_status"] == "confirmed", cancelled
    monkeypatch.setattr(d8_docker, "connect", lambda *a: pytest.fail("read or replay accessed Docker"))
    assert snapshot_v3_task_result(directory, declaration)["cleanup_status"] == "confirmed"
    assert d8_simulation.run_simulated_task(directory, declaration, operation="cancel")["cleanup_status"] == "confirmed"
    declaration["sandbox_backend"] = "local_simulated"
    assert snapshot_v3_task_result(directory, declaration)["recovery"]["required"]


@pytest.fixture
def port(tmp_path):
    reader, writer = socket.socketpair()
    api = SimpleNamespace(calls=[], output={"status": "present", "byte_size": 4,
                                           "sha256": hashlib.sha256(b"data").hexdigest()})
    def create(sandbox_id, argv, **kwargs):
        api.calls.append((sandbox_id, argv, kwargs))
        return {"Id": "fixed"}
    def start(identity, **kwargs):
        payload = json.dumps(api.output).encode()
        writer.sendall(bytes([1, 0, 0, 0])+len(payload).to_bytes(4, "big")+payload)
        writer.shutdown(socket.SHUT_WR)
        return reader
    api.exec_create, api.exec_start = create, start
    api.exec_inspect = lambda _: {"Running": False, "ExitCode": 0}
    binding = SimpleNamespace(task_run_id=str(uuid4()), execution_lock=threading.Lock())
    sandbox = SimpleNamespace(_binding=lambda _: binding, _verify_active=lambda *a: None,
                              _admit=lambda _: nullcontext(),
                              _runtime=SimpleNamespace(client=SimpleNamespace(api=api)))
    workspace = D8DockerWorkspace(sandbox, tmp_path)
    yield workspace, api, binding, SimpleNamespace(is_cancelled=False)
    reader.close(); writer.close()


def test_fixed_import_root_and_nonroot_copy_have_real_readback_shape(port):
    workspace, api, _, token = port
    got = asyncio.run(workspace.create_and_readback("sandbox", "/workspace/input/d3-attachment-0", b"data", 2, token))
    assert got == AttachmentImportReadback(4, hashlib.sha256(b"data").hexdigest())
    assert api.calls[0][2]["user"] == "0:0" and api.calls[0][2]["privileged"] is False
    assert api.calls[0][1][7] == WORKSPACE_SCRIPT


@pytest.mark.parametrize("path", ["/etc/passwd", "/workspace/input/../secret", "/workspace/input/d3-attachment-2",
                                 "/workspace/work/intents/not-uuid/attachment-x"])
def test_untrusted_paths_never_dispatch(port, path):
    workspace, api, _, token = port
    with pytest.raises(ValueError):
        asyncio.run(workspace.readback("sandbox", path, 1, token))
    assert not api.calls


def test_fixed_copy_is_nonroot_and_version_scoped(port):
    workspace, api, _, token = port
    target = artifact_path(str(uuid4()), str(uuid4()))
    asyncio.run(workspace.copy_exclusive_and_readback("sandbox", "/workspace/input/d3-attachment-0", target, 2, token))
    assert api.calls[0][2]["user"] == "10001:10001"
    assert api.calls[0][1][-4:] == ["copy", "/workspace/input/d3-attachment-0", target, "0"]


def test_existing_canonical_uuid5_workspace_identity_is_accepted(port):
    workspace, api, _, token = port
    target = artifact_path(str(uuid5(NAMESPACE_URL, "intent")), str(uuid5(NAMESPACE_URL, "artifact")))
    asyncio.run(workspace.copy_exclusive_and_readback("sandbox", "/workspace/input/d3-attachment-0", target, 2, token))
    assert len(api.calls) == 1


def test_cancel_before_fixed_operation_has_no_dispatch(port):
    workspace, api, _, token = port
    token.is_cancelled = True
    with pytest.raises(TimeoutError):
        asyncio.run(workspace.readback("sandbox", "/workspace/input/d3-attachment-0", 1, token))
    assert not api.calls


def test_lost_exec_start_does_not_retry(port):
    workspace, api, _, token = port
    starts = []
    def lost(*a, **kw):
        starts.append(1)
        raise TimeoutError("private provider detail")
    api.exec_start = lost
    with pytest.raises(TaskRunSandboxError, match="outcome_unknown") as raised:
        asyncio.run(workspace.readback("sandbox", "/workspace/input/d3-attachment-0", 1, token))
    assert "private" not in str(raised.value)
    assert len(starts) == len(api.calls) == 1


@pytest.mark.parametrize("status", ["missing", "unsafe"])
def test_digest_failure_status_is_not_fabricated_as_present(port, status):
    workspace, api, binding, token = port
    api.output = {"status": status}
    got = asyncio.run(workspace.hash_file(binding.task_run_id, "sandbox", artifact_path(str(uuid4()), str(uuid4())), 1, token))
    assert got.status == status and got.sha256 is None


def test_missing_termination_evidence_does_not_claim_absence_or_kill(port):
    workspace, api, binding, token = port
    ref = str(uuid4())
    claim = D3StopClaim(str(uuid4()), binding.task_run_id, str(uuid4()), str(uuid4()), str(uuid4()),
                        "sibling_switch", (ref,), 1.0)
    result = asyncio.run(workspace.stop_intent_processes(claim, "sandbox", 1, token))
    assert result.unknown_refs == (ref,) and not result.absent_refs and not api.calls


@pytest.mark.parametrize("output_case", [18], indirect=True)
@pytest.mark.parametrize("scenario", ["ready_complete", "unknown_review", "source_damage", "governance_damage", "unknown_governance"])
def test_new18_preserves_prior_trusted_gates(output_case, monkeypatch, scenario):
    from test_s5_governance_selection import modern
    from test_s5_governance_selection import test_known_binding_damage_pauses_without_another_model_call
    from test_s5_governance_selection import test_governance_unknown_does_not_redispatch_on_reopen
    from test_s4_completion_evaluation import test_missing_or_changed_size_stops_before_local_evaluation
    from test_d9_close_feasibility import test_current_chain_and_restart_accept_ready_complete_only
    from test_d9_close_feasibility import test_unknown_start_review_is_not_reasked
    for model in output_case.models.values():
        model.transform = modern
    if scenario == "ready_complete":
        test_current_chain_and_restart_accept_ready_complete_only(output_case)
    elif scenario == "unknown_review":
        test_unknown_start_review_is_not_reasked(output_case)
    elif scenario == "source_damage":
        test_missing_or_changed_size_stops_before_local_evaluation(output_case, monkeypatch, "missing")
    elif scenario == "governance_damage":
        test_known_binding_damage_pauses_without_another_model_call(output_case, "corrupt")
    else:
        test_governance_unknown_does_not_redispatch_on_reopen(output_case)
