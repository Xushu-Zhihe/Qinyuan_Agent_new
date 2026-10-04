"""Trusted copy preparation and one bounded digest observation for D1."""

from __future__ import annotations

import asyncio
import math
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from agent_core.attachment_import import (
    AttachmentImportReadback,
    AttachmentImportReceipt,
)
from agent_core.command_contracts import (
    SandboxCommandPolicy,
    T6EvidenceError,
    validate_working_directory,
)
from agent_core.domain import IntentStatus, PlanStatus
from agent_core.intent_postconditions import (
    FileDigestObservation,
    FileSha256,
    IntentPostconditions,
)
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus
from agent_core.turn_contracts import (
    D1OperationCompletion,
    D1OperationStart,
    D1RecoveryRequired,
    D1RunConfig,
    D1TurnBinding,
    d1_sha256,
)

D1_DIGEST_SCRIPT = r"""import hashlib,json,os,stat,sys
path=sys.argv[1];root='/workspace/work'
parts=path[len(root)+1:].split('/')
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    for part in parts[:-1]:
        nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        os.close(fd);fd=nextfd
    item=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        if not stat.S_ISREG(os.fstat(item).st_mode):
            print(json.dumps({'status':'unsafe'}));sys.exit(0)
        digest=hashlib.sha256()
        while True:
            block=os.read(item,65536)
            if not block:break
            digest.update(block)
        print(json.dumps({'status':'present','sha256':digest.hexdigest()}))
    finally:os.close(item)
except FileNotFoundError:
    print(json.dumps({'status':'missing'}))
except OSError:
    print(json.dumps({'status':'unsafe'}))
finally:os.close(fd)
"""


def fixed_digest_script(path: str) -> str:
    validate_working_directory(path)
    if path == "/workspace/work":
        raise ValueError("digest_path_is_directory")
    return (
        "exec python3 -I -c " + shlex.quote(D1_DIGEST_SCRIPT) + " " + shlex.quote(path)
    )


def _digest(value: object) -> str:
    return d1_sha256(value)


def _postconditions_match(
    conditions: IntentPostconditions, binding: D1TurnBinding
) -> bool:
    return (
        conditions.task_run_id == binding.task_run_id
        and conditions.intent_id == binding.intent_id
        and conditions.conditions
        == (FileSha256(0, binding.work_path, binding.expected_sha256),)
    )


class D1CommandPolicy(SandboxCommandPolicy):
    def __init__(
        self, binding: D1TurnBinding, config: D1RunConfig, postconditions, ledger
    ) -> None:
        self.binding = binding
        self.config = config
        self.postconditions = postconditions
        self.ledger = ledger

    def authorize(self, request, authorization, state, *, now, step_limit_seconds):
        binding = self.binding
        if (
            self.ledger.load_d1_run_config(binding.task_run_id) != self.config
            or self.ledger.load_d1_turn_binding(binding.turn_id) != binding
            or state.task_run.task_run_id != binding.task_run_id
            or self.config.authorization.authorization_id
            != authorization.authorization_id
            or self.config.authorization.attachment_index != binding.attachment_index
        ):
            raise ValueError("d1_authorization_binding_mismatch")
        dispatch = self.ledger.load_d1_operation(request.request_id)
        if (
            dispatch is None
            or dispatch.start.category != "inner_tool"
            or dispatch.start.task_run_id != binding.task_run_id
            or dispatch.start.resource_ref != request.plan_id
            or dispatch.completion is not None
        ):
            raise ValueError("d1_tool_dispatch_not_declared")
        plan = next(
            (item for item in state.plans if item.plan_id == request.plan_id), None
        )
        intent = next(
            (item for item in state.intents if item.intent_id == binding.intent_id),
            None,
        )
        declared = self.postconditions.read(request.plan_id, binding.intent_id)
        if (
            plan is None
            or plan.intent_id != binding.intent_id
            or intent is None
            or intent.turn_id != binding.turn_id
            or len(plan.steps) != 1
            or request.step_index != 0
            or declared is None
            or declared.plan_id != request.plan_id
            or not _postconditions_match(declared, binding)
            or request.script != fixed_digest_script(binding.work_path)
            or request.working_directory != binding.work_path.rsplit("/", 1)[0]
            or request.stdin != b""
        ):
            raise ValueError("d1_fixed_check_mismatch")
        return super().authorize(
            request,
            authorization,
            state,
            now=now,
            step_limit_seconds=step_limit_seconds,
        )


