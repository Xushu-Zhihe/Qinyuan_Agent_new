from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from uuid import uuid4

import pytest

from agent_adapters.process_fact_evidence import D5EvidenceError, FileD5Evidence
from agent_adapters.process_fact_model import D5RedactingModel
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.process_facts import (
    D5Attempt,
    D5Candidate,
    D5CandidateRejected,
    D5Claim,
    D5ClaimRef,
    D5Conflict,
    D5ProcessFact,
    D5RefinementRequest,
    D5Source,
    derive_d5_conflicts,
    parse_d5_candidate,
)
from agent_core.redaction import (
    D5Redactor,
    D5SensitiveValue,
    d5_sensitive_configuration_matches,
)
from agent_core.review_contracts import D2ModelFailure, D2ModelResponse


def source(
    *,
    object_id: str | None = None,
    artifact_version_id: str | None = None,
    scope: str = "attachment_content",
    value: str = "one",
    kind: str = "execution",
    rule_version: int | None = 1,
) -> D5Source:
    object_id = object_id or str(uuid4())
    return D5Source(
        kind=kind,  # type: ignore[arg-type]
        object_id=object_id,
        content_sha256=hashlib.sha256(value.encode()).hexdigest(),
        subject_ref=artifact_version_id or object_id,
        observation_scope=scope,
        artifact_version_id=artifact_version_id,
        rule_version=rule_version,
    )


