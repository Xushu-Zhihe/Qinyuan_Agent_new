"""D7 candidate bodies remain exact, protected, and explicitly readable."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_d7_turn_budget import make_case

from agent_adapters import d7_candidate_evidence as candidate_evidence
from agent_adapters.d7_candidate_evidence import (
    MAX_D7_CANDIDATE_BYTES,
    D7CandidateEvidenceConflict,
    D7CandidateEvidenceError,
    D7CandidateEvidenceReadPort,
    D7CandidateEvidenceService,
)
from agent_core.domain import ExecutionStatus
from agent_core.goal_candidates import D7AnswerCandidate


class _TestOnlyProtection:
    """Reversible test protection; production composition never accepts it."""

    identity = "S-1-5-21-TEST-USER"

    def restrict_directory(self, directory: Path) -> None:
        assert directory.is_dir()

    def verify_directory(self, directory: Path) -> None:
        assert directory.is_dir()

    @staticmethod
    def _transform(value: bytes, entropy: bytes) -> bytes:
        key = hashlib.sha256(b"test-only-protection" + entropy).digest()
        return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(value))

    def protect(self, value: bytes, entropy: bytes) -> bytes:
        return self._transform(value, entropy)

    def unprotect(self, value: bytes, entropy: bytes) -> bytes:
        return self._transform(value, entropy)


def _register_candidate(app) -> D7AnswerCandidate:
    state = app.state
    execution = next(
        item for item in state.executions if item.status is ExecutionStatus.SUCCEEDED
    )
    plan = next(item for item in state.plans if item.plan_id == execution.plan_id)
    intent = next(item for item in state.intents if item.intent_id == plan.intent_id)
    turn = next(item for item in state.turns if item.turn_id == intent.turn_id)
    binding = app.store.load_d3_plan_binding(plan.plan_id)
    result = app.store.load_d3_plan_result(plan.plan_id)
    assert binding is not None and result is not None
    candidate = D7AnswerCandidate(
        task_run_id=state.task_run.task_run_id,
        candidate_id=str(uuid4()),
        turn_id=turn.turn_id,
        intent_id=intent.intent_id,
        plan_id=plan.plan_id,
        execution_id=execution.execution_id,
        evidence_version=result.evidence_sha256,
        source_ref=binding.artifact_version_id,
    )
    app.controller.register_answer_candidate(candidate, str(uuid4()))
    return candidate


@pytest.fixture
def candidate_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection
    )
    app, token = make_case(tmp_path, 2)
    try:
        result = asyncio.run(app.run(token))
        assert result.stop_reason == "turn_completed_sandbox_retained"
        candidate = _register_candidate(app)
        service = D7CandidateEvidenceService(
            app.directory,
            candidate.task_run_id,
            trusted_adapter_ids=("accepted-result-adapter",),
        )
        yield app, candidate, service
    finally:
        try:
            app.cancel()
        finally:
            app.close()


def test_candidate_text_is_exact_protected_idempotent_and_only_read_by_bound_port(
    candidate_run,
):
    app, candidate, service = candidate_run
    value = " 旗標：AΩ " + "\n"

    saved = service.save(candidate, value)
    replay = service.save(candidate, value)
    queried = service.query(candidate.candidate_id)

    assert saved == replay
    assert saved["body_sha256"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert queried["body"] is None
    assert queried["body_status"] == "withheld"
    assert value not in json.dumps(queried, ensure_ascii=False)
    assert (
        service.read_port("accepted-result-adapter").read(candidate.candidate_id)
        == value
    )

    forged = D7CandidateEvidenceReadPort(service, "accepted-result-adapter")
    with pytest.raises(
        D7CandidateEvidenceError, match="^d7_candidate_reader_untrusted$"
    ):
        forged.read(candidate.candidate_id)
    with pytest.raises(
        D7CandidateEvidenceError, match="^d7_candidate_reader_untrusted$"
    ):
        service.read_port("unregistered-adapter")

    files = [path.read_bytes() for path in app.directory.rglob("*") if path.is_file()]
    assert all(value.encode("utf-8") not in content for content in files)
    audit_path = app.directory / "d7-candidate-evidence" / "audit.jsonl"
    audit = audit_path.read_text(encoding="utf-8")
    assert "accepted-result-adapter" in audit
    assert '"result":"success"' in audit
    assert value not in audit


def test_candidate_body_rejects_invalid_text_and_oversize_without_truncation(
    candidate_run,
):
    _app, candidate, service = candidate_run
    invalid_values = (
        "",
        "valid\x00suffix",
        "\ud800",
        "x" * (MAX_D7_CANDIDATE_BYTES + 1),
    )
    for value in invalid_values:
        with pytest.raises(D7CandidateEvidenceError):
            service.save(candidate, value)

    assert list(service.store.directory.glob("*.meta.json")) == []
    exact_limit = "x" * MAX_D7_CANDIDATE_BYTES
    saved = service.save(candidate, exact_limit)
    assert saved["byte_size"] == MAX_D7_CANDIDATE_BYTES


def test_candidate_conflicts_and_unowned_bindings_are_rejected_before_storage(
    candidate_run,
):
    _app, candidate, service = candidate_run
    service.save(candidate, "first")
    with pytest.raises(D7CandidateEvidenceConflict):
        service.save(candidate, "second")

    changed = replace(candidate, candidate_id=str(uuid4()))
    with pytest.raises(
        D7CandidateEvidenceError, match="^d7_candidate_ownership_invalid$"
    ):
        service.save(changed, "unowned")

    changed_binding = replace(candidate, source_ref="artifact:other-version")
    with pytest.raises(
        D7CandidateEvidenceError, match="^d7_candidate_binding_mismatch$"
    ):
        service.save(changed_binding, "wrong binding")
    assert list(service.store.directory.glob("*.meta.json")) == [
        service.store.directory / f"{candidate.candidate_id}.meta.json"
    ]


def test_retry_recovers_encrypted_body_after_interrupted_manifest_commit(
    candidate_run, monkeypatch
):
    _app, candidate, service = candidate_run
    value = "retryable candidate"
    original_write = service.store._write_exclusive

    class ProcessLoss(BaseException):
        pass

    def interrupt_after_cipher(path, payload):
        if path.name.endswith(".meta.json"):
            raise ProcessLoss()
        original_write(path, payload)

    monkeypatch.setattr(service.store, "_write_exclusive", interrupt_after_cipher)
    with pytest.raises(ProcessLoss):
        service.save(candidate, value)
    assert list(service.store.directory.glob("*.cipher")) == [
        service.store.directory / f"{candidate.candidate_id}.cipher"
    ]
    assert list(service.store.directory.glob("*.meta.json")) == []

    monkeypatch.setattr(service.store, "_write_exclusive", original_write)
    recovered = service.save(candidate, value)
    assert recovered["body_sha256"] == hashlib.sha256(value.encode()).hexdigest()
    assert (
        service.read_port("accepted-result-adapter").read(candidate.candidate_id)
        == value
    )


def test_candidate_read_requires_a_committed_audit_before_unprotect(
    candidate_run, monkeypatch
):
    _app, candidate, service = candidate_run
    service.save(candidate, "private candidate")
    decrypt_calls = []
    monkeypatch.setattr(
        service.store.protection,
        "unprotect",
        lambda *args: decrypt_calls.append(args),
    )

    def fail_audit(**_kwargs):
        raise D7CandidateEvidenceError("d7_candidate_audit_write_failed")

    monkeypatch.setattr(service.store.audit, "record", fail_audit)
    with pytest.raises(
        D7CandidateEvidenceError, match="^d7_candidate_audit_write_failed$"
    ):
        service.read_port("accepted-result-adapter").read(candidate.candidate_id)
    assert decrypt_calls == []
