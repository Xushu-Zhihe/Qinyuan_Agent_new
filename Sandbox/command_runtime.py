"""Bounded Docker exec for a policy-approved bash command."""

from __future__ import annotations

import asyncio
import math
import time

from agent_core.command_contracts import (
    MAX_OUTPUT_BYTES,
    WORK_ROOT,
    AuthorizedCommand,
    T6CommandResult,
    T6CommandExecutionError,
)
from Sandbox.task_run_sandbox import TaskRunSandboxError

OUTPUT_READ_POLL_SECONDS = 0.25
OUTPUT_STREAM_DRAIN_GRACE_SECONDS = 1.0

BASH_WRAPPER = r"""import os,subprocess,sys
path,size,script=sys.argv[1],int(sys.argv[2]),sys.argv[3]
root='/workspace/work'
if path!=root and not path.startswith(root+'/'): sys.exit(41)
parts=[] if path==root else path[len(root)+1:].split('/')
if any(part in ('','.','..') or '\x00' in part for part in parts): sys.exit(41)
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    for part in parts:
        child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        os.close(fd);fd=child
    os.fchdir(fd)
    data=sys.stdin.buffer.read(size)
    if len(data)!=size: sys.exit(42)
    result=subprocess.run(['/bin/bash','-c',script],input=data)
    sys.exit(result.returncode)
finally:
    os.close(fd)
"""

PROCESS_CLEANUP_SCRIPT = r"""import os,signal,sys,time
own=os.getpid()
def active():
    result=[]
    for name in os.listdir('/proc'):
        if not name.isdecimal(): continue
        pid=int(name)
        if pid in (1,own): continue
        try:
            with open('/proc/'+name+'/status') as stream:
                lines=stream.read().splitlines()
            uid=next(int(line.split()[1]) for line in lines if line.startswith('Uid:'))
            if uid==10001: result.append(pid)
        except (FileNotFoundError,ProcessLookupError): pass
    return result
for _ in range(30):
    pids=active()
    if not pids:
        print('clean',flush=True)
        sys.exit(0)
    for pid in pids:
        try: os.kill(pid,signal.SIGKILL)
        except ProcessLookupError: pass
    time.sleep(0.05)
sys.exit(1)
"""


class _DockerFrames:
    def __init__(self) -> None:
        self.header = bytearray()
        self.remaining = 0
        self.channel = 0
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.stdout_truncated = False
        self.stderr_truncated = False

    @property
    def complete(self) -> bool:
        return not self.header and self.remaining == 0

    def feed(self, data: bytes) -> None:
        index = 0
        while index < len(data):
            if self.remaining == 0:
                count = min(8 - len(self.header), len(data) - index)
                self.header.extend(data[index : index + count])
                index += count
                if len(self.header) < 8:
                    return
                if self.header[0] not in (1, 2) or self.header[1:4] != b"\x00\x00\x00":
                    raise ValueError("invalid_docker_frame")
                self.channel = self.header[0]
                self.remaining = int.from_bytes(self.header[4:8], "big")
                self.header.clear()
                if self.remaining == 0:
                    continue
            count = min(self.remaining, len(data) - index)
            chunk = data[index : index + count]
            output = self.stdout if self.channel == 1 else self.stderr
            remaining_capacity = MAX_OUTPUT_BYTES - len(output)
            output.extend(chunk[:remaining_capacity])
            if count > remaining_capacity:
                if self.channel == 1:
                    self.stdout_truncated = True
                else:
                    self.stderr_truncated = True
            index += count
            self.remaining -= count


