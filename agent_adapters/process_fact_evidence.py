"""Immutable evidence ledger for D5 refinement requests and outcomes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from agent_core.process_facts import (
    D5_OUTCOMES,
    D5Candidate,
    D5RefinementRequest,
    d5_json_value,
)

_MAX_RECORD_BYTES = 16 * 1024 * 1024
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class D5EvidenceError(RuntimeError):
    """D5 evidence is unavailable, invalid, or conflicts with a saved record."""


class FileD5Evidence:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.directory = Path(directory).resolve()
        self._writable = create
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, reference: str) -> Path:
        try:
            parsed = UUID(reference)
        except (TypeError, ValueError, AttributeError) as exc:
            raise D5EvidenceError("invalid_evidence_reference") from exc
        if parsed.version not in {4, 5} or str(parsed) != reference:
            raise D5EvidenceError("invalid_evidence_reference")
        if kind not in {"request", "candidate", "result"}:
            raise D5EvidenceError("invalid_evidence_kind")
        return self.directory / f"{kind}-{reference}.json"

    def _write(self, kind: str, reference: str, value: object) -> None:
        if not self._writable:
            raise D5EvidenceError("evidence_store_read_only")
        payload = d5_json_value(value)
        encoded = _encode(payload)
        envelope = _encode(
            {"sha256": hashlib.sha256(encoded).hexdigest(), "payload": payload}
        )
        destination = self._path(kind, reference)
        temporary = self.directory / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        except FileExistsError:
            if self._read(kind, reference) != payload:
                raise D5EvidenceError("immutable_evidence_conflict") from None
        except OSError as exc:
            raise D5EvidenceError("evidence_write_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, kind: str, reference: str) -> dict[str, object] | None:
        path = self._path(kind, reference)
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_RECORD_BYTES + 1)
            if len(raw) > _MAX_RECORD_BYTES:
                raise ValueError("oversized evidence")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or set(envelope) != {"sha256", "payload"}:
                raise ValueError("invalid evidence envelope")
            payload = envelope["payload"]
            if (
                not isinstance(payload, dict)
                or envelope["sha256"] != hashlib.sha256(_encode(payload)).hexdigest()
            ):
                raise ValueError("invalid evidence checksum")
            return payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise D5EvidenceError("evidence_read_failed") from exc

    def save_request(self, request: D5RefinementRequest) -> None:
        if not isinstance(request, D5RefinementRequest):
            raise TypeError("request must be a D5RefinementRequest")
        self._write("request", request.attempt.model_request_id, asdict(request))

    def read_request(self, model_request_id: str) -> D5RefinementRequest | None:
        payload = self._read("request", model_request_id)
        if payload is None:
            return None
        try:
            request = D5RefinementRequest.from_dict(payload)
            if request.attempt.model_request_id != model_request_id:
                raise ValueError("request owner mismatch")
            return request
        except (TypeError, ValueError, KeyError) as exc:
            raise D5EvidenceError("invalid_saved_request") from exc

    def save_candidate(self, model_request_id: str, candidate: D5Candidate) -> None:
        self._require_request(model_request_id)
        if not isinstance(candidate, D5Candidate):
            raise TypeError("candidate must be a D5Candidate")
        self._write("candidate", model_request_id, asdict(candidate))

    def read_candidate(self, model_request_id: str) -> D5Candidate | None:
        payload = self._read("candidate", model_request_id)
        if payload is None:
            return None
        try:
            candidate = D5Candidate.from_dict(payload)
            self._require_request(model_request_id)
            return candidate
        except (TypeError, ValueError, KeyError) as exc:
            raise D5EvidenceError("invalid_saved_candidate") from exc

    def save_result(
        self,
        model_request_id: str,
        outcome: str,
        finished_at: float,
        reason: str | None,
    ) -> None:
        self._require_request(model_request_id)
        if not isinstance(outcome, str) or outcome not in D5_OUTCOMES:
            raise ValueError("unsupported D5 result outcome")
        if isinstance(finished_at, bool) or not isinstance(finished_at, (int, float)):
            raise TypeError("finished_at must be numeric")
        if not math.isfinite(finished_at):
            raise ValueError("finished_at must be a finite timestamp")
        if reason is not None and (
            not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None
        ):
            raise ValueError("reason must be a fixed code")
        if outcome == "accepted" and self.read_candidate(model_request_id) is None:
            raise D5EvidenceError("accepted_result_requires_candidate")
        self._write(
            "result",
            model_request_id,
            {
                "model_request_id": model_request_id,
                "outcome": outcome,
                "finished_at": finished_at,
                "reason": reason,
            },
        )

    def read_result(self, model_request_id: str) -> dict[str, object] | None:
        payload = self._read("result", model_request_id)
        if payload is None:
            return None
        if set(payload) != {"model_request_id", "outcome", "finished_at", "reason"}:
            raise D5EvidenceError("invalid_saved_result")
        try:
            if payload["model_request_id"] != model_request_id:
                raise ValueError("result owner mismatch")
            outcome = payload["outcome"]
            if not isinstance(outcome, str) or outcome not in D5_OUTCOMES:
                raise ValueError("invalid outcome")
            finished_at = payload["finished_at"]
            if (
                isinstance(finished_at, bool)
                or not isinstance(finished_at, (int, float))
                or not math.isfinite(finished_at)
            ):
                raise ValueError("invalid finish timestamp")
            reason = payload["reason"]
            if reason is not None and (
                not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None
            ):
                raise ValueError("invalid result reason")
            return payload
        except (TypeError, ValueError) as exc:
            raise D5EvidenceError("invalid_saved_result") from exc

    def _require_request(self, model_request_id: str) -> D5RefinementRequest:
        request = self.read_request(model_request_id)
        if request is None:
            raise D5EvidenceError("request_evidence_missing")
        return request


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
