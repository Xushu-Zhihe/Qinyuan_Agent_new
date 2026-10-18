"""T6 input admission remains bounded independently of workbench uploads."""

import hashlib
from pathlib import Path

import pytest

from Sandbox.command_attachment import (
    MAX_T6_INPUT_BYTES,
    T6Attachment,
    validate_attachment_batch,
)


def attachment(name: str, source: Path, size: int) -> T6Attachment:
    return T6Attachment(name, source, size, hashlib.sha256(b"").hexdigest())


def test_batch_accepts_multiple_files_and_exact_capacity(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"")
    with second.open("wb") as output:
        output.truncate(MAX_T6_INPUT_BYTES)
    validate_attachment_batch(
        (
            attachment("first", first, 0),
            attachment("second", second, MAX_T6_INPUT_BYTES),
        )
    )


def test_batch_rejects_aggregate_over_capacity_before_import(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    with first.open("wb") as output:
        output.truncate(MAX_T6_INPUT_BYTES)
    second.write_bytes(b"x")
    with pytest.raises(ValueError, match="attachment_run_capacity_exceeded"):
        validate_attachment_batch(
            (
                attachment("first", first, MAX_T6_INPUT_BYTES),
                attachment("second", second, 1),
            )
        )


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "\x00"])
def test_batch_rejects_unsafe_names(tmp_path, name):
    source = tmp_path / "source"
    source.write_bytes(b"")
    with pytest.raises(ValueError, match="invalid_attachment_name"):
        validate_attachment_batch((attachment(name, source, 0),))


def test_batch_rejects_duplicate_names_and_changed_file(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x")
    one = attachment("same", source, 1)
    with pytest.raises(ValueError, match="invalid_attachment_name"):
        validate_attachment_batch((one, one))
    with pytest.raises(ValueError, match="attachment_source_size_changed"):
        validate_attachment_batch((attachment("different", source, 0),))
