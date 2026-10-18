from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from agent_adapters.file_type_proof_recorder import (
    FileTypeProofRecorder,
    FileTypeProofStorageError,
)
from agent_core.file_type_proof import FileTypeVerificationProof


def proof() -> FileTypeVerificationProof:
    return FileTypeVerificationProof(
        proof_id=str(uuid4()),
        task_run_id=str(uuid4()),
        intent_id=str(uuid4()),
        execution_id=str(uuid4()),
        import_id=str(uuid4()),
        path="/workspace/input/sample.txt",
        local_path="sample.txt",
        observed_type="ASCII text",
        verified_at=10.0,
    )


@pytest.fixture
def proof_directory() -> Path:
    runtime = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    directory = runtime / f"file-type-proof-{uuid4()}"
    yield directory
    if directory.exists():
        assert directory.resolve().is_relative_to(runtime.resolve())
        shutil.rmtree(directory)


def test_proof_is_immutable_and_replayable(proof_directory: Path) -> None:
    recorder = FileTypeProofRecorder(proof_directory)
    original = proof()
    assert recorder.read(original.proof_id) is None
    recorder.save(original)
    recorder.save(original)
    assert recorder.read(original.proof_id) == original

    with pytest.raises(FileTypeProofStorageError, match="immutable proof conflict"):
        recorder.save(replace(original, observed_type="Python source"))
    assert recorder.read(original.proof_id) == original


def test_proof_corruption_never_becomes_missing_evidence(proof_directory: Path) -> None:
    recorder = FileTypeProofRecorder(proof_directory)
    original = proof()
    recorder.save(original)
    stored = recorder.directory / f"proof-{original.proof_id}.json"
    stored.write_text('{"payload":{},"sha256":"invalid"}', encoding="utf-8")
    with pytest.raises(FileTypeProofStorageError, match="integrity check failed"):
        recorder.read(original.proof_id)


def test_proof_rejects_invalid_identity_and_observation() -> None:
    original = proof()
    with pytest.raises(ValueError, match="proof_id"):
        replace(original, proof_id="not-a-uuid")
    with pytest.raises(ValueError, match="observed_type"):
        replace(original, observed_type="ASCII\ntext")
