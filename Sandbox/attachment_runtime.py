"""Trusted attachment import through fixed code and stdin, never shell interpolation."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time

from agent_core.attachment_import import MAX_ATTACHMENT_BYTES, AttachmentImportReadback
from agent_core.file_inspection import validate_inspection_path
from Sandbox.task_run_sandbox import TaskRunSandbox

IMPORT_SCRIPT = r"""import hashlib,json,os,stat,sys
path,mode,size=sys.argv[1],sys.argv[2],int(sys.argv[3])
fds=[]
try:
    parts=path.split('/')
    if parts[:3]!=['','workspace','input'] or len(parts)<4 or any(p in ('','.','..') for p in parts[1:]):
        raise ValueError('path')
    current=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(current)
    for index,part in enumerate(parts[1:-1],1):
        if mode=='create' and index>=2:
            try: os.mkdir(part,0o700,dir_fd=current)
            except FileExistsError: pass
        current=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current);fds.append(current)
    flags=os.O_NOFOLLOW|os.O_NONBLOCK
    flags|=(os.O_RDWR|os.O_CREAT|os.O_EXCL) if mode=='create' else os.O_RDONLY
    fd=os.open(parts[-1],flags,0o600,dir_fd=current);fds.append(fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not_regular')
    if mode=='create':
        if not 0<=size<=16777216: raise ValueError('size')
        left=size
        while left:
            block=sys.stdin.buffer.read(min(left,65536))
            if not block: raise ValueError('short_input')
            left-=len(block)
            view=memoryview(block)
            while view:
                written=os.write(fd,view)
                if written<=0: raise ValueError('short_write')
                view=view[written:]
        os.fsync(fd);os.lseek(fd,0,0)
    digest=hashlib.sha256();length=0
    while True:
        block=os.read(fd,65536)
        if not block: break
        length+=len(block)
        if length>16777216: raise ValueError('size')
        digest.update(block)
    print(json.dumps({'byte_size':length,'sha256':digest.hexdigest()}),flush=True)
except Exception:
    print('attachment_operation_rejected',file=sys.stderr)
    sys.exit(1)
finally:
    for fd in reversed(fds): os.close(fd)
"""


class ImportableTaskRunSandbox(TaskRunSandbox):
    async def create_and_readback(
        self, sandbox_id, path, content, timeout_seconds, cancellation_token
    ):
        if not isinstance(content, bytes) or len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment_size_rejected")
        return await asyncio.to_thread(
            self._transfer,
            sandbox_id,
            path,
            content,
            timeout_seconds,
            cancellation_token,
        )

    async def readback(self, sandbox_id, path, timeout_seconds, cancellation_token):
        return await asyncio.to_thread(
            self._transfer, sandbox_id, path, None, timeout_seconds, cancellation_token
        )

    def _transfer(self, sandbox_id, path, content, timeout, token):
        from docker.utils.socket import next_frame_header, read_exactly

        validate_inspection_path(path)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError()
        binding = self._binding(sandbox_id)
        end = time.monotonic() + timeout
        if not binding.execution_lock.acquire(timeout=timeout):
            raise TimeoutError()
        sock = None
        done = threading.Event()
        try:
            if token.is_cancelled or time.monotonic() >= end:
                raise TimeoutError()
            self._verify_active(binding.task_run_id, sandbox_id)
            api = self._runtime.client.api
            mode = "readback" if content is None else "create"
            with self._admit(binding):
                execution = api.exec_create(
                    sandbox_id,
                    [
                        "python3",
                        "-I",
                        "-c",
                        IMPORT_SCRIPT,
                        path,
                        mode,
                        str(len(content or b"")),
                    ],
                    stdin=content is not None,
                    stdout=True,
                    stderr=True,
                    tty=False,
                    user="10001:10001",
                    environment={"PATH": "/usr/bin:/bin"},
                )
            with self._admit(binding):
                sock = api.exec_start(execution["Id"], socket=True, tty=False)
            raw = getattr(sock, "_sock", sock)
            if hasattr(raw, "settimeout"):
                raw.settimeout(max(0.01, end - time.monotonic()))

            def interrupt_wait():
                while not done.wait(0.025):
                    if token.is_cancelled or time.monotonic() >= end:
                        try:
                            raw.close()
                        except OSError:
                            pass
                        return

            monitor = threading.Thread(target=interrupt_wait, daemon=True)
            monitor.start()
            if content:
                for start in range(0, len(content), 65536):
                    if token.is_cancelled or time.monotonic() >= end:
                        raise TimeoutError()
                    raw.sendall(content[start : start + 65536])
            stdout, stderr = bytearray(), bytearray()
            while True:
                stream, length = next_frame_header(raw)
                if stream < 0:
                    break
                if (
                    stream not in (1, 2)
                    or length < 0
                    or length + len(stdout) + len(stderr) > 4096
                ):
                    raise ValueError("import_output_rejected")
                (stdout if stream == 1 else stderr).extend(read_exactly(raw, length))
            if token.is_cancelled or time.monotonic() >= end:
                raise TimeoutError()
            result = api.exec_inspect(execution["Id"])
            if (
                result.get("Running") is not False
                or result.get("ExitCode") != 0
                or stderr
            ):
                raise ValueError("import_not_confirmed")
            value = json.loads(stdout)
            if not isinstance(value, dict) or set(value) != {"byte_size", "sha256"}:
                raise ValueError("import_output_rejected")
            return AttachmentImportReadback(**value)
        finally:
            done.set()
            if sock is not None:
                sock.close()
            binding.execution_lock.release()
