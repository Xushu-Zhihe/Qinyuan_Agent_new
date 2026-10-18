"""S5 summary rejection must not require an accepted selection binding."""

import hashlib
import json

import pytest

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4RecoveryRequired
from agent_core.output_contracts import D8ModelResponse
from test_chain_multi_turn import ProcessLoss
from test_output import output_case, reopen, run as run_output
from test_governance_selection import modern, s5_case


pytestmark = pytest.mark.parametrize("output_case", [15], indirect=True)


@pytest.mark.parametrize("field", ["turn_body", "global_body"])
def test_overlong_summary_completes_rejection_and_corrects_once(s5_case, field):
    c = s5_case
    marker = "rejected-summary-body-never-persisted"
    calls = []

    def response(envelope, raw):
        if envelope.output_contract["stage"] == "handoff_summary":
            calls.append(envelope)
            if len(calls) == 1:
                return {**raw, field: marker + "x" * getattr(envelope.request.capacity, field)}
        return modern(envelope, raw)

    c.models["handoff"].transform = response
    result = run_output(c)
    assert result.stop_reason == "search_exhausted", result
    root, corrected = calls[:2]
    identity = root.request.model_request_id
    assert corrected.request.correction_of_model_request_id == identity
    assert corrected.correction["issues"] == [{"code": "string_too_long", "path": "$/" + field,
        "expected": "allowed_length", "actual": "string"}]
    saved = c.app.handoff.evidence.read("result", identity)
    assert saved["outcome"] == "invalid" and saved["raw"] is None
    assert c.app.output_boundary.evidence.read("selection-binding", identity) is None
    operation = c.app.store.load_d4_operation(identity)
    assert operation.completion.result_kind == "rejected"
    assert operation.application_ref is None
    accepted = c.app.store.load_d4_operation(corrected.request.model_request_id)
    assert accepted.completion.result_kind == "completed" and accepted.application_ref is not None
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert marker not in "".join(p.read_text(encoding="utf-8") for p in c.directory.rglob("*.json"))
    before = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    assert run_output(c).stop_reason == "search_exhausted"
    assert before == {name: len(model.calls) for name, model in c.models.items()}


def summary_calls(case):
    return [envelope for envelope in case.models["handoff"].calls
            if envelope.output_contract["stage"] == "handoff_summary"]


def reject_first_summary(case):
    def response(envelope, raw):
        if envelope.output_contract["stage"] == "handoff_summary" and len(summary_calls(case)) == 1:
            return {**raw, "turn_body": "x" * (envelope.request.capacity.turn_body + 1)}
        return modern(envelope, raw)
    case.models["handoff"].transform = response


def assert_paused(case):
    try:
        result = run_output(case)
    except (D3RecoveryRequired, D4RecoveryRequired):
        return
    assert result.recovery_required, result


def test_second_bad_summary_uses_existing_fallback_without_third_attempt(s5_case):
    c = s5_case
    def response(envelope, raw):
        if envelope.output_contract["stage"] == "handoff_summary" and len(summary_calls(c)) <= 2:
            return {**raw, "turn_body": "x" * (envelope.request.capacity.turn_body + 1)}
        return modern(envelope, raw)
    c.models["handoff"].transform = response
    assert run_output(c).stop_reason == "search_exhausted"
    first, second = summary_calls(c)[:2]
    root = first.request.model_request_id
    assert second.request.correction_of_model_request_id == root
    assert sum(e.request.correction_of_model_request_id == root for e in summary_calls(c)) == 1
    for envelope in (first, second):
        identity = envelope.request.model_request_id
        saved = c.app.handoff.evidence.read("result", identity)
        assert saved["outcome"] == "invalid" and saved["raw"] is None
        assert c.app.store.load_d4_operation(identity).completion.result_kind == "rejected"
        assert c.app.output_boundary.evidence.read("selection-binding", identity) is None
    versions = c.app.store.list_d8_handoffs(c.task)
    assert versions[0].origin == "trusted_code_fallback"
    assert not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize("response,code", [
    (D8ModelResponse('{"turn_body":', "length"), "output_truncated"),
    (" " * 65537, "output_size_exceeded"),
    ("[" * 33 + "0" + "]" * 33, "output_depth_exceeded"),
], ids=["truncated", "oversized", "deep"])
def test_terminal_summary_never_corrects_or_falls_back(s5_case, response, code):
    c = s5_case
    c.models["handoff"].transform = lambda e, raw: response if (
        e.output_contract["stage"] == "handoff_summary") else modern(e, raw)
    result = run_output(c)
    assert result.recovery_required and code in result.stop_reason
    assert len(summary_calls(c)) == 1
    assert not c.app.store.list_d8_handoffs(c.task)
    identity = summary_calls(c)[0].request.model_request_id
    assert c.app.output_boundary.diagnostic(identity)["terminal"] == code
    before = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    assert_paused(c)
    assert before == {name: len(model.calls) for name, model in c.models.items()}