def raw_candidate(
    src: D5Source,
    value: str = "present",
    *,
    subject_ref: str | None = None,
    scope: str | None = None,
    conflicts_with: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    claim: dict[str, object] = {
        "subject_ref": subject_ref or src.subject_ref,
        "observation_scope": scope or src.observation_scope,
        "claim_key": "file_state",
        "claim_value": value,
        "source_refs": [src.object_id],
    }
    if conflicts_with is not None:
        claim["conflicts_with"] = conflicts_with
    return {"summary_text": f"File state: {value}", "claims": [claim]}


def process_fact(
    src: D5Source,
    candidate: D5Candidate,
    *,
    task_run_id: str | None = None,
) -> D5ProcessFact:
    return D5ProcessFact(
        process_fact_id=str(uuid4()),
        task_run_id=task_run_id or str(uuid4()),
        version=1,
        review_id=str(uuid4()),
        model_request_id=str(uuid4()),
        created_at=10.0,
        sources=(src,),
        evidence_sha256=hashlib.sha256(b"evidence").hexdigest(),
    )


def attempt(src: D5Source) -> D5Attempt:
    return D5Attempt(
        model_request_id=str(uuid4()),
        task_run_id=str(uuid4()),
        review_id=str(uuid4()),
        base_checkpoint_sequence=3,
        started_at=12.0,
        role_config_ref="sha256:" + "a" * 64,
        sources=(src,),
        context_sha256=hashlib.sha256(b"context").hexdigest(),
    )


def test_source_version_key_and_from_dict_are_stable() -> None:
    src = source()

    assert D5Source.from_dict(asdict(src)) == src
    assert src.version_key == D5Source.from_dict(asdict(src)).version_key
    assert len(src.version_key) == 64


def test_source_requires_canonical_subject_version_and_hash() -> None:
    src = source(artifact_version_id=str(uuid4()))

    with pytest.raises(ValueError):
        D5Source(
            **{
                **asdict(src),
                "subject_ref": str(uuid4()),
            }
        )
    with pytest.raises(ValueError):
        D5Source(**{**asdict(src), "content_sha256": "A" * 64})


def test_parse_candidate_binds_sources_and_claim_scope() -> None:
    src = source(artifact_version_id=str(uuid4()))
    parsed = parse_d5_candidate(raw_candidate(src), (src,), ())

    assert parsed.claims[0].source_refs == (src.object_id,)
    assert parsed.claims[0].subject_ref == src.artifact_version_id

    with pytest.raises(D5CandidateRejected, match="d5_candidate_invalid"):
        parse_d5_candidate(raw_candidate(src, value="x") | {"extra": True}, (src,), ())
    with pytest.raises(D5CandidateRejected, match="d5_claim_scope_mismatch"):
        parse_d5_candidate(raw_candidate(src, scope="other_scope"), (src,), ())
    with pytest.raises(D5CandidateRejected, match="d5_candidate_invalid"):
        parse_d5_candidate(
            {"summary_text": "ok", "claims": [], "unknown": 1}, (src,), ()
        )


def test_parse_candidate_rejects_unknown_sources_and_cross_run_conflicts() -> None:
    src = source()
    unrelated = source()
    prior_candidate = parse_d5_candidate(raw_candidate(src), (src,), ())
    prior_fact = process_fact(src, prior_candidate)
    foreign = {
        "process_fact_id": str(uuid4()),
        "claim_index": 0,
    }

    foreign_source = raw_candidate(src)
    foreign_source["claims"][0]["source_refs"] = [unrelated.object_id]  # type: ignore[index]
    with pytest.raises(D5CandidateRejected, match="d5_claim_source_not_allowed"):
        parse_d5_candidate(foreign_source, (src,), ())
    with pytest.raises(D5CandidateRejected, match="d5_claim_conflict_not_found"):
        parse_d5_candidate(
            raw_candidate(src, conflicts_with=[foreign]),
            (src,),
            (prior_fact,),
            (prior_candidate,),
        )
    with pytest.raises(D5CandidateRejected, match="d5_claim_conflict_not_found"):
        parse_d5_candidate(
            raw_candidate(
                src,
                conflicts_with=[
                    {"process_fact_id": prior_fact.process_fact_id, "claim_index": 1}
                ],
            ),
            (src,),
            (prior_fact,),
            (prior_candidate,),
        )


def test_parse_candidate_accepts_tuple_replay_and_valid_prior_reference() -> None:
    src = source()
    prior_candidate = parse_d5_candidate(raw_candidate(src), (src,), ())
    prior_fact = process_fact(src, prior_candidate)
    ref = {
        "process_fact_id": prior_fact.process_fact_id,
        "claim_index": 0,
    }
    raw = raw_candidate(src, "changed", conflicts_with=[ref])
    raw["claims"] = tuple(raw["claims"])  # type: ignore[arg-type]
    parsed = parse_d5_candidate(raw, (src,), (prior_fact,), (prior_candidate,))

    assert parsed.claims[0].conflicts_with == (D5ClaimRef(**ref),)


def test_candidate_bounds_reject_without_truncation() -> None:
    src = source()
    too_long = {"summary_text": "x" * 2049, "claims": []}
    too_many = {
        "summary_text": "ok",
        "claims": [raw_candidate(src)["claims"][0]] * 9,
    }

    for raw in (too_long, too_many):
        with pytest.raises(D5CandidateRejected, match="d5_candidate_invalid"):
            parse_d5_candidate(raw, (src,), ())
    with pytest.raises(ValueError, match="character limit"):
        D5Claim("subject", "scope", "k" * 129, "v", (src.object_id,))
    with pytest.raises(ValueError, match="character limit"):
        D5Claim("subject", "scope", "k", "v" * 513, (src.object_id,))
    with pytest.raises(ValueError, match="1..32"):
        D5Claim("subject", "scope", "k", "v", tuple(str(uuid4()) for _ in range(33)))


def test_derive_conflicts_only_for_same_version_scope_and_property() -> None:
    artifact_v1 = str(uuid4())
    old_source = source(artifact_version_id=artifact_v1, value="old")
    old_candidate = parse_d5_candidate(
        raw_candidate(old_source, "present"), (old_source,), ()
    )
    old_fact = process_fact(old_source, old_candidate)

    other_execution_same_version = source(
        artifact_version_id=artifact_v1, value="new", scope="attachment_content"
    )
    new_candidate = parse_d5_candidate(
        raw_candidate(other_execution_same_version, "absent"),
        (other_execution_same_version,),
        (old_fact,),
        (old_candidate,),
    )
    conflicts = derive_d5_conflicts(
        new_candidate,
        (other_execution_same_version,),
        ((old_fact, old_candidate),),
    )

    assert conflicts == (
        D5Conflict(0, old_fact.process_fact_id, 0, "candidate_disagreement"),
    )

    new_version = str(uuid4())
    source_v2 = source(artifact_version_id=new_version, value="new")
    v2_candidate = parse_d5_candidate(
        raw_candidate(source_v2, "absent"), (source_v2,), (old_fact,), (old_candidate,)
    )
    assert (
        derive_d5_conflicts(v2_candidate, (source_v2,), ((old_fact, old_candidate),))
        == ()
    )

    different_scope = source(
        artifact_version_id=artifact_v1, scope="metadata", value="new"
    )
    scoped_candidate = parse_d5_candidate(
        raw_candidate(different_scope, "absent"),
        (different_scope,),
        (old_fact,),
        (old_candidate,),
    )
    assert (
        derive_d5_conflicts(
            scoped_candidate, (different_scope,), ((old_fact, old_candidate),)
        )
        == ()
    )


def test_derive_keeps_model_proposals_separate_from_detected_disagreement() -> None:
    old_source = source()
    old_candidate = parse_d5_candidate(raw_candidate(old_source), (old_source,), ())
    old_fact = process_fact(old_source, old_candidate)
    current_source = source()
    candidate = parse_d5_candidate(
        raw_candidate(
            current_source,
            "different",
            conflicts_with=[
                {"process_fact_id": old_fact.process_fact_id, "claim_index": 0}
            ],
        ),
        (current_source,),
        (old_fact,),
        (old_candidate,),
    )

    assert derive_d5_conflicts(
        candidate, (current_source,), ((old_fact, old_candidate),)
    ) == (D5Conflict(0, old_fact.process_fact_id, 0, "model_proposed"),)


def test_refinement_request_normalizes_nested_json_for_immutable_replay() -> None:
    src = source()
    original = D5RefinementRequest(
        attempt(src),
        ({"source_id": src.object_id, "safe": {"items": ("a", "b")}},),
        ({"version": 1, "sources": (src.object_id,)},),
    )
    restored = D5RefinementRequest.from_dict(asdict(original))

    assert restored == original
    assert restored.safe_sources[0]["safe"] == {"items": ["a", "b"]}


def test_file_evidence_roundtrip_is_immutable_and_checksummed(tmp_path) -> None:
    src = source()
    request = D5RefinementRequest(
        attempt(src), ({"source_id": src.object_id, "safe": "summary"},)
    )
    candidate = parse_d5_candidate(raw_candidate(src), (src,), ())
    ledger = FileD5Evidence(tmp_path / "evidence")

    ledger.save_request(request)
    assert ledger.read_request(request.attempt.model_request_id) == request
    ledger.save_candidate(request.attempt.model_request_id, candidate)
    assert ledger.read_candidate(request.attempt.model_request_id) == candidate
    ledger.save_result(request.attempt.model_request_id, "accepted", 20.0, None)
    assert ledger.read_result(request.attempt.model_request_id) == {
        "model_request_id": request.attempt.model_request_id,
        "outcome": "accepted",
        "finished_at": 20.0,
        "reason": None,
    }

    with pytest.raises(D5EvidenceError, match="immutable_evidence_conflict"):
        ledger.save_result(request.attempt.model_request_id, "accepted", 21.0, None)

    path = tmp_path / "evidence" / f"candidate-{request.attempt.model_request_id}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["summary_text"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(D5EvidenceError, match="evidence_read_failed"):
        ledger.read_candidate(request.attempt.model_request_id)


def test_file_evidence_readonly_does_not_create_directory_and_paths_are_uuid_only(
    tmp_path,
) -> None:
    missing = tmp_path / "not-created"
    ledger = FileD5Evidence(missing, create=False)

    assert ledger.read_request(str(uuid4())) is None
    assert not missing.exists()
    with pytest.raises(D5EvidenceError, match="evidence_store_read_only"):
        ledger.save_request(D5RefinementRequest(attempt(source()), ({},)))
    with pytest.raises(D5EvidenceError, match="invalid_evidence_reference"):
        ledger.read_request("../outside")


def test_file_evidence_requires_fixed_result_reason_codes(tmp_path) -> None:
    src = source()
    request = D5RefinementRequest(attempt(src), ({"source_id": src.object_id},))
    ledger = FileD5Evidence(tmp_path / "evidence")
    ledger.save_request(request)

    with pytest.raises(ValueError, match="fixed code"):
        ledger.save_result(
            request.attempt.model_request_id, "failed", 20.0, "provider failed"
        )


def test_redactor_removes_explicit_and_common_credential_forms() -> None:
    redactor = D5Redactor(("test-only-secret",))
    text = (
        'password="p a s s" api_key: abc123 Authorization: Bearer token123 '
        "Basic dXNlcjpwYXNz https://user:pass@example.test/path "
        "private=-----BEGIN PRIVATE KEY-----\nkey-data\n-----END PRIVATE KEY----- "
        "test-only-secret"
    )

    cleaned = redactor.redact(text)

    assert "p a s s" not in cleaned
    assert "abc123" not in cleaned
    assert "token123" not in cleaned
    assert "dXNlcjpwYXNz" not in cleaned
    assert "user:pass" not in cleaned
    assert "key-data" not in cleaned
    assert "test-only-secret" not in cleaned
    assert not redactor.contains_sensitive(cleaned)
    assert redactor.redact(cleaned) == cleaned


def test_redactor_handles_nested_structures_without_mutating_input() -> None:
    redactor = D5Redactor(("secret-value",))
    original = {"message": "contains secret-value", "items": ["token=abc", 3]}

    cleaned = redactor.redact_value(original)

    assert original["message"] == "contains secret-value"
    assert cleaned == {
        "message": "contains [REDACTED]",
        "items": ["token=[REDACTED]", 3],
    }


def test_sensitive_configuration_checks_keys_versions_and_nonempty_values() -> None:
    required = (("model_credential", "v1"),)
    correct = {"model_credential": D5SensitiveValue("v1", "test-only-secret")}

    assert d5_sensitive_configuration_matches(required, correct)
    assert not d5_sensitive_configuration_matches(required, None)
    assert not d5_sensitive_configuration_matches(required, {})
    assert not d5_sensitive_configuration_matches(
        required, {"model_credential": D5SensitiveValue("v2", "test-only-secret")}
    )
    assert not d5_sensitive_configuration_matches(
        required, {"model_credential": D5SensitiveValue("v1", " ")}
    )
    assert "test-only-secret" not in repr(correct)
    # No required sensitive keys means the absent and empty configurations both match.
    assert d5_sensitive_configuration_matches((), {})
    assert d5_sensitive_configuration_matches((), None)
    assert not d5_sensitive_configuration_matches(None, None)
    assert not d5_sensitive_configuration_matches(None, {})
    assert not d5_sensitive_configuration_matches(None, correct)


def test_model_input_redaction_preserves_refinement_identity_and_provenance() -> None:
    src = source()
    request = D5RefinementRequest(
        attempt(src),
        ({"source": asdict(src), "summary": "password=unsafe-token"},),
        ({"identity": "model_summary", "summary_text": "test-only-secret"},),
    )

    cleaned = D5Redactor(("test-only-secret",)).redact_model_input(request)

    assert isinstance(cleaned, D5RefinementRequest)
    assert cleaned.attempt == request.attempt
    assert cleaned.safe_sources[0]["source"] == asdict(src)
    assert cleaned.safe_sources[0]["summary"] == "password=[REDACTED]"
    assert cleaned.process_facts[0]["summary_text"] == "[REDACTED]"  # type: ignore[index]
    assert request.safe_sources[0]["summary"] == "password=unsafe-token"


def test_model_input_redaction_keeps_context_json_valid() -> None:
    @dataclass(frozen=True)
    class Context:
        model_request_id: str
        context_summary: str

    request_id = str(uuid4())
    context = Context(
        request_id,
        json.dumps({"text": 'the secret is quote"inside', "items": ["api_key=unsafe"]}),
    )

    cleaned = D5Redactor(('quote"inside',)).redact_model_input(context)

    assert isinstance(cleaned, Context)
    assert cleaned.model_request_id == request_id
    assert json.loads(cleaned.context_summary) == {
        "text": "the secret is [REDACTED]",
        "items": ["api_key=[REDACTED]"],
    }


def test_model_response_redaction_preserves_typed_failure_envelopes() -> None:
    class Adapter:
        role_config = object()

        def __init__(self, response: object) -> None:
            self.response = response
            self.request = None

        def invoke(self, request: object, *_args: object) -> object:
            self.request = request
            return self.response

    redactor = D5Redactor(("test-only-secret",))
    cases = (
        (
            ModelError(ModelErrorKind.TIMED_OUT, "test-only-secret"),
            ModelError,
            ModelErrorKind.TIMED_OUT,
            "[REDACTED]",
        ),
        (
            D2ModelFailure("provider_failure", "test-only-secret", "provider-id"),
            D2ModelFailure,
            "provider_failure",
            "[REDACTED]",
        ),
        (
            D2ModelResponse({"summary_text": "test-only-secret"}, "provider-id"),
            D2ModelResponse,
            None,
            None,
        ),
    )

    for raw, expected_type, expected_kind, expected_text in cases:
        adapter = Adapter(raw)
        model = D5RedactingModel(adapter, redactor)

        result = model.invoke("safe request")

        assert isinstance(result, expected_type)
        if isinstance(result, ModelError):
            assert result.kind is expected_kind
            assert result.short_reason == expected_text
        elif isinstance(result, D2ModelFailure):
            assert result.kind == expected_kind
            assert result.reason == expected_text
        else:
            assert isinstance(result, D2ModelResponse)
            assert result.response == {"summary_text": "[REDACTED]"}
        assert adapter.request == "safe request"
