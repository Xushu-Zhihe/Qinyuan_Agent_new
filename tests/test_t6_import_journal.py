"""Unknown imports require explicit read-only recovery, never a second write."""

import hashlib
from uuid import uuid4

import pytest

from agent_adapters.attachment_import_journal import (
    T6AttachmentImportService,
    T6ImportClaim,
    T6ImportConflict,
    T6ImportJournal,
    T6ImportRecoveryRequired,
)
from Sandbox.command_attachment import T6Attachment, T6AttachmentReadback


class FakeImporter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.import_calls = 0
        self.readback_calls = 0

    def import_one(self, _sandbox_id, attachment, _timeout, _token):
        self.import_calls += 1
        if self.fail:
            raise RuntimeError("unknown external outcome")
        return T6AttachmentReadback(attachment.byte_size, attachment.sha256)

    def readback(self, _sandbox_id, attachment, _timeout, _token):
        self.readback_calls += 1
        return T6AttachmentReadback(attachment.byte_size, attachment.sha256)


def test_completed_import_replays_without_external_call(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"sample")
    item = T6Attachment("sample.bin", source, 6, hashlib.sha256(b"sample").hexdigest())
    import_id, task_id = str(uuid4()), str(uuid4())
    importer = FakeImporter()
    journal_path = tmp_path / "imports.sqlite3"
    service = T6AttachmentImportService(importer, T6ImportJournal(journal_path))
    expected = T6AttachmentReadback(6, item.sha256)
    assert service.import_one(import_id, task_id, "sandbox", item, 10, None) == expected
    source.unlink()
    restarted = T6AttachmentImportService(importer, T6ImportJournal(journal_path))
    assert (
        restarted.import_one(import_id, task_id, "sandbox", item, 10, None) == expected
    )
    assert importer.import_calls == 1


def test_work_copy_uses_verified_input_without_host_source(tmp_path):
    missing = tmp_path / "no-longer-present"
    item = T6Attachment("sample.bin", missing, 6, hashlib.sha256(b"sample").hexdigest())
    importer = FakeImporter()
    service = T6AttachmentImportService(
        importer, T6ImportJournal(tmp_path / "copies.sqlite3"),
        source_required=False,
    )
    result = service.import_one(str(uuid4()), str(uuid4()), "sandbox", item, 10, None)
    assert result == T6AttachmentReadback(6, item.sha256)
    assert importer.import_calls == 1


def test_unknown_import_requires_explicit_readback(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"sample")
    item = T6Attachment("sample.bin", source, 6, hashlib.sha256(b"sample").hexdigest())
    import_id, task_id = str(uuid4()), str(uuid4())
    journal_path = tmp_path / "imports.sqlite3"
    importer = FakeImporter(fail=True)
    service = T6AttachmentImportService(importer, T6ImportJournal(journal_path))
    with pytest.raises(RuntimeError, match="unknown external outcome"):
        service.import_one(import_id, task_id, "sandbox", item, 10, None)
    assert service.journal.get(import_id)[1] == "claimed"

    restarted = T6AttachmentImportService(importer, T6ImportJournal(journal_path))
    with pytest.raises(T6ImportRecoveryRequired, match="import_outcome_unknown"):
        restarted.import_one(import_id, task_id, "sandbox", item, 10, None)
    assert importer.import_calls == 1
    claim = T6ImportClaim.for_attachment(import_id, task_id, "sandbox", item)
    assert restarted.recover_one(claim, 10, None) == T6AttachmentReadback(
        6, item.sha256
    )
    assert importer.import_calls == 1 and importer.readback_calls == 1
    assert restarted.journal.get(import_id)[1] == "verified"


def test_path_claim_never_overwrites_prior_import(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x")
    item = T6Attachment("same.bin", source, 1, hashlib.sha256(b"x").hexdigest())
    importer = FakeImporter()
    service = T6AttachmentImportService(
        importer, T6ImportJournal(tmp_path / "imports.sqlite3")
    )
    task_id = str(uuid4())
    service.import_one(str(uuid4()), task_id, "sandbox", item, 10, None)
    with pytest.raises(T6ImportConflict, match="sandbox_path_claimed"):
        service.import_one(str(uuid4()), task_id, "sandbox", item, 10, None)
    assert importer.import_calls == 1
