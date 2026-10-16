"""Explicitly gated real Docker acceptance, with no real model or target calls."""

import asyncio
import hashlib
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import d8_docker, d8_model, d8_simulation, task_cli
from agent_adapters.run_files import immutable_read
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_core.artifact_workspace import artifact_path
from agent_core.artifact_operations import fixed_size_script
from agent_core.command_contracts import AuthorizedCommand, SandboxCommandRequest
from agent_core.command_contracts import T6CommandExecutionError
from Sandbox.command_runtime import T6CommandSandbox
from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.task_run_sandbox import TaskRunSandboxError
from test_d9_docker import new_request
from test_d9_models import private_fixture


pytestmark = pytest.mark.skipif(os.environ.get("QINYUAN_D9_DOCKER_ACCEPTANCE") != "1",
                                reason="requires explicit real Docker acceptance")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")


def test_real_workspace_isolation_recovery_timeout_and_explicit_cleanup(tmp_path):
    resources, workspace = d8_docker.connect(d8_docker.sandbox_config(), tmp_path)
    task = str(uuid4())
    sid = resources.create(task)
    save(tmp_path / "owned-resource.json", {"task_run_id": task, "sandbox_id": sid})
    token = SimpleNamespace(is_cancelled=False)
    intent, version = str(uuid4()), str(uuid4())
    path = artifact_path(intent, version)
    content = b"D9-3 real Docker fixed workspace acceptance\n"
    try:
        imported = asyncio.run(workspace.create_and_readback(sid, "/workspace/input/d3-attachment-0", content, 10, token))
        copied = asyncio.run(workspace.copy_exclusive_and_readback(sid, "/workspace/input/d3-attachment-0", path, 10, token))
        assert copied == imported
        assert copied.sha256 == hashlib.sha256(content).hexdigest()
        request = SandboxCommandRequest(str(uuid4()), task, str(uuid4()), 0,
                                        fixed_size_script(path), b"", path.rsplit("/", 1)[0], 10)
        approved = AuthorizedCommand(request, str(uuid4()), sid, 10)
        observed = asyncio.run(workspace.execute(approved, token))
        assert observed.termination_confirmed and observed.stream_complete and observed.exit_code == 0
        assert json.loads(observed.stdout)["byte_size"] == len(content)
        before = resources._runtime.client.containers.get(sid)
        assert before.attrs["HostConfig"]["NetworkMode"] == "none"
        assert before.attrs["HostConfig"]["ReadonlyRootfs"] is True
        assert before.attrs["Image"] == d8_docker.IMAGE_ID
        # Fixed trusted test only, not a newly exposed model action.
        probe = before.exec_run(["python3", "-I", "-c",
                                "import os,stat; p='/workspace/input/d3-attachment-0'; "
                                "assert stat.S_IMODE(os.stat(p).st_mode)==0o444; "
                                "assert not os.access(p,os.W_OK); "
                                "assert os.getuid()==10001"], user="10001:10001")
        assert probe.exit_code == 0
        other_task = str(uuid4())
        other_sid = resources.create(other_task)
        save(tmp_path / "other-owned-resource.json", {"task_run_id": other_task, "sandbox_id": other_sid})
        assert asyncio.run(workspace.hash_file(other_task, other_sid, path, 10, token)).status == "missing"
        resources.destroy(other_sid)
        assert resources.inspect_presence(other_task, other_sid) == "absent"
        wrong = T6TaskRunSandbox(resources._runtime)
        with pytest.raises(TaskRunSandboxError, match="ownership_mismatch"):
            wrong.bind(str(uuid4()), sid, terminal=False)
        # Reconnect to the exact container; never create or start during bind.
        resources.close()
        resources, workspace = d8_docker.connect(d8_docker.sandbox_config(), tmp_path)
        resources.bind(task, sid, terminal=False)
        assert asyncio.run(workspace.readback(sid, path, 10, token)) == copied
        # This trusted fixed sleep exercises the existing runtime timeout seam;
        # it is not admitted by D8DockerWorkspace or the public action Schema.
        wait = replace(request, request_id=str(uuid4()), script="exec python3 -I -c 'import time; time.sleep(30)'",
                       timeout_seconds=1, working_directory="/workspace/work")
        result = asyncio.run(T6CommandSandbox(resources).execute(AuthorizedCommand(wait, str(uuid4()), sid, 1), token))
        assert result.timed_out and result.termination_confirmed
        assert asyncio.run(workspace.readback(sid, path, 10, token)) == copied
        # Exact owned fixture resource, explicit cleanup after confirmed checks.
        resources.destroy(sid)
        assert resources.inspect_presence(task, sid) == "absent"
        save(tmp_path / "verified.json", {"task_run_id": task, "sandbox_id": sid,
             "import_and_copy_sha256": copied.sha256, "read_only_input": True,
             "same_container_rebound": True, "other_container_isolated": True, "timeout_termination_confirmed": True,
             "cleanup_confirmed": True})
    finally:
        # Failure keeps its container/evidence; no automatic retry or deletion.
        resources.close()


