"""Durable evidence files, separate from the control-plane SQLite database."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4

from agent_core.domain import ExecutionEvidence, FailureAction, FailureType
from agent_core.tool_contracts import (
    ExecutionRecord,
    MCPCommand,
    ToolAuthorization,
    ToolRecoveryRequired,
    ToolRequest,
    ToolResult,
    StoredToolRequest,
)


class EvidenceStorageError(RuntimeError):
    """Evidence could not be durably written or validated."""


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


class FileToolRecorder:
    def __init__(
        self, directory: Path, *, sensitive_values: tuple[str, ...] = (), read_only: bool = False
    ) -> None:
        if not isinstance(sensitive_values, tuple) or any(
            not isinstance(v, str) or not v for v in sensitive_values
        ):
            raise ValueError("sensitive_values must contain non-empty strings")
        self.directory = directory.resolve()
        if not read_only:
            self.directory.mkdir(parents=True, exist_ok=True)
        self._sensitive = tuple(sorted(sensitive_values, key=len, reverse=True))

    def _path(self, kind: str, reference: str) -> Path:
        if str(UUID(reference)) != reference:
            raise EvidenceStorageError("invalid evidence reference")
        return self.directory / f"{kind}-{reference}.json"

    def _write(self, kind: str, reference: str, value: object) -> None:
        path = self._path(kind, reference)
        payload = _encode(value)
        text = payload.decode("utf-8")
        if any(
            json.dumps(secret, ensure_ascii=True)[1:-1] in text
            for secret in self._sensitive
        ):
            raise EvidenceStorageError("sensitive evidence metadata rejected")
        envelope = _encode(
            {"sha256": hashlib.sha256(payload).hexdigest(), "payload": value}
        )
        temporary = self.directory / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link creation is atomic and refuses to replace existing evidence.
            os.link(temporary, path)
        except FileExistsError:
            if self._read(kind, reference) != value:
                raise EvidenceStorageError("immutable evidence conflict") from None
        except OSError:
            raise EvidenceStorageError("evidence write failed") from None
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, kind: str, reference: str) -> dict | None:
        path = self._path(kind, reference)
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("oversized evidence")
            envelope = json.loads(raw)
            value = envelope["payload"]
            if (
                not isinstance(value, dict)
                or envelope["sha256"] != hashlib.sha256(_encode(value)).hexdigest()
            ):
                raise ValueError("invalid evidence checksum")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            raise EvidenceStorageError(
                "evidence read or integrity check failed"
            ) from None

    def begin(
        self, request: ToolRequest, authorization: ToolAuthorization, sandbox_id: str
    ) -> ToolResult | None:
        snapshot = {
            "request": asdict(request),
            "authorization": asdict(authorization),
            "sandbox_id": sandbox_id,
        }
        serialized = _encode(snapshot)
        fingerprint = hashlib.sha256(serialized).hexdigest()
        receipt = {"fingerprint": fingerprint, "snapshot": json.loads(serialized)}
        existing = self._read("receipt", request.request_id)
        if existing is not None:
            if existing != receipt:
                raise EvidenceStorageError(
                    "request_id conflicts with an earlier request"
                )
            finished = self._read("finished", request.request_id)
            if finished is None:
                raise ToolRecoveryRequired(
                    "request already started; explicit recovery required"
                )
            record = self._record(request.request_id)
            if record is None:
                raise EvidenceStorageError("finished request has no evidence")
            return ToolResult(
                record,
                FailureAction(finished["action"])
                if finished["action"] is not None
                else None,
            )
        # A separate exclusive claim prevents concurrent first dispatch even if
        # two callers race between checking and writing the receipt.
        claim = self.directory / f"claim-{request.request_id}"
        try:
            claim.mkdir()
        except FileExistsError:
            raise ToolRecoveryRequired("request is already claimed") from None
        self._write("receipt", request.request_id, receipt)
        return None

    def save_command(self, command_ref: str, command: MCPCommand) -> None:
        self._write(
            "command",
            command_ref,
            {
                "tool": command.tool,
                "arguments": list(command.arguments),
                "timeout_seconds": command.timeout_seconds,
            },
        )

    def load_command(self, command_ref: str) -> MCPCommand:
        value = self._read("command", command_ref)
        if value is None:
            raise EvidenceStorageError("command evidence is missing")
        return MCPCommand(
            value["tool"], tuple(value["arguments"]), value["timeout_seconds"]
        )

    def command_exists(self, command_ref: str) -> bool:
        return self._read("command", command_ref) is not None

    def _sanitize(self, value: str, truncated: bool) -> tuple[str, bool]:
        for secret in self._sensitive:
            value = value.replace(secret, "[REDACTED]")
        # If the upstream stream was truncated inside a secret, remove its
        # trailing prefix as well before writing any bytes.
        if truncated:
            for secret in self._sensitive:
                for length in range(min(len(secret) - 1, len(value)), 0, -1):
                    if value.endswith(secret[:length]):
                        value = value[:-length] + "[REDACTED]"
                        break
        encoded = value.encode("utf-8")
        return encoded[:65536].decode("utf-8", errors="ignore"), truncated or len(
            encoded
        ) > 65536

    def save_record(self, record: ExecutionRecord) -> ExecutionRecord:
        stdout, out_cut = self._sanitize(record.stdout, record.stdout_truncated)
        stderr, err_cut = self._sanitize(record.stderr, record.stderr_truncated)
        record = replace(
            record,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=out_cut,
            stderr_truncated=err_cut,
        )
        self._write("output", record.output_ref, asdict(record))
        return record

    def _record(self, output_ref: str) -> ExecutionRecord | None:
        value = self._read("output", output_ref)
        if value is None:
            return None
        value["failure_type"] = (
            FailureType(value["failure_type"])
            if value["failure_type"] is not None
            else None
        )
        return ExecutionRecord(**value)

    def output_exists(self, output_ref: str) -> bool:
        return self._record(output_ref) is not None

    def read_record(self, output_ref: str) -> ExecutionRecord | None:
        return self._record(output_ref)

    def read_request_receipt(self, request_id: str) -> StoredToolRequest | None:
        receipt = self._read("receipt", request_id)
        if receipt is None:
            return None
        try:
            snapshot = receipt["snapshot"]
            if receipt["fingerprint"] != hashlib.sha256(_encode(snapshot)).hexdigest():
                raise ValueError("request fingerprint mismatch")
            request = ToolRequest(**snapshot["request"])
            authorization_data = snapshot["authorization"]
            authorization = ToolAuthorization(
                authorization_id=authorization_data["authorization_id"],
                task_run_id=authorization_data["task_run_id"],
                allowed_paths=tuple(authorization_data["allowed_paths"]),
                allowed_tools=tuple(authorization_data["allowed_tools"]),
            )
            if request.request_id != request_id:
                raise ValueError("request ID mismatch")
            return StoredToolRequest(request, authorization, snapshot["sandbox_id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise EvidenceStorageError("invalid request receipt") from None

    def read_completed(self, request_id: str) -> ToolResult | None:
        finished = self._read("finished", request_id)
        if finished is None:
            return None
        try:
            record = self._record(request_id)
            if record is None or record.request_id != request_id:
                raise ValueError("finished request has no matching record")
            action = (
                FailureAction(finished["action"])
                if finished["action"] is not None
                else None
            )
            return ToolResult(record, action)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise EvidenceStorageError("invalid finished request") from None

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        record = self._record(execution_id)
        if record is None or record.execution_id != execution_id:
            return None
        # T3 records are intentionally not assertions about a verified target.
        return ExecutionEvidence(record.task_run_id, None, record.finished_at)

    def finish(self, result: ToolResult) -> None:
        self._write(
            "finished",
            result.record.request_id,
            {"action": result.action.value if result.action else None},
        )