def test_unknown_summary_never_reasks_or_falls_back(s5_case):
    c = s5_case
    c.models["handoff"].transform = lambda e, raw: ModelError(ModelErrorKind.TIMED_OUT, "test_timeout") if (
        e.output_contract["stage"] == "handoff_summary") else modern(e, raw)
    assert_paused(c)
    assert len(summary_calls(c)) == 1
    identity = summary_calls(c)[0].request.model_request_id
    assert c.app.handoff.evidence.read("result", identity) is None
    assert c.app.output_boundary.diagnostic(identity) is None
    assert c.app.store.load_d4_operation(identity).completion is None
    assert not c.app.store.list_d8_handoffs(c.task)
    before = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    assert_paused(c)
    assert before == {name: len(model.calls) for name, model in c.models.items()}


@pytest.mark.parametrize("boundary", ["before_result", "before_completion"])
def test_known_summary_rejection_recovers_original_id_without_replaying_model(s5_case, boundary):
    c = s5_case
    reject_first_summary(c)
    if boundary == "before_result":
        original = c.app.handoff.evidence.save
        def lost(kind, identity, payload):
            if kind == "result" and payload.get("outcome") == "invalid":
                raise ProcessLoss()
            return original(kind, identity, payload)
        c.app.handoff.evidence.save = lost
    else:
        original = c.app.store.complete_d4_operation
        def lost(completion):
            start = c.app.store.load_d4_operation(completion.operation_id).start
            if start.category == "handoff_summary" and completion.result_kind == "rejected":
                raise ProcessLoss()
            return original(completion)
        c.app.store.complete_d4_operation = lost
    with pytest.raises(ProcessLoss):
        run_output(c)
    identity = summary_calls(c)[0].request.model_request_id
    assert c.app.output_boundary.diagnostic(identity) is not None
    assert c.app.store.load_d4_operation(identity).completion is None
    assert (c.app.handoff.evidence.read("result", identity) is None) == (boundary == "before_result")
    executed = list(c.sandbox.command_calls)
    reopen(c)
    assert run_output(c).stop_reason == "search_exhausted"
    assert sum(e.request.model_request_id == identity for e in summary_calls(c)) == 1
    assert sum(e.request.correction_of_model_request_id == identity for e in summary_calls(c)) == 1
    assert c.app.store.load_d4_operation(identity).completion.result_kind == "rejected"
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert c.sandbox.command_calls == executed


@pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign_contract"])
def test_rejection_diagnostic_damage_pauses_without_a_new_call(s5_case, damage):
    c = s5_case
    reject_first_summary(c)
    original = c.app.handoff.evidence.save
    def lost(kind, identity, payload):
        if kind == "result" and payload.get("outcome") == "invalid":
            raise ProcessLoss()
        return original(kind, identity, payload)
    c.app.handoff.evidence.save = lost
    with pytest.raises(ProcessLoss):
        run_output(c)
    identity = summary_calls(c)[0].request.model_request_id
    path = next((c.directory / "d8-output").glob("*diagnostic-" + identity + ".json"))
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{}", encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["payload"]["contract_sha256"] = "0" * 64
        encoded = json.dumps(payload["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        payload["sha256"] = hashlib.sha256(encoded).hexdigest()
        path.write_text(json.dumps(payload), encoding="utf-8")
    before = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    assert_paused(c)
    assert before == {name: len(model.calls) for name, model in c.models.items()}
    assert not c.app.store.list_d8_handoffs(c.task)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_legal_summary_still_requires_binding_before_native_result(s5_case, damage):
    c = s5_case
    original = c.app.selection_bindings.normalize
    def normalize(request, wire, clock):
        canonical = original(request, wire, clock)
        if request.stage == "handoff_summary":
            path = next((c.directory / "d8-output").glob("*selection-binding-" + request.model_request_id + ".json"))
            if damage == "missing":
                path.unlink()
            else:
                path.write_text("{}", encoding="utf-8")
        return canonical
    c.app.selection_bindings.normalize = normalize
    assert_paused(c)
    assert len(summary_calls(c)) == 1
    identity = summary_calls(c)[0].request.model_request_id
    assert c.app.output_boundary.diagnostic(identity) is None
    assert c.app.handoff.evidence.read("result", identity) is None
    assert not c.app.store.list_d8_handoffs(c.task)
    before = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    assert_paused(c)
    assert before == {name: len(model.calls) for name, model in c.models.items()}
