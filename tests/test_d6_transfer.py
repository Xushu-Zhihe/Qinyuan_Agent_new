from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token, _application
from test_d5_chain import RefinementModel

from agent_adapters.cross_run_artifacts import (
    CrossTaskRunArtifactStore,
    D6ArtifactImportError,
    D6ArtifactImportRecoveryRequired,
)
from agent_adapters.cross_run_transfer import D6ArtifactTransfer
from agent_adapters.multi_turn_application import D3Application
from agent_core.contracts import TaskRequest
from agent_core.d4_contracts import D4BudgetPolicy
from agent_core.d6_contracts import D6ArtifactImportRequest, D6ArtifactReference
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.execution_authorization import D6Authorization
from agent_core.turn_contracts import D1RoleConfig
from agent_core.turn_modeling import digest


class BoundedArtifactReader:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.calls: list[tuple[str, str, int]] = []
        self.fail = False

    async def read_artifact_bytes(self, sandbox_id, path, max_bytes, timeout_seconds, token):
        self.calls.append((sandbox_id, path, max_bytes))
        assert not token.is_cancelled
        assert max_bytes == 8 * 1024 * 1024
        assert timeout_seconds == 60.0
        if self.fail:
            raise TimeoutError("source read outcome lost")
        content = self.sandbox.files[path]
        if len(content) > max_bytes:
            raise ValueError("reader limit exceeded")
        return content


def make_completed_artifact(tmp_path: Path):
    source_directory = tmp_path / "source-run"
    input_directory = tmp_path / "inputs"
    input_directory.mkdir()
    content = b"source artifact bytes"
    (input_directory / "artifact.txt").write_bytes(content)
    config = D3RunConfig(
        D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1"),
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D3Authorization(str(uuid4()), (0,)),
        D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1").role_config_ref,
    )
    sandbox, clock, token = Sandbox(), Clock(), Token()
    outer, inner = Model(config.outer_role), Model(config.inner_role)
    review = ReviewModel(config.outer_role)
    case = [None, sandbox, outer, inner, review, clock, token]
    app = _application(source_directory, config, case)
    app.start(
        TaskRequest.from_dict({
            "goal": "verify an explicitly selected local artifact",
            "target": {"attachments": [{"kind": "local_path", "path": "artifact.txt"}]},
        }),
        input_directory,
    )
    result = asyncio.run(app.run(token))
    assert result.stop_reason == "turn_completed_sandbox_retained"
    receipts = app.workspace_evidence.list_artifacts(result.task_run_id)
    assert len(receipts) == 1 and receipts[0].status == "confirmed"
    return app, sandbox, token, receipts[0]


def close_source(app, sandbox) -> None:
    if app.state.task_run.status.value == "running":
        app.controller.cancel(str(uuid4()))
    app.controller.cleanup_sandbox()
    assert sandbox.destroyed
    app.close()


def test_readonly_source_snapshot_and_independent_target_copy(tmp_path: Path) -> None:
    app, sandbox, token, receipt = make_completed_artifact(tmp_path)
    try:
        reference = D6ArtifactReference(
            receipt.claim.task_run_id,
            receipt.claim.artifact_version_id,
            receipt.claim.expected_sha256,
        )
        before_db = (app.directory / "control.sqlite").read_bytes()
        snapshot_store = CrossTaskRunArtifactStore(tmp_path / "snapshots")
        transfer = D6ArtifactTransfer(snapshot_store, app.clock)
        reader = BoundedArtifactReader(sandbox)
        snapshot = asyncio.run(transfer.snapshot_from_run(
            app.directory, reference, classification="non_sensitive", reader=reader, token=token,
        ))
        declaration = transfer.verify_declared_snapshot(snapshot)
        assert declaration.source == reference
        assert declaration.classification == "non_sensitive"
        assert declaration.source_path == receipt.claim.path
        source_result = app.store.load_d3_plan_result(receipt.claim.plan_id)
        assert source_result is not None
        assert declaration.source_proof_refs == (source_result.proof_id,)
        assert declaration.source_fact_refs == (source_result.supporting_fact_id,)
        assert declaration.source_process_fact_refs == app.store.list_process_fact_refs(
            receipt.claim.task_run_id
        )
        assert len(reader.calls) == 1
        assert (app.directory / "control.sqlite").read_bytes() == before_db

        original = sandbox.files.pop(receipt.claim.path)
        assert hashlib.sha256(original).hexdigest() == reference.sha256
        target_run = str(uuid4())
        request = D6ArtifactImportRequest(str(uuid4()), reference, target_run, True)
        prepared = snapshot_store.prepare_import(
            request=request, snapshot=snapshot, existing_import_count=0,
        )
        confirmed = snapshot_store.copy_and_confirm(prepared)
        assert confirmed.status == "confirmed"
        assert Path(confirmed.target_path).read_bytes() == original
        assert not (Path(confirmed.target_path).parent / "proofs").exists()
        assert not (Path(confirmed.target_path).parent / "facts").exists()
    finally:
        close_source(app, sandbox)


