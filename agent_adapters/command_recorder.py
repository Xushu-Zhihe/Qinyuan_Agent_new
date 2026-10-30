"""Immutable command receipts and byte-preserving Execution evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from agent_core.command_contracts import (
    AuthorizedCommand,
    SandboxCommandAuthorization,
    SandboxCommandRequest,
    SandboxCommandResult,
    T6CommandEvidence,
    T6CommandRecoveryRequired,
    T6EvidenceError,
    T6EvidenceCapacityError,
    T6ExecutionRecord,
)
from agent_core.domain import ExecutionEvidence, FailureAction


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class T6CommandRecorder:
    @staticmethod
    def limit(kind):
        # Stage requests reserve one extra MiB for the original, undispatched
        # source of bounded compaction. Model admission still uses one MiB.
        return (2 if kind in {"model-action", "stage-request"} else 1) * 1024 * 1024

    def encode(self, kind, reference, value, *, limit=None):
        payload = _encode(value)
        envelope = _encode({"sha256": hashlib.sha256(payload).hexdigest(), "payload": value})
        maximum = self.limit(kind) if limit is None else limit
        if len(envelope) > maximum:
            raise T6EvidenceCapacityError(kind, reference, len(envelope), maximum)
        return envelope

    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.directory = directory.resolve()
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, reference: str) -> Path:
        try:
            if str(UUID(reference)) != reference:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise T6EvidenceError("invalid_evidence_reference") from exc
        return self.directory / f"{kind}-{reference}.json"

    def _write(self, kind: str, reference: str, value: dict) -> None:
        envelope = self.encode(kind, reference, value)
        destination = self._path(kind, reference)
        temporary = self.directory / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as output:
                output.write(envelope)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, destination)
        except FileExistsError:
            if self._read(kind, reference) != value:
                raise T6EvidenceError("immutable_evidence_conflict") from None
        except OSError as exc:
            raise T6EvidenceError("evidence_write_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, kind: str, reference: str) -> dict | None:
        try:
            with self._path(kind, reference).open("rb") as source:
                limit = self.limit(kind)
                actual = os.fstat(source.fileno()).st_size
                if actual > limit:
                    raise T6EvidenceCapacityError(kind, reference, actual, limit)
                raw = source.read(limit + 1)
                if len(raw) > limit:
                    raise T6EvidenceCapacityError(kind, reference, len(raw), limit)
            envelope = json.loads(raw)
            payload = envelope["payload"]
            if (
                not isinstance(payload, dict)
                or envelope["sha256"] != hashlib.sha256(_encode(payload)).hexdigest()
            ):
                raise ValueError("invalid_evidence_checksum")
            return payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise T6EvidenceError("evidence_read_failed") from exc

    def replay(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        sandbox_id: str,
    ) -> SandboxCommandResult | None:
        snapshot = {
            "request_id": request.request_id,
            "task_run_id": request.task_run_id,
            "plan_id": request.plan_id,
            "step_index": request.step_index,
            "script": request.script,
            "stdin_sha256": request.stdin_sha256,
            "stdin_size": len(request.stdin),
            "working_directory": request.working_directory,
            "requested_timeout_seconds": request.timeout_seconds,
            "authorization": {
                "authorization_id": authorization.authorization_id,
                "task_run_id": authorization.task_run_id,
                "allowed_tools": list(authorization.allowed_tools),
            },
            "sandbox_id": sandbox_id,
        }
        existing = self._read("receipt", request.request_id)
        if existing is not None:
            if existing != snapshot:
                raise T6EvidenceError("request_id_conflict")
            if self._read("finished", request.request_id) is None:
                raise T6CommandRecoveryRequired("request_outcome_unknown")
            result = self.read_completed(request.request_id)
            if result is None:
                raise T6EvidenceError("finished_record_missing")
            return result
        return None

    def begin(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        sandbox_id: str,
    ) -> SandboxCommandResult | None:
        replay = self.replay(request, authorization, sandbox_id)
        if replay is not None:
            return replay
        # One evidence directory belongs to one TaskRun. Incomplete claims also
        # block a new request ID after a crash before Execution was committed.
        for claim in self.directory.glob("claim-*"):
            previous = self.read_completed(claim.name[len("claim-") :])
            if previous is None or previous.record.status == "outcome_unknown":
                raise T6CommandRecoveryRequired("prior_request_requires_recovery")
        snapshot = {
            "request_id": request.request_id,
            "task_run_id": request.task_run_id,
            "plan_id": request.plan_id,
            "step_index": request.step_index,
            "script": request.script,
            "stdin_sha256": request.stdin_sha256,
            "stdin_size": len(request.stdin),
            "working_directory": request.working_directory,
            "requested_timeout_seconds": request.timeout_seconds,
            "authorization": {
                "authorization_id": authorization.authorization_id,
                "task_run_id": authorization.task_run_id,
                "allowed_tools": list(authorization.allowed_tools),
            },
            "sandbox_id": sandbox_id,
        }
        claim = self.directory / f"claim-{request.request_id}"
        try:
            claim.mkdir()
        except FileExistsError:
            raise T6CommandRecoveryRequired("request_already_claimed") from None
        self._write("receipt", request.request_id, snapshot)
        return None

    def save_command(
        self, reference: str, approved: AuthorizedCommand, argv: tuple[str, ...]
    ) -> None:
        request = approved.request
        if argv != ("bash", "-c", request.script):
            raise T6EvidenceError("command_mismatch")
        self._write(
            "command",
            reference,
            {
                "request_id": request.request_id,
                "task_run_id": request.task_run_id,
                "authorization_id": approved.authorization_id,
                "sandbox_id": approved.sandbox_id,
                "argv": list(argv),
                "stdin_sha256": request.stdin_sha256,
                "stdin_size": len(request.stdin),
                "working_directory": request.working_directory,
                "effective_timeout_seconds": approved.effective_timeout_seconds,
            },
        )

    def read_command(self, reference: str) -> T6CommandEvidence | None:
        value = self._read("command", reference)
        if value is None:
            return None
        try:
            value["argv"] = tuple(value["argv"])
            return T6CommandEvidence(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise T6EvidenceError("invalid_command_evidence") from exc

    def command_exists(self, reference: str) -> bool:
        return self.read_command(reference) is not None

    def save_record(self, record: T6ExecutionRecord) -> None:
        value = asdict(record)
        value["stdout"] = base64.b64encode(record.stdout).decode("ascii")
        value["stderr"] = base64.b64encode(record.stderr).decode("ascii")
        self._write("output", record.request_id, value)

    def read_record(self, reference: str) -> T6ExecutionRecord | None:
        value = self._read("output", reference)
        if value is None:
            return None
        try:
            value["stdout"] = base64.b64decode(value["stdout"], validate=True)
            value["stderr"] = base64.b64decode(value["stderr"], validate=True)
            return T6ExecutionRecord(**value)
        except (ValueError, TypeError, KeyError) as exc:
            raise T6EvidenceError("invalid_execution_record") from exc

    def output_exists(self, reference: str) -> bool:
        return self.read_record(reference) is not None

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        record = self.read_record(execution_id)
        if (
            record is None
            or record.request_id != execution_id
            or record.status == "outcome_unknown"
            or not record.dispatched
            or not record.termination_confirmed
        ):
            return None
        return ExecutionEvidence(record.task_run_id, None, record.finished_at)

    def read_completed(self, request_id: str) -> SandboxCommandResult | None:
        marker = self._read("finished", request_id)
        if marker is None:
            return None
        record = self.read_record(request_id)
        if record is None or marker.get("status") != record.status:
            raise T6EvidenceError("finished_record_invalid")
        receipt = self._read("receipt", request_id)
        if (
            receipt is None
            or any(
                receipt.get(key) != getattr(record, key)
                for key in (
                    "request_id",
                    "task_run_id",
                    "sandbox_id",
                    "plan_id",
                    "step_index",
                )
            )
            or receipt["authorization"]["authorization_id"] != record.authorization_id
            or receipt["authorization"]["task_run_id"] != record.task_run_id
            or "sandbox.command" not in receipt["authorization"]["allowed_tools"]
            and record.status != "policy_rejected"
        ):
            raise T6EvidenceError("finished_receipt_mismatch")
        if record.command_ref is not None:
            command = self.read_command(record.command_ref)
            if command is None or (
                command.task_run_id != record.task_run_id
                or command.sandbox_id != record.sandbox_id
                or command.authorization_id != record.authorization_id
                or command.argv != ("bash", "-c", receipt["script"])
                or command.stdin_sha256 != receipt["stdin_sha256"]
                or command.stdin_size != receipt["stdin_size"]
                or command.working_directory != receipt["working_directory"]
            ):
                raise T6EvidenceError("finished_command_mismatch")
        try:
            action = (
                FailureAction(marker["action"])
                if marker["action"] is not None
                else None
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise T6EvidenceError("finished_action_invalid") from exc
        return SandboxCommandResult(record, action)

    def finish(self, result: SandboxCommandResult) -> None:
        record = result.record
        if self.read_record(record.request_id) != record:
            raise T6EvidenceError("record_not_saved")
        self._write(
            "finished",
            record.request_id,
            {
                "status": record.status,
                "action": result.action.value if result.action else None,
            },
        )
