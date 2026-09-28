"""Process bridge from the local workbench to the real Agent CLI.

The workbench is a control surface, not an alternate Agent implementation.
Every running row below corresponds to one child process and one CLI state
directory. Scheduling and lifecycle transitions remain owned by the CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent_adapters.run_files import encode, immutable_write
from agent_adapters.workbench_contracts import ACTIVE_RUN_STATUSES, WorkbenchError
from agent_core.command_contracts import T6EvidenceError
from agent_core.runtime import TERMINAL_STATUSES


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

    def _preflight(self, challenge):
        if challenge["archived"]:
            raise WorkbenchError("challenge_archived", 409)
        if challenge["status"] in ACTIVE_RUN_STATUSES:
            raise WorkbenchError("challenge_already_running", 409)
        if not self.workbench.api_key:
            raise WorkbenchError("model_key_not_configured", 409)
        if not 1 <= challenge["step_timeout_seconds"] <= 300:
            raise WorkbenchError("invalid_step_timeout", 409)
        if not challenge["description"].strip():
            raise WorkbenchError("challenge_description_required_for_run", 409)
        attachments = challenge["attachments"]
        if not challenge["endpoint"] and not attachments:
            raise WorkbenchError("runner_requires_local_attachment", 409)
        if len(attachments) > 16:
            raise WorkbenchError("too_many_attachments", 409)
        if sum(int(item["byte_size"]) for item in attachments) > 64 * 1024 * 1024:
            raise WorkbenchError("attachment_run_capacity_exceeded", 409)

    def start(self, challenge_id: str):
        with self.workbench.lock:
            challenge = self.workbench.get(challenge_id)
            self._preflight(challenge)
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
            env = os.environ.copy()
            project_root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
            env.update(
                {
                    "QINYUAN_RUNTIME_MODEL": self.workbench.model,
                    "QINYUAN_RUNTIME_API_KEY": self.workbench.api_key,
                    "QINYUAN_RUNTIME_BASE_URL": self.workbench.base_url,
                    "QINYUAN_RUNTIME_PROVIDER": self.workbench.provider,
                    "QINYUAN_RUNTIME_MODEL_TIMEOUT": "180",
                    "QINYUAN_T6_STEP_TIMEOUT_SECONDS": str(challenge["step_timeout_seconds"]),
                }
            )
            command = [
                sys.executable,
                "-m",
                "agent",
                "run-task",
                "--mode",
                "real",
                "--request",
                str(directory / "request.json"),
                "--state-dir",
                str(state_dir),
                "--events",
                str(events),
                "--env-file",
                str(directory / "unused.env"),
            ]
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
        if run and run["status"] == "paused":
            return self._cancel_paused(challenge_id)
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

    def _cancel_paused(self, challenge_id: str):
        # Explicit user cancellation re-enters the CLI lifecycle boundary. It
        # never resumes the model, changes control rows, or calls Docker here.
        with self.workbench.lock:
            run = self._current(challenge_id)
            if run["status"] != "paused":
                raise WorkbenchError("challenge_not_paused", 409)
            directory = Path(run["directory"])
            agent_dir = self._find_agent_directory(directory)
            if agent_dir is None or agent_dir.name != run["agent_task_run_id"]:
                raise WorkbenchError("run_identity_unavailable", 409)
            command = [sys.executable, "-m", "agent", "cancel", "--task-run-id",
                       agent_dir.name, "--state-dir", str(agent_dir.parent)]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
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
            if (not run or run["status"] != "recovery_required"
                    or result.get("protocol_version") != 2
                    or not isinstance(task, dict)
                    or task.get("status") not in TERMINAL_STATUSES
                    or not isinstance(cleanup, dict)
                    or cleanup.get("status") == "succeeded"):
                raise WorkbenchError("cleanup_not_available", 409)
            directory = Path(run["directory"])
            agent_dir = self._find_agent_directory(directory)
            if agent_dir is None or agent_dir.name != run["agent_task_run_id"]:
                raise WorkbenchError("run_identity_unavailable", 409)
            command = [
                sys.executable, "-m", "agent", "resume", "--task-run-id",
                agent_dir.name, "--state-dir", str(agent_dir.parent), "--cleanup-only",
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
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
                from agent_adapters.task_result import snapshot_task_progress

                progress = snapshot_task_progress(
                    directory, immutable_read(directory / "run.json")
                )
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
            return execution_detail(directory, immutable_read(directory / "run.json"), execution_id)
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
        agent_status = (result.get("task_run") or {}).get("status")
        exit_code = int(result.get("exit_code", 4))
        if (active.cleanup_only and result.get("protocol_version") == 2
                and agent_status in TERMINAL_STATUSES
                and result.get("cleanup", {}).get("status") == "succeeded"):
            status = "cleanup_confirmed"
        elif exit_code == 2 and result.get("task_run_id") is None:
            status = "failed"
        elif (result.get("protocol_version") == 2 and exit_code == 4
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
