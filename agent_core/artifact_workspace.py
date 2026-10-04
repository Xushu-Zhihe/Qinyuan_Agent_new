"""Versioned workspace, scoped stop, and explicit share evidence for D3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from agent_core.attachment_import import AttachmentImportReadback
from agent_core.command_contracts import validate_working_directory
from agent_core.dual_llm_contracts import _sha, _time, _uuid
from agent_core.intent_postconditions import FileDigestObservation
from agent_core.modeling import CancellationToken


def artifact_path(intent_id: str, artifact_version_id: str) -> str:
    _uuid(intent_id, "intent_id")
    _uuid(artifact_version_id, "artifact_version_id")
    return f"/workspace/work/intents/{intent_id}/attachment-{artifact_version_id}"


def shared_path(intent_id: str, share_id: str) -> str:
    _uuid(intent_id, "intent_id")
    _uuid(share_id, "share_id")
    return f"/workspace/work/intents/{intent_id}/shared-{share_id}"


@dataclass(frozen=True, slots=True)
class D3ArtifactClaim:
    artifact_version_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    import_id: str
    attachment_index: int
    sandbox_id: str
    source_path: str
    path: str
    expected_byte_size: int
    expected_sha256: str
    started_at: float

    def __post_init__(self) -> None:
        for name in (
            "artifact_version_id",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "import_id",
        ):
            _uuid(getattr(self, name), name)
        if type(self.attachment_index) is not int or self.attachment_index not in {
            0,
            1,
        }:
            raise ValueError("D3 artifact attachment index is invalid")
        if not isinstance(self.sandbox_id, str) or not self.sandbox_id:
            raise ValueError("D3 artifact Sandbox is invalid")
        if (
            self.source_path
            != f"/workspace/input/d3-attachment-{self.attachment_index}"
        ):
            raise ValueError("D3 artifact source must be the frozen import")
        if self.path != artifact_path(self.intent_id, self.artifact_version_id):
            raise ValueError("D3 artifact path differs from its immutable version")
        if (
            type(self.expected_byte_size) is not int
            or not 0 <= self.expected_byte_size <= 16 * 1024 * 1024
        ):
            raise ValueError("D3 artifact expected size is invalid")
        _sha(self.expected_sha256, "expected_sha256")
        _time(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class D3ArtifactReceipt:
    claim: D3ArtifactClaim
    readback: AttachmentImportReadback
    status: Literal["confirmed", "integrity_exception"]
    completed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.claim, D3ArtifactClaim) or not isinstance(
            self.readback, AttachmentImportReadback
        ):
            raise TypeError("D3 artifact receipt requires its claim and readback")
        _time(self.completed_at, "completed_at")
        if self.completed_at < self.claim.started_at:
            raise ValueError("D3 artifact receipt precedes its claim")
        matches = self.readback == AttachmentImportReadback(
            self.claim.expected_byte_size, self.claim.expected_sha256
        )
        if (self.status == "confirmed") != matches:
            raise ValueError("D3 artifact status differs from its readback")


@dataclass(frozen=True, slots=True)
class D3SizeObservation:
    task_run_id: str
    plan_id: str
    artifact_version_id: str
    execution_id: str
    expected_byte_size: int
    observed_status: Literal["present", "missing", "unsafe", "invalid"]
    observed_byte_size: int | None
    output_sha256: str
    status: Literal["confirmed", "integrity_exception"]

    def __post_init__(self) -> None:
        for name in ("task_run_id", "plan_id", "artifact_version_id", "execution_id"):
            _uuid(getattr(self, name), name)
        if (
            type(self.expected_byte_size) is not int
            or not 0 <= self.expected_byte_size <= 16 * 1024 * 1024
        ):
            raise ValueError("D3 size expectation is invalid")
        if self.observed_status not in {"present", "missing", "unsafe", "invalid"}:
            raise ValueError("D3 size observation status is invalid")
        if self.observed_status == "present":
            if (
                type(self.observed_byte_size) is not int
                or not 0 <= self.observed_byte_size <= 16 * 1024 * 1024
            ):
                raise ValueError("D3 observed size is invalid")
        elif self.observed_byte_size is not None:
            raise ValueError("D3 non-present size cannot have a byte count")
        _sha(self.output_sha256, "output_sha256")
        if (self.status == "confirmed") != (
            self.observed_status == "present"
            and self.observed_byte_size == self.expected_byte_size
        ):
            raise ValueError("D3 size comparison differs from its observation")


@dataclass(frozen=True, slots=True)
class D3DigestClaim:
    observation_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    artifact_version_id: str
    execution_id: str
    sandbox_id: str
    path: str
    expected_sha256: str
    started_at: float

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "artifact_version_id",
            "execution_id",
        ):
            _uuid(getattr(self, name), name)
        if not isinstance(self.sandbox_id, str) or not self.sandbox_id:
            raise ValueError("D3 digest Sandbox is invalid")
        if self.path != artifact_path(self.intent_id, self.artifact_version_id):
            raise ValueError("D3 digest path differs from its artifact version")
        _sha(self.expected_sha256, "expected_sha256")
        _time(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class D3DigestReceipt:
    claim: D3DigestClaim
    observed: FileDigestObservation
    status: Literal["confirmed", "integrity_exception"]
    completed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.claim, D3DigestClaim) or not isinstance(
            self.observed, FileDigestObservation
        ):
            raise TypeError("D3 digest receipt requires typed claim and observation")
        if self.observed.status == "unknown":
            raise ValueError("unknown D3 digest result cannot receive a receipt")
        _time(self.completed_at, "completed_at")
        if self.completed_at < self.claim.started_at:
            raise ValueError("D3 digest receipt precedes its claim")
        matches = (
            self.observed.status == "present"
            and self.observed.sha256 == self.claim.expected_sha256
        )
        if (self.status == "confirmed") != matches:
            raise ValueError("D3 digest status differs from its observation")


@dataclass(frozen=True, slots=True)
class D3ShareClaim:
    share_id: str
    task_run_id: str
    source_turn_id: str
    source_intent_id: str
    artifact_version_id: str
    source_path: str
    source_byte_size: int
    source_sha256: str
    original_verification_status: Literal["verified", "unverified"]
    target_turn_id: str
    target_intent_id: str
    target_path: str
    started_at: float
    source_proof_id: str | None = None
    source_fact_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "share_id",
            "task_run_id",
            "source_turn_id",
            "source_intent_id",
            "artifact_version_id",
            "target_turn_id",
            "target_intent_id",
        ):
            _uuid(getattr(self, name), name)
        if self.source_intent_id == self.target_intent_id:
            raise ValueError("D3 share requires another Intent")
        if self.source_path != artifact_path(
            self.source_intent_id, self.artifact_version_id
        ):
            raise ValueError("D3 share source path differs from its version")
        if self.target_path != shared_path(self.target_intent_id, self.share_id):
            raise ValueError("D3 share target must remain in the receiving Intent")
        validate_working_directory(self.source_path)
        validate_working_directory(self.target_path)
        if (
            type(self.source_byte_size) is not int
            or not 0 <= self.source_byte_size <= 16 * 1024 * 1024
        ):
            raise ValueError("D3 share source size is invalid")
        _sha(self.source_sha256, "source_sha256")
        if self.original_verification_status not in {"verified", "unverified"}:
            raise ValueError("D3 share verification status is invalid")
        if self.original_verification_status == "verified":
            _uuid(self.source_proof_id, "source_proof_id")
            _uuid(self.source_fact_id, "source_fact_id")
        elif self.source_proof_id is not None or self.source_fact_id is not None:
            raise ValueError("unverified D3 share cannot claim verified support")
        _time(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class D3ShareReadback:
    source: AttachmentImportReadback
    target: AttachmentImportReadback


@dataclass(frozen=True, slots=True)
class D3ShareReceipt:
    claim: D3ShareClaim
    source: AttachmentImportReadback
    target: AttachmentImportReadback
    status: Literal["confirmed", "rejected"]
    completed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.claim, D3ShareClaim):
            raise TypeError("D3 share receipt requires its claim")
        if not isinstance(self.source, AttachmentImportReadback) or not isinstance(
            self.target, AttachmentImportReadback
        ):
            raise TypeError("D3 share receipt requires both readbacks")
        _time(self.completed_at, "completed_at")
        if self.completed_at < self.claim.started_at:
            raise ValueError("D3 share receipt precedes its claim")
        matches = (
            self.source
            == self.target
            == AttachmentImportReadback(
                self.claim.source_byte_size, self.claim.source_sha256
            )
        )
        if (self.status == "confirmed") != matches:
            raise ValueError("D3 share status differs from both readbacks")


@dataclass(frozen=True, slots=True)
class D3StopClaim:
    stop_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    review_id: str
    reason: Literal["sibling_switch", "turn_target_adjusted"]
    process_refs: tuple[str, ...]
    started_at: float

    def __post_init__(self) -> None:
        for name in ("stop_id", "task_run_id", "turn_id", "intent_id", "review_id"):
            _uuid(getattr(self, name), name)
        if self.reason not in {"sibling_switch", "turn_target_adjusted"}:
            raise ValueError("D3 stop reason is invalid")
        if not isinstance(self.process_refs, tuple) or len(
            set(self.process_refs)
        ) != len(self.process_refs):
            raise ValueError("D3 process ownership declaration is invalid")
        for process_ref in self.process_refs:
            _uuid(process_ref, "process_ref")
        _time(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class D3StopReadback:
    stopped_refs: tuple[str, ...]
    absent_refs: tuple[str, ...]
    unknown_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        all_refs = self.stopped_refs + self.absent_refs + self.unknown_refs
        if len(set(all_refs)) != len(all_refs):
            raise ValueError("D3 stop readback has duplicate process references")
        for process_ref in all_refs:
            _uuid(process_ref, "process_ref")


@dataclass(frozen=True, slots=True)
class D3StopReceipt:
    claim: D3StopClaim
    readback: D3StopReadback
    status: Literal["confirmed", "unknown"]
    completed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.claim, D3StopClaim) or not isinstance(
            self.readback, D3StopReadback
        ):
            raise TypeError("D3 stop receipt requires typed claim and readback")
        _time(self.completed_at, "completed_at")
        if self.completed_at < self.claim.started_at:
            raise ValueError("D3 stop receipt precedes its claim")
        reported = set(
            self.readback.stopped_refs
            + self.readback.absent_refs
            + self.readback.unknown_refs
        )
        if reported != set(self.claim.process_refs):
            raise ValueError("D3 stop result omits an owned process")
        if (self.status == "confirmed") != (not self.readback.unknown_refs):
            raise ValueError("D3 stop status differs from its process results")


@dataclass(frozen=True, slots=True)
class D3ReadbackAudit:
    audit_id: str
    operation_id: str
    task_run_id: str
    category: Literal["copy_preparation", "artifact_share", "intent_stop"]
    source: AttachmentImportReadback | None
    target: AttachmentImportReadback | None
    process_readback: D3StopReadback | None
    checked_at: float

    def __post_init__(self) -> None:
        for name in ("audit_id", "operation_id", "task_run_id"):
            _uuid(getattr(self, name), name)
        if self.category not in {"copy_preparation", "artifact_share", "intent_stop"}:
            raise ValueError("D3 audit category is invalid")
        if self.category == "intent_stop":
            if (
                self.source is not None
                or self.target is not None
                or not isinstance(self.process_readback, D3StopReadback)
            ):
                raise ValueError(
                    "D3 stop audit requires only a read-only process query"
                )
        elif (
            not isinstance(self.source, AttachmentImportReadback)
            or not isinstance(self.target, AttachmentImportReadback)
            or self.process_readback is not None
        ):
            raise ValueError("D3 copy/share audit requires both file readbacks")
        _time(self.checked_at, "checked_at")


class D3WorkspaceSandboxPort(Protocol):
    async def copy_exclusive_and_readback(
        self,
        sandbox_id: str,
        source_path: str,
        target_path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> AttachmentImportReadback: ...

    async def readback(
        self,
        sandbox_id: str,
        path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> AttachmentImportReadback: ...

    async def hash_file(
        self,
        task_run_id: str,
        sandbox_id: str,
        path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> FileDigestObservation: ...

    async def share_exclusive_and_readback(
        self,
        sandbox_id: str,
        source_path: str,
        target_path: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> D3ShareReadback: ...

    async def stop_intent_processes(
        self,
        claim: D3StopClaim,
        sandbox_id: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> D3StopReadback: ...

    async def query_intent_processes(
        self,
        claim: D3StopClaim,
        sandbox_id: str,
        timeout_seconds: float,
        token: CancellationToken,
    ) -> D3StopReadback: ...
