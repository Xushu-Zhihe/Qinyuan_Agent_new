"""D8-5 observable output contracts over real scheduling and persisted evidence."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_chain_multi_turn import Clock, Model, Sandbox, Token, ProcessLoss
from test_sessions import SessionInner, ProgressModel
from test_sessions import session_case
from test_conclusion_review import FactReviewer
from test_handoff import HandoffModel
from test_chain_process_fact import RefinementModel

from agent_adapters.multi_turn_application import D3Application
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.handoff_contracts import D8CapacityPolicy, D8HandoffConfig
from agent_core.operation_accounting import D8HandoffBudgetPolicy
from agent_core.turn_contracts import D1RoleConfig
from agent_core.output_contracts import D8OutputRequest, D8ModelResponse, output_schema, decode_response


class EnvelopeModel:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = []
        self.transform = lambda request, raw: raw

    @property
    def role_config(self):
        return self.adapter.role_config

    def invoke(self, envelope, *args):
        assert isinstance(envelope, D8OutputRequest)
        self.calls.append(envelope)
        raw = self.adapter.invoke(envelope.request, *args)
        from agent_core.fixed_probe import FixedProbeContext, fixed_probe_policy
        if (isinstance(getattr(envelope.request, "context", None), FixedProbeContext)
                and isinstance(raw, dict) and isinstance(raw.get("action"), dict)
                and raw["action"].get("kind") == "probe"):
            raw = {"action": {"kind": "fixed_probe", "operation": fixed_probe_policy(envelope.request)["operation"],
                              "timeout_seconds": raw["action"]["timeout_seconds"]},
                   "review_requested": raw["review_requested"]}
        if envelope.output_contract['stage'] == 'tree_planning' and isinstance(raw, dict) and raw.get('kind') == 'candidates' and (
                'turn_target' not in envelope.output_contract['schema']['anyOf'][0]['properties']):
            raw.pop('turn_target')
            raw['completion_rule'].pop('version')
        if envelope.output_contract['stage'] == 'fact_proposal' and isinstance(raw, dict) and raw.get('assertion') is not None and (
                set(envelope.output_contract['schema']['anyOf'][0]['properties']['assertion']['anyOf'][1]['properties']) == {'rule_id'}):
            raw['assertion'] = {'rule_id': raw['assertion']['rule_id']}
        if envelope.output_contract['stage'] == 'progress_review' and 'anyOf' in envelope.output_contract['schema'] and isinstance(raw, dict):
            raw['constraints'] = [{k: v for k, v in c.items() if k != 'scope_id'} for c in raw['constraints']]
        return self.transform(envelope, raw)


@pytest.fixture(params=(6, 7, 8, 9, 10, 11, 12, 13, 14))
def output_case(tmp_path, request):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D8-5 authorized local observation")
    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-d8-5-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "same-model", "inner-d8-5-v1")
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 262144, 8192)
    config = D3RunConfig(outer_role, inner_role, D3Authorization(str(uuid4()), (0,)),
        outer_role.role_config_ref, contract_version=22 if request.param == "rolling" else request.param, d4_budget=D8HandoffBudgetPolicy(),
        intent_execution_policy="rolling_v1" if request.param == "rolling" else None,
        d5_enabled=True, d5_sensitive_requirements=(), d8_enabled=True,
        handoff_config=D8HandoffConfig(policy, policy))
    sandbox, clock, token = Sandbox(), Clock(), Token()
    handoff = HandoffModel(outer_role)
    def planning(request, raw):
        if request.stage == "tree_planning" and raw["kind"] == "candidates":
            if request.turn_id is not None:
                return {"kind": "exhausted", "reason": "No more internal directions.",
                        "evidence_refs": [request.history[0].ref]}
            return {**raw, "intents": raw["intents"][:1]}
        return raw
    handoff.modify = planning
    models = {"outer": EnvelopeModel(Model(outer_role)),
              "inner": EnvelopeModel(SessionInner(inner_role)),
              "review": EnvelopeModel(ProgressModel(outer_role)),
              "fact": EnvelopeModel(FactReviewer(outer_role)),
              "refinement": EnvelopeModel(RefinementModel(outer_role)),
              "handoff": EnvelopeModel(handoff)}
    capacity = FakeUnicodeCapacity()
    def create():
        return D3Application(tmp_path / "run", config=config, sandbox=sandbox,
            import_sandbox=sandbox, workspace_sandbox=sandbox, command_sandbox=sandbox,
            outer_model=models["outer"], inner_model=models["inner"], review_model=models["review"],
            fact_review_model=models["fact"], refinement_model=models["refinement"],
            handoff_model=models["handoff"], capacity_port=capacity, clock=clock)
    app = create()
    task = app.start(TaskRequest.from_dict({"goal": "Observe the authorized copy",
        "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}), source)
    current_capacity = app.handoff.capacity().capacity
    if current_capacity.initial_history < 32768 or current_capacity.visible_history < 131072:
        app.handoff.increase_capacity({"initial_history": max(32768, current_capacity.initial_history),
                                      "visible_history": max(131072, current_capacity.visible_history)})
    case = SimpleNamespace(app=app, models=models, token=token, sandbox=sandbox, clock=clock,
                           create=create, task=task, directory=tmp_path / "run", capacity=capacity)
    yield case
    case.app.store.close()


def run(case):
    return asyncio.run(case.app.run(case.token))


def reopen(case):
    case.app.store.close()
    case.app = case.create()
    case.app.controller.attach(case.task)
    case.app._bound_task_run_id = case.task


def initial_planning(envelope):
    return envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id is None


@pytest.mark.parametrize("wire_text", [False, True])
def test_actual_chain_schemas_sessions_and_known_resume(output_case, wire_text):
    case = output_case
    if wire_text:
        for model in case.models.values():
            model.transform = lambda _, raw: D8ModelResponse(json.dumps(raw))
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    stages = {e.output_contract["stage"] for m in case.models.values() for e in m.calls}
    assert {"tree_planning", "inner_plan", "inner_size", "inner_sha256", "fact_proposal",
            "fact_review", "inner_complete", "intent_evaluation", "handoff_summary", "progress_review"} <= stages
    assert len(case.app.sessions.records()) == 5
    before = {name: len(model.calls) for name, model in case.models.items()}
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert before == {name: len(model.calls) for name, model in case.models.items()}


@pytest.mark.parametrize("bad", [{}, "not-an-array", None, 1])
def test_wrong_intent_array_corrected_once_and_safely_audited(output_case, bad):
    case = output_case
    marker = "raw-error-marker-never-saved"
    def response(envelope, raw):
        if initial_planning(envelope) and envelope.correction is None:
            return {**raw, "intents": bad, marker: marker}
        return raw
    case.models["handoff"].transform = response
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    calls = [e for e in case.models["handoff"].calls if initial_planning(e)]
    assert len(calls) == 2
    assert calls[1].correction["correction_of_model_request_id"] == calls[0].request.model_request_id
    assert "complete response again" in calls[1].instruction
    issues = calls[1].correction["issues"]
    assert any(i["path"] == "$/intents" and i["expected"] == "array" for i in issues)
    assert marker not in "".join(p.read_text(encoding="utf-8") for p in (case.directory / "d8-output").glob("*.json"))
    assert all(len(i["path"]) <= 128 for i in issues)


def test_two_bad_candidates_stop_without_tools(output_case):
    case = output_case
    case.models["handoff"].transform = lambda e, raw: {**raw, "intents": {}} if initial_planning(e) else raw
    result = run(case)
    assert result.stop_reason != "search_exhausted"
    assert len(case.models["handoff"].calls) == 2
    assert case.sandbox.command_calls == []
    assert case.app.state.turns == ()


@pytest.mark.parametrize("raw,code", [('{"intents":', "json_parse_failed"),
    ('{"intents":[],"intents":[]}', "json_duplicate_key"),
    ('{"intents":NaN}', "json_parse_failed"), ('```json\n{}\n```', "json_parse_failed")])
def test_json_parse_errors_share_one_correction(output_case, raw, code):
    case = output_case
    case.models["handoff"].transform = lambda e, good: raw if initial_planning(e) and e.correction is None else good
    assert run(case).stop_reason == "search_exhausted"
    calls = [e for e in case.models["handoff"].calls if initial_planning(e)]
    assert len(calls) == 2 and calls[1].correction["issues"][0]["code"] == code


@pytest.mark.parametrize("response,code", [
    (D8ModelResponse('{"intents":', "length"), "output_truncated"),
    (" " * 65537, "output_size_exceeded"),
    ("[" * 33 + "0" + "]" * 33, "output_depth_exceeded")], ids=["truncated", "oversized", "too_deep"])
def test_explicit_terminal_output_failures_never_correct(output_case, response, code):
    case = output_case
    case.models["handoff"].transform = lambda _, __: response
    result = run(case)
    assert code in result.stop_reason
    assert len(case.models["handoff"].calls) == 1
    assert case.sandbox.command_calls == []
    reopen(case)
    assert code in run(case).stop_reason
    assert len(case.models["handoff"].calls) == 1


def test_added_envelope_capacity_blocks_before_model_admission(output_case):
    case = output_case
    original = case.capacity.fits
    case.capacity.fits = lambda request, policy: False if isinstance(request, D8OutputRequest) else original(request, policy)
    assert "capacity_paused" in run(case).stop_reason
    assert all(not model.calls for model in case.models.values())
    assert dict(case.app.store.d4_budget_usage(case.task).category_counts)["planning"] == 0


def test_saved_format_diagnostic_recovers_lost_native_result_without_reasking(output_case):
    case = output_case
    case.models["handoff"].transform = lambda e, raw: {**raw, "intents": {}} if initial_planning(e) and e.correction is None else raw
    original = case.app.handoff.evidence.save
    def lost(kind, identity, payload):
        if kind == "result":
            raise ProcessLoss()
        return original(kind, identity, payload)
    case.app.handoff.evidence.save = lost
    with pytest.raises(ProcessLoss):
        run(case)
    first = case.models["handoff"].calls[0].request.model_request_id
    assert case.app.output_boundary.diagnostic(first) is not None
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    calls = [e for e in case.models["handoff"].calls if initial_planning(e)]
    assert len(calls) == 2 and calls[0].request.model_request_id == first


def test_d5_format_correction_charged_and_committed_by_correction_id(output_case):
    case = output_case
    case.models["inner"].transform = lambda e, raw: {**raw, "review_requested": True} if e.output_contract["stage"] == "inner_size" else raw
    case.models["refinement"].transform = lambda e, raw: {**raw, "claims": {}} if e.correction is None else raw
    assert run(case).stop_reason == "search_exhausted"
    assert case.models["refinement"].calls
    correction_calls = [e for e in case.models["refinement"].calls if e.correction is not None]
    assert correction_calls
    facts = case.app.store.list_process_facts(case.task)
    assert facts
    for fact in facts:
        root = case.app.store.d8_refinement_root(fact.model_request_id)
        assert root is not None
        assert case.app.refinement.evidence.read_result(root)["outcome"] == "invalid"
        assert case.app.store.load_d4_operation(fact.model_request_id).application_ref == fact.process_fact_id
    used = dict(case.app.store.d4_budget_usage(case.task).category_counts)["refinement"]
    assert used == len(case.models["refinement"].calls) <= case.app.config.d4_budget.refinement
    before = len(case.models["refinement"].calls)
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert len(case.models["refinement"].calls) == before


def test_d5_business_rejection_does_not_gain_format_retry(output_case):
    case = output_case
    case.models["inner"].transform = lambda e, raw: {**raw, "review_requested": True} if e.output_contract["stage"] == "inner_size" else raw
    def unsupported(envelope, raw):
        return {**raw, "claims": [{**raw["claims"][0], "source_refs": [str(uuid4())]}]}
    case.models["refinement"].transform = unsupported
    assert run(case).stop_reason == "search_exhausted"
    assert case.models["refinement"].calls
    assert all(e.correction is None for e in case.models["refinement"].calls)
    assert not case.app.store.list_process_facts(case.task)


@pytest.mark.parametrize("value", [True, "1", None])
def test_integer_type_is_not_coerced(value):
    schema = output_schema(SimpleNamespace(stage="inner_plan"))[1]
    raw = {"kind": "conclusion_change", "operation": "withdraw",
           "targets": [{"root_id": str(uuid4()), "version": value, "ref": "fact-ref"}],
           "replacement_ref": None, "content": None, "evidence_refs": [], "reason": "saved reason",
           "resolution": None, "relation_kind": None, "relation_owner_id": None}
    parsed, issues, terminal, _ = decode_response(raw, schema)
    assert parsed is None and terminal is None
    assert any(i.path == "$/targets/0/version" and i.expected == "integer" for i in issues)


@pytest.mark.parametrize("stage,field", [("inner_plan", "source_proof_id"),
    ("fact_proposal", "evidence_refs"), ("fact_review", "decision"),
    ("progress_review", "progress"), ("handoff_summary", "open_questions")])
def test_each_role_stage_corrects_its_own_format_and_preserves_logical_decisions(output_case, stage, field):
    case = output_case
    def malformed(e, raw):
        return {**raw, field: {"unexpected": "private-body"}} if e.output_contract["stage"] == stage and e.correction is None else raw
    for model in case.models.values():
        model.transform = malformed
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    corrections = [e for m in case.models.values() for e in m.calls
                   if e.output_contract["stage"] == stage and e.correction is not None]
    assert corrections
    assert len(case.app.sessions.records()) == 5
    for e in corrections:
        assert e.correction["issues"]
    assert "private-body" not in "".join(p.read_text(encoding="utf-8") for p in case.directory.rglob("*.json"))


def test_transport_unknown_never_gains_a_format_retry(output_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    case = output_case
    case.models["handoff"].transform = lambda _, __: ModelError(ModelErrorKind.TIMED_OUT, "no reliable response")
    assert run(case).recovery_required
    assert len(case.models["handoff"].calls) == 1
    reopen(case)
    assert run(case).recovery_required
    assert len(case.models["handoff"].calls) == 1


def test_diagnostic_write_failure_stops_and_unknown_resume_does_not_reask(output_case):
    case = output_case
    case.models["handoff"].transform = lambda e, raw: {**raw, "intents": {}} if initial_planning(e) else raw
    original = case.app.output_boundary.evidence.save
    def failed(kind, identity, value):
        if kind == "diagnostic":
            raise OSError("private exception must not be saved")
        return original(kind, identity, value)
    case.app.output_boundary.evidence.save = failed
    assert run(case).recovery_required
    assert len(case.models["handoff"].calls) == 1
    reopen(case)
    assert run(case).recovery_required
    assert len(case.models["handoff"].calls) == 1
    assert case.sandbox.command_calls == []


def test_d5_exhausted_quota_keeps_original_invalid_without_correction(output_case):
    from agent_core.operation_accounting import D4OperationStart, D4OperationCompletion
    case = output_case
    limit = case.app.config.d4_budget.refinement
    for _ in range(limit - 1):
        identity = str(uuid4())
        case.app.store.begin_d4_operation(D4OperationStart(identity, case.task, "refinement", case.clock(),
            case.app.state.latest_checkpoint.sequence, "0" * 64, case.task,
            case.app.config.d4_budget.budget_policy_ref, "outer_planning", case.app.config.outer_role.role_config_ref))
        case.app.store.complete_d4_operation(D4OperationCompletion(identity, case.clock(), "failed", identity, "0" * 64))
    case.models["inner"].transform = lambda e, raw: {**raw, "review_requested": True} if e.output_contract["stage"] == "inner_size" else raw
    case.models["refinement"].transform = lambda _, raw: {**raw, "claims": {}}
    assert run(case).stop_reason == "search_exhausted"
    assert len(case.models["refinement"].calls) == 1
    assert dict(case.app.store.d4_budget_usage(case.task).category_counts)["refinement"] == limit
    assert not case.app.store.list_process_facts(case.task)
    request = case.models["refinement"].calls[0].request
    assert case.app.refinement.evidence.read_result(request.attempt.model_request_id)["outcome"] == "invalid"


def test_schema26_readonly_and_upgrade_keep_v5_identity_and_history(session_case):
    import sqlite3
    from agent_core.sqlite_control_store import SqliteControlStore
    c = session_case
    assert asyncio.run(c[0].run(c[6])).stop_reason == "search_exhausted"
    path, config = c[0].directory / "control.sqlite", c[0].config
    decisions = c[0].store.list_d8_decisions(c[8])
    c[0].store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE d8_refinement_process_facts")
        connection.execute("DROP TABLE d8_refinement_corrections")
        connection.execute("DELETE FROM schema_versions WHERE version=27")
    before = path.read_bytes()
    with SqliteControlStore.open_readonly(path) as readonly:
        assert readonly._schema_version == 26
        assert readonly.load_d3_run_config(c[8]) == config
        assert readonly.list_d8_decisions(c[8]) == decisions
        assert readonly.d8_refinement_root(str(uuid4())) is None
    assert path.read_bytes() == before
    c[0].store = SqliteControlStore.open(path, clock=c[5])
    assert c[0].store._schema_version == 27
    assert c[0].store.load_d3_run_config(c[8]).contract_version == 5
    assert c[0].store.list_d8_decisions(c[8]) == decisions
    assert c[0].store._connection.execute("SELECT count(*) FROM d8_refinement_corrections").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["conclusion_repair", "change_review"])
def test_revision_format_correction_uses_frozen_actor_and_reviewer(output_case, stage):
    from agent_core.modeling import ModelError, ModelErrorKind
    case = output_case
    case.models["inner"].transform = lambda e, raw: ModelError(ModelErrorKind.INVALID_RESPONSE, "known stop") if e.output_contract["stage"] == "inner_complete" else raw
    run(case)
    fact = next(i for i in case.app.handoff.history() if i.record_type == "verified_fact")
    actor = next(i.intent_id for i in case.app.state.intents if i.status.value == "running")
    change = {"kind": "conclusion_change", "operation": "withdraw",
        "targets": [{"root_id": fact.logical_root_id, "version": fact.revision, "ref": fact.last_version_ref}],
        "replacement_ref": None, "content": None, "evidence_refs": [],
        "reason": "Review this saved observation adoption.", "resolution": None,
        "relation_kind": None, "relation_owner_id": None}
    def inner(e, raw):
        if e.output_contract["stage"] == "conclusion_repair":
            return {**change, "targets": {}} if stage == "conclusion_repair" and e.correction is None else change
        return raw
    def reviewer(e, raw):
        if e.output_contract["stage"] == "change_review":
            return {"decision": "reject", "reason": "Original observation remains applicable.",
                    "goal_supported": {} if stage == "change_review" and e.correction is None else None,
                    "scope_explanation": None}
        return raw
    case.models["inner"].transform, case.models["fact"].transform = inner, reviewer
    before = len(case.sandbox.command_calls)
    identity = str(uuid4())
    record = asyncio.run(case.app.repair_conclusions(actor, identity, case.token))
    assert record.decision == "reject"
    calls = [e for m in case.models.values() for e in m.calls if e.output_contract["stage"] == stage]
    assert len(calls) == 2 and calls[1].correction is not None
    counts = {name: len(m.calls) for name, m in case.models.items()}
    reopen(case)
    assert asyncio.run(case.app.repair_conclusions(actor, identity, case.token)) == record
    assert counts == {name: len(m.calls) for name, m in case.models.items()}
    assert len(case.sandbox.command_calls) == before


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stop_during_invalid_response_prevents_correction_and_application(output_case, stop):
    case = output_case
    def invalid(e, raw):
        if stop == "cancel":
            case.token.is_cancelled = True
        else:
            case.clock.now = case.app.state.task_run.deadline_at
        return {**raw, "intents": {}}
    case.models["handoff"].transform = invalid
    result = run(case)
    if stop == "cancel":
        assert result.stop_reason == "task_run_cancelled"
    else:
        assert case.clock() >= case.app.state.task_run.deadline_at
        assert result.recovery_required  # Existing stale planning boundary; no new stop semantics.
    request = case.models["handoff"].calls[0].request
    assert case.app.handoff.evidence.read("result", request.model_request_id)["outcome"] == "stale"
    assert len(case.models["handoff"].calls) == 1
    assert not case.app.state.turns and not case.sandbox.command_calls
    assert len(list((case.directory / "d8-output").glob("*diagnostic*.json"))) == 1


def test_schema27_migration_failure_rolls_back_both_new_tables(tmp_path, monkeypatch):
    import sqlite3
    import agent_core.sqlite_control_store as module
    from agent_core.control_store import StorageError
    path = tmp_path / "migration.sqlite"
    with module.SqliteControlStore.open(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE d8_refinement_process_facts")
        db.execute("DROP TABLE d8_refinement_corrections")
        db.execute("DELETE FROM schema_versions WHERE version=27")
    original = module._sql_statements
    def interrupted(script):
        statements = original(script)
        return [statements[0], "INVALID SQL"] if script == module.D8_OUTPUT_SCHEMA else statements
    monkeypatch.setattr(module, "_sql_statements", interrupted)
    with pytest.raises(StorageError):
        module.SqliteControlStore.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT max(version) FROM schema_versions").fetchone()[0] == 26
        assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('d8_refinement_corrections', 'd8_refinement_process_facts')").fetchall() == []


def test_unpaired_unicode_and_nonfinite_exponents_fail_without_exception():
    for value in ['{"reason":"\\ud800"}', '{"reason":1e400}', {"reason": float("inf")}]:
        parsed, issues, terminal, _ = decode_response(value, {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]})
        assert parsed is None and issues and terminal is None


def test_semantic_retry_and_format_retry_share_one_opportunity(output_case):
    case = output_case
    def response(e, raw):
        if initial_planning(e):
            if case.app.config.contract_version in {12, 13, 14} and e.correction is None:
                return {**raw, 'intents': [{**raw['intents'][0], 'description': ' '}]}
            return {**raw, "turn_target": "Different goal"} if e.correction is None else {**raw, "intents": {}}
        return raw
    case.models["handoff"].transform = response
    assert run(case).stop_reason != "search_exhausted"
    assert len(case.models["handoff"].calls) == 2
    expected = "output_business_invalid" if case.app.config.contract_version in {7, 8, 9, 10, 11, 12, 13, 14} else "proposal_rejected"
    assert case.models["handoff"].calls[1].correction["reason"] == expected
    assert not case.app.state.turns and not case.sandbox.command_calls


def test_d5_known_correction_result_survives_process_loss_without_recall(output_case, monkeypatch):
    case = output_case
    case.models["inner"].transform = lambda e, raw: {**raw, "review_requested": True} if e.output_contract["stage"] == "inner_size" else raw
    case.models["refinement"].transform = lambda e, raw: {**raw, "claims": {}} if e.correction is None else raw
    def lost(*args):
        raise ProcessLoss()
    monkeypatch.setattr(case.app.controller, "commit_process_fact", lost)
    with pytest.raises(ProcessLoss):
        run(case)
    calls = case.models["refinement"].calls
    assert len(calls) == 2 and calls[-1].correction is not None
    correction_id = calls[-1].request.attempt.model_request_id
    assert case.app.refinement.evidence.read_result(correction_id)["outcome"] == "accepted"
    assert not case.app.store.list_process_facts(case.task)
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert any(f.model_request_id == correction_id for f in case.app.store.list_process_facts(case.task))
    ids = [e.request.attempt.model_request_id for e in calls]
    assert len(ids) == len(set(ids))


def test_d5_unknown_correction_is_not_resent_after_restart(output_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    case = output_case
    case.models["inner"].transform = lambda e, raw: {**raw, "review_requested": True} if e.output_contract["stage"] == "inner_size" else raw
    case.models["refinement"].transform = lambda e, raw: {**raw, "claims": {}} if e.correction is None else ModelError(ModelErrorKind.TIMED_OUT, "unknown")
    run(case)
    corrections = [e for e in case.models["refinement"].calls if e.correction is not None]
    assert corrections
    ids = [e.request.attempt.model_request_id for e in corrections]
    for identity in ids:
        assert case.app.refinement.evidence.read_result(identity)["outcome"] == "response_unknown"
    reopen(case)
    run(case)
    for identity in ids:
        assert sum(e.request.attempt.model_request_id == identity for e in case.models["refinement"].calls) == 1
    assert not case.app.store.list_process_facts(case.task)
