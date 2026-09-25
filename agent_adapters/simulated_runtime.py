"""Explicit synthetic fixtures; never claims to identify actual file content."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from agent_adapters.cli_contract import fields
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.file_inspection import FILE_INSPECTION_SCRIPT
from agent_core.modeling import (
    ModelError,
    ModelErrorKind,
    ModelRequestType,
    NormalizedIntentActionResponse,
    NormalizedPlanResponse,
)
from Sandbox.models import CommandResult


def validate_fixture(value):
    fields(
        value,
        ("stdout",),
        (
            "stderr",
            "exit_code",
            "model_delay_seconds",
            "tool_delay_seconds",
            "cleanup_delay_seconds",
            "cleanup_failure",
            "model_stop",
        ),
    )
    for name in ("stdout", "stderr"):
        v = value.get(name, "")
        if not isinstance(v, str) or len(v.encode()) > 65536:
            raise ValueError("invalid_simulated_output")
    for name in ("model_delay_seconds", "tool_delay_seconds", "cleanup_delay_seconds"):
        v = value.get(name, 0)
        if type(v) not in (int, float) or not 0 <= v <= 300:
            raise ValueError("invalid_simulated_delay")
    if type(value.get("exit_code", 0)) is not int or value.get("exit_code", 0) not in (
        0,
        40,
        41,
        42,
        43,
    ):
        raise ValueError("invalid_simulated_exit")
    for name in ("cleanup_failure", "model_stop"):
        if type(value.get(name, False)) is not bool:
            raise ValueError("invalid_simulated_flag")
    return value


class SimulatedModel:
    def __init__(self, fixture, path):
        self.fixture, self.path = fixture, path

    def invoke(self, request, timeout_seconds, cancellation_token):
        end = time.monotonic() + min(
            self.fixture.get("model_delay_seconds", 0), timeout_seconds
        )
        while time.monotonic() < end:
            if cancellation_token.is_cancelled:
                return ModelError(ModelErrorKind.CANCELLED, "simulated_cancelled")
            time.sleep(0.01)
        if request.request_type is ModelRequestType.PLAN_GENERATION:
            return NormalizedPlanResponse(
                [{"description": "inspect the authorized file type"}]
            )
        if self.fixture.get("model_stop"):
            return NormalizedIntentActionResponse(
                {"kind": "stop", "short_reason": "simulated_stop"}
            )
        observation = request.action_context.latest_tool_observation
        if observation is None:
            action = {"kind": "inspect_file", "path": self.path}
        else:
            text = observation.stdout.removesuffix("\n").removesuffix("\r")
            action = {
                "kind": "file_type_candidate",
                "path": self.path,
                "observed_type": text or "unsupported",
                "execution_id": observation.execution_id,
            }
        return NormalizedIntentActionResponse(action)


class SimulatedSandbox:
    def __init__(self, directory: Path, fixture):
        self.directory, self.fixture = directory / "simulated-sandbox", fixture
        self.directory.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.sandbox_id = None

    def create(self, task_run_id):
        self.sandbox_id = "simulated-" + task_run_id
        (self.directory / "active").write_text(self.sandbox_id, encoding="utf-8")
        return self.sandbox_id

    def bind(self, task_run_id, sandbox_id, *, terminal):
        if sandbox_id != "simulated-" + task_run_id:
            raise ValueError("simulated_owner_mismatch")
        self.sandbox_id = sandbox_id
        if not terminal and not (self.directory / "active").exists():
            raise ValueError("simulated_sandbox_missing")
        if terminal:
            self.stopped.set()

    def quiesce(self, sandbox_id):
        self.stopped.set()

    def interrupt(self, sandbox_id):
        self.stopped.set()

    def destroy(self, sandbox_id):
        self.stopped.set()
        time.sleep(self.fixture.get("cleanup_delay_seconds", 0))
        if self.fixture.get("cleanup_failure"):
            raise RuntimeError("simulated_cleanup_failure")
        (self.directory / "active").unlink(missing_ok=True)
        for path in self.directory.glob("*.data"):
            path.unlink()

    def _file(self, path):
        return self.directory / (hashlib.sha256(path.encode()).hexdigest() + ".data")

    async def create_and_readback(
        self, sandbox_id, path, content, timeout_seconds, cancellation_token
    ):
        with self.lock:
            if self.stopped.is_set() or cancellation_token.is_cancelled:
                raise RuntimeError("simulated_import_interrupted")
            with self._file(path).open("xb") as stream:
                stream.write(content)
            return self._read(path)

    def _read(self, path):
        content = self._file(path).read_bytes()
        return AttachmentImportReadback(
            len(content), hashlib.sha256(content).hexdigest()
        )

    async def readback(self, sandbox_id, path, timeout_seconds, cancellation_token):
        with self.lock:
            if self.stopped.is_set() or cancellation_token.is_cancelled:
                raise RuntimeError("simulated_import_interrupted")
            return self._read(path)

    def execute(self, sandbox_id, request):
        with self.lock:
            if request.arguments[:3] != ("-I", "-c", FILE_INSPECTION_SCRIPT):
                raise ValueError("simulated_command_rejected")
            delay = self.fixture.get("tool_delay_seconds", 0)
            interrupted = self.stopped.wait(min(delay, request.timeout_seconds))
            if interrupted or delay > request.timeout_seconds:
                return CommandResult(
                    sandbox_id,
                    -1,
                    "",
                    "",
                    delay > request.timeout_seconds,
                    termination_confirmed=True,
                )
            if not self._file(request.arguments[3]).exists():
                return CommandResult(
                    sandbox_id, 40, "", "file_missing", termination_confirmed=True
                )
            return CommandResult(
                sandbox_id,
                self.fixture.get("exit_code", 0),
                self.fixture["stdout"],
                self.fixture.get("stderr", ""),
                termination_confirmed=True,
            )

    def close(self):
        self.stopped.set()
