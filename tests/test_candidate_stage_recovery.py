"""Candidate model responses are protected before Controller registration."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from test_candidate_evidence import _TestOnlyProtection
from test_turn_budget import make_case

from agent_adapters.web import candidate_evidence
from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.dual_llm_modeling import ExecutionProposalRejected
from agent_core.runtime import TaskRunStatus


def _make_run(tmp_path, monkeypatch, candidate_value):
    monkeypatch.setattr(
        candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection
    )
    app, token = make_case(tmp_path, 2)
    app.inner.adapter.adapter.candidate_values = [candidate_value]
    result = asyncio.run(app.run(token))
    assert result.stop_reason == "turn_completed_sandbox_retained"
    service = D7CandidateEvidenceService(
        app.directory,
        app.state.task_run.task_run_id,
        trusted_adapter_ids=("d7_candidate_stage", "candidate-stage-test"),
    )
    return app, token, service, app.inner.adapter.adapter


def test_candidate_stage_protects_then_recovers_controller_commit_without_redispatch(
    tmp_path, monkeypatch,
):
    value = "d7_private_flag_xYz_83"
    app, token, service, model = _make_run(tmp_path, monkeypatch, value)
    original_save = app.model_evidence.save
    candidate_stage_calls = []

    def interrupt_after_protected_result(evidence):
        if evidence.stage == "inner_candidate":
            candidate_stage_calls.append(evidence.model_request_id)
            raise RuntimeError("simulated process interruption")
        return original_save(evidence)

    monkeypatch.setattr(app.model_evidence, "save", interrupt_after_protected_result)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        asyncio.run(app.propose_d7_candidate(token, service))

    assert len([item for item in model.calls if item.stage == "inner_candidate"]) == 1
    assert app.store.list_d7_answer_candidates(app.state.task_run.task_run_id) == ()
    pending = service.load_pending_for_request(candidate_stage_calls[0])
    assert pending is not None
    candidate, metadata = pending
    assert metadata["body_sha256"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert app.state.task_run.status is TaskRunStatus.RUNNING

    monkeypatch.setattr(app.model_evidence, "save", original_save)
    recovered = asyncio.run(app.propose_d7_candidate(token, service))
    assert recovered == candidate
    assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    assert app.store.list_d7_answer_candidates(app.state.task_run.task_run_id) == (candidate,)
    assert len([item for item in model.calls if item.stage == "inner_candidate"]) == 1
    assert service.read_port("candidate-stage-test").read(candidate.candidate_id) == value
    replay = asyncio.run(app.propose_d7_candidate(token, service))
    assert replay == candidate
    assert app.store.list_d7_answer_candidates(app.state.task_run.task_run_id) == (candidate,)
    assert len([item for item in model.calls if item.stage == "inner_candidate"]) == 1

    d3_files = [
        path.read_bytes()
        for path in (app.directory / "d3-models").rglob("*")
        if path.is_file()
    ]
    assert all(value.encode("utf-8") not in content for content in d3_files)
    assert value.encode("utf-8") not in (app.directory / "control.sqlite").read_bytes()
    assert value.encode("utf-8") not in json.dumps(candidate.to_dict()).encode("utf-8")
    app.cancel()
    app.close()


def test_candidate_stage_rejects_unsupported_review_signal_without_saving_body(
    tmp_path, monkeypatch,
):
    value = "review-signal-private-value"
    app, token, service, model = _make_run(tmp_path, monkeypatch, value)
    model.candidate_review_requested = True
    with pytest.raises(ExecutionProposalRejected, match="d3_candidate_review_not_supported"):
        asyncio.run(app.propose_d7_candidate(token, service))

    assert app.store.list_d7_answer_candidates(app.state.task_run.task_run_id) == ()
    files = [
        path.read_bytes()
        for path in (app.directory / "d3-models").rglob("*")
        if path.is_file()
    ]
    assert all(value.encode("utf-8") not in content for content in files)
    assert list(service.store.directory.glob("*.meta.json")) == []
    app.cancel()
    app.close()


def test_unknown_candidate_model_call_is_never_redispatched(tmp_path, monkeypatch):
    app, token, service, model = _make_run(tmp_path, monkeypatch, "unused")
    original_invoke = model.invoke
    candidate_calls = []

    def lose_candidate_response(request, *args):
        if request.stage == "inner_candidate":
            candidate_calls.append(request.model_request_id)
            raise TimeoutError("candidate response unavailable")
        return original_invoke(request, *args)

    monkeypatch.setattr(model, "invoke", lose_candidate_response)
    with pytest.raises(ExecutionRecoveryRequired, match="d3_model_call_outcome_unknown"):
        asyncio.run(app.propose_d7_candidate(token, service))
    with pytest.raises(
        ExecutionRecoveryRequired, match="d3_candidate_model_call_outcome_unknown"
    ):
        asyncio.run(app.propose_d7_candidate(token, service))

    assert len(candidate_calls) == 1
    assert len([item for item in model.calls if item.stage == "inner_candidate"]) == 0
    assert app.store.list_d7_answer_candidates(app.state.task_run.task_run_id) == ()
    cancelled = app.cancel()
    assert cancelled.recovery_required
    assert app.state.task_run.status is TaskRunStatus.RUNNING
    # An admitted model call with unknown outcome keeps the TaskRun open so
    # recovery can reconcile it; Controller.close() must reject this state.
    app.store.close()
