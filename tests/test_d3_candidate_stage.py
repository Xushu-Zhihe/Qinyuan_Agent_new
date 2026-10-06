"""Recovery behavior for the D3 candidate-generation operation."""

from __future__ import annotations

import asyncio

import pytest
from test_d7_candidate_evidence import _TestOnlyProtection
from test_d7_turn_budget import make_case

from agent_adapters.v7 import candidate_evidence
from agent_adapters.v7.candidate_evidence import D7CandidateEvidenceService
from agent_core.dual_llm_modeling import D3ProposalRejected


@pytest.mark.parametrize(
    ("review_requested", "expected_code", "structural"),
    [
        (True, "d3_candidate_review_not_supported", False),
        (1, "d3_review_requested_invalid", True),
    ],
)
def test_completed_candidate_rejection_replays_without_model_or_candidate_io(
    tmp_path, monkeypatch, review_requested, expected_code, structural
):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection
    )
    app, token = make_case(tmp_path, 2)
    try:
        app.inner.adapter.adapter.candidate_review_requested = review_requested
        result = asyncio.run(app.run(token))
        assert result.stop_reason == "turn_completed_sandbox_retained"

        task_run_id = app.state.task_run.task_run_id
        protected = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage",),
        )
        model = app.inner.adapter.adapter
        before = len(model.calls)

        with pytest.raises(D3ProposalRejected) as first:
            asyncio.run(app.propose_d7_candidate(token, protected))

        candidate_requests = [
            request for request in model.calls if request.stage == "inner_candidate"
        ]
        assert len(candidate_requests) == 1
        request_id = candidate_requests[0].model_request_id
        record = app.store.load_d3_operation(request_id)
        evidence = app.inner.evidence.read(request_id)
        assert record is not None and record.completion is not None
        assert record.completion.result_kind == "rejected"
        assert evidence is not None
        assert evidence.outcome == ("invalid_structure" if structural else "rejected")
        assert evidence.reason == expected_code
        assert list(protected.store.directory.glob("*.cipher")) == []
        assert list(protected.store.directory.glob("*.meta.json")) == []
        assert app.store.list_d7_answer_candidates(task_run_id) == ()

        def reject_candidate_evidence_read(_request_id):
            raise AssertionError("rejected replay must not read candidate evidence")

        monkeypatch.setattr(
            protected, "load_pending_for_request", reject_candidate_evidence_read
        )

        with pytest.raises(D3ProposalRejected) as replay:
            asyncio.run(app.propose_d7_candidate(token, protected))

        assert replay.value.code == first.value.code
        assert replay.value.structural == first.value.structural
        assert replay.value.retryable == first.value.retryable
        assert replay.value.model_request_id == first.value.model_request_id
        assert replay.value.code == expected_code
        assert replay.value.structural is structural
        assert len(model.calls) == before + 1
        assert len(
            [request for request in model.calls if request.stage == "inner_candidate"]
        ) == 1
        assert app.store.list_d7_answer_candidates(task_run_id) == ()
    finally:
        if app.state.task_run.status.value in {"running", "awaiting_external_feedback"}:
            app.cancel()
        app.close()
