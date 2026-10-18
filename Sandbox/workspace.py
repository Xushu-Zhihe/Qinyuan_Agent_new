"""Fixed D3 workspace operations over one already owned T6 container."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import socket
import threading
import time
from uuid import UUID

from agent_adapters.command_recorder import T6CommandRecorder
from agent_core.artifact_operations import fixed_size_script
from agent_core.artifact_workspace import D3ShareReadback, D3StopReadback
from agent_core.attachment_import import AttachmentImportReadback, MAX_ATTACHMENT_BYTES
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.command_contracts import T6EvidenceError
from agent_core.intent_postconditions import FileDigestObservation
from Sandbox.command_runtime import T6CommandSandbox, _DockerFrames
from Sandbox.task_run_sandbox import TaskRunSandboxError


# Neither this program nor its arguments originate in model-generated code.
WORKSPACE_SCRIPT = r'''import hashlib,json,os,stat,sys,uuid
mode,source,target,size=sys.argv[1:];size=int(size)
fds=[];temporary=None;parent=None
def open_parent(path,create=False):
    parts=path.split('/')[1:]
    if parts[:1]!=['workspace'] or any(p in ('','.','..') for p in parts): raise ValueError('path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
    for part in parts[:-1]:
        if create:
            try: os.mkdir(part,0o700 if part!='input' else 0o755,dir_fd=fd)
            except FileExistsError: pass
        fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
    return fd,parts[-1]
def observe(path):
    directory,name=open_parent(path)
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory);fds.append(fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not_regular')
    digest=hashlib.sha256();length=0
    while True:
        block=os.read(fd,65536)
        if not block: break
        length+=len(block)
        if length>16777216: raise ValueError('size')
        digest.update(block)
    return {'status':'present','byte_size':length,'sha256':digest.hexdigest()}
try:
    if mode in ('import','copy'):
        parent,name=open_parent(target,True)
        temporary='.pending-'+uuid.uuid4().hex
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent);fds.append(fd)
        if mode=='import':
            left=size
            if not 0<=left<=16777216: raise ValueError('size')
            def read():
                global left
                block=sys.stdin.buffer.read(min(left,65536)) if left else b''
                if left and not block: raise ValueError('short_input')
                left-=len(block)
                return block
        else:
            directory,source_name=open_parent(source)
            item=os.open(source_name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory);fds.append(item)
            if not stat.S_ISREG(os.fstat(item).st_mode): raise ValueError('not_regular')
            def read(): return os.read(item,65536)
        length=0
        while True:
            block=read()
            if not block: break
            length+=len(block)
            if length>16777216: raise ValueError('size')
            view=memoryview(block)
            while view:
                count=os.write(fd,view)
                if count<=0: raise ValueError('write')
                view=view[count:]
        os.fchmod(fd,0o444 if mode=='import' else 0o600);os.fsync(fd)
        os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
        os.unlink(temporary,dir_fd=parent);temporary=None;os.fsync(parent)
        result=observe(target)
    elif mode=='read':
        try: result=observe(source)
        except FileNotFoundError: result={'status':'missing'}
        except OSError: result={'status':'unsafe'}
    else: raise ValueError('mode')
    print(json.dumps(result),flush=True)
except Exception:
    print('fixed_workspace_operation_rejected',file=sys.stderr);sys.exit(1)
finally:
    if temporary is not None and parent is not None:
        try: os.unlink(temporary,dir_fd=parent)
        except OSError: pass
    for fd in reversed(fds): os.close(fd)
'''


def _path(path, *, input_only=False, artifact_only=False):
    if not isinstance(path, str):
        raise ValueError("d9_workspace_path_invalid")
    if re.fullmatch(r"/workspace/input/d3-attachment-[01]", path):
        if artifact_only:
            raise ValueError("d9_workspace_artifact_required")
        return None
    if not input_only:
        match = re.fullmatch(r"/workspace/work/intents/([^/]+)/(attachment|shared)-([^/]+)", path)
        if match:
            for value in (match[1], match[3]):
                parsed = UUID(value)
                if parsed.version not in {4, 5} or str(parsed) != value:
                    raise ValueError("d9_workspace_path_invalid")
            return match[1]
    raise ValueError("d9_workspace_path_out_of_scope")


class D8DockerWorkspace:
    """No arbitrary command endpoint; approved fixed probes reuse T6 execution."""

    def __init__(self, sandbox, directory):
        self.sandbox = sandbox
        self.commands = T6CommandSandbox(sandbox)
        self.evidence = T6CommandRecorder(directory / "commands", create=False)

    def quiesce(self, sandbox_id):
        self.sandbox.quiesce(sandbox_id)

    def _run(self, sandbox_id, mode, source, target, content, timeout, token):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 300):
            raise ValueError("d9_workspace_timeout_invalid")
        binding = self.sandbox._binding(sandbox_id)
        end = time.monotonic() + timeout
        if not binding.execution_lock.acquire(timeout=timeout):
            raise TimeoutError("d9_workspace_lock_timeout")
        stream = None
        done = threading.Event()
        sent = False
        try:
            if token.is_cancelled or time.monotonic() >= end:
                raise TimeoutError("d9_workspace_cancelled_before_dispatch")
            self.sandbox._verify_active(binding.task_run_id, sandbox_id)
            api = self.sandbox._runtime.client.api
            with self.sandbox._admit(binding):
                execution = api.exec_create(
                    sandbox_id, ["timeout", "--signal=TERM", "--kill-after=1s",
                                 f"{max(1, math.ceil(end-time.monotonic()))}s", "python3", "-I", "-c",
                                 WORKSPACE_SCRIPT, mode, source, target, str(len(content or b""))],
                    stdin=content is not None, stdout=True, stderr=True, tty=False,
                    privileged=False, user="0:0" if mode == "import" else "10001:10001",
                    workdir="/workspace", environment={"PATH": "/usr/bin:/bin"},
                )
            with self.sandbox._admit(binding):
                if token.is_cancelled or time.monotonic() >= end:
                    raise TimeoutError("d9_workspace_cancelled_before_dispatch")
                sent = True
                stream = api.exec_start(execution["Id"], socket=True, tty=False)
            raw = getattr(stream, "_sock", stream)
            raw.settimeout(min(0.25, max(0.01, end-time.monotonic())))

            def watch():
                while not done.wait(0.025):
                    if token.is_cancelled or time.monotonic() >= end:
                        try:
                            raw.close()
                        except OSError:
                            pass
                        return

            threading.Thread(target=watch, daemon=True).start()
            if content is not None:
                for offset in range(0, len(content), 65536):
                    if token.is_cancelled or time.monotonic() >= end:
                        raise TimeoutError("d9_workspace_transfer_timeout")
                    raw.sendall(content[offset:offset+65536])
            frames = _DockerFrames()
            while True:
                if token.is_cancelled or time.monotonic() >= end:
                    raise TimeoutError("d9_workspace_read_timeout")
                try:
                    block = self.commands._read_output(raw)
                except (TimeoutError, socket.timeout):
                    continue
                if not block:
                    break
                frames.feed(block)
                if (len(frames.stdout)+len(frames.stderr) > 4096
                        or frames.stdout_truncated or frames.stderr_truncated):
                    raise ValueError("d9_workspace_output_invalid")
            info = api.exec_inspect(execution["Id"])
            if (token.is_cancelled or time.monotonic() >= end or not frames.complete
                    or info.get("Running") is not False or info.get("ExitCode") != 0 or frames.stderr):
                raise ValueError("d9_workspace_result_unconfirmed")
            value = json.loads(frames.stdout)
            if not isinstance(value, dict):
                raise ValueError("d9_workspace_output_invalid")
            if value.get("status") == "present" and set(value) == {"status", "byte_size", "sha256"}:
                AttachmentImportReadback(value["byte_size"], value["sha256"])
            elif value not in ({"status": "missing"}, {"status": "unsafe"}):
                raise ValueError("d9_workspace_output_invalid")
            return value
        except Exception as exc:
            if sent:
                raise TaskRunSandboxError("fixed_workspace", "outcome_unknown", binding.task_run_id,
                                          (sandbox_id,), original_error=exc) from exc
            raise
        finally:
            done.set()
            if stream is not None:
                stream.close()
            binding.execution_lock.release()

    async def create_and_readback(self, sandbox_id, path, content, timeout_seconds, token):
        _path(path, input_only=True)
        if not isinstance(content, bytes) or len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("d9_import_content_invalid")
        value = await asyncio.to_thread(self._run, sandbox_id, "import", "", path, content, timeout_seconds, token)
        result = self._readback(value)
        if result != AttachmentImportReadback(len(content), hashlib.sha256(content).hexdigest()):
            raise ValueError("d9_import_readback_mismatch")
        return result

    async def copy_exclusive_and_readback(self, sandbox_id, source_path, target_path, timeout_seconds, token):
        _path(source_path, input_only=True)
        _path(target_path, artifact_only=True)
        value = await asyncio.to_thread(self._run, sandbox_id, "copy", source_path, target_path, None,
                                        timeout_seconds, token)
        return self._readback(value)

    @staticmethod
    def _readback(value):
        if value.get("status") != "present":
            raise ValueError("d9_workspace_readback_unavailable")
        return AttachmentImportReadback(value["byte_size"], value["sha256"])

    async def readback(self, sandbox_id, path, timeout_seconds, token):
        _path(path)
        value = await asyncio.to_thread(self._run, sandbox_id, "read", path, "", None, timeout_seconds, token)
        return self._readback(value)

    async def hash_file(self, task_run_id, sandbox_id, path, timeout_seconds, token):
        _path(path, artifact_only=True)
        if self.sandbox._binding(sandbox_id).task_run_id != task_run_id:
            raise ValueError("d9_digest_owner_mismatch")
        value = await asyncio.to_thread(self._run, sandbox_id, "read", path, "", None, timeout_seconds, token)
        return FileDigestObservation(value["status"], value.get("sha256"))

    async def share_exclusive_and_readback(self, sandbox_id, source_path, target_path, timeout_seconds, token):
        _path(source_path, artifact_only=True)
        _path(target_path, artifact_only=True)
        source = await self.readback(sandbox_id, source_path, timeout_seconds, token)
        value = await asyncio.to_thread(self._run, sandbox_id, "copy", source_path, target_path, None,
                                        timeout_seconds, token)
        return D3ShareReadback(source, self._readback(value))

    async def execute(self, approved, token):
        request = approved.request
        path = request.script.rsplit(" ", 1)[-1]
        _path(path, artifact_only=True)
        if (request.stdin or request.working_directory != path.rsplit("/", 1)[0]
                or request.script not in (fixed_size_script(path), fixed_digest_script(path))
                or self.sandbox._binding(approved.sandbox_id).task_run_id != request.task_run_id):
            raise ValueError("d9_fixed_command_required")
        return await self.commands.execute(approved, token)

    async def query_intent_processes(self, claim, sandbox_id, timeout_seconds, token):
        binding = self.sandbox._binding(sandbox_id)
        if binding.task_run_id != claim.task_run_id:
            raise ValueError("d9_stop_owner_mismatch")
        absent, unknown = [], []
        for ref in claim.process_refs:
            try:
                record = self.evidence.read_record(ref)
                command = self.evidence.read_command(record.command_ref) if record and record.command_ref else None
                path = command.argv[2].rsplit(" ", 1)[-1] if command and len(command.argv) == 3 else ""
                valid = (record is not None and command is not None
                         and record.task_run_id == claim.task_run_id and record.sandbox_id == sandbox_id
                         and record.request_id == command.request_id == ref
                         and record.termination_confirmed and record.status != "outcome_unknown"
                         and record.authorization_id == command.authorization_id
                         and command.task_run_id == claim.task_run_id and command.sandbox_id == sandbox_id
                         and command.stdin_size == 0 and command.stdin_sha256 == hashlib.sha256(b"").hexdigest()
                         and _path(path, artifact_only=True) == claim.intent_id
                         and command.working_directory == path.rsplit("/", 1)[0]
                         and command.argv in (("bash", "-c", fixed_size_script(path)),
                                              ("bash", "-c", fixed_digest_script(path))))
            except (OSError, ValueError, TypeError, T6EvidenceError):
                valid = False
            (absent if valid else unknown).append(ref)
        return D3StopReadback((), tuple(absent), tuple(unknown))

    async def stop_intent_processes(self, claim, sandbox_id, timeout_seconds, token):
        # No broad PID kill: only already-confirmed fixed executions are absent.
        # Missing/unknown execution evidence is not permission to kill processes.
        return await self.query_intent_processes(claim, sandbox_id, timeout_seconds, token)
