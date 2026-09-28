"""Durable, non-overwriting attachment claims and verified receipts."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from Sandbox.command_attachment import T6Attachment, T6AttachmentReadback


class T6ImportRecoveryRequired(RuntimeError):
    pass


class T6ImportConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class T6ImportClaim:
    import_id: str
    task_run_id: str
    sandbox_id: str
    name: str
    byte_size: int
    sha256: str

    @classmethod
    def for_attachment(
        cls, import_id: str, task_run_id: str, sandbox_id: str, item: T6Attachment
    ) -> T6ImportClaim:
        for value in (import_id, task_run_id):
            try:
                parsed = UUID(value)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("invalid_import_identity") from exc
            if parsed.version != 4 or str(parsed) != value:
                raise ValueError("invalid_import_identity")
        if not sandbox_id:
            raise ValueError("invalid_sandbox_identity")
        return cls(
            import_id, task_run_id, sandbox_id, item.name, item.byte_size, item.sha256
        )


class T6ImportJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS imports (
                    import_id TEXT PRIMARY KEY,
                    task_run_id TEXT NOT NULL,
                    sandbox_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('claimed','verified')),
                    UNIQUE(sandbox_id, name)
                )"""
            )

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, import_id: str):
        row = db.execute(
            "SELECT task_run_id,sandbox_id,name,byte_size,sha256,status FROM imports WHERE import_id=?",
            (import_id,),
        ).fetchone()
        if row is None:
            return None
        return T6ImportClaim(import_id, *row[:5]), row[5]

    def begin(self, claim: T6ImportClaim) -> bool:
        """Commit the claim before Docker work; True means already verified."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._read(db, claim.import_id)
            if previous is not None:
                old, status = previous
                if old != claim:
                    raise T6ImportConflict("import_id_conflict")
                if status != "verified":
                    raise T6ImportRecoveryRequired("import_outcome_unknown")
                return True
            try:
                db.execute(
                    "INSERT INTO imports VALUES(?,?,?,?,?,?,?)",
                    (
                        claim.import_id,
                        claim.task_run_id,
                        claim.sandbox_id,
                        claim.name,
                        claim.byte_size,
                        claim.sha256,
                        "claimed",
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise T6ImportConflict("sandbox_path_claimed") from exc
        return False

    def get(self, import_id: str) -> tuple[T6ImportClaim, str] | None:
        with self._connection() as db:
            return self._read(db, import_id)

    def complete(self, claim: T6ImportClaim, readback: T6AttachmentReadback) -> None:
        if (readback.byte_size, readback.sha256) != (claim.byte_size, claim.sha256):
            raise T6ImportRecoveryRequired("import_readback_mismatch")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._read(db, claim.import_id)
            if previous is None or previous[0] != claim:
                raise T6ImportConflict("import_claim_missing_or_changed")
            if previous[1] == "verified":
                return
            db.execute(
                "UPDATE imports SET status='verified' WHERE import_id=? AND status='claimed'",
                (claim.import_id,),
            )


class T6AttachmentImportService:
    def __init__(
        self, importer, journal: T6ImportJournal, *, source_required: bool = True,
    ) -> None:
        self.importer = importer
        self.journal = journal
        self.source_required = source_required

    def import_one(
        self,
        import_id: str,
        task_run_id: str,
        sandbox_id: str,
        attachment: T6Attachment,
        timeout_seconds: float,
        cancellation_token,
    ) -> T6AttachmentReadback:
        from Sandbox.command_attachment import validate_attachment_batch

        claim = T6ImportClaim.for_attachment(
            import_id, task_run_id, sandbox_id, attachment
        )
        previous = self.journal.get(import_id)
        if previous is not None:
            if previous[0] != claim:
                raise T6ImportConflict("import_id_conflict")
            if previous[1] != "verified":
                raise T6ImportRecoveryRequired("import_outcome_unknown")
            return T6AttachmentReadback(claim.byte_size, claim.sha256)
        validate_attachment_batch((attachment,), check_source=self.source_required)
        if self.journal.begin(claim):
            return T6AttachmentReadback(claim.byte_size, claim.sha256)
        readback = self.importer.import_one(
            sandbox_id, attachment, timeout_seconds, cancellation_token
        )
        self.journal.complete(claim, readback)
        return readback

    def import_batch(
        self,
        import_ids: tuple[str, ...],
        task_run_id: str,
        sandbox_id: str,
        attachments: tuple[T6Attachment, ...],
        timeout_seconds: float,
        cancellation_token,
    ) -> tuple[T6AttachmentReadback, ...]:
        from Sandbox.command_attachment import validate_attachment_batch

        validate_attachment_batch(attachments)
        if len(import_ids) != len(attachments) or len(set(import_ids)) != len(
            import_ids
        ):
            raise ValueError("invalid_import_ids")
        return tuple(
            self.import_one(
                import_id,
                task_run_id,
                sandbox_id,
                attachment,
                timeout_seconds,
                cancellation_token,
            )
            for import_id, attachment in zip(import_ids, attachments)
        )

    def recover_one(
        self,
        claim: T6ImportClaim,
        timeout_seconds: float,
        cancellation_token,
    ) -> T6AttachmentReadback:
        """Explicit read-only reconciliation; never resend an import."""
        previous = self.journal.get(claim.import_id)
        if previous is None or previous[0] != claim:
            raise T6ImportConflict("import_claim_missing_or_changed")
        readback = self.importer.readback(
            claim.sandbox_id,
            T6Attachment(claim.name, Path(), claim.byte_size, claim.sha256),
            timeout_seconds,
            cancellation_token,
        )
        self.journal.complete(claim, readback)
        return readback
