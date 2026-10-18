"""Runtime deadlines, byte streams and uncertain Docker start/cleanup results."""

import asyncio
import socket
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_core.command_contracts import (
    AuthorizedCommand,
    SandboxCommandRequest,
    T6CommandExecutionError,
)
from Sandbox.command_runtime import T6CommandSandbox


class API:
    def __init__(self):
        self.reader, self.writer = socket.socketpair()
        self.running = True
        self.exit_code = 0
        self.start_error = False
        self.cleanup_error = False
        self.cleanup_calls = 0
        self.creates = []
        self.writer_thread = None

    def exec_create(self, sandbox_id, argv, **kwargs):
        self.creates.append((sandbox_id, argv, kwargs))
        return {"Id": "main" if len(self.creates) == 1 else "cleanup"}

    def exec_start(self, identity, **kwargs):
        if identity == "cleanup":
            self.cleanup_calls += 1
            if self.cleanup_error:
                raise TimeoutError("lost cleanup response")
            self.running = False
            return (b"clean\n", b"")
        if self.start_error:
            raise TimeoutError("lost start response")
        return self.reader

    def exec_inspect(self, identity):
        return {
            "Running": self.running if identity == "main" else False,
            "ExitCode": self.exit_code if identity == "main" else 0,
        }

    def finished(self, code, output):
        self.running = False
        self.exit_code = code
        frame = bytes([1, 0, 0, 0]) + len(output).to_bytes(4, "big") + output
        self.writer.sendall(frame)
        self.writer.shutdown(socket.SHUT_WR)

    def finished_with_delayed_output(self, code, output, delay):
        self.running = False
        self.exit_code = code
        frame = bytes([1, 0, 0, 0]) + len(output).to_bytes(4, "big") + output

        def deliver():
            time.sleep(delay)
            self.writer.sendall(frame)
            self.writer.shutdown(socket.SHUT_WR)

        self.writer_thread = threading.Thread(target=deliver, daemon=True)
        self.writer_thread.start()


@pytest.fixture
def runtime():
    api = API()
    task_id = str(uuid4())
    binding = SimpleNamespace(task_run_id=task_id, execution_lock=threading.Lock())
    sandbox = SimpleNamespace(
        _binding=lambda _: binding,
        _verify_active=lambda *_: None,
        _admit=lambda _: nullcontext(),
        _runtime=SimpleNamespace(client=SimpleNamespace(api=api)),
    )
    request = SandboxCommandRequest(
        str(uuid4()),
        task_id,
        "plan",
        0,
        "test script",
        b"",
        "/workspace/work",
        2,
    )
    approved = AuthorizedCommand(request, str(uuid4()), "sandbox", 2)
    yield T6CommandSandbox(sandbox), api, approved, binding
    if api.writer_thread is not None:
        api.writer_thread.join(timeout=2)
    api.reader.close()
    api.writer.close()


@pytest.mark.parametrize("code", [0, 1, 124, 137])
def test_exit_codes_are_observations_and_bytes_are_preserved(runtime, code):
    executor, api, approved, _ = runtime
    api.finished(code, b"raw\x00\xff")
    result = asyncio.run(
        executor.execute(approved, SimpleNamespace(is_cancelled=False))
    )
    assert result.exit_code == code and not result.timed_out
    assert result.stdout == b"raw\x00\xff" and result.stream_complete
    assert api.cleanup_calls == 0


def test_output_is_drained_after_exec_stops(runtime):
    executor, api, approved, _ = runtime
    api.finished_with_delayed_output(0, b"late output", 0.35)

    result = asyncio.run(
        executor.execute(approved, SimpleNamespace(is_cancelled=False))
    )

    assert result.termination_confirmed
    assert result.stream_complete
    assert result.stdout == b"late output"
    assert not result.stdout_truncated and not result.stderr_truncated


def test_stopped_exec_without_stream_eof_remains_incomplete_after_grace(runtime):
    executor, api, approved, _ = runtime
    api.running = False
    started = time.monotonic()

    result = asyncio.run(
        executor.execute(approved, SimpleNamespace(is_cancelled=False))
    )

    assert result.termination_confirmed
    assert not result.timed_out
    assert not result.stream_complete
    assert result.stdout_truncated and result.stderr_truncated
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_silent_process_obeys_deadline_and_unknown_cleanup_never_retries(
    runtime, cleanup_error
):
    executor, api, approved, binding = runtime
    api.cleanup_error = cleanup_error
    started = time.monotonic()
    if cleanup_error:
        with pytest.raises(T6CommandExecutionError) as caught:
            asyncio.run(executor.execute(approved, SimpleNamespace(is_cancelled=False)))
        assert caught.value.dispatched
    else:
        result = asyncio.run(
            executor.execute(approved, SimpleNamespace(is_cancelled=False))
        )
        assert result.timed_out and result.termination_confirmed
    assert time.monotonic() - started < 4
    assert api.cleanup_calls == 1
    assert not binding.execution_lock.locked()


def test_start_response_loss_is_unknown_not_environment_retry(runtime):
    executor, api, approved, binding = runtime
    api.start_error = True
    with pytest.raises(T6CommandExecutionError) as caught:
        asyncio.run(executor.execute(approved, SimpleNamespace(is_cancelled=False)))
    assert caught.value.dispatched
    assert len(api.creates) == 1
    assert not binding.execution_lock.locked()
