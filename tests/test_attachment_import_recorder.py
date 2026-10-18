from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_core.attachment_import import (
    AttachmentImportClaim,
    AttachmentImportConflict,
    AttachmentImportRecoveryRequired,
    AttachmentImportReceipt,
    AttachmentImportStorageError,
)


def claim() -> AttachmentImportClaim:
    return AttachmentImportClaim(
        import_id=str(uuid4()),
        task_run_id=str(uuid4()),
        sandbox_id="sandbox-1",
        attachment_index=0,
        local_path="sample.txt",
        source_root="C:/source",
        sandbox_path="/workspace/input/sample.txt",
        byte_size=4,
        sha256="0" * 64,
        claimed_at=100.0,
    )


def receipt(request: AttachmentImportClaim) -> AttachmentImportReceipt:
    return AttachmentImportReceipt(
        **{name: getattr(request, name) for name in request.__dataclass_fields__},
        completed_at=101.0,
    )


def test_import_evidence_survives_reopen_and_exact_replay(tmp_path) -> None:
    request = claim()
    completed = receipt(request)
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    assert recorder.begin(request) is None
    recorder.save_receipt(completed)
    recorder.save_receipt(completed)
    assert recorder.begin(request) == completed

    reopened = FileAttachmentImportRecorder(tmp_path / "imports")
    assert reopened.read_claim(request.import_id) == request
    assert reopened.read_receipt(request.import_id) == completed


def test_import_evidence_is_immutable(tmp_path) -> None:
    request = claim()
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    assert recorder.begin(request) is None
    with pytest.raises(AttachmentImportConflict):
        recorder.begin(replace(request, sandbox_path="/workspace/input/other.txt"))

    completed = receipt(request)
    recorder.save_receipt(completed)
    with pytest.raises(AttachmentImportConflict):
        recorder.save_receipt(replace(completed, completed_at=102.0))


def test_import_receipt_requires_its_original_claim(tmp_path) -> None:
    request = claim()
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    with pytest.raises(AttachmentImportStorageError):
        recorder.save_receipt(receipt(request))

    assert recorder.begin(request) is None
    with pytest.raises(AttachmentImportStorageError):
        recorder.save_receipt(receipt(replace(request, byte_size=5)))
    assert recorder.read_receipt(request.import_id) is None


def test_corrupted_claim_or_receipt_is_not_treated_as_missing(tmp_path) -> None:
    request = claim()
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    assert recorder.begin(request) is None
    completed = receipt(request)
    recorder.save_receipt(completed)
    (recorder.directory / f"success-{request.import_id}.json").write_text(
        '{"sha256":"invalid","payload":{}}', encoding="utf-8"
    )
    with pytest.raises(AttachmentImportStorageError):
        recorder.read_receipt(request.import_id)

    (recorder.directory / f"claim-{request.import_id}.json").write_text(
        '{"sha256":"invalid","payload":{}}', encoding="utf-8"
    )
    with pytest.raises(AttachmentImportStorageError):
        recorder.read_claim(request.import_id)


def test_unknown_claim_blocks_replay_and_other_import_ids(tmp_path) -> None:
    request = claim()
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    assert recorder.begin(request) is None
    reopened = FileAttachmentImportRecorder(tmp_path / "imports")
    with pytest.raises(AttachmentImportRecoveryRequired):
        reopened.begin(replace(request, claimed_at=102.0))
    with pytest.raises(AttachmentImportConflict):
        reopened.begin(replace(request, import_id=str(uuid4())))
    with pytest.raises(AttachmentImportConflict):
        reopened.begin(
            replace(
                request,
                import_id=str(uuid4()),
                attachment_index=1,
                local_path="other.txt",
            )
        )
    assert len(list(recorder.directory.glob("claim-*.json"))) == 1


def test_concurrent_recorders_cannot_claim_one_attachment_twice(tmp_path) -> None:
    request = claim()
    second = replace(request, import_id=str(uuid4()))
    directory = tmp_path / "imports"

    def reserve(candidate: AttachmentImportClaim) -> str:
        try:
            FileAttachmentImportRecorder(directory).begin(candidate)
        except AttachmentImportConflict:
            return "conflict"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (request, second)))
    assert sorted(outcomes) == ["claimed", "conflict"]


def test_receipt_must_preserve_claim_time(tmp_path) -> None:
    request = claim()
    recorder = FileAttachmentImportRecorder(tmp_path / "imports")
    assert recorder.begin(request) is None
    with pytest.raises(AttachmentImportStorageError):
        recorder.save_receipt(replace(receipt(request), claimed_at=100.5))