class T6CommandSandbox:
    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox

    def quiesce(self, sandbox_id: str) -> None:
        self.sandbox.quiesce(sandbox_id)

    async def execute(self, approved: AuthorizedCommand, token) -> T6CommandResult:
        return await asyncio.to_thread(self._execute, approved, token)

    @staticmethod
    def _read_output(raw) -> bytes:
        # docker.utils.socket.read uses an unbounded select on Unix and recv
        # on NpipeSocket; the latter ignores settimeout. recv_into implements
        # overlapped, timed reads on Windows and respects socket timeouts.
        buffer = bytearray(65536)
        try:
            size = raw.recv_into(buffer)
        except Exception as exc:
            if exc.args and exc.args[0] == 109:  # Windows ERROR_BROKEN_PIPE
                return b""
            raise
        return bytes(buffer[:size])

    def _cleanup_model_processes(self, binding, sandbox_id: str) -> None:
        api = self.sandbox._runtime.client.api
        with self.sandbox._admit(binding):
            execution = api.exec_create(
                sandbox_id,
                ["python3", "-I", "-c", PROCESS_CLEANUP_SCRIPT],
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
                privileged=False,
                user="10001:10001",
                workdir=WORK_ROOT,
                environment={"PATH": "/usr/bin:/bin"},
            )
        output = api.exec_start(execution["Id"], stream=False, demux=True, tty=False)
        info = api.exec_inspect(execution["Id"])
        if (
            not isinstance(output, tuple)
            or output[0] != b"clean\n"
            or output[1]
            or info.get("Running") is not False
            or info.get("ExitCode") != 0
        ):
            raise ValueError("process_cleanup_not_confirmed")

    def _execute(self, approved: AuthorizedCommand, token) -> T6CommandResult:
        request = approved.request
        binding = self.sandbox._binding(approved.sandbox_id)
        if binding.task_run_id != request.task_run_id:
            raise ValueError("sandbox_task_mismatch")
        timeout = approved.effective_timeout_seconds
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("invalid_effective_timeout")
        end = approved.deadline_monotonic
        if end is None:
            end = time.monotonic() + timeout
        if not math.isfinite(end):
            raise ValueError("invalid_command_deadline")

        def undispatched_timeout():
            return T6CommandResult(
                approved.sandbox_id,
                None,
                b"",
                b"",
                False,
                False,
                True,
                True,
                True,
                False,
            )

        if not binding.execution_lock.acquire(timeout=max(0, end - time.monotonic())):
            return undispatched_timeout()
        raw = None
        dispatched = False
        start_attempted = False
        cleanup_attempted = False
        frames = _DockerFrames()
        try:
            if token.is_cancelled:
                raise TaskRunSandboxError(
                    "command", "cancelled_before_dispatch", request.task_run_id
                )
            self.sandbox._verify_active(request.task_run_id, approved.sandbox_id)
            if end - time.monotonic() < 1:
                return undispatched_timeout()
            api = self.sandbox._runtime.client.api
            with self.sandbox._admit(binding):
                execution = api.exec_create(
                    approved.sandbox_id,
                    [
                        "python3",
                        "-I",
                        "-c",
                        BASH_WRAPPER,
                        request.working_directory,
                        str(len(request.stdin)),
                        request.script,
                    ],
                    stdin=True,
                    stdout=True,
                    stderr=True,
                    tty=False,
                    privileged=False,
                    user="10001:10001",
                    workdir=WORK_ROOT,
                    environment={"PATH": "/usr/bin:/bin"},
                )
            with self.sandbox._admit(binding):
                if token.is_cancelled:
                    raise T6CommandExecutionError(
                        dispatched=False, reason="cancelled_before_dispatch"
                    )
                if end - time.monotonic() < 1:
                    return undispatched_timeout()
                start_attempted = True
                stream = api.exec_start(execution["Id"], socket=True, tty=False)
                dispatched = True
            raw = getattr(stream, "_sock", stream)
            offset = 0
            while offset < len(request.stdin):
                if token.is_cancelled or time.monotonic() >= end:
                    raise TimeoutError("command_input_interrupted")
                raw.settimeout(max(0.01, end - time.monotonic()))
                chunk = request.stdin[offset : offset + 65536]
                sent = raw.send(chunk)
                if not isinstance(sent, int) or sent <= 0:
                    raise OSError("short_command_input")
                offset += sent
            stream_complete = False
            drain_deadline = None
            while not token.is_cancelled:
                now = time.monotonic()
                if drain_deadline is None and now >= end:
                    info = api.exec_inspect(execution["Id"])
                    if info.get("Running") is not False:
                        break
                    drain_deadline = now + OUTPUT_STREAM_DRAIN_GRACE_SECONDS
                active_deadline = end if drain_deadline is None else drain_deadline
                remaining = active_deadline - now
                if remaining <= 0:
                    break
                raw.settimeout(min(OUTPUT_READ_POLL_SECONDS, remaining))
                try:
                    block = self._read_output(raw)
                except TimeoutError:
                    info = api.exec_inspect(execution["Id"])
                    if info.get("Running") is False and drain_deadline is None:
                        drain_deadline = (
                            time.monotonic() + OUTPUT_STREAM_DRAIN_GRACE_SECONDS
                        )
                    continue
                if not block:
                    stream_complete = True
                    break
                frames.feed(block)
            info = api.exec_inspect(execution["Id"])
            stopped = info.get("Running") is False
            code = info.get("ExitCode") if stopped else None
            if type(code) is not int:
                code = None
            # Any script may deliberately exit 124 or 137. Only the trusted
            # deadline proves a timeout; no exit code is reserved for scripts.
            timed_out = not stopped and time.monotonic() >= end
            if timed_out and not token.is_cancelled:
                cleanup_attempted = True
                self._cleanup_model_processes(binding, approved.sandbox_id)
                info = api.exec_inspect(execution["Id"])
                stopped = info.get("Running") is False
                if not stopped:
                    raise ValueError("timed_out_execution_still_running")
            complete = frames.complete and stream_complete
            return T6CommandResult(
                approved.sandbox_id,
                code,
                bytes(frames.stdout),
                bytes(frames.stderr),
                frames.stdout_truncated or not complete,
                frames.stderr_truncated or not complete,
                timed_out,
                stopped and not token.is_cancelled,
                complete,
                dispatched,
            )
        except T6CommandExecutionError:
            raise
        except TimeoutError as exc:
            if (
                dispatched
                and not cleanup_attempted
                and time.monotonic() >= end
                and not token.is_cancelled
            ):
                try:
                    cleanup_attempted = True
                    self._cleanup_model_processes(binding, approved.sandbox_id)
                    if api.exec_inspect(execution["Id"]).get("Running") is not False:
                        raise ValueError("timed_out_execution_still_running")
                except Exception as cleanup_error:
                    raise T6CommandExecutionError(
                        dispatched=True,
                        reason="timeout_cleanup_unknown",
                    ) from cleanup_error
                return T6CommandResult(
                    approved.sandbox_id,
                    None,
                    bytes(frames.stdout),
                    bytes(frames.stderr),
                    True,
                    True,
                    True,
                    True,
                    False,
                    True,
                )
            raise T6CommandExecutionError(
                dispatched=start_attempted,
                reason="command_io_unknown",
            ) from exc
        except Exception as exc:
            attempted = dispatched or start_attempted
            raise T6CommandExecutionError(
                dispatched=attempted,
                reason="outcome_unknown" if attempted else "environment_fault",
            ) from exc
        finally:
            try:
                if raw is not None:
                    raw.close()
            finally:
                binding.execution_lock.release()
