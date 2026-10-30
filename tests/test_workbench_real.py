"""D7 real Web profile stays an explicit, isolated workbench mode."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from agent_adapters.run_files import immutable_write
from agent_adapters.web.web_request import D7WebRequest
from agent_adapters.workbench import Workbench, WorkbenchError
from agent_adapters.workbench_runner import D7_ALLOWED_METHODS
from agent_core.web_contracts import canonical_web_origin
from agent_core.operation_accounting import D4BudgetPolicy


class _Process:
    returncode = None

    def poll(self):
        return self.returncode


class _NoThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        pass


def _challenge(*, endpoint: str, description: str = "Find the flag on this authorized Web origin"):
    return {
        "title": "D7 Web profile",
        "category": "web",
        "description": description,
        "endpoint": endpoint,
        "deadline_minutes": 5,
        "step_timeout_seconds": 30,
    }


def _write_valid_env(path: Path) -> None:
    path.write_text(
        "QINYUAN_LLM_BACKEND=openai\n"
        "QINYUAN_MODEL=deepseek-chat\n"
        "OPENAI_API_KEY=fixture-secret-not-for-child-env\n"
        "OPENAI_BASE_URL=https://api.deepseek.com/v1\n"
        "QINYUAN_LLM_TIMEOUT_SECONDS=180\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("category", ["web", "pwn"])
def test_real_profile_builds_authorized_web_request_and_keeps_secret_in_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category
):
    env_file = tmp_path / ".env"
    _write_valid_env(env_file)
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", env_file)
    workbench = Workbench(tmp_path / "data", protocol_version=3, d7_mode="real")
    challenge = workbench.create(
        {**_challenge(endpoint="http://node4.anna.nssctf.cn:23522/"), "category": category}
    )

    with patch.dict(
        "os.environ",
        {
            "QINYUAN_RUNTIME_MODEL": "ambient-model",
            "QINYUAN_RUNTIME_API_KEY": "ambient-secret",
            "OPENAI_API_KEY": "ambient-openai-secret",
            "QINYUAN_MODEL": "ambient-model",
        },
    ), patch(
        "agent_adapters.workbench_runner.subprocess.Popen", return_value=_Process()
    ) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", _NoThread
    ):
        run = workbench.runner.start(challenge["id"])

    command = popen.call_args.args[0]
    assert command[command.index("--mode") + 1] == "real"
    assert command[command.index("--env-file") + 1] == str(env_file)
    request_path = Path(run["directory"]) / "request.json"
    request_text = request_path.read_text(encoding="utf-8")
    request = json.loads(request_text)
    assert request["protocol_version"] == 3
    assert f"题型（任务提供）：{category}" in request["task_request"]["goal"]
    assert request["task_request"]["target"] == {
        "attachments": [],
        "endpoints": [{"url": "http://node4.anna.nssctf.cn:23522"}],
    }
    assert request["authorization"] == {
        "authorization_id": request["authorization"]["authorization_id"],
        "allowed_origin": "http://node4.anna.nssctf.cn:23522",
        "allowed_methods": list(D7_ALLOWED_METHODS),
        "allowed_tool": "d7_http_request",
        "network_allowed": True,
        "attachment_indices": [],
    }
    assert request["dual_llm_config"]["outer_role"] == {
        "role": "outer_planning",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "prompt_template": "qinyuan.d7.outer.v1",
        "parameters": [],
    }
    assert request["dual_llm_config"]["inner_role"]["prompt_template"] == "qinyuan.d7.inner.v1"
    assert request["dual_llm_config"]["inner_role"]["model"] == "deepseek-chat"
    assert request["dual_llm_config"]["d4_budget"]["total"] == D4BudgetPolicy().total
    D7WebRequest.parse(request)
    assert "fixture-secret" not in request_text
    assert "ambient-secret" not in request_text
    child_env = popen.call_args.kwargs["env"]
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "QINYUAN_RUNTIME_API_KEY",
        "QINYUAN_MODEL",
    ):
        assert name not in child_env
    assert canonical_web_origin(request["authorization"]["allowed_origin"]) == request[
        "authorization"
    ]["allowed_origin"]


@pytest.mark.parametrize(
    ("endpoint", "attachment"),
    [
        ("", False),
        ("tcp://node4.anna.nssctf.cn:23522", False),
        ("http://node4.anna.nssctf.cn:23522/extra", False),
        ("http://node4.anna.nssctf.cn:23522/", True),
    ],
)
def test_real_profile_rejects_non_web_or_attachment_inputs_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    attachment: bool,
):
    env_file = tmp_path / ".env"
    _write_valid_env(env_file)
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", env_file)
    workbench = Workbench(tmp_path / "data", protocol_version=3, d7_mode="real")
    challenge = workbench.create(_challenge(endpoint=endpoint))
    if attachment:
        workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"x"), 1)

    with patch("agent_adapters.workbench_runner.subprocess.Popen") as popen:
        with pytest.raises(WorkbenchError):
            workbench.runner.start(challenge["id"])

    popen.assert_not_called()
    assert workbench.get(challenge["id"])["run"] is None


def test_real_profile_rejects_incompatible_env_without_exposing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QINYUAN_LLM_BACKEND=openai\n"
        "QINYUAN_MODEL=private-model-name\n"
        "OPENAI_API_KEY=fixture-secret-not-for-output\n"
        "OPENAI_BASE_URL=https://model.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", env_file)
    workbench = Workbench(tmp_path / "data", protocol_version=3, d7_mode="real")
    challenge = workbench.create(
        _challenge(endpoint="http://node4.anna.nssctf.cn:23522/")
    )

    with patch("agent_adapters.workbench_runner.subprocess.Popen") as popen:
        with pytest.raises(WorkbenchError, match="d7_model_env_configuration_unsupported") as caught:
            workbench.runner.start(challenge["id"])

    popen.assert_not_called()
    assert "fixture-secret" not in str(caught.value)
    assert "private-model-name" not in str(caught.value)
    assert workbench.get(challenge["id"])["run"] is None


def _saved_real_run(workbench: Workbench, *, task_status: str):
    challenge = workbench.create(
        _challenge(endpoint="http://node4.anna.nssctf.cn:23522/")
    )
    run_id = str(uuid4())
    task_id = str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(
        agent_directory / "run.json",
        {"protocol_version": 3, "mode": "real", "task_run_id": task_id},
    )
    workbench._insert_run(run_id, challenge["id"], directory)
    result = {
        "protocol_version": 3,
        "task_run_id": task_id,
        "task_run": {"status": task_status},
        "cleanup": {"status": "not_confirmed"},
        "recovery": {"required": True},
    }
    workbench._finish_run(
        run_id,
        status="recovery_required",
        exit_code=5,
        result=result,
        agent_task_run_id=task_id,
    )
    return challenge["id"], task_id


@pytest.mark.parametrize(
    ("operation", "task_status"),
    [("cancel", "awaiting_external_feedback"), ("cleanup", "cancelled")],
)
def test_stopped_real_run_lifecycle_reuses_cli_and_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    task_status: str,
):
    env_file = tmp_path / ".env"
    _write_valid_env(env_file)
    monkeypatch.setattr("agent_adapters.workbench_runner.D7_ENV_FILE", env_file)
    workbench = Workbench(tmp_path / "data", protocol_version=3, d7_mode="simulated")
    challenge_id, task_id = _saved_real_run(workbench, task_status=task_status)

    with patch(
        "agent_adapters.workbench_runner.subprocess.Popen", return_value=_Process()
    ) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", _NoThread
    ):
        if operation == "cancel":
            workbench.runner.cancel(challenge_id)
        else:
            workbench.runner.cleanup(challenge_id)

    command = popen.call_args.args[0]
    assert command[3] == ("cancel" if operation == "cancel" else "resume")
    if operation == "cleanup":
        assert "--cleanup-only" in command
    assert "--task-run-id" in command and task_id in command
    assert command[command.index("--env-file") + 1] == str(env_file)
    assert "OPENAI_API_KEY" not in popen.call_args.kwargs["env"]


def test_real_mode_requires_protocol_v3(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported_d7_mode"):
        Workbench(tmp_path, protocol_version=2, d7_mode="real")
    with pytest.raises(ValueError, match="unsupported_d7_mode"):
        Workbench(tmp_path / "simulated", protocol_version=2, d7_mode="simulated")
