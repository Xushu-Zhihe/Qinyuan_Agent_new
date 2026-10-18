from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agent_adapters.cross_run_artifacts import (
    CrossTaskRunArtifactStore,
    D6ArtifactImportRecoveryRequired,
)
from agent_core.cross_run_artifact_contracts import (
    D6ArtifactImportRequest,
    D6ArtifactReference,
)
from agent_core.sensitive_evidence_contracts import (
    D6SensitiveEvidenceMetadata,
)


def test_d6_artifact_import_copies_exact_snapshot_once(tmp_path: Path) -> None:
    source_run, target_run = str(uuid4()), str(uuid4())
    version = str(uuid5(NAMESPACE_URL, "d6-version"))
    content = b"non-sensitive artifact"
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store = CrossTaskRunArtifactStore(tmp_path / "runs")
    snapshot = store.export_snapshot(
        source_task_run_id=source_run,
        object_version_id=version,
        source_path=source,
        expected_sha256=sha,
        created_at=1.0,
    )
    request = D6ArtifactImportRequest(
        str(uuid4()), D6ArtifactReference(source_run, version, sha), target_run, True
    )
    prepared = store.prepare_import(
        request=request, snapshot=snapshot, target_path=None, existing_import_count=0
    )
    confirmed = store.copy_and_confirm(prepared)
    assert confirmed.status == "confirmed"
    assert Path(confirmed.target_path).read_bytes() == content
    assert store.prepare_import(
        request=request, snapshot=snapshot, target_path=None, existing_import_count=0
    ).status == "confirmed"


def test_d6_snapshot_is_independent_after_source_file_changes(tmp_path: Path) -> None:
    source_run, target_run = str(uuid4()), str(uuid4())
    content = b"x"
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    version = str(uuid5(NAMESPACE_URL, "unknown"))
    store = CrossTaskRunArtifactStore(tmp_path / "runs")
    snapshot = store.export_snapshot(
        source_task_run_id=source_run, object_version_id=version,
        source_path=source, expected_sha256=sha, created_at=1.0,
    )
    request = D6ArtifactImportRequest(
        str(uuid4()), D6ArtifactReference(source_run, version, sha), target_run, True
    )
    prepared = store.prepare_import(
        request=request, snapshot=snapshot, target_path=None, existing_import_count=0
    )
    source.unlink()
    confirmed = store.copy_and_confirm(prepared)
    assert confirmed.status == "confirmed"
    assert Path(confirmed.target_path).read_bytes() == content


def test_d6_unknown_import_is_inspected_without_redispatch(tmp_path: Path, monkeypatch) -> None:
    import agent_adapters.cross_run_artifacts as module

    source_run, target_run = str(uuid4()), str(uuid4())
    content = b"copy whose result becomes uncertain"
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    version = str(uuid5(NAMESPACE_URL, "unknown"))
    store = CrossTaskRunArtifactStore(tmp_path / "runs")
    snapshot = store.export_snapshot(
        source_task_run_id=source_run, object_version_id=version,
        source_path=source, expected_sha256=sha, created_at=1.0,
    )
    request = D6ArtifactImportRequest(
        str(uuid4()), D6ArtifactReference(source_run, version, sha), target_run, True
    )
    prepared = store.prepare_import(
        request=request, snapshot=snapshot, target_path=None, existing_import_count=0
    )
    real_link = module.os.link
    calls = 0

    def link_then_lose_result(source_path, target_path):
        nonlocal calls
        calls += 1
        real_link(source_path, target_path)
        raise TimeoutError("copy result was lost")

    monkeypatch.setattr(module.os, "link", link_then_lose_result)
    with pytest.raises(D6ArtifactImportRecoveryRequired):
        store.copy_and_confirm(prepared)
    inspection = store.inspect_import(request)
    assert inspection.manifest.status == "unknown"
    assert inspection.target_status == "matching"
    with pytest.raises(D6ArtifactImportRecoveryRequired):
        store.copy_and_confirm(prepared)
    assert calls == 1


def test_d6_contracts_reject_sensitive_import_and_unlisted_kind() -> None:
    run_id, target_id = str(uuid4()), str(uuid4())
    version = str(uuid5(NAMESPACE_URL, "contract"))
    sha = "0" * 64
    with pytest.raises(ValueError, match="d6_sensitive_import_forbidden"):
        D6ArtifactImportRequest(
            str(uuid4()), D6ArtifactReference(run_id, version, sha), target_id, False
        )
    with pytest.raises(ValueError, match="kind_invalid"):
        D6SensitiveEvidenceMetadata(
            str(uuid4()), run_id, "arbitrary", sha, 1, 1.0, "available"
        )