def test_classification_is_required_before_source_read(tmp_path: Path) -> None:
    app, sandbox, token, receipt = make_completed_artifact(tmp_path)
    try:
        reference = D6ArtifactReference(
            receipt.claim.task_run_id, receipt.claim.artifact_version_id,
            receipt.claim.expected_sha256,
        )
        reader = BoundedArtifactReader(sandbox)
        transfer = D6ArtifactTransfer(CrossTaskRunArtifactStore(tmp_path / "snapshots"), app.clock)
        with pytest.raises(D6ArtifactImportError, match="d6_non_sensitive_declaration_required"):
            asyncio.run(transfer.snapshot_from_run(
                app.directory, reference, classification="unknown", reader=reader, token=token,
            ))
        assert reader.calls == []
    finally:
        close_source(app, sandbox)


def test_unknown_source_read_keeps_declaration_and_never_retries(tmp_path: Path) -> None:
    app, sandbox, token, receipt = make_completed_artifact(tmp_path)
    try:
        reference = D6ArtifactReference(
            receipt.claim.task_run_id, receipt.claim.artifact_version_id,
            receipt.claim.expected_sha256,
        )
        reader = BoundedArtifactReader(sandbox)
        reader.fail = True
        transfer = D6ArtifactTransfer(CrossTaskRunArtifactStore(tmp_path / "snapshots"), app.clock)
        for _ in range(2):
            with pytest.raises(D6ArtifactImportRecoveryRequired, match="d6_source_read_outcome_unknown"):
                asyncio.run(transfer.snapshot_from_run(
                    app.directory, reference, classification="non_sensitive", reader=reader, token=token,
                ))
        assert len(reader.calls) == 1
    finally:
        close_source(app, sandbox)


def test_source_hash_mismatch_is_rejected_before_bounded_read(tmp_path: Path) -> None:
    app, sandbox, token, receipt = make_completed_artifact(tmp_path)
    try:
        reference = D6ArtifactReference(
            receipt.claim.task_run_id, receipt.claim.artifact_version_id,
            "0" * 64,
        )
        reader = BoundedArtifactReader(sandbox)
        transfer = D6ArtifactTransfer(CrossTaskRunArtifactStore(tmp_path / "snapshots"), app.clock)
        with pytest.raises(D6ArtifactImportError, match="d6_source_success_receipt_required"):
            asyncio.run(transfer.snapshot_from_run(
                app.directory, reference, classification="non_sensitive", reader=reader, token=token,
            ))
        assert reader.calls == []
    finally:
        close_source(app, sandbox)


def test_snapshot_store_must_not_contain_or_be_contained_by_source_run(tmp_path: Path) -> None:
    source_task_run_id = str(uuid4())
    run_directory = tmp_path / "runs" / source_task_run_id
    run_directory.mkdir(parents=True)
    reference = D6ArtifactReference(
        source_task_run_id, str(uuid4()), "0" * 64,
    )
    transfer = D6ArtifactTransfer(
        CrossTaskRunArtifactStore(tmp_path / "runs"), Clock(),
    )

    with pytest.raises(D6ArtifactImportError, match="d6_snapshot_store_must_be_external"):
        asyncio.run(transfer.snapshot_from_run(
            run_directory, reference, classification="non_sensitive",
            reader=BoundedArtifactReader(Sandbox()), token=Token(),
        ))


