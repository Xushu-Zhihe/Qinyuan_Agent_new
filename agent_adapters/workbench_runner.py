"""Process bridge from the local workbench to the real Agent CLI.

The workbench is a control surface, not an alternate Agent implementation.
Every running row below corresponds to one child process and one CLI state
directory. Scheduling and lifecycle transitions remain owned by the CLI.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from agent_adapters.run_files import encode, immutable_read, immutable_write
from agent_adapters.workbench_contracts import ACTIVE_RUN_STATUSES, WorkbenchError
from agent_core.web_contracts import canonical_web_origin
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.command_contracts import T6EvidenceError
from agent_core.runtime import TERMINAL_STATUSES

D7_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
D7_ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


@dataclass
class ActiveRun:
    run_id: str
    challenge_id: str
    directory: Path
    process: subprocess.Popen
    cancel_requested: bool = False
    cleanup_only: bool = False


class Runner:
    """Own independent CLI processes and durable workbench run metadata."""

    def __init__(self, workbench):
        self.workbench = workbench
        self.root = workbench.root / "runs"
        self.root.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._active: dict[str, ActiveRun] = {}

    @staticmethod
    def _safe_filename(name: str) -> str:
        if not name or name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
            raise ValueError("invalid_attachment_name")
        return name

    @staticmethod
    def _subprocess_environment(protocol_version: int):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        if protocol_version == 3:
            for key in (
                "QINYUAN_RUNTIME_MODEL", "QINYUAN_RUNTIME_API_KEY",
                "QINYUAN_RUNTIME_BASE_URL", "QINYUAN_RUNTIME_PROVIDER",
                "QINYUAN_MODEL", "QINYUAN_LLM_BACKEND", "QINYUAN_LLM_TIMEOUT_SECONDS",
                "OPENAI_API_KEY", "OPENAI_BASE_URL",
            ):
                env.pop(key, None)
        return env

    @staticmethod
    def _d7_model_from_env() -> str:
        """Read only the non-secret D7 model identifier from the repository env file."""
        env_file = D7_ENV_FILE
        try:
            if env_file.is_symlink() or not env_file.is_file():
                raise ValueError("env_file_unavailable")
            from dotenv import dotenv_values

            values = dotenv_values(env_file, interpolate=False)
            backend = values.get("QINYUAN_LLM_BACKEND")
            model = values.get("QINYUAN_MODEL")
            api_key = values.get("OPENAI_API_KEY")
            base_url = values.get("OPENAI_BASE_URL")
            timeout_raw = values.get("QINYUAN_LLM_TIMEOUT_SECONDS") or "180"
            if (
                backend != "openai"
                or not isinstance(model, str)
                or not model.strip()
                or len(model.strip()) > 128
                or not isinstance(api_key, str)
                or not api_key.strip()
                or len(api_key) > 8192
                or any(ord(char) < 32 for char in api_key)
                or not isinstance(base_url, str)
            ):
                raise ValueError("env_configuration_unsupported")
            try:
                timeout = float(timeout_raw)
                parsed = urlsplit(base_url)
            except (TypeError, ValueError):
                raise ValueError("env_configuration_unsupported") from None
            if (
                not math.isfinite(timeout)
                or not 0 < timeout <= 180
                or parsed.scheme != "https"
                or parsed.hostname != "api.deepseek.com"
                or parsed.netloc not in {"api.deepseek.com", "api.deepseek.com:443"}
                or parsed.path not in {"", "/", "/v1", "/v1/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("env_configuration_unsupported")
            return model.strip()
        except (OSError, UnicodeError, ValueError, ImportError):
            raise WorkbenchError("d7_model_env_configuration_unsupported", 409) from None

    @staticmethod
    def _append_saved_d7_env_file(command: list[str], agent_directory: Path) -> None:
        try:
            declaration = immutable_read(agent_directory / "run.json")
        except (OSError, ValueError, TypeError):
            raise WorkbenchError("run_identity_unavailable", 409) from None
        if (
            not isinstance(declaration, dict)
            or declaration.get("task_run_id") != agent_directory.name
        ):
            raise WorkbenchError("run_identity_unavailable", 409)
        if (declaration.get("protocol_version") == 3 and declaration.get("mode") == "real"
                and declaration.get('execution_profile') not in {'d8_v19', 'd8_v20', 'd8_v21', 'd8_v22'}):
            command.extend(["--env-file", str(D7_ENV_FILE)])

    def _preflight(self, challenge, *, mode=None):
        if challenge["archived"]:
            raise WorkbenchError("challenge_archived", 409)
        if challenge["status"] in ACTIVE_RUN_STATUSES:
            raise WorkbenchError("challenge_already_running", 409)
        if self.workbench.protocol_version == 2 and not self.workbench.api_key:
            raise WorkbenchError("model_key_not_configured", 409)
        if not 1 <= challenge["step_timeout_seconds"] <= 300:
            raise WorkbenchError("invalid_step_timeout", 409)
        if not challenge["description"].strip():
            raise WorkbenchError("challenge_description_required_for_run", 409)
        attachments = challenge["attachments"]
        d7_model = None
        if self.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
            if mode not in {"simulated", "real"}:
                raise WorkbenchError("mode_required", 409)
            if mode == "real":
                from agent_adapters.deepseek_roles import role_config, settings_from_file
                try:
                    prompt_version = int(self.workbench.execution_profile.removeprefix("d8_v")) - 5
                    settings_from_file(role_config("outer_planning", version=prompt_version), D7_ENV_FILE)
                    settings_from_file(role_config("inner_action", version=prompt_version), D7_ENV_FILE)
                except (TypeError, ValueError, OSError):
                    raise WorkbenchError("d8_model_configuration_unavailable", 409) from None
        if self.workbench.protocol_version == 3:
            if self.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} or mode == "simulated" or self.workbench.d7_mode == "simulated":
                if challenge["endpoint"]:
                    if self.workbench.execution_profile not in {'d8_v15', 'd8_v16', 'd8_v17', 'd8_v18', 'd8_v19', 'd8_v20', 'd8_v21', 'd8_v22'}:
                        raise WorkbenchError("v3_network_not_supported", 409)
                    if mode != 'real':
                        raise WorkbenchError('d20_http_requires_real_mode', 409)
                    if attachments and self.workbench.execution_profile=='d8_v20':
                        raise WorkbenchError('d20_http_attachments_unsupported', 409)
                    from agent_adapters.workbench import http_methods
                    http_methods(challenge.get('http_methods'))
                    try:
                        canonical_web_origin(challenge['endpoint'])
                    except (TypeError, ValueError):
                        raise WorkbenchError('d20_http_endpoint_invalid', 409) from None
            else:
                if not challenge["endpoint"]:
                    raise WorkbenchError("d7_web_endpoint_required", 409)
                if attachments:
                    raise WorkbenchError("d7_web_attachments_unsupported", 409)
                try:
                    canonical_web_origin(challenge["endpoint"])
                except (TypeError, ValueError):
                    raise WorkbenchError("d7_web_endpoint_invalid", 409) from None
                d7_model = self._d7_model_from_env()
        if self.workbench.protocol_version == 2 and not challenge["endpoint"] and not attachments:
            raise WorkbenchError("runner_requires_local_attachment", 409)
        if len(attachments) > 16:
            raise WorkbenchError("too_many_attachments", 409)
        if sum(int(item["byte_size"]) for item in attachments) > 64 * 1024 * 1024:
            raise WorkbenchError("attachment_run_capacity_exceeded", 409)
        return d7_model

    def start(self, challenge_id: str, *, mode=None):
        with self.workbench.lock:
            challenge = self.workbench.get(challenge_id)
            if self.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
                if mode is not None and mode not in {"simulated", "real"}:
                    raise WorkbenchError("mode_required", 409)
                if (mode is not None and self.workbench.d7_mode is not None
                        and mode != self.workbench.d7_mode):
                    raise WorkbenchError("mode_configuration_mismatch", 409)
                mode = mode if mode is not None else self.workbench.d7_mode
            else:
                mode = self.workbench.d7_mode
            d7_model = self._preflight(challenge, mode=mode)
            names = [self._safe_filename(item["name"]) for item in challenge["attachments"]]
            if len(set(names)) != len(names):
                raise WorkbenchError("duplicate_attachment_names", 409)
            run_id = str(uuid4())
            directory = self.root / run_id
            input_dir = directory / "input"
            state_dir = directory / "agent-state"
            input_dir.mkdir(parents=True)
            state_dir.mkdir()
            for item, name in zip(challenge["attachments"], names):
                source = self.workbench.uploads / challenge_id / item["id"]
                target = input_dir / name
                if source.is_symlink() or not source.is_file():
                    raise WorkbenchError("attachment_path_rejected", 409)
                shutil.copyfile(source, target)

            if self.workbench.protocol_version == 3 and (mode == "simulated" or self.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}):
                # Temporary D7 simulation envelope.  It intentionally has no
                # endpoint/network capability and uses deterministic local
                # role snapshots; the child remains the sole scheduler.
                role_outer = {
                    "role": "outer_planning", "provider": "fixture",
                    "model": "d7-simulated-outer", "prompt_template": "d7-simulated-outer-v1",
                    "parameters": [],
                }
                role_inner = {
                    "role": "inner_action", "provider": "fixture",
                    "model": "d7-simulated-inner", "prompt_template": "d7-simulated-inner-v1",
                    "parameters": [],
                }
                request = {
                    "protocol_version": 3,
                    "request_id": str(uuid4()),
                    "task_request": {
                        "goal": challenge["description"],
                        "target": {
                            "source_root": str(input_dir.resolve()),
                            "attachments": [
                                {"kind": "local_path", "local_path": name}
                                for name in names
                            ],
                            "endpoints": [],
                        },
                        "deadline_seconds": challenge["deadline_minutes"] * 60,
                    },
                    "authorization": {
                        "authorization_id": str(uuid4()),
                        "attachment_indices": list(range(len(names))),
                        "allowed_tool": "d3_fixed_readonly",
                        "network_allowed": False,
                    },
                    "dual_llm_config": {
                        "outer_role": role_outer,
                        "inner_role": role_inner,
                        "d4_budget": asdict(D4BudgetPolicy()),
                    },
                }
                if self.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
                    from agent_adapters.simulation import simulation_config
                    from agent_core.operation_accounting import D8HandoffBudgetPolicy
                    request.update(execution_profile=self.workbench.execution_profile, mode=mode,
                                   d8_config=simulation_config())
                    request["dual_llm_config"]["d4_budget"] = asdict(D8HandoffBudgetPolicy())
                    for role, value in (("outer", role_outer), ("inner", role_inner)):
                        value["model"] = f"d8-simulated-{role}"
                        value["prompt_template"] = f"d8-simulated-{role}-v{int(self.workbench.execution_profile.removeprefix('d8_v')) - 5}"
                    if mode == "real":
                        from agent_adapters.simulation import real_config
                        from agent_adapters.deepseek_roles import role_config
                        is_http = self.workbench.execution_profile in {'d8_v20','d8_v21','d8_v22'} and bool(challenge['endpoint'])
                        request["d8_config"] = real_config(contract_version=int(self.workbench.execution_profile.removeprefix("d8_v")),
                            http=is_http, http_timeout_seconds=challenge['step_timeout_seconds'])
                        if is_http:
                            origin = canonical_web_origin(challenge['endpoint'])
                            request['task_request']['target']['endpoints'] = [{'url': origin}]
                            request['authorization'] = dict(authorization_id=str(uuid4()), allowed_origin=origin,
                                allowed_methods=challenge['http_methods'], allowed_tool='d7_http_request',
                                network_allowed=True, attachment_indices=list(range(len(names))) if self.workbench.execution_profile in {'d8_v21','d8_v22'} else [])
                        prompt_version = int(self.workbench.execution_profile.removeprefix("d8_v")) - 5
                        request["dual_llm_config"]["outer_role"] = asdict(role_config("outer_planning", version=prompt_version))
                        request["dual_llm_config"]["inner_role"] = asdict(role_config("inner_action", version=prompt_version))
            elif self.workbench.protocol_version == 3:
                origin = canonical_web_origin(challenge["endpoint"])
                role_outer = {
                    "role": "outer_planning", "provider": "deepseek",
                    "model": d7_model, "prompt_template": "qinyuan.d7.outer.v1",
                    "parameters": [],
                }
                role_inner = {
                    "role": "inner_action", "provider": "deepseek",
                    "model": d7_model, "prompt_template": "qinyuan.d7.inner.v1",
                    "parameters": [],
                }
                request = {
                    "protocol_version": 3,
                    "request_id": str(uuid4()),
                    "task_request": {
                        "goal": challenge["description"],
                        "target": {
                            "attachments": [],
                            "endpoints": [{"url": origin}],
                        },
                        "deadline_seconds": challenge["deadline_minutes"] * 60,
                    },
                    "authorization": {
                        "authorization_id": str(uuid4()),
                        "allowed_origin": origin,
                        "allowed_methods": list(D7_ALLOWED_METHODS),
                        "allowed_tool": "d7_http_request",
                        "network_allowed": True,
                        "attachment_indices": [],
                    },
                    "dual_llm_config": {
                        "outer_role": role_outer,
                        "inner_role": role_inner,
                        "d4_budget": asdict(D4BudgetPolicy()),
                    },
                }
                from agent_adapters.web.web_request import D7WebRequest

                try:
                    D7WebRequest.parse(request)
                except (TypeError, ValueError):
                    raise WorkbenchError("d7_web_request_invalid", 409) from None
            else:
                request = {
                    "protocol_version": 2,
                    "task_request": {
                        "goal": challenge["description"],
                        "target": {"attachments": [
                            {"kind": "local_path", "path": "input/" + name}
                            for name in names
                        ], "endpoints": ([{"url": challenge["endpoint"]}]
                                        if challenge["endpoint"] else [])},
                        "deadline_seconds": challenge["deadline_minutes"] * 60,
                        "model_backend": "deepseek_openai",
                    },
                    "authorization": {
                        "allowed_tools": ["sandbox.command"],
                        "allowed_target_urls": ([challenge["endpoint"]]
                                                if challenge["endpoint"] else []),
                    },
                }
            (directory / "request.json").write_bytes(encode(request))
            events = directory / "events.jsonl"
            env = self._subprocess_environment(self.workbench.protocol_version)
            # v3 loads its frozen provider settings from the explicit env file;
            # neither simulation nor real mode receives secrets in process env.
            env["QINYUAN_RUNTIME_MODEL_TIMEOUT"] = "180"
            env["QINYUAN_T6_STEP_TIMEOUT_SECONDS"] = str(challenge["step_timeout_seconds"])
            if self.workbench.protocol_version == 2:
                env.update(
                    {
                        "QINYUAN_RUNTIME_MODEL": self.workbench.model,
                        "QINYUAN_RUNTIME_API_KEY": self.workbench.api_key,
                        "QINYUAN_RUNTIME_BASE_URL": self.workbench.base_url,
                        "QINYUAN_RUNTIME_PROVIDER": self.workbench.provider,
                    }
                )
            command = [
                sys.executable,
                "-m",
                "agent",
                "run-task",
                "--mode",
                (
                    mode
                    if self.workbench.protocol_version == 3
                    else "real"
                ),
                "--request",
                str(directory / "request.json"),
                "--state-dir",
                str(state_dir),
                "--events",
                str(events),
            ]
            if self.workbench.protocol_version == 3 and mode == "real":
                command.extend(["--env-file", str(D7_ENV_FILE)])
            elif self.workbench.protocol_version == 2:
                command.extend(["--env-file", str(directory / "unused.env")])
            self.workbench._insert_run(run_id, challenge_id, directory)
            try:
                stdout_file = (directory / "agent.stdout.log").open("wb")
                stderr_file = (directory / "agent.stderr.log").open("wb")
                process = subprocess.Popen(
                    command,
                    cwd=directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            except OSError as exc:
                for stream in (locals().get("stdout_file"), locals().get("stderr_file")):
                    if stream is not None:
                        stream.close()
                self.workbench._finish_run(
                    run_id,
                    status="failed",
                    exit_code=None,
                    result={"protocol_version": 1, "errors": [{"code": "agent_process_start_failed"}]},
                    agent_task_run_id=None,
                )
                raise WorkbenchError("agent_process_start_failed", 503) from exc
            else:
                stdout_file.close()
                stderr_file.close()
            with self._lock:
                self._active[run_id] = ActiveRun(run_id, challenge_id, directory, process)
            self.workbench._started_run(run_id)
        threading.Thread(target=self._watch, args=(run_id,), daemon=True).start()
        return self.status(challenge_id)

    def _agent_run_directory(self, active: ActiveRun):
        return self._find_agent_directory(active.directory)

    def cancel(self, challenge_id: str):
        run = self._current(challenge_id)
        if run and run["status"] in {"paused", "recovery_required"}:
            return self._cancel_stopped(challenge_id)
        if not run or run["status"] not in {"starting", "running", "cancelling"}:
            raise WorkbenchError("challenge_not_running", 409)
        with self.workbench.lock, self._lock:
            active = self._active.get(run["run_id"])
            if active is None:
                raise WorkbenchError("run_process_not_owned", 409)
            self.workbench._mark_cancel_requested(run["run_id"])
            active.cancel_requested = True
            agent_dir = self._agent_run_directory(active)
            if agent_dir:
                immutable_write(agent_dir / "cancel-request.json", {"task_run_id": agent_dir.name})
        return self.status(challenge_id)

    def _cancel_stopped(self, challenge_id: str):
        # Explicit user cancellation re-enters the CLI lifecycle boundary. It
        # never resumes the model, changes control rows, or calls Docker here.
        with self.workbench.lock:
            run = self._current(challenge_id)
            if run is None or run["status"] not in {"paused", "recovery_required"}:
                raise WorkbenchError("challenge_not_running", 409)
            if run["status"] == "recovery_required":
                result = run.get("result")
                task = result.get("task_run") if isinstance(result, dict) else None
                if (not isinstance(result, dict)
                        or result.get("protocol_version") not in {2, 3}
                        or not run["agent_task_run_id"]
                        or result.get("task_run_id") != run["agent_task_run_id"]
                        or not isinstance(task, dict)
                        or task.get("status") not in {"running", "awaiting_confirmation", "awaiting_external_feedback"}):
                    raise WorkbenchError("cancel_not_available", 409)
            directory = Path(run["directory"])
            agent_dir = self._find_agent_directory(directory)
            if agent_dir is None or agent_dir.name != run["agent_task_run_id"]:
                raise WorkbenchError("run_identity_unavailable", 409)
            command = [sys.executable, "-m", "agent", "cancel", "--task-run-id",
                       agent_dir.name, "--state-dir", str(agent_dir.parent),
                       "--events", str(directory / "events.jsonl")]
            self._append_saved_d7_env_file(command, agent_dir)
            env = self._subprocess_environment((run.get("result") or {}).get("protocol_version", 2))
            try:
                with (directory / "agent.stdout.log").open("wb") as stdout, (directory / "agent.stderr.log").open("wb") as stderr:
                    process = subprocess.Popen(command, cwd=directory, env=env,
                                               stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            except OSError as exc:
                raise WorkbenchError("agent_cancel_start_failed", 503) from exc
            self.workbench._mark_cancel_requested(run["run_id"])
            with self._lock:
                self._active[run["run_id"]] = ActiveRun(
                    run["run_id"], challenge_id, directory, process,
                )
        threading.Thread(target=self._watch, args=(run["run_id"],), daemon=True).start()
        return self.status(challenge_id)

    def cleanup(self, challenge_id: str):
        """Explicitly retry CLI cleanup for a terminal TaskRun with unknown cleanup."""
        with self.workbench.lock, self._lock:
            run = self._current(challenge_id)
            result = run.get("result") if run else None
            task = result.get("task_run") if isinstance(result, dict) else None
            cleanup = result.get("cleanup") if isinstance(result, dict) else None

            # Relaxed conditions: allow cleanup if task is in recovery_required OR has incomplete operations
            if not run:
                raise WorkbenchError("cleanup_not_available", 409)

            # Allow cleanup for recovery_required tasks or tasks with failed cleanup
            if run["status"] not in {"recovery_required", "failed", "completed"}:
                # Task is still running, cannot cleanup
                if run["status"] == "running":
                    raise WorkbenchError("cleanup_not_available", 409)

            # Skip protocol version check for more flexibility
            if result and result.get("protocol_version") not in {2, 3}:
                raise WorkbenchError("cleanup_not_available", 409)

            # If cleanup already succeeded, no need to retry
            if isinstance(cleanup, dict) and cleanup.get("status") == "succeeded":
                raise WorkbenchError("cleanup_not_available", 409)

            directory = Path(run["directory"])
            agent_dir = self._find_agent_directory(directory)
            if agent_dir is None or agent_dir.name != run["agent_task_run_id"]:
                raise WorkbenchError("run_identity_unavailable", 409)
            command = [
                sys.executable, "-m", "agent", "resume", "--task-run-id",
                agent_dir.name, "--state-dir", str(agent_dir.parent), "--cleanup-only",
                "--events", str(directory / "events.jsonl"),
            ]
            self._append_saved_d7_env_file(command, agent_dir)
            env = self._subprocess_environment(result.get("protocol_version", 2))
            try:
                with (directory / "agent.stdout.log").open("wb") as stdout, (directory / "agent.stderr.log").open("wb") as stderr:
                    process = subprocess.Popen(
                        command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                        stdout=stdout, stderr=stderr,
                    )
            except OSError as exc:
                raise WorkbenchError("agent_cleanup_start_failed", 503) from exc
            self.workbench._mark_cleanup_started(run["run_id"])
            self._active[run["run_id"]] = ActiveRun(
                run["run_id"], challenge_id, directory, process,
                cleanup_only=True,
            )
        threading.Thread(target=self._watch, args=(run["run_id"],), daemon=True).start()
        return self.status(challenge_id)

    def resume(self, challenge_id: str):
        return self._d9_operation(challenge_id, 'resume')

    def validation_check(self, challenge_id: str, selection: dict):
        return self._d9_operation(challenge_id, 'validation-check', selection)

    def _d9_operation(self, challenge_id, operation, selection=None):
        """Only the CLI owns checks, continuation and persisted transitions."""
        from agent_adapters.web.task_result import snapshot_v3_task_result
        from agent_adapters.validation_check import validate_selection
        with self.workbench.lock, self._lock:
            run = self._current(challenge_id)
            if run is None or run['status'] not in {'paused', 'recovery_required', 'finished', 'cleanup_confirmed'}:
                raise WorkbenchError('d9_operation_not_available', 409)
            directory = Path(run['directory'])
            agent_dir = self._find_agent_directory(directory)
            if agent_dir is None or agent_dir.name != run['agent_task_run_id']:
                raise WorkbenchError('run_identity_unavailable', 409)
            declaration = immutable_read(agent_dir / 'run.json')
            if (not isinstance(declaration, dict) or declaration.get('task_run_id') != agent_dir.name
                    or (declaration.get('contract_version'), declaration.get('execution_profile')) not in {(19,'d8_v19'),(20,'d8_v20'),(21,'d8_v21'),(22,'d8_v22')}):
                raise WorkbenchError('d9_operation_profile_unsupported', 409)
            result = snapshot_v3_task_result(agent_dir, declaration)
            if operation == 'resume' and result.get('task_run', {}).get('status') != 'running':
                raise WorkbenchError('d9_resume_requires_running_task', 409)
            command = [sys.executable, '-m', 'agent', operation, '--task-run-id',
                       agent_dir.name, '--state-dir', str(agent_dir.parent),
                       '--events', str(directory / 'events.jsonl')]
            if operation == 'validation-check':
                selection = validate_selection(agent_dir.name, selection)
                for key, value in selection.items():
                    command.extend(['--' + key.replace('_', '-'), value])
            try:
                with (directory / 'agent.stdout.log').open('wb') as stdout, (directory / 'agent.stderr.log').open('wb') as stderr:
                    process = subprocess.Popen(command, cwd=directory,
                        env=self._subprocess_environment(3), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            except OSError as exc:
                raise WorkbenchError('d9_operation_start_failed', 503) from exc
            self._active[run['run_id']] = ActiveRun(run['run_id'], challenge_id, directory, process)
            self.workbench._started_run(run['run_id'])
        threading.Thread(target=self._watch, args=(run['run_id'],), daemon=True).start()
        return self.status(challenge_id)

    def batch_cleanup(self, challenge_ids: list[str]):
        """Cleanup multiple challenges at once."""
        results = []
        for challenge_id in challenge_ids:
            try:
                # Get current run info for better error reporting
                run = self._current(challenge_id)
                if not run:
                    results.append({
                        "challenge_id": challenge_id,
                        "status": "skipped",
                        "reason": "no_active_run"
                    })
                    continue

                result = self.cleanup(challenge_id)
                results.append({
                    "challenge_id": challenge_id,
                    "status": "started",
                    "result": result
                })
            except WorkbenchError as exc:
                results.append({
                    "challenge_id": challenge_id,
                    "status": "failed",
                    "error": exc.code,
                    "run_status": run.get("status") if run else None
                })
            except Exception as exc:
                results.append({
                    "challenge_id": challenge_id,
                    "status": "failed",
                    "error": str(exc)
                })
        return results

    def force_delete(self, challenge_id: str):
        """Force delete a challenge and all its data without any checks."""
        import docker
        import shutil

        with self.workbench.lock, self._lock:
            run = self._current(challenge_id)
            if not run:
                raise WorkbenchError("no_run_found", 404)

            directory = Path(run["directory"])

            # Stop any active process
            if run["run_id"] in self._active:
                active = self._active[run["run_id"]]
                if active.process.poll() is None:
                    active.process.terminate()
                    active.process.wait(timeout=5)
                del self._active[run["run_id"]]

            # Stop all Docker containers and networks
            try:
                client = docker.from_env()
                agent_dirs = list(directory.glob("agent-state/*"))
                agent_dirs = [d for d in agent_dirs if d.is_dir() and d.name != "requests"]

                for agent_dir in agent_dirs:
                    task_run_id = agent_dir.name

                    # Stop containers
                    for container_name in [f"qinyuan-task-{task_run_id}", f"qinyuan-t6-forwarder-{task_run_id}"]:
                        try:
                            container = client.containers.get(container_name)
                            container.stop(timeout=1)
                            container.remove(force=True)
                        except docker.errors.NotFound:
                            pass
                        except Exception:
                            pass

                    # Remove networks
                    for network_type in ["in", "out"]:
                        try:
                            network = client.networks.get(f"qinyuan-t6-{network_type}-{task_run_id}")
                            network.remove()
                        except docker.errors.NotFound:
                            pass
                        except Exception:
                            pass
            except Exception:
                pass  # Ignore Docker errors

            # Delete directory
            if directory.exists():
                try:
                    shutil.rmtree(directory)
                except Exception:
                    pass

            # Delete from database
            with self.workbench._db() as db:
                db.execute("DELETE FROM runs WHERE challenge_id=?", (challenge_id,))
                db.execute("DELETE FROM attachments WHERE challenge_id=?", (challenge_id,))
                db.execute("DELETE FROM challenges WHERE id=?", (challenge_id,))

            return {"challenge_id": challenge_id, "status": "deleted"}

    def batch_force_delete(self, challenge_ids: list[str]):
        """Force delete multiple challenges at once."""
        results = []
        for challenge_id in challenge_ids:
            try:
                result = self.force_delete(challenge_id)
                results.append(result)
            except Exception as exc:
                results.append({
                    "challenge_id": challenge_id,
                    "status": "failed",
                    "error": str(exc)
                })
        return results

    @staticmethod
    def _find_agent_directory(directory: Path):
        candidates = list((directory / "agent-state").glob("*/run.json"))
        return candidates[0].parent if len(candidates) == 1 else None

    def _current(self, challenge_id):
        return self.workbench._current_run(challenge_id)

    def status(self, challenge_id):
        return self._current(challenge_id)

    def events(self, challenge_id):
        run = self._current(challenge_id)
        if not run:
            return {"run_id": None, "events": []}
        path = Path(run["directory"]) / "events.jsonl"
        events = deque(maxlen=256)
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        events.append(value)
        return {"run_id": run["run_id"], "events": list(events)}

    def progress(self, challenge_id):
        """Return a bounded, read-only projection of committed T6 state."""
        run = self._current(challenge_id)
        if not run:
            return {"run_id": None, "run": None, "events": [], "progress": None}
        events = self.events(challenge_id)["events"]
        progress = None
        directory = self._find_agent_directory(Path(run["directory"]))
        if directory is not None:
            # The CLI writes run.json before it creates control.sqlite. During
            # startup this is a normal in-flight state, not a recovery verdict.
            startup_pending = (
                run["status"] in {"starting", "running", "cancelling"}
                and not (directory / "control.sqlite").exists()
                and not (directory / "start-failure.json").exists()
            )
            if startup_pending:
                return {"run_id": run["run_id"], "run": run, "events": events, "progress": None}
            try:
                from agent_adapters.run_files import immutable_read
                declaration = immutable_read(directory / "run.json")
                if declaration.get("protocol_version") == 3:
                    # v3's projection is deliberately safe and read-only;
                    # workbench never interprets or schedules its state.
                    from agent_adapters.web.task_result import snapshot_v3_task_result

                    progress = snapshot_v3_task_result(directory, declaration)
                else:
                    from agent_adapters.task_result import snapshot_task_progress

                    progress = snapshot_task_progress(directory, declaration)
            except (OSError, ValueError, KeyError, TypeError):
                progress = {
                    "recovery": {"required": True, "reason": "state_or_evidence_unreadable"}
                }
        return {"run_id": run["run_id"], "run": run, "events": events, "progress": progress}

    def execution(self, challenge_id: str, execution_id: str):
        from agent_adapters.run_files import immutable_read
        from agent_adapters.task_progress import execution_detail

        run = self._current(challenge_id)
        directory = self._find_agent_directory(Path(run["directory"])) if run else None
        if directory is None:
            raise WorkbenchError("execution_not_available", 404)
        try:
            declaration = immutable_read(directory / "run.json")
            if declaration.get("protocol_version") == 3:
                from agent_adapters.web.task_result import snapshot_v3_task_result

                progress = snapshot_v3_task_result(directory, declaration)
                item = next((value for value in progress.get("executions", [])
                             if value["execution_id"] == execution_id), None)
                if item is None:
                    raise ValueError("execution_not_available")
                # The v3 operator reads candidate plaintext through its
                # separately authorised CLI, never the legacy raw-output API.
                return {"protocol_version": 3, "task_run_id": declaration["task_run_id"],
                        "execution": item, "body_withheld": True}
            return execution_detail(directory, declaration, execution_id)
        except (OSError, ValueError, KeyError, TypeError, T6EvidenceError) as exc:
            raise WorkbenchError("execution_evidence_unavailable", 409) from exc

    def _watch(self, run_id):
        with self._lock:
            active = self._active.get(run_id)
        if not active:
            return
        process = active.process
        while process.poll() is None:
            if active.cancel_requested:
                agent_dir = self._agent_run_directory(active)
                if agent_dir:
                    immutable_write(agent_dir / "cancel-request.json", {"task_run_id": agent_dir.name})
            time.sleep(0.1)
        stdout, stderr = process.communicate()
        if stdout is None:
            stdout = ""
        if stderr is None:
            stderr = ""
        stdout_file = active.directory / "agent.stdout.log"
        stderr_file = active.directory / "agent.stderr.log"
        if stdout_file.exists():
            stdout = stdout_file.read_text(encoding="utf-8", errors="replace")
        if stderr_file.exists():
            stderr = stderr_file.read_text(encoding="utf-8", errors="replace")
        result = None
        for line in reversed((stdout or "").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result = value
                break
        if result is None:
            result = {
                "protocol_version": 1,
                "task_run_id": None,
                "errors": [{"code": "agent_process_no_result"}],
                "exit_code": process.returncode if process.returncode is not None else 4,
            }
        if stderr and not result.get("errors"):
            result["errors"] = [{"code": "agent_process_stderr"}]
        known = self._current(active.challenge_id)
        if (known and known['run_id'] == run_id and known.get('agent_task_run_id')
                and (known.get('result') or {}).get('contract_version') in {19, 20, 21, 22}
                and (result.get('protocol_version') != 3 or result.get('contract_version') != (known.get('result') or {}).get('contract_version')
                     or result.get('task_run_id') != known['agent_task_run_id'])):
            expected = known['agent_task_run_id']
            # Preserve the existing owner; stdout cannot select a replacement TaskRun.
            try:
                agent_dir = self._find_agent_directory(active.directory)
                if agent_dir is None or agent_dir.name != expected:
                    raise ValueError('d9_run_identity_unavailable')
                declaration = immutable_read(agent_dir / 'run.json')
                if (declaration.get('task_run_id') != expected
                        or (declaration.get('contract_version'), declaration.get('execution_profile')) not in {(19,'d8_v19'),(20,'d8_v20'),(21,'d8_v21'),(22,'d8_v22')}):
                    raise ValueError('d9_run_identity_changed')
                from agent_adapters.web.task_result import snapshot_v3_task_result
                result = snapshot_v3_task_result(agent_dir, declaration)
            except (OSError, ValueError, TypeError, AttributeError):
                result = {'protocol_version': 3, 'contract_version': (known.get('result') or {})['contract_version'], 'task_run_id': expected,
                          'task_run': None, 'goal_evaluation': 'unknown', 'errors': []}
            result.setdefault('errors', []).append({'code': 'd9_public_operation_result_unknown'})
            result['recovery'] = {'required': True, 'reason': 'd9_public_operation_result_unknown'}
            result['exit_code'] = 5
        agent_status = (result.get("task_run") or {}).get("status")
        exit_code = int(result.get("exit_code", 4))
        if result.get("protocol_version") == 3 and "run_exit_code" in result:
            exit_code = int(result["run_exit_code"])
        if (result.get("protocol_version") in {2, 3}
                and agent_status in TERMINAL_STATUSES
                and result.get("cleanup", {}).get("status") == "succeeded"
                and (active.cleanup_only or exit_code in (4, 5)
                     or result.get("recovery", {}).get("required"))):
            status = "cleanup_confirmed"
        elif (result.get('protocol_version') == 3 and result.get('contract_version') in {19, 20, 21, 22}
              and agent_status in {'running', 'awaiting_external_feedback'}):
            status = 'recovery_required' if result.get('recovery', {}).get('required') else 'paused'
        elif exit_code == 2 and result.get("task_run_id") is None:
            status = "failed"
        elif (result.get("protocol_version") in {2, 3} and exit_code == 4
              and result.get("task_run_id") and not result.get("recovery", {}).get("required", True)):
            status = "paused"
        elif exit_code in (4, 5) or agent_status in {"pending", "running", "awaiting_confirmation"} or result.get("task_run_id") is None:
            status = "recovery_required"
        elif exit_code == 130 and agent_status == "cancelled":
            status = "cancelled"
        elif exit_code == 3 and agent_status == "deadline_exceeded":
            status = "deadline_exceeded"
        elif exit_code != 0:
            status = "failed"
        else:
            status = "finished"
        with self.workbench.lock:
            self.workbench._finish_run(
                run_id,
                status=status,
                exit_code=process.returncode,
                result=result,
                agent_task_run_id=result.get("task_run_id"),
            )
            with self._lock:
                self._active.pop(run_id, None)

    def shutdown(self):
        """Request cancellation and wait briefly for all owned children."""
        with self.workbench.lock, self._lock:
            active = list(self._active.values())
            for item in active:
                item.cancel_requested = True
                self.workbench._mark_cancel_requested(item.run_id)
        deadline = time.monotonic() + 5.0
        for item in active:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                item.process.kill()
                item.process.wait(timeout=1.0)
        with self._lock:
            self._active.clear()