@dataclass(frozen=True, slots=True)
class D1CopyClaim:
    preparation_ref: str
    binding_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    import_id: str
    sandbox_id: str
    source_path: str
    work_path: str
    byte_size: int
    expected_sha256: str
    started_at: float

    def __post_init__(self):
        _validate_claim(self)
        if (
            type(self.byte_size) is not int
            or not 0 <= self.byte_size <= 16 * 1024 * 1024
        ):
            raise ValueError("invalid_d1_copy_size")
        if not isinstance(self.source_path, str) or not self.source_path.startswith(
            "/workspace/"
        ):
            raise ValueError("invalid_d1_copy_source")


@dataclass(frozen=True, slots=True)
class D1CopyReceipt:
    claim: D1CopyClaim
    sha256: str
    completed_at: float

    def __post_init__(self):
        if (
            not isinstance(self.claim, D1CopyClaim)
            or self.sha256 != self.claim.expected_sha256
        ):
            raise ValueError("invalid_d1_copy_receipt")
        _validate_completion_time(self.claim.started_at, self.completed_at)


@dataclass(frozen=True, slots=True)
class D1DigestClaim:
    operation_id: str
    binding_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    sandbox_id: str
    path: str
    expected_sha256: str
    started_at: float

    def __post_init__(self):
        _validate_claim(self)
        if str(UUID(self.operation_id)) != self.operation_id or not self.plan_id:
            raise ValueError("invalid_d1_digest_claim")


@dataclass(frozen=True, slots=True)
class D1DigestReceipt:
    claim: D1DigestClaim
    observed: FileDigestObservation
    completed_at: float

    def __post_init__(self):
        if not isinstance(self.claim, D1DigestClaim) or not isinstance(
            self.observed, FileDigestObservation
        ):
            raise ValueError("invalid_d1_digest_receipt")
        if self.observed.status == "unknown":
            raise ValueError("unknown_digest_has_no_confirmed_receipt")
        _validate_completion_time(self.claim.started_at, self.completed_at)


def _validate_completion_time(started, completed):
    if (
        isinstance(completed, bool)
        or not isinstance(completed, (int, float))
        or not math.isfinite(completed)
        or completed < started
    ):
        raise ValueError("invalid_d1_workspace_time")


def _validate_claim(claim):
    for name in ("binding_id", "task_run_id", "turn_id", "intent_id"):
        value = getattr(claim, name)
        parsed = UUID(value)
        if parsed.version not in {4, 5} or str(parsed) != value:
            raise ValueError("invalid_d1_workspace_identity")
    if not isinstance(claim.sandbox_id, str) or not claim.sandbox_id:
        raise ValueError("invalid_d1_workspace_sandbox")
    _validate_completion_time(claim.started_at, claim.started_at)
    if len(claim.expected_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in claim.expected_sha256
    ):
        raise ValueError("invalid_d1_workspace_digest")
    path = claim.work_path if isinstance(claim, D1CopyClaim) else claim.path
    validate_working_directory(path)
    if not path.startswith(f"/workspace/work/intents/{claim.intent_id}/"):
        raise ValueError("invalid_d1_workspace_path")


class D1WorkspaceRecorderPort(Protocol):
    def begin_copy(self, claim: D1CopyClaim) -> None: ...
    def read_copy_claim(self, reference: str) -> D1CopyClaim | None: ...
    def save_copy(self, receipt: D1CopyReceipt) -> None: ...
    def read_copy(self, reference: str) -> D1CopyReceipt | None: ...
    def begin_digest(self, claim: D1DigestClaim) -> None: ...
    def read_digest_claim(self, reference: str) -> D1DigestClaim | None: ...
    def save_digest(self, receipt: D1DigestReceipt) -> None: ...
    def read_digest(self, reference: str) -> D1DigestReceipt | None: ...


@dataclass(frozen=True, slots=True)
class D1ProofClaim:
    proof_id: str
    task_run_id: str
    binding_id: str
    intent_id: str
    plan_id: str
    observation_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            parsed = UUID(value)
            if parsed.version not in {4, 5} or str(parsed) != value:
                raise ValueError("invalid_d1_proof_claim")


class D1ProofClaimsPort(Protocol):
    def begin_proof(self, claim: D1ProofClaim) -> None: ...
    def read_proof_claim(self, proof_id: str) -> D1ProofClaim | None: ...