def test_d6_import_is_the_only_input_and_binds_the_d3_receipt(tmp_path: Path) -> None:
    source_app, source_sandbox, source_token, source_receipt = make_completed_artifact(tmp_path)
    target_app = None
    target_sandbox = Sandbox()
    try:
        reference = D6ArtifactReference(
            source_receipt.claim.task_run_id,
            source_receipt.claim.artifact_version_id,
            source_receipt.claim.expected_sha256,
        )
        target_task_run_id = str(uuid4())
        snapshots = CrossTaskRunArtifactStore(tmp_path / "shared-d6-store")
        transfer = D6ArtifactTransfer(snapshots, Clock())
        snapshot = asyncio.run(transfer.snapshot_from_run(
            source_app.directory, reference, classification="non_sensitive",
            reader=BoundedArtifactReader(source_sandbox), token=source_token,
        ))
        import_request = D6ArtifactImportRequest(
            str(uuid4()), reference, target_task_run_id, True,
        )
        prepared = snapshots.prepare_import(
            request=import_request, snapshot=snapshot, existing_import_count=0,
        )
        target_manifest = snapshots.copy_and_confirm(prepared)
        outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1")
        config = D3RunConfig(
            outer_role,
            D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
            D3Authorization(str(uuid4()), (0,)),
            outer_role.role_config_ref,
            d4_budget=D4BudgetPolicy(),
            d5_enabled=True,
            d5_sensitive_requirements=(),
            d6_authorization=D6Authorization(
                str(uuid4()), target_task_run_id, (reference,),
            ),
        )
        clock, token = Clock(), Token()
        outer, inner, review = Model(config.outer_role), Model(config.inner_role), ReviewModel(config.outer_role)
        inner.review_when = lambda model_request: model_request.stage in {"inner_size", "inner_sha256"}
        target_app = D3Application(
            tmp_path / "target-control-run",
            config=config,
            sandbox=target_sandbox,
            import_sandbox=target_sandbox,
            workspace_sandbox=target_sandbox,
            command_sandbox=target_sandbox,
            outer_model=outer,
            inner_model=inner,
            review_model=review,
            clock=clock,
            refinement_model=RefinementModel(config.outer_role),
            d6_artifact_store=snapshots,
            d6_artifact_transfer=transfer,
        )
        target_app.start(
            TaskRequest.from_dict({
                "goal": "verify the imported prior-run artifact",
                "target": {"attachments": [{"kind": "local_path", "path": "ignored-user-path.txt"}]},
            }),
            tmp_path / "ignored-user-root",
            task_run_id=target_task_run_id,
            d6_artifacts=(reference,),
        )
        target_state = target_app.state.task_run
        assert target_state.source_root == str(Path(target_manifest.target_path).parent.resolve())
        assert target_state.request.target.attachments[0].value == Path(target_manifest.target_path).name
        assert not (tmp_path / "ignored-user-root" / "ignored-user-path.txt").exists()

        result = asyncio.run(target_app.run(token))
        assert result.task_run_id == target_task_run_id
        assert not result.recovery_required, result
        assert len(result.verified_fact_ids) == 1
        plan_result = target_app.store.load_d3_plan_result(target_app.state.plans[0].plan_id)
        assert plan_result is not None and plan_result.proof_id is not None
        imported_receipt = target_app.imports.read_receipt(target_app._id("import/0"))
        assert imported_receipt is not None
        assert imported_receipt.sha256 == reference.sha256
        binding = snapshots.verify_attachment_receipt(
            request=import_request,
            attachment_index=0,
            receipt_sha256=digest(asdict(imported_receipt)),
        )
        assert binding.target_path == target_manifest.target_path
        assert binding.artifact_sha256 == reference.sha256
        assert binding.receipt_sha256 == digest(asdict(imported_receipt))
        assert target_sandbox.import_calls == 1

        replay = asyncio.run(target_app.run(token))
        assert replay == result
        assert target_sandbox.import_calls == 1
        snapshots.verify_attachment_receipt(
            request=import_request,
            attachment_index=0,
            receipt_sha256=digest(asdict(imported_receipt)),
        )
        source_result = source_app.store.load_d3_plan_result(source_app.state.plans[0].plan_id)
        assert source_result is not None and source_result.proof_id != plan_result.proof_id
        new_proof = target_app.proof_store.read(plan_result.proof_id)
        assert new_proof is not None and new_proof.task_run_id == target_task_run_id
    finally:
        if target_app is not None:
            close_source(target_app, target_sandbox)
        close_source(source_app, source_sandbox)
