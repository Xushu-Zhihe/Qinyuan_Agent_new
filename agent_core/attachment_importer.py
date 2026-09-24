"""Trusted local attachment import with durable, conservative recovery."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import stat
import time
from pathlib import Path
from typing import Callable, Protocol

from agent_core.attachment_import import (
    MAX_ATTACHMENT_BYTES,
    AttachmentImportClaim,
    AttachmentImportConflict,
    AttachmentImportReadback,
    AttachmentImportReceipt,
    AttachmentImportRecoveryRequired,
    AttachmentImportRejected,
    AttachmentImportRequest,
    AttachmentImportSandboxPort,
)
from agent_core.control_store import ControlStateSnapshot
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus


class AttachmentImportControllerPort(Protocol):
    @property
    def control_state(self) -> ControlStateSnapshot: ...


class AttachmentImportRecorderPort(Protocol):
    def read_claim(self, import_id: str) -> AttachmentImportClaim | None: ...

    def read_receipt(self, import_id: str) -> AttachmentImportReceipt | None: ...

    def begin(self, claim: AttachmentImportClaim) -> AttachmentImportReceipt | None: ...

    def save_receipt(self, receipt: AttachmentImportReceipt) -> None: ...


def _same_request(
    request: AttachmentImportRequest, claim: AttachmentImportClaim
) -> bool:
    return (
        request.import_id == claim.import_id
        and request.task_run_id == claim.task_run_id
        and request.sandbox_id == claim.sandbox_id
        and request.attachment_index == claim.attachment_index
        and request.source_root == claim.source_root
        and request.sandbox_path == claim.sandbox_path
    )


def _read_source(source_root: str, local_path: str) -> bytes:
    root = Path(source_root)
    try:
        if (
            not root.is_absolute()
            or str(root.resolve(strict=True)) != source_root
            or not root.is_dir()
        ):
            raise AttachmentImportRejected("source_root_unavailable")
    except (OSError, RuntimeError):
        raise AttachmentImportRejected("source_root_unavailable") from None

    relative = Path(local_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise AttachmentImportRejected("source_path_rejected")
    target = root
    try:
        for index, component in enumerate(relative.parts):
            target = target / component
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise AttachmentImportRejected("source_path_rejected")
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise AttachmentImportRejected("source_path_rejected")
        if not stat.S_ISREG(info.st_mode):
            raise AttachmentImportRejected("source_path_rejected")
        if not target.resolve(strict=True).is_relative_to(root):
            raise AttachmentImportRejected("source_path_rejected")
        if info.st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentImportRejected("attachment_too_large")
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise AttachmentImportRejected("source_path_rejected")
            content = stream.read(MAX_ATTACHMENT_BYTES + 1)
            finished = os.fstat(stream.fileno())
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise AttachmentImportRejected("attachment_too_large")
        if (
            (opened.st_size, opened.st_mtime_ns, opened.st_dev, opened.st_ino)
            != (
                finished.st_size,
                finished.st_mtime_ns,
                finished.st_dev,
                finished.st_ino,
            )
            or len(content) != finished.st_size
            or not stat.S_ISREG(target.lstat().st_mode)
        ):
            raise AttachmentImportRejected("source_unavailable")
        return content
    except AttachmentImportRejected:
        raise
    except FileNotFoundError:
        raise AttachmentImportRejected("source_unavailable") from None
    except (OSError, RuntimeError):
        raise AttachmentImportRejected("source_unavailable") from None


class AttachmentImporter:
    def __init__(
        self,
        *,
        controller: AttachmentImportControllerPort,
        recorder: AttachmentImportRecorderPort,
        sandbox: AttachmentImportSandboxPort,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._recorder = recorder
        self._sandbox = sandbox
        self._clock = clock
        self._monotonic = monotonic

    def _state(self) -> ControlStateSnapshot:
        return self._controller.control_state

    def _budget(
        self, state: ControlStateSnapshot, started: float, token: CancellationToken
    ) -> float:
        if token.is_cancelled or state.task_run.status is not TaskRunStatus.RUNNING:
            return 0.0
        remaining = min(
            60.0 - (self._monotonic() - started),
            state.task_run.deadline_at - self._clock(),
        )
        if not math.isfinite(remaining):
            raise ValueError("invalid import clock")
        return max(0.0, remaining)

    @staticmethod
    def _match_readback(
        claim: AttachmentImportClaim, result: AttachmentImportReadback
    ) -> bool:
        return result.byte_size == claim.byte_size and result.sha256 == claim.sha256

    async def import_attachment(
        self, request: AttachmentImportRequest, cancellation_token: CancellationToken
    ) -> AttachmentImportReceipt:
        if not isinstance(request, AttachmentImportRequest):
            raise TypeError("request must be an AttachmentImportRequest")
        state = self._state()
        task = state.task_run
        if request.task_run_id != task.task_run_id:
            raise AttachmentImportRejected("task_run_mismatch")
        if request.sandbox_id != task.sandbox_id:
            raise AttachmentImportRejected("sandbox_mismatch")
        if task.source_root is None or request.source_root != task.source_root:
            raise AttachmentImportRejected("source_root_unavailable")

        previous = self._recorder.read_claim(request.import_id)
        if previous is not None:
            if not _same_request(request, previous):
                raise AttachmentImportConflict(
                    "import ID has different request content"
                )
            completed = self._recorder.read_receipt(request.import_id)
            if completed is None:
                raise AttachmentImportRecoveryRequired(
                    "import claim exists without a confirmed result"
                )
            return completed

        if task.status is not TaskRunStatus.RUNNING:
            raise AttachmentImportRejected("task_run_not_running")
        if (
            request.attachment_index >= len(task.request.target.attachments)
            or task.request.target.attachments[request.attachment_index].kind
            != "local_path"
        ):
            raise AttachmentImportRejected("attachment_not_local")

        started = self._monotonic()
        budget = self._budget(state, started, cancellation_token)
        if budget <= 0:
            raise AttachmentImportRejected("deadline_or_cancelled")
        local_path = task.request.target.attachments[request.attachment_index].value
        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(_read_source, request.source_root, local_path),
                timeout=budget,
            )
        except (TimeoutError, asyncio.CancelledError):
            raise AttachmentImportRejected("deadline_or_cancelled") from None
        state = self._state()
        if self._budget(state, started, cancellation_token) <= 0:
            raise AttachmentImportRejected("deadline_or_cancelled")

        claim = AttachmentImportClaim(
            import_id=request.import_id,
            task_run_id=request.task_run_id,
            sandbox_id=request.sandbox_id,
            attachment_index=request.attachment_index,
            local_path=local_path,
            source_root=request.source_root,
            sandbox_path=request.sandbox_path,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            claimed_at=self._clock(),
        )
        completed = self._recorder.begin(claim)
        if completed is not None:
            return completed
        return await self._confirm(claim, started, cancellation_token, content=content)

    async def recover_import(
        self, import_id: str, cancellation_token: CancellationToken
    ) -> AttachmentImportReceipt:
        claim = self._recorder.read_claim(import_id)
        if claim is None:
            raise AttachmentImportRejected("import_not_found")
        state = self._state()
        task = state.task_run
        if claim.task_run_id != task.task_run_id:
            raise AttachmentImportRejected("task_run_mismatch")
        if claim.sandbox_id != task.sandbox_id:
            raise AttachmentImportRejected("sandbox_mismatch")
        if task.source_root != claim.source_root:
            raise AttachmentImportRejected("source_root_unavailable")
        completed = self._recorder.read_receipt(import_id)
        if completed is not None:
            return completed
        return await self._confirm(
            claim, self._monotonic(), cancellation_token, content=None
        )

    async def _confirm(
        self,
        claim: AttachmentImportClaim,
        started: float,
        token: CancellationToken,
        *,
        content: bytes | None,
    ) -> AttachmentImportReceipt:
        state = self._state()
        budget = self._budget(state, started, token)
        if budget <= 0:
            raise AttachmentImportRecoveryRequired(
                "import claim requires explicit recovery"
            )
        try:
            if content is None:
                result = await asyncio.wait_for(
                    self._sandbox.readback(
                        claim.sandbox_id, claim.sandbox_path, budget, token
                    ),
                    timeout=budget,
                )
            else:
                result = await asyncio.wait_for(
                    self._sandbox.create_and_readback(
                        claim.sandbox_id, claim.sandbox_path, content, budget, token
                    ),
                    timeout=budget,
                )
        except (Exception, asyncio.CancelledError):
            raise AttachmentImportRecoveryRequired(
                "import outcome is unknown; explicit recovery required"
            ) from None
        if (
            not isinstance(result, AttachmentImportReadback)
            or not self._match_readback(claim, result)
            or self._budget(self._state(), started, token) <= 0
        ):
            raise AttachmentImportRecoveryRequired(
                "import readback does not confirm the original content"
            )
        receipt = AttachmentImportReceipt(
            import_id=claim.import_id,
            task_run_id=claim.task_run_id,
            sandbox_id=claim.sandbox_id,
            attachment_index=claim.attachment_index,
            local_path=claim.local_path,
            source_root=claim.source_root,
            sandbox_path=claim.sandbox_path,
            byte_size=claim.byte_size,
            sha256=claim.sha256,
            claimed_at=claim.claimed_at,
            completed_at=max(claim.claimed_at, self._clock()),
        )
        self._recorder.save_receipt(receipt)
        return receipt
