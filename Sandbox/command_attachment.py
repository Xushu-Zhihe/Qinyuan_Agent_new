"""Attachment admission and fixed-code import into immutable Sandbox input."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from Sandbox.task_run_sandbox import TaskRunSandboxError
from qinyuan_core import CodedError

MAX_T6_ATTACHMENTS = 16
MAX_T6_INPUT_BYTES = 64 * 1024 * 1024

IMPORT_SCRIPT = r"""import hashlib,json,os,stat,sys,uuid
name,mode,size=sys.argv[1],sys.argv[2],int(sys.argv[3])
if not name or name in ('.','..') or '/' in name or '\x00' in name or len(name.encode())>255:
    sys.exit(2)
root=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
directory=-1
work=-1
temporary=None
try:
    try: os.mkdir('input',0o755,dir_fd=root)
    except FileExistsError: pass
    directory=os.open('input',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
    if mode=='create':
        if not 0<=size<=67108864: sys.exit(2)
        temporary='.pending-'+uuid.uuid4().hex
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        digest=hashlib.sha256()
        try:
            left=size
            while left:
                block=sys.stdin.buffer.read(min(left,65536))
                if not block: raise ValueError('short_input')
                digest.update(block)
                view=memoryview(block)
                while view:
                    written=os.write(fd,view)
                    if written<=0: raise ValueError('short_write')
                    view=view[written:]
                left-=len(block)
            os.fsync(fd)
            os.fchmod(fd,0o444)
        finally: os.close(fd)
        os.link(temporary,name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
        os.unlink(temporary,dir_fd=directory)
        temporary=None
        os.fsync(directory)
        result={'byte_size':size,'sha256':digest.hexdigest()}
    elif mode=='readback':
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not_regular')
            digest=hashlib.sha256();length=0
            while True:
                block=os.read(fd,65536)
                if not block: break
                length+=len(block)
                if length>67108864: raise ValueError('too_large')
                digest.update(block)
            result={'byte_size':length,'sha256':digest.hexdigest()}
        finally: os.close(fd)
    elif mode in ('copy_work','readback_work'):
        work=os.open('work',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
        if mode=='copy_work':
            source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            temporary='.pending-'+uuid.uuid4().hex
            target=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700,dir_fd=work)
            digest=hashlib.sha256();length=0
            try:
                if not stat.S_ISREG(os.fstat(source).st_mode): raise ValueError('not_regular')
                while True:
                    block=os.read(source,65536)
                    if not block: break
                    length+=len(block)
                    if length>67108864: raise ValueError('too_large')
                    digest.update(block)
                    view=memoryview(block)
                    while view:
                        written=os.write(target,view)
                        if written<=0: raise ValueError('short_write')
                        view=view[written:]
                os.fchmod(target,0o500)
                os.fsync(target)
            finally:
                os.close(target);os.close(source)
            os.link(temporary,name,src_dir_fd=work,dst_dir_fd=work,follow_symlinks=False)
            os.unlink(temporary,dir_fd=work);temporary=None
            os.fsync(work)
            result={'byte_size':length,'sha256':digest.hexdigest()}
        else:
            item=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=work)
            try:
                if not stat.S_ISREG(os.fstat(item).st_mode): raise ValueError('not_regular')
                digest=hashlib.sha256();length=0
                while True:
                    block=os.read(item,65536)
                    if not block: break
                    length+=len(block)
                    if length>67108864: raise ValueError('too_large')
                    digest.update(block)
                result={'byte_size':length,'sha256':digest.hexdigest()}
            finally: os.close(item)
    else: sys.exit(2)
    print(json.dumps(result),flush=True)
except Exception:
    print('attachment_operation_rejected',file=sys.stderr)
    sys.exit(1)
finally:
    if temporary is not None and directory>=0:
        try: os.unlink(temporary,dir_fd=directory)
        except OSError: pass
    if directory>=0: os.close(directory)
    if work>=0: os.close(work)
    os.close(root)
"""


@dataclass(frozen=True, slots=True)
class T6Attachment:
    name: str
    source: Path
    byte_size: int
    sha256: str

    @property
    def sandbox_path(self) -> str:
        return "/workspace/input/" + self.name


@dataclass(frozen=True, slots=True)
class T6AttachmentReadback:
    byte_size: int
    sha256: str


def validate_attachment_batch(
    attachments: Sequence[T6Attachment], *, check_source: bool = True,
) -> None:
    if len(attachments) > MAX_T6_ATTACHMENTS:
        raise ValueError("attachment_count_exceeded")
    names: set[str] = set()
    total = 0
    for item in attachments:
        if not isinstance(item, T6Attachment):
            raise TypeError("invalid_attachment")
        if (
            not item.name
            or item.name in {".", ".."}
            or any(char in item.name for char in ("/", "\\", "\x00"))
            or len(item.name.encode("utf-8")) > 255
            or item.name in names
        ):
            raise ValueError("invalid_attachment_name")
        names.add(item.name)
        if type(item.byte_size) is not int or item.byte_size < 0:
            raise ValueError("invalid_attachment_size")
        total += item.byte_size
        if total > MAX_T6_INPUT_BYTES:
            raise ValueError("attachment_run_capacity_exceeded")
        if (
            not isinstance(item.sha256, str)
            or len(item.sha256) != 64
            or any(char not in "0123456789abcdef" for char in item.sha256)
        ):
            raise ValueError("invalid_attachment_sha256")
        if check_source:
            if item.source.is_symlink() or not item.source.is_file():
                raise ValueError("attachment_source_rejected")
            if item.source.stat().st_size != item.byte_size:
                raise ValueError("attachment_source_size_changed")


class T6AttachmentImportError(CodedError):
    pass


class T6AttachmentImporter:
    """Only fixed import/readback code runs as root; tool commands stay non-root."""

    def __init__(self, sandbox):
        self.sandbox = sandbox

    def import_one(self, sandbox_id, attachment, timeout_seconds, cancellation_token):
        validate_attachment_batch((attachment,))
        with attachment.source.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise T6AttachmentImportError("attachment_source_rejected")
            if os.fstat(source.fileno()).st_size != attachment.byte_size:
                raise T6AttachmentImportError("attachment_source_size_changed")
            written, streamed_hash = self._execute(
                sandbox_id,
                attachment.name,
                "create",
                attachment.byte_size,
                source,
                timeout_seconds,
                cancellation_token,
                user="0:0",
            )
        if written != attachment.byte_size or streamed_hash != attachment.sha256:
            raise T6AttachmentImportError("attachment_source_hash_changed")
        readback = self.readback(
            sandbox_id, attachment, timeout_seconds, cancellation_token
        )
        if readback != T6AttachmentReadback(attachment.byte_size, attachment.sha256):
            raise T6AttachmentImportError("attachment_readback_mismatch")
        return readback

    def readback(self, sandbox_id, attachment, timeout_seconds, cancellation_token):
        length, digest = self._execute(
            sandbox_id,
            attachment.name,
            "readback",
            0,
            None,
            timeout_seconds,
            cancellation_token,
            user="10001:10001",
        )
        return T6AttachmentReadback(length, digest)

    def copy_to_work(self, sandbox_id, attachment, timeout_seconds, cancellation_token):
        validate_attachment_batch((attachment,), check_source=False)
        length, digest = self._execute(
            sandbox_id, attachment.name, "copy_work", 0, None,
            timeout_seconds, cancellation_token, user="10001:10001",
        )
        if (length, digest) != (attachment.byte_size, attachment.sha256):
            raise T6AttachmentImportError("work_copy_hash_mismatch")
        return self.readback_work(
            sandbox_id, attachment, timeout_seconds, cancellation_token,
        )

    def readback_work(self, sandbox_id, attachment, timeout_seconds, cancellation_token):
        length, digest = self._execute(
            sandbox_id, attachment.name, "readback_work", 0, None,
            timeout_seconds, cancellation_token, user="10001:10001",
        )
        if (length, digest) != (attachment.byte_size, attachment.sha256):
            raise T6AttachmentImportError("work_copy_hash_mismatch")
        return T6AttachmentReadback(length, digest)

    def _execute(
        self,
        sandbox_id: str,
        name: str,
        mode: str,
        size: int,
        source: BinaryIO | None,
        timeout_seconds: float,
        cancellation_token,
        *,
        user: str,
    ) -> tuple[int, str]:
        from docker.utils.socket import next_frame_header, read_exactly

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid_import_timeout")
        binding = self.sandbox._binding(sandbox_id)
        end = time.monotonic() + timeout_seconds
        if not binding.execution_lock.acquire(timeout=timeout_seconds):
            raise TimeoutError("import_lock_timeout")
        sock = None
        done = threading.Event()
        dispatched = False
        try:
            if cancellation_token.is_cancelled or time.monotonic() >= end:
                raise TimeoutError("import_cancelled_or_timed_out")
            self.sandbox._verify_active(binding.task_run_id, sandbox_id)
            api = self.sandbox._runtime.client.api
            with self.sandbox._admit(binding):
                execution = api.exec_create(
                    sandbox_id,
                    [
                        "timeout",
                        "--signal=TERM",
                        "--kill-after=5s",
                        f"{max(1, math.ceil(timeout_seconds))}s",
                        "python3",
                        "-I",
                        "-c",
                        IMPORT_SCRIPT,
                        name,
                        mode,
                        str(size),
                    ],
                    stdin=source is not None,
                    stdout=True,
                    stderr=True,
                    tty=False,
                    privileged=False,
                    user=user,
                    workdir="/workspace",
                    environment={"PATH": "/usr/bin:/bin"},
                )
                dispatched = True
            with self.sandbox._admit(binding):
                sock = api.exec_start(execution["Id"], socket=True, tty=False)
            raw = getattr(sock, "_sock", sock)
            if hasattr(raw, "settimeout"):
                raw.settimeout(max(0.01, end - time.monotonic()))

            def interrupt_wait():
                while not done.wait(0.025):
                    if cancellation_token.is_cancelled or time.monotonic() >= end:
                        try:
                            raw.close()
                        except OSError:
                            pass
                        return

            threading.Thread(target=interrupt_wait, daemon=True).start()
            digest = hashlib.sha256()
            if source is not None:
                remaining = size
                while remaining:
                    if cancellation_token.is_cancelled or time.monotonic() >= end:
                        raise TimeoutError("import_cancelled_or_timed_out")
                    block = source.read(min(65536, remaining))
                    if not block:
                        raise T6AttachmentImportError("attachment_source_size_changed")
                    raw.sendall(block)
                    digest.update(block)
                    remaining -= len(block)
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
                    raise T6AttachmentImportError("import_output_rejected")
                (stdout if stream == 1 else stderr).extend(read_exactly(raw, length))
            if cancellation_token.is_cancelled or time.monotonic() >= end:
                raise TimeoutError("import_cancelled_or_timed_out")
            result = api.exec_inspect(execution["Id"])
            if (
                result.get("Running") is not False
                or result.get("ExitCode") != 0
                or stderr
            ):
                raise T6AttachmentImportError("import_not_confirmed")
            value = json.loads(stdout)
            if not isinstance(value, dict) or set(value) != {"byte_size", "sha256"}:
                raise T6AttachmentImportError("import_output_rejected")
            readback = T6AttachmentReadback(**value)
            if (
                type(readback.byte_size) is not int
                or not 0 <= readback.byte_size <= MAX_T6_INPUT_BYTES
                or not isinstance(readback.sha256, str)
                or len(readback.sha256) != 64
                or any(char not in "0123456789abcdef" for char in readback.sha256)
            ):
                raise T6AttachmentImportError("import_output_rejected")
            if source is not None and readback.sha256 != digest.hexdigest():
                raise T6AttachmentImportError("import_stream_mismatch")
            return readback.byte_size, readback.sha256
        except TaskRunSandboxError:
            raise
        except Exception as exc:
            if dispatched:
                raise TaskRunSandboxError(
                    "attachment_import",
                    "outcome_unknown",
                    binding.task_run_id,
                    (sandbox_id,),
                    original_error=exc,
                ) from exc
            raise
        finally:
            done.set()
            if sock is not None:
                sock.close()
            binding.execution_lock.release()