def test_public_core_with_controlled_models_and_real_docker(tmp_path, monkeypatch):
    class Model(d8_simulation.SimulationModel):
        calls = []
        def __init__(self, role, settings):
            super().__init__(role)
        def bind_runtime(self, *args):
            pass
        def invoke(self, envelope, *args):
            self.calls.append(envelope.output_contract["stage"])
            return super().invoke(envelope, *args)
    monkeypatch.setattr(d8_model, "D8DeepSeekModel", Model)
    raw = new_request(tmp_path, real=True)
    monkeypatch.setattr(task_cli, "_input", lambda _: raw)
    args = SimpleNamespace(request="unused", mode="real", state_dir=tmp_path / "state",
                           events=None, env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    save(tmp_path / "result.json", result)
    assert not result["errors"] and not result["recovery"]["required"], result
    assert result["sandbox_backend"] == "docker" and len(result["verified_facts"]) == 1
    assert result["goal_evaluation"] == "not_evaluated"
    directory = args.state_dir / result["task_run_id"]
    declaration = immutable_read(directory / "run.json")
    sid = result["task_run"]["sandbox_id"]
    count = len(Model.calls)
    resumed = d8_simulation.run_simulated_task(directory, declaration, operation="resume")
    assert len(Model.calls) == count and resumed["task_run"]["sandbox_id"] == sid
    assert len(resumed["verified_facts"]) == 1 and not resumed["recovery"]["required"]
    args.env_file.unlink()
    monkeypatch.setattr(d8_model, "D8DeepSeekModel", lambda *a: pytest.fail("cancel invoked model"))
    cancelled = d8_simulation.run_simulated_task(directory, declaration, operation="cancel")
    save(tmp_path / "cancelled.json", cancelled)
    assert cancelled["task_run"]["status"] == "cancelled" and cancelled["cleanup_status"] == "confirmed"
    monkeypatch.setattr(d8_docker, "connect", lambda *a: pytest.fail("readonly/replay connected to Docker"))
    assert snapshot_v3_task_result(directory, declaration)["cleanup_status"] == "confirmed"
    assert d8_simulation.run_simulated_task(directory, declaration, operation="cancel")["cleanup_status"] == "confirmed"
    save(tmp_path / "verified.json", {"model_kind": "controlled_not_real_api", "sandbox_kind": "real_docker",
         "task_run_id": result["task_run_id"], "sandbox_id": sid, "model_calls": count,
         "fact_count": 1, "known_resume_no_redispatch": True, "cancel_without_credentials": True,
         "cleanup_confirmed": True, "final_goal": "not_evaluated"})


def test_real_cancel_interrupts_started_owned_process(tmp_path, monkeypatch):
    resources, _ = d8_docker.connect(d8_docker.sandbox_config(), tmp_path)
    task = str(uuid4())
    sid = resources.create(task)
    save(tmp_path / "owned-resource.json", {"task_run_id": task, "sandbox_id": sid})
    token = SimpleNamespace(is_cancelled=False)
    started = threading.Event()
    execution_ids = []
    original = resources._runtime.client.api.exec_start
    def start_exec(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs.get("socket"):
            execution_ids.append(args[0])
            started.set()
        return value
    monkeypatch.setattr(resources._runtime.client.api, "exec_start", start_exec)
    errors = []
    def cancel():
        try:
            if not started.wait(10):
                raise RuntimeError("test_exec_not_started")
            token.is_cancelled = True
            resources.interrupt(sid)
        except Exception as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=cancel, daemon=True)
    request = SandboxCommandRequest(str(uuid4()), task, str(uuid4()), 0,
        "exec python3 -I -c 'import time; time.sleep(30)'", b"", "/workspace/work", 15)
    try:
        worker.start()
        try:
            asyncio.run(T6CommandSandbox(resources).execute(AuthorizedCommand(request, str(uuid4()), sid, 15), token))
        except T6CommandExecutionError:
            pass
        worker.join(10)
        assert not worker.is_alive() and not errors and started.is_set()
        assert len(execution_ids) == 1
        assert resources._runtime.client.api.exec_inspect(execution_ids[0])["Running"] is False
        container = resources._runtime.client.containers.get(sid)
        container.reload()
        assert container.attrs["State"]["Running"] is False
        resources.destroy(sid)
        assert resources.inspect_presence(task, sid) == "absent"
        save(tmp_path / "verified.json", {"task_run_id": task, "sandbox_id": sid,
             "started_before_cancel": True, "container_processes_stopped": True, "cleanup_confirmed": True})
    finally:
        resources.close()
