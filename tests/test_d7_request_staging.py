from __future__ import annotations

from uuid import uuid4

import pytest

from agent_adapters.v7.request_staging import (
    MAX_V3_REQUEST_ATTACHMENTS,
    D7RequestStager,
    RequestStagingError,
)


def _request_id():
    return str(uuid4())


def test_stage_is_complete_safe_and_idempotent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_bytes(b"fixture input")
    state = tmp_path / "state"
    stager = D7RequestStager(state, clock=lambda: 10.0)
    request_id = _request_id()

    first = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "inspect local evidence"},
    )
    replay = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "inspect local evidence"},
    )

    assert first.receipt.state == "staged"
    assert first.receipt.attachments[0].name == "evidence.txt"
    assert first.receipt.attachments[0].byte_size == len(b"fixture input")
    assert first.staged_directory is not None
    assert (first.staged_directory / "00.input").read_bytes() == b"fixture input"
    assert replay.replay is True
    assert replay.receipt.request_sha256 == first.receipt.request_sha256
    assert replay.staged_directory == first.staged_directory
    assert "source" not in str(
        (state / "requests" / request_id / "receipt.json").read_bytes()
    )


def test_changed_content_with_same_request_id_records_conflict_and_keeps_original(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    attachment = source / "evidence.txt"
    attachment.write_bytes(b"first")
    stager = D7RequestStager(tmp_path / "state", clock=lambda: 11.0)
    request_id = _request_id()
    initial = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "inspect"},
    )

    attachment.write_bytes(b"changed")
    conflict = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "inspect"},
    )

    assert conflict.conflict is True
    assert conflict.receipt.request_sha256 == initial.receipt.request_sha256
    assert conflict.receipt.conflict_count == 1
    assert initial.staged_directory is not None
    assert (initial.staged_directory / "00.input").read_bytes() == b"first"


def test_atomic_stage_failure_leaves_only_error_receipt(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_bytes(b"first")
    stager = D7RequestStager(tmp_path / "state", clock=lambda: 12.0)
    request_id = _request_id()

    result = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("first.txt", "missing.txt"),
        request_identity={"goal": "inspect"},
    )

    assert result.receipt.state == "input_staging_error"
    assert result.receipt.staging_status == "failed"
    assert result.receipt.reason == "attachment_source_unavailable"
    assert result.staged_directory is None
    request_dir = tmp_path / "state" / "requests" / request_id
    assert not (request_dir / "attachments").exists()
    assert not tuple(request_dir.glob(".stage-*"))


def test_rejected_request_keeps_metadata_after_staged_bytes_are_removed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "too-large-for-d3.txt").write_bytes(b"not large in the staging adapter")
    stager = D7RequestStager(tmp_path / "state", clock=lambda: 13.0)
    request_id = _request_id()
    staged = stager.stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("too-large-for-d3.txt",),
        request_identity={"goal": "inspect"},
    )

    receipt = stager.record_outcome(
        request_id,
        state="capability_not_supported",
        reason="attachment_too_large_for_d3",
    )
    cleanup = stager.remove_rejected_payload(request_id)
    stager.record_outcome(
        request_id,
        state="capability_not_supported",
        reason=receipt.reason,
        staging_status=cleanup,
    )
    queried = stager.read_receipt(request_id)

    assert staged.staged_directory is not None
    assert cleanup == "removed"
    assert queried is not None
    assert queried.state == "capability_not_supported"
    assert queried.staging_status == "removed"
    assert queried.request_exit_code == 2
    assert queried.attachments[0].name == "too-large-for-d3.txt"
    assert not (staged.staged_directory / "00.input").exists()


def test_zero_and_sixteen_attachment_request_boundaries(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    names = []
    for index in range(MAX_V3_REQUEST_ATTACHMENTS):
        name = f"input-{index:02d}.txt"
        (source / name).write_bytes(b"")
        names.append(name)
    stager = D7RequestStager(tmp_path / "state", clock=lambda: 14.0)

    empty = stager.stage(
        request_id=_request_id(),
        source_root=str(source.resolve()),
        attachment_paths=(),
        request_identity={"goal": "no input"},
    )
    full = stager.stage(
        request_id=_request_id(),
        source_root=str(source.resolve()),
        attachment_paths=tuple(names),
        request_identity={"goal": "many inputs"},
    )

    assert empty.receipt.attachments == ()
    assert len(full.receipt.attachments) == MAX_V3_REQUEST_ATTACHMENTS
    assert sum(item.byte_size for item in full.receipt.attachments) == 0
    with pytest.raises(RequestStagingError, match="attachment_count_invalid"):
        stager.stage(
            request_id=_request_id(),
            source_root=str(source.resolve()),
            attachment_paths=tuple(names) + (names[0],),
            request_identity={"goal": "too many"},
        )


def test_read_only_request_query_does_not_create_request_directory(tmp_path):
    state_dir = tmp_path / "state"
    request_id = _request_id()
    stager = D7RequestStager.open_existing(state_dir, clock=lambda: 15.0)

    assert stager.read_receipt(request_id) is None
    assert not state_dir.exists()


def test_existing_request_query_does_not_change_receipt_or_payload(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_bytes(b"receipt query")
    state = tmp_path / "state"
    request_id = _request_id()
    staged = D7RequestStager(state, clock=lambda: 16.0).stage(
        request_id=request_id,
        source_root=str(source.resolve()),
        attachment_paths=("evidence.txt",),
        request_identity={"goal": "inspect"},
    )
    receipt_path = state / "requests" / request_id / "receipt.json"
    before = receipt_path.read_bytes()

    readonly = D7RequestStager.open_existing(state, clock=lambda: 17.0)
    queried = readonly.read_receipt(request_id)

    assert queried == staged.receipt
    assert receipt_path.read_bytes() == before
    assert staged.staged_directory is not None
    assert (staged.staged_directory / "00.input").read_bytes() == b"receipt query"
