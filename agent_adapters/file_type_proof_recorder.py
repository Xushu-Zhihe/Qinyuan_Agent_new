"""Checksummed, non-overwriting local files for T4 verification proofs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from agent_core.file_type_proof import FileTypeVerificationProof


class FileTypeProofStorageError(RuntimeError):
    pass


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


class FileTypeProofRecorder:
    def __init__(self, directory: Path, *, read_only: bool = False) -> None:
        self.directory = directory.resolve()
        if not read_only:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, proof_id: str) -> Path:
        try:
            parsed = UUID(proof_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise FileTypeProofStorageError("invalid proof ID") from exc
        if parsed.version != 4 or str(parsed) != proof_id:
            raise FileTypeProofStorageError("invalid proof ID")
        return self.directory / f"proof-{proof_id}.json"

    def read(self, proof_id: str) -> FileTypeVerificationProof | None:
        path = self._path(proof_id)
        try:
            with path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("oversized proof")
            envelope = json.loads(raw)
            value = envelope["payload"]
            if (
                not isinstance(value, dict)
                or envelope["sha256"] != hashlib.sha256(_encode(value)).hexdigest()
            ):
                raise ValueError("invalid proof checksum")
            proof = FileTypeVerificationProof(**value)
            if proof.proof_id != proof_id:
                raise ValueError("proof ID mismatch")
            return proof
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            raise FileTypeProofStorageError(
                "proof read or integrity check failed"
            ) from None

    def save(self, proof: FileTypeVerificationProof) -> None:
        if not isinstance(proof, FileTypeVerificationProof):
            raise TypeError("proof must be a FileTypeVerificationProof")
        path = self._path(proof.proof_id)
        payload = asdict(proof)
        envelope = _encode(
            {"sha256": hashlib.sha256(_encode(payload)).hexdigest(), "payload": payload}
        )
        temporary = self.directory / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if self.read(proof.proof_id) != proof:
                raise FileTypeProofStorageError("immutable proof conflict") from None
        except OSError:
            raise FileTypeProofStorageError("proof write failed") from None
        finally:
            temporary.unlink(missing_ok=True)