class D1CopySandboxPort(Protocol):
    async def copy_and_readback(
        self,
        sandbox_id: str,
        source_path: str,
        work_path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> AttachmentImportReadback: ...
    def quiesce(self, sandbox_id: str) -> None: ...


class D1DigestPort(Protocol):
    async def hash_file(
        self,
        task_run_id: str,
        sandbox_id: str,
        path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> FileDigestObservation: ...
    def quiesce(self, sandbox_id: str) -> None: ...


class _BoundedOperation:
    def __init__(
        self,
        controller,
        ledger,
        recorder,
        *,
        clock=time.time,
        monotonic=time.monotonic,
        timeout_seconds=60.0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid_d1_local_timeout")
        self.controller = controller
        self.ledger = ledger
        self.recorder = recorder
        self.clock = clock
        self.monotonic = monotonic
        self.timeout_seconds = timeout_seconds

    def _interrupt(self, token) -> bool:
        state = self.controller.control_state
        if state.task_run.status is not TaskRunStatus.RUNNING:
            return True
        if self.clock() >= state.task_run.deadline_at:
            self.controller.advance_time(str(uuid4()))
            return True
        if token.is_cancelled:
            self.controller.cancel(str(uuid4()))
            return True
        return False

    def _budget(self, started: float, token) -> float:
        if self._interrupt(token):
            return 0.0
        budget = min(
            self.timeout_seconds - (self.monotonic() - started),
            self.controller.control_state.task_run.deadline_at - self.clock(),
        )
        if not math.isfinite(budget):
            raise ValueError("invalid_d1_clock")
        return max(0.0, budget)

    async def _wait(self, invocation, started, token, sandbox, sandbox_id):
        task = asyncio.create_task(invocation)
        loop = asyncio.get_running_loop()
        expires = loop.time() + self._budget(started, token)
        try:
            while not task.done():
                budget = min(self._budget(started, token), expires - loop.time())
                if budget <= 0:
                    raise D1RecoveryRequired("d1_operation_outcome_unknown")
                await asyncio.wait({task}, timeout=min(0.025, budget))
            result = task.result()
            if loop.time() >= expires or self._budget(started, token) <= 0:
                # A known response is still saved by the caller, but cannot
                # support a postcondition outside its admitted time window.
                return result, True
            return result, False
        except asyncio.CancelledError:
            self.controller.cancel(str(uuid4()))
            sandbox.quiesce(sandbox_id)
            raise
        except BaseException:
            sandbox.quiesce(sandbox_id)
            raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _start(self, operation_id, category, claim):
        state = self.controller.control_state
        previous = self.ledger.begin_d1_operation(
            D1OperationStart(
                operation_id=operation_id,
                task_run_id=claim.task_run_id,
                category=category,
                started_at=claim.started_at,
                base_checkpoint_sequence=state.latest_checkpoint.sequence,
                context_sha256=_digest(asdict(claim)),
                resource_ref=claim.binding_id,
            )
        )
        if previous is not None:
            raise D1RecoveryRequired("d1_operation_already_admitted")

    def _finish(self, operation_id, receipt, kind, reference):
        self.ledger.complete_d1_operation(
            D1OperationCompletion(
                operation_id=operation_id,
                finished_at=receipt.completed_at,
                result_kind=kind,
                evidence_ref=reference,
                evidence_sha256=_digest(asdict(receipt)),
            )
        )

    def _validate_binding(self, binding):
        state = self.controller.control_state
        if (
            state.task_run.task_run_id != binding.task_run_id
            or self.ledger.load_d1_run_config(binding.task_run_id) is None
            or self.ledger.load_d1_turn_binding(binding.turn_id) != binding
        ):
            raise D1RecoveryRequired("d1_workspace_binding_mismatch")
        return state


class D1CopyPreparer(_BoundedOperation):
    def __init__(self, controller, ledger, recorder, sandbox, imports, **kwargs):
        super().__init__(controller, ledger, recorder, **kwargs)
        self.sandbox = sandbox
        self.imports = imports
        self._lock = asyncio.Lock()

    def read_copy(self, binding: D1TurnBinding) -> D1CopyReceipt | None:
        state = self._validate_binding(binding)
        source = self.imports.read_receipt(binding.import_id)
        if (
            not isinstance(source, AttachmentImportReceipt)
            or source.task_run_id != binding.task_run_id
            or source.sandbox_id != state.task_run.sandbox_id
            or source.attachment_index != binding.attachment_index
            or source.sha256 != binding.expected_sha256
        ):
            raise T6EvidenceError("d1_import_receipt_mismatch")
        previous = self.recorder.read_copy(binding.preparation_ref)
        if previous is None:
            return None
        self._check_copy(binding, source, previous)
        operation = self.ledger.load_d1_operation(binding.preparation_ref)
        if (
            operation is None
            or operation.completion is None
            or operation.start.category != "copy_preparation"
            or operation.start.task_run_id != binding.task_run_id
            or operation.start.resource_ref != binding.binding_id
            or operation.start.context_sha256 != _digest(asdict(previous.claim))
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_ref != binding.preparation_ref
            or operation.completion.evidence_sha256 != _digest(asdict(previous))
        ):
            raise D1RecoveryRequired("d1_copy_completion_unavailable")
        return previous

    async def prepare(
        self, binding: D1TurnBinding, token: CancellationToken
    ) -> D1CopyReceipt:
        async with self._lock:
            state = self._validate_binding(binding)
            source = self.imports.read_receipt(binding.import_id)
            if (
                not isinstance(source, AttachmentImportReceipt)
                or source.task_run_id != binding.task_run_id
                or source.sandbox_id != state.task_run.sandbox_id
                or source.attachment_index != binding.attachment_index
                or source.sha256 != binding.expected_sha256
            ):
                raise T6EvidenceError("d1_import_receipt_mismatch")
            previous = self.read_copy(binding)
            if previous is not None:
                return previous
            if (
                self.recorder.read_copy_claim(binding.preparation_ref) is not None
                or self.ledger.load_d1_operation(binding.preparation_ref) is not None
            ):
                raise D1RecoveryRequired("d1_copy_outcome_unknown")
            started = self.monotonic()
            budget = self._budget(started, token)
            if budget <= 0:
                raise D1RecoveryRequired("d1_deadline_or_cancelled")
            claim = D1CopyClaim(
                binding.preparation_ref,
                binding.binding_id,
                binding.task_run_id,
                binding.turn_id,
                binding.intent_id,
                binding.import_id,
                source.sandbox_id,
                source.sandbox_path,
                binding.work_path,
                source.byte_size,
                source.sha256,
                self.clock(),
            )
            self._start(binding.preparation_ref, "copy_preparation", claim)
            self.recorder.begin_copy(claim)
            try:
                result, expired = await self._wait(
                    self.sandbox.copy_and_readback(
                        source.sandbox_id,
                        source.sandbox_path,
                        binding.work_path,
                        budget,
                        token,
                    ),
                    started,
                    token,
                    self.sandbox,
                    source.sandbox_id,
                )
            except (Exception, asyncio.CancelledError) as exc:
                raise D1RecoveryRequired("d1_copy_outcome_unknown") from exc
            if (
                not isinstance(result, AttachmentImportReadback)
                or result.byte_size != source.byte_size
                or result.sha256 != source.sha256
            ):
                self.sandbox.quiesce(source.sandbox_id)
                raise D1RecoveryRequired("d1_copy_readback_unconfirmed")
            receipt = D1CopyReceipt(
                claim, result.sha256, max(claim.started_at, self.clock())
            )
            self.recorder.save_copy(receipt)
            interrupted = expired or self._interrupt(token)
            self._finish(
                binding.preparation_ref,
                receipt,
                "stale" if interrupted else "completed",
                binding.preparation_ref,
            )
            if interrupted:
                raise D1RecoveryRequired("d1_deadline_or_cancelled")
            self.ledger.mark_d1_operation_applied(
                binding.preparation_ref, binding.intent_id
            )
            return receipt

    @staticmethod
    def _check_copy(binding, source, receipt):
        claim = receipt.claim
        if (
            claim.preparation_ref != binding.preparation_ref
            or claim.binding_id != binding.binding_id
            or claim.task_run_id != binding.task_run_id
            or claim.turn_id != binding.turn_id
            or claim.intent_id != binding.intent_id
            or claim.import_id != binding.import_id
            or claim.work_path != binding.work_path
            or claim.source_path != source.sandbox_path
            or claim.sandbox_id != source.sandbox_id
            or claim.byte_size != source.byte_size
            or claim.expected_sha256 != source.sha256
            or receipt.sha256 != source.sha256
        ):
            raise T6EvidenceError("d1_copy_receipt_mismatch")


class D1DigestObserver(_BoundedOperation):
    def __init__(
        self,
        controller,
        ledger,
        recorder,
        files,
        postconditions,
        binding: D1TurnBinding,
        **kwargs,
    ):
        super().__init__(controller, ledger, recorder, **kwargs)
        self.files = files
        self.postconditions = postconditions
        self.binding = binding
        self._lock = asyncio.Lock()

    def _require_copy(self):
        receipt = self.recorder.read_copy(self.binding.preparation_ref)
        operation = self.ledger.load_d1_operation(self.binding.preparation_ref)
        if receipt is None or operation is None or operation.completion is None:
            raise D1RecoveryRequired("d1_digest_copy_unavailable")
        claim = receipt.claim
        if (
            claim.binding_id != self.binding.binding_id
            or claim.task_run_id != self.binding.task_run_id
            or claim.turn_id != self.binding.turn_id
            or claim.intent_id != self.binding.intent_id
            or claim.import_id != self.binding.import_id
            or claim.work_path != self.binding.work_path
            or claim.sandbox_id != self.controller.control_state.task_run.sandbox_id
            or receipt.sha256 != self.binding.expected_sha256
            or operation.start.category != "copy_preparation"
            or operation.start.task_run_id != self.binding.task_run_id
            or operation.start.context_sha256 != _digest(asdict(claim))
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_ref != self.binding.preparation_ref
            or operation.completion.evidence_sha256 != _digest(asdict(receipt))
        ):
            raise T6EvidenceError("d1_digest_copy_mismatch")

    def operation_id(self, postconditions):
        return str(
            uuid5(
                UUID(self.binding.task_run_id),
                f"d1-first-digest:{postconditions.intent_id}:{postconditions.plan_id}",
            )
        )

    def _validate_postconditions(self, postconditions):
        if (
            not _postconditions_match(postconditions, self.binding)
            or self.postconditions.read(
                postconditions.plan_id, postconditions.intent_id
            )
            != postconditions
        ):
            raise T6EvidenceError("d1_digest_postcondition_mismatch")

    def read(self, plan_id, intent_id=None):
        self._validate_binding(self.binding)
        result = self.postconditions.read(plan_id, intent_id)
        if result is not None and not _postconditions_match(result, self.binding):
            raise T6EvidenceError("d1_digest_postcondition_mismatch")
        return result

    def bind(self, binding):
        self._validate_binding(self.binding)
        if not _postconditions_match(binding, self.binding):
            raise ValueError("d1_digest_postcondition_mismatch")
        return self.postconditions.bind(binding)

    def _read_confirmed(self, binding, index, path):
        self._validate_binding(self.binding)
        self._validate_postconditions(binding)
        self._require_copy()
        if index != 0 or path != self.binding.work_path:
            raise T6EvidenceError("d1_digest_path_mismatch")
        operation_id = self.operation_id(binding)
        receipt = self.recorder.read_digest(operation_id)
        operation = self.ledger.load_d1_operation(operation_id)
        if receipt is None or operation is None or operation.completion is None:
            return None
        claim = receipt.claim
        state = self.controller.control_state
        if (
            claim.binding_id != self.binding.binding_id
            or claim.task_run_id != binding.task_run_id
            or claim.turn_id != self.binding.turn_id
            or claim.intent_id != binding.intent_id
            or claim.plan_id != binding.plan_id
            or claim.sandbox_id != state.task_run.sandbox_id
            or claim.path != path
            or claim.expected_sha256 != self.binding.expected_sha256
            or operation.start.operation_id != operation_id
            or operation.start.task_run_id != binding.task_run_id
            or operation.start.started_at != claim.started_at
            or operation.start.resource_ref != self.binding.binding_id
            or operation.start.context_sha256 != _digest(asdict(claim))
            or operation.start.category != "first_digest"
            or operation.completion.finished_at != receipt.completed_at
            or operation.completion.evidence_sha256 != _digest(asdict(receipt))
            or operation.completion.evidence_ref != operation_id
        ):
            raise T6EvidenceError("d1_digest_evidence_mismatch")
        if operation.completion.result_kind not in {"completed", "not_satisfied"}:
            return None
        satisfied = (
            receipt.observed.status == "present"
            and receipt.observed.sha256 == self.binding.expected_sha256
        )
        if (operation.completion.result_kind == "completed") != satisfied:
            raise T6EvidenceError("d1_digest_result_mismatch")
        if (
            not state.task_run.started_at
            <= claim.started_at
            <= receipt.completed_at
            <= state.task_run.deadline_at
        ):
            raise T6EvidenceError("d1_digest_time_mismatch")
        return receipt.observed

    def read_file(self, binding, index, path):
        confirmed = self._read_confirmed(binding, index, path)
        if confirmed is None:
            return None
        mirrored = self.postconditions.read_file(binding, index, path)
        if mirrored != confirmed:
            raise T6EvidenceError("d1_digest_saved_observation_mismatch")
        return confirmed

    def observe_file(self, binding, index, path, loader):
        # Proof consumers only replay the separately admitted observation.
        return self.read_file(binding, index, path) or FileDigestObservation("unknown")

    async def observe(
        self, binding: IntentPostconditions, token: CancellationToken
    ) -> FileDigestObservation:
        async with self._lock:
            state = self._validate_binding(self.binding)
            self._validate_postconditions(binding)
            self._require_copy()
            operation_id = self.operation_id(binding)
            saved = self.recorder.read_digest(operation_id)
            operation = self.ledger.load_d1_operation(operation_id)
            if (
                saved is not None
                and operation is not None
                and operation.completion is not None
            ):
                if operation.completion.result_kind not in {
                    "completed",
                    "not_satisfied",
                }:
                    return FileDigestObservation("unknown")
                confirmed = self._read_confirmed(binding, 0, self.binding.work_path)
                self.postconditions.observe_file(
                    binding, 0, self.binding.work_path, lambda: confirmed
                )
                return self.read_file(
                    binding, 0, self.binding.work_path
                ) or FileDigestObservation("unknown")
            if (
                self.recorder.read_digest_claim(operation_id) is not None
                or operation is not None
            ):
                return FileDigestObservation("unknown")
            intent = next(
                (i for i in state.intents if i.intent_id == binding.intent_id), None
            )
            plan = next((p for p in state.plans if p.plan_id == binding.plan_id), None)
            if (
                intent is None
                or intent.status is not IntentStatus.RUNNING
                or plan is None
                or plan.intent_id != binding.intent_id
                or plan.status is not PlanStatus.COMPLETED
            ):
                raise ValueError("d1_digest_plan_not_completed")
            started = self.monotonic()
            budget = self._budget(started, token)
            if budget <= 0:
                raise D1RecoveryRequired("d1_deadline_or_cancelled")
            claim = D1DigestClaim(
                operation_id,
                self.binding.binding_id,
                binding.task_run_id,
                self.binding.turn_id,
                binding.intent_id,
                binding.plan_id,
                state.task_run.sandbox_id,
                self.binding.work_path,
                self.binding.expected_sha256,
                self.clock(),
            )
            self._start(operation_id, "first_digest", claim)
            self.recorder.begin_digest(claim)
            try:
                observed, expired = await self._wait(
                    self.files.hash_file(
                        binding.task_run_id,
                        state.task_run.sandbox_id,
                        self.binding.work_path,
                        budget,
                        token,
                    ),
                    started,
                    token,
                    self.files,
                    state.task_run.sandbox_id,
                )
            except (Exception, asyncio.CancelledError):
                return FileDigestObservation("unknown")
            if (
                not isinstance(observed, FileDigestObservation)
                or observed.status == "unknown"
            ):
                self.files.quiesce(state.task_run.sandbox_id)
                return FileDigestObservation("unknown")
            receipt = D1DigestReceipt(
                claim, observed, max(claim.started_at, self.clock())
            )
            self.recorder.save_digest(receipt)
            interrupted = expired or self._interrupt(token)
            satisfied = (
                observed.status == "present"
                and observed.sha256 == self.binding.expected_sha256
            )
            self._finish(
                operation_id,
                receipt,
                "stale"
                if interrupted
                else "completed"
                if satisfied
                else "not_satisfied",
                operation_id,
            )
            if interrupted:
                return FileDigestObservation("unknown")
            self.postconditions.observe_file(
                binding, 0, self.binding.work_path, lambda: observed
            )
            return self.read_file(
                binding, 0, self.binding.work_path
            ) or FileDigestObservation("unknown")
