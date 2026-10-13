"""D8-3 observable behavior with real SQLite/evidence and Fake roles/tools."""

import asyncio
import json
from dataclasses import asdict
from uuid import uuid4

import pytest

from agent_adapters.multi_turn_application import D3Application
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.conclusion_revision_contracts import D8ChangeRequest, D8ConclusionChange
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3Authorization, D3RecoveryRequired, D3RunConfig
from agent_core.handoff_contracts import D8CapacityPolicy, D8HandoffConfig
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4RecoveryRequired, D8HandoffBudgetPolicy
from agent_core.turn_contracts import D1RoleConfig
from test_d8_handoff import HandoffModel
from test_d8_conclusion_review import Inner, FactReviewer, Refinement
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token


class RevisionInner(Inner):
    def __init__(self, role):
        super().__init__(role)
        self.change_calls, self.responses, self.hold = [], [], True
        self.normal_change = None

    def invoke(self, request, timeout, token):
        if isinstance(request, D8ChangeRequest):
            self.change_calls.append(request)
            response = self.responses.pop(0)
            return response(request) if callable(response) else response
        if self.normal_change is not None and request.stage in {"inner_plan", "inner_complete"}:
            response = self.normal_change(request)
            if response is not None:
                self.normal_change = None
                return response
        if self.hold and request.stage == "inner_complete":
            packet = json.loads(request.history_context)
            if len([i for i in packet["items"] if i["record_type"] == "verified_fact"]) >= 2:
                return ModelError(ModelErrorKind.INVALID_RESPONSE, "Fake checkpoint after two saved observations")
        return super().invoke(request, timeout, token)


class RevisionReviewer(FactReviewer):
    def __init__(self, role):
        super().__init__(role)
        self.change_calls = []
        self.change_response = lambda request: {"decision": "accept", "reason": "Supports this scoped change.",
                                                 "goal_supported": True, "scope_explanation": None}

    def invoke(self, request, timeout, token):
        if isinstance(request, D8ChangeRequest):
            self.change_calls.append(request)
            return self.change_response(request)
        return super().invoke(request, timeout, token)


@pytest.fixture
def revision_case(tmp_path, request):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D8-3 authorized local copy")
    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-d8-3-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "same-model", "inner-d8-3-v1")
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 131072, 8192)
    config = D3RunConfig(outer_role, inner_role, D3Authorization(str(uuid4()), (0,)),
                         outer_role.role_config_ref, contract_version=4,
                         d4_budget=D8HandoffBudgetPolicy(), d5_enabled=True,
                         d5_sensitive_requirements=(), d8_enabled=True,
                         handoff_config=D8HandoffConfig(policy, policy))
    sandbox, clock, token = Sandbox(), Clock(), Token()
    inner, handoff, reviewer = RevisionInner(inner_role), HandoffModel(outer_role), RevisionReviewer(outer_role)
    handoff.modify = lambda req, raw: ({**raw, "intents": raw["intents"][:1]}
                                       if req.stage == "tree_planning" and req.turn_id is None and raw["kind"] == "candidates"
                                       else raw)
    mode = getattr(request, "param", None)
    if mode == "inference":
        normal = inner.response
        inner.response = lambda req: {**normal(req), "assertion": None} if len(inner.fact_calls) == 2 else normal(req)
    if mode == "dependency":
        normal = handoff.modify
        def declare_dependency(req, raw):
            raw = normal(req, raw)
            if req.stage == "tree_planning" and req.turn_id is not None and raw["kind"] == "candidates":
                fact = next(i for i in req.history if i.record_type == "verified_fact")
                raw = {**raw, "intents": [{**i, "dependency_refs": [fact.ref]} for i in raw["intents"]]}
            return raw
        handoff.modify = declare_dependency
    if mode in {"normal_plan", "normal_action", "normal_sensitive"}:
        def normal_change(req):
            stage = "inner_complete" if mode == "normal_action" else "inner_plan"
            if req.stage != stage:
                return None
            facts = [i for i in json.loads(req.history_context)["items"] if i["record_type"] == "verified_fact"]
            if not facts:
                return None
            item = facts[0]
            return {"kind": "conclusion_change", "operation": "withdraw",
                    "targets": [{"root_id": item["logical_root_id"], "version": item["revision"],
                                 "ref": item["last_version_ref"]}],
                    "replacement_ref": None, "content": None, "evidence_refs": [],
                    "reason": "password=example-d8-3-secret" if mode == "normal_sensitive" else "Review this observation adoption.",
                    "resolution": None, "relation_kind": None, "relation_owner_id": None}
        inner.normal_change = normal_change
        reviewer.change_response = lambda req: {"decision": "reject", "reason": "The original observation remains usable.",
                                                 "goal_supported": None, "scope_explanation": None}

    def create():
        return D3Application(tmp_path / "run", config=config, sandbox=sandbox,
                             import_sandbox=sandbox, workspace_sandbox=sandbox,
                             command_sandbox=sandbox, outer_model=Model(outer_role), inner_model=inner,
                             review_model=ReviewModel(outer_role), fact_review_model=reviewer,
                             refinement_model=Refinement(outer_role), handoff_model=handoff,
                             capacity_port=FakeUnicodeCapacity(), clock=clock)
    app = create()
    task_id = app.start(TaskRequest.from_dict({"goal": "Observe the authorized local copy",
                                               "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}), source)
    result = asyncio.run(app.run(token))
    if result.stop_reason == "d8_context_paused:initial_history":
        assert app.handoff.pause_report["known_needed"] > 8192
        app.handoff.increase_capacity({"initial_history": 32768, "visible_history": 65536})
        result = asyncio.run(app.run(token))
    assert result.recovery_required
    assert len(app.state.facts) == (1 if mode == "normal_sensitive" else 2), result.stop_reason
    actor = next(i for i in app.state.intents if i.status.value == "running")
    case = [app, inner, reviewer, sandbox, clock, token, create, task_id, actor.intent_id]
    yield case
    case[0].store.close()


def history(case):
    return case[0].handoff.history()


def target(case, root):
    item = next(i for i in history(case) if getattr(i, "logical_root_id", None) == root)
    return {"root_id": root, "version": item.revision, "ref": item.last_version_ref}


def action(case, operation, roots, **updates):
    value = {"kind": "conclusion_change", "operation": operation, "targets": [target(case, r) for r in roots],
             "replacement_ref": None, "content": None, "evidence_refs": [],
             "reason": "The previous adoption needs the stated scoped change.", "resolution": None,
             "relation_kind": None, "relation_owner_id": None}
    return {**value, **updates}


def repair(case, raw, request_id=None):
    case[1].responses.append(raw)
    return asyncio.run(case[0].repair_conclusions(case[8], request_id or str(uuid4()), case[5]))


def reopen(case):
    case[0].store.close()
    case[0] = case[6]()
    case[0].controller.attach(case[7])
    case[0]._bound_task_run_id = case[7]
    return case[0]


def test_verified_correction_keeps_old_fact_and_blocks_parent_until_goal_reapproval(revision_case):
    c = revision_case
    old, new = c[0].state.facts
    old_bytes = tuple(asdict(f) for f in c[0].state.facts)
    new_ref = next(i.ref for i in history(c) if i.record_id == new.fact_id)
    before_tools = len(c[3].command_calls)
    record = repair(c, action(c, "correct", [old.fact_id], replacement_ref=new_ref))
    assert record.decision == "accept"
    view = next(i for i in history(c) if i.record_id == old.fact_id)
    assert view.current_status == "superseded" and view.effective_ref == new_ref
    assert view.effective_version_id == record.record_id
    assert tuple(asdict(f) for f in c[0].state.facts) == old_bytes
    assert len(c[3].command_calls) == before_tools
    assert c[0].revisions._dirty_turns() == ()
    with pytest.raises(D3RecoveryRequired, match="support"):
        c[0]._check_dispatch()
    parent = next(i for i in c[0].state.intents if i.status.value == "succeeded")
    repair(c, action(c, "replace_support", [old.fact_id], replacement_ref=new_ref,
                     relation_kind="parent_support", relation_owner_id=parent.intent_id))
    assert c[2].change_calls[-1].original_goal == parent.description
    assert next(i for i in c[0].state.intents if i.intent_id == parent.intent_id).status.value == "succeeded"
    c[0]._check_dispatch()
    reopen(c)
    c[0]._check_dispatch()
    c[1].hold = False
    result = asyncio.run(c[0].run(c[5]))
    assert result.stop_reason == "search_exhausted", result.stop_reason
    assert len(c[3].command_calls) == before_tools


def test_rejected_attempt_preserves_effective_version_and_is_persisted(revision_case):
    c = revision_case
    fact = c[0].state.facts[0]
    before = next(i for i in history(c) if i.record_id == fact.fact_id)
    c[2].change_response = lambda req: {"decision": "reject", "reason": "The observation is still usable.",
                                      "goal_supported": None, "scope_explanation": None}
    record = repair(c, action(c, "withdraw", [fact.fact_id]))
    assert record.decision == "reject" and len(c[0].revisions.records()) == 1
    assert next(i for i in history(c) if i.record_id == fact.fact_id) == before
    assert c[0].revisions._dirty_turns() == ()


def test_withdraw_restore_appends_new_version_and_requires_support_reapproval(revision_case):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    original = target(c, root)["ref"]
    repair(c, action(c, "withdraw", [root]))
    withdrawn = next(i for i in history(c) if i.record_id == root)
    assert withdrawn.effective_ref is None and withdrawn.effective_version_id is None
    with pytest.raises(D3RecoveryRequired, match="support"):
        c[0]._check_dispatch()
    restored = repair(c, action(c, "restore", [root], replacement_ref=original,
                                 reason="The withdrawal concern was resolved by rechecking the saved source."))
    row = next(i for i in history(c) if i.record_id == root)
    assert row.current_status == "effective" and row.revision == 3
    assert row.effective_version_id == restored.record_id
    assert c[2].change_calls[-1].prior_changes[0].operation == "withdraw"
    with pytest.raises(D3RecoveryRequired, match="reapproval"):
        c[0]._check_dispatch()
    parent = next(i for i in c[0].state.intents if i.status.value == "succeeded")
    repair(c, action(c, "replace_support", [root], replacement_ref=original,
                     relation_kind="parent_support", relation_owner_id=parent.intent_id))
    c[0]._check_dispatch()
    assert len(c[0].revisions.records()) == 3


def test_conflict_resolution_different_versions_requires_explanation(revision_case):
    c = revision_case
    roots = [f.fact_id for f in c[0].state.facts]
    repair(c, action(c, "report_conflict", roots))
    assert all(i.conflict for i in history(c) if i.record_id in roots)
    raw = action(c, "resolve_conflict", roots, resolution="different_scope")
    with pytest.raises(D3RecoveryRequired, match="explanation"):
        repair(c, raw)
    c[2].change_response = lambda req: {"decision": "accept", "reason": "Both scoped observations remain supported.",
                                      "goal_supported": None,
                                      "scope_explanation": "They describe distinct saved copy versions, not every version."}
    repair(c, raw)
    assert all(not i.conflict for i in history(c) if i.record_id in roots)
    assert all(i.identity == "verified_observation_only" for i in history(c) if i.record_id in roots)


def test_stale_target_cannot_reuse_an_old_approval(revision_case):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    old = action(c, "withdraw", [root])
    repair(c, old)
    calls = len(c[2].change_calls)
    with pytest.raises(D3RecoveryRequired, match="head"):
        repair(c, old)
    assert len(c[2].change_calls) == calls
    assert len(c[0].revisions.records()) == 1


def test_unknown_change_call_never_reissues_after_reopen(revision_case):
    c = revision_case
    request_id = str(uuid4())
    def unknown(request):
        raise RuntimeError("Fake lost provider result")
    c[1].responses.append(unknown)
    with pytest.raises((D3RecoveryRequired, D4RecoveryRequired), match="unknown"):
        asyncio.run(c[0].repair_conclusions(c[8], request_id, c[5]))
    before = len(c[1].change_calls)
    reopen(c)
    with pytest.raises((D3RecoveryRequired, D4RecoveryRequired), match="unknown"):
        asyncio.run(c[0].repair_conclusions(c[8], request_id, c[5]))
    assert len(c[1].change_calls) == before


def test_repair_entry_cannot_dispatch_probe_and_only_corrects_format_once(revision_case):
    c = revision_case
    before = len(c[3].command_calls)
    c[1].responses.extend([{"kind": "probe"}, {"kind": "probe"}])
    with pytest.raises(D3RecoveryRequired, match="model_invalid"):
        asyncio.run(c[0].repair_conclusions(c[8], str(uuid4()), c[5]))
    assert len(c[1].change_calls) == 2
    assert len(c[3].command_calls) == before
    for call in c[1].change_calls:
        assert c[0].revisions.evidence.read("result", call.model_request_id)["raw"] is None


@pytest.mark.parametrize("revision_case", ["inference"], indirect=True)
def test_inference_versions_and_verified_link_preserve_identity(revision_case):
    c = revision_case
    inference = next(r for r in c[0].store.list_d8_conclusions(c[7]) if r.outcome == "reviewed_inference")
    fact = c[0].state.facts[0]
    fact_ref = next(i.ref for i in history(c) if i.record_id == fact.fact_id)
    original_facts = c[0].state.facts
    first = repair(c, action(c, "correct", [inference.record_id], content="A scoped, reviewed interpretation.",
                            evidence_refs=[fact_ref]))
    second = repair(c, action(c, "correct", [inference.record_id], content="A revised interpretation, still unverified.",
                             evidence_refs=[fact_ref]))
    old = next(i for i in history(c) if i.record_id == first.record_id)
    new = next(i for i in history(c) if i.record_id == second.record_id)
    assert old.current_status == "superseded" and new.current_status == "effective"
    assert new.revision == 3 and new.logical_root_id == inference.record_id
    assert new.identity == "reviewed_inference_not_verified"
    repair(c, action(c, "link_verified", [inference.record_id], replacement_ref=fact_ref))
    new = next(i for i in history(c) if i.record_id == second.record_id)
    assert new.validated_by_refs == (fact_ref,) and new.record_type == "reviewed_inference"
    assert c[0].state.facts == original_facts
    c[0]._check_dispatch()


@pytest.mark.parametrize("revision_case", ["inference"], indirect=True)
def test_resolving_one_pair_does_not_release_another_conflict(revision_case):
    c = revision_case
    a, b = [f.fact_id for f in c[0].state.facts]
    d = next(r.record_id for r in c[0].store.list_d8_conclusions(c[7]) if r.outcome == "reviewed_inference")
    repair(c, action(c, "report_conflict", [a, b]))
    repair(c, action(c, "report_conflict", [a, d]))
    repair(c, action(c, "resolve_conflict", [a, b], resolution="keep_first"))
    views = {i.record_id: i for i in history(c)}
    assert views[a].conflict and views[d].conflict
    assert views[b].current_status == "withdrawn" and not views[b].conflict
    assert views[a].effective_version_id is None
    with pytest.raises(D3RecoveryRequired):
        c[0]._check_dispatch()


@pytest.mark.parametrize("revision_case", ["dependency"], indirect=True)
def test_explicit_dependency_needs_separate_approval_from_parent_support(revision_case):
    c = revision_case
    a, b = c[0].state.facts
    new_ref = next(i.ref for i in history(c) if i.record_id == b.fact_id)
    parent = next(i for i in c[0].state.intents if i.status.value == "succeeded")
    repair(c, action(c, "correct", [a.fact_id], replacement_ref=new_ref))
    repair(c, action(c, "replace_support", [a.fact_id], replacement_ref=new_ref,
                     relation_kind="parent_support", relation_owner_id=parent.intent_id))
    with pytest.raises(D3RecoveryRequired, match="binding"):
        c[0]._check_dispatch()
    repair(c, action(c, "replace_support", [a.fact_id], replacement_ref=new_ref,
                     relation_kind="intent_dependency", relation_owner_id=c[8]))
    c[0]._check_dispatch()
    assert c[0].handoff._dependencies(c[8]) == (new_ref,)


@pytest.mark.parametrize("boundary", ["completion", "commit", "reload", "transaction"])
def test_saved_change_survives_fault_without_recalling_models(revision_case, monkeypatch, boundary):
    from agent_core.control_store import StorageError
    import agent_core.sqlite_control_store as schema
    c, fired = revision_case, False
    app = c[0]
    if boundary == "completion":
        original = app.store.complete_d4_operation
        def interrupt(value):
            nonlocal fired
            op = app.store.load_d4_operation(value.operation_id)
            if not fired and op.start.category == "fact_review":
                fired = True
                raise StorageError("Fake completion fault")
            return original(value)
        monkeypatch.setattr(app.store, "complete_d4_operation", interrupt)
    elif boundary == "commit":
        original = app.controller.commit_d3_transition
        def interrupt(operation, *args, **kwargs):
            nonlocal fired
            if not fired and operation == "append_d8_revision":
                fired = True
                raise StorageError("Fake control commit fault")
            return original(operation, *args, **kwargs)
        monkeypatch.setattr(app.controller, "commit_d3_transition", interrupt)
    elif boundary == "reload":
        original = app.controller._reload_after_commit
        def interrupt(commit=None):
            nonlocal fired
            if not fired and commit is not None and commit.event.details["operation"] == "append_d8_revision":
                fired = True
                raise StorageError("Fake reload fault after durable commit")
            return original(commit)
        monkeypatch.setattr(app.controller, "_reload_after_commit", interrupt)
    else:
        original = schema.save_handoff_mutation
        def interrupt(store, mutation, sequence):
            nonlocal fired
            original(store, mutation, sequence)
            if not fired and mutation.operation == "append_d8_revision":
                fired = True
                raise StorageError("Fake fault after revision/application insertion")
        monkeypatch.setattr(schema, "save_handoff_mutation", interrupt)
    identity = str(uuid4())
    raw = action(c, "withdraw", [app.state.facts[0].fact_id])
    before_checkpoint = app.state.latest_checkpoint.sequence
    with pytest.raises((StorageError, D3RecoveryRequired)):
        repair(c, raw, identity)
    assert fired
    inner_count, reviewer_count = len(c[1].change_calls), len(c[2].change_calls)
    if boundary == "transaction":
        assert not app.revisions.records() and app.state.latest_checkpoint.sequence == before_checkpoint
        assert app.store.load_d4_operation(identity).application_ref is None
    reopen(c)
    record = asyncio.run(c[0].repair_conclusions(c[8], identity, c[5]))
    assert record.decision == "accept" and len(c[0].revisions.records()) == 1
    assert len(c[1].change_calls) == inner_count and len(c[2].change_calls) == reviewer_count
    assert c[0].revisions._dirty_turns() == ()


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stoppage_during_change_review_never_applies_approval(revision_case, stop):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    def stopped(req):
        if stop == "cancel":
            c[5].is_cancelled = True
        else:
            c[4].now = c[0].state.task_run.deadline_at + 1
        return {"decision": "accept", "reason": "Late Fake approval.", "goal_supported": None, "scope_explanation": None}
    c[2].change_response = stopped
    with pytest.raises(D3RecoveryRequired, match="stale"):
        repair(c, action(c, "withdraw", [root]))
    assert not c[0].revisions.records()
    assert next(i for i in history(c) if i.record_id == root).current_status == "effective"


def test_repair_reads_safe_history_without_dispatching_tools(revision_case):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    repair(c, action(c, "withdraw", [root]))
    tools = len(c[3].command_calls)
    identity = str(uuid4())
    first = repair(c, {"kind": "history_query", "query": {"record_type": "verified_fact"}}, identity)
    assert any(i["current_status"] == "withdrawn" for i in first["items"])
    assert all(i["identity"] == "verified_observation_only" for i in first["items"])
    calls = len(c[1].change_calls)
    replay = asyncio.run(c[0].repair_conclusions(c[8], identity, c[5]))
    assert replay == first and len(c[1].change_calls) == calls
    ref = first["items"][0]["ref"]
    details = repair(c, {"kind": "history_details", "refs": [ref]})
    assert details["items"][0]["start"] == 0 and details["items"][0]["end"] == details["items"][0]["total"]
    assert len(c[3].command_calls) == tools


def test_invalid_verified_text_correction_is_refused_before_review(revision_case):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    before = len(c[2].change_calls)
    ref = next(i.ref for i in history(c) if i.record_id == root)
    with pytest.raises(D3RecoveryRequired, match="text_cannot_create_fact"):
        repair(c, action(c, "correct", [root], content="A model-only fact claim.", evidence_refs=[ref]))
    assert len(c[2].change_calls) == before and not c[0].revisions.records()


def test_parent_replacement_requires_explicit_original_goal_support(revision_case):
    c = revision_case
    a, b = c[0].state.facts
    parent = next(i for i in c[0].state.intents if i.status.value == "succeeded")
    new_ref = next(i.ref for i in history(c) if i.record_id == b.fact_id)
    c[2].change_response = lambda req: {"decision": "accept", "reason": "The Fact itself is verified.",
                                      "goal_supported": False, "scope_explanation": None}
    with pytest.raises(D3RecoveryRequired, match="original_goal"):
        repair(c, action(c, "replace_support", [a.fact_id], replacement_ref=new_ref,
                         relation_kind="parent_support", relation_owner_id=parent.intent_id))
    assert not c[0].revisions.records()


@pytest.mark.parametrize("revision_case", ["normal_plan", "normal_action"], indirect=True)
def test_normal_plan_and_action_change_rejection_continues_without_replaying_request(revision_case):
    c = revision_case
    rows = c[0].revisions.records()
    assert len(rows) == 1 and rows[0].decision == "reject"
    assert len(c[3].command_calls) == 4
    assert all(i.revision == 1 for i in history(c) if i.record_type == "verified_fact")
    assert c[0].store.load_d3_operation(rows[0].proposal_request_id).application_ref == rows[0].record_id


@pytest.mark.parametrize("revision_case", ["normal_sensitive"], indirect=True)
def test_normal_change_sensitive_response_is_not_persisted_or_reviewed(revision_case):
    c = revision_case
    assert not c[0].revisions.records() and not c[2].change_calls
    files = list(c[0].directory.rglob("*.json"))
    assert files and all(b"example-d8-3-secret" not in p.read_bytes() for p in files)


def test_corrupt_change_evidence_never_falls_back_to_old_effective_version(revision_case):
    c = revision_case
    row = repair(c, action(c, "withdraw", [c[0].state.facts[0].fact_id]))
    path = c[0].revisions.evidence.records._path("d8-handoff-handoff", row.evidence_ref)
    path.write_bytes(b"{broken")
    before = len(c[1].change_calls)
    with pytest.raises(D3RecoveryRequired, match="unavailable"):
        history(c)
    assert len(c[1].change_calls) == before


def test_foreign_actor_and_foreign_replacement_are_refused(revision_case):
    c = revision_case
    with pytest.raises(D3RecoveryRequired, match="actor_scope"):
        asyncio.run(c[0].repair_conclusions(str(uuid4()), str(uuid4()), c[5]))
    before = len(c[2].change_calls)
    fake = "verified_fact:" + str(uuid4()) + ":" + "0" * 64
    with pytest.raises(D3RecoveryRequired, match="replacement_unavailable"):
        repair(c, action(c, "correct", [c[0].state.facts[0].fact_id], replacement_ref=fake))
    assert len(c[2].change_calls) == before and not c[0].revisions.records()


def test_borrowed_verified_version_withdrawal_invalidates_alias_without_overwriting_it(revision_case):
    c = revision_case
    a, b = c[0].state.facts
    ref = next(i.ref for i in history(c) if i.record_id == b.fact_id)
    repair(c, action(c, "correct", [a.fact_id], replacement_ref=ref))
    adopted_id = next(i.effective_version_id for i in history(c) if i.record_id == a.fact_id)
    repair(c, action(c, "withdraw", [b.fact_id]))
    rows = {i.record_id: i for i in history(c)}
    assert rows[a.fact_id].last_version_ref == ref
    assert rows[a.fact_id].effective_ref is None and rows[a.fact_id].effective_version_id is None
    assert rows[a.fact_id].committed_version_id == adopted_id
    assert rows[b.fact_id].current_status == "withdrawn"


def test_conflict_adjudication_preserves_statement_version_identity(revision_case):
    c = revision_case
    roots = [f.fact_id for f in c[0].state.facts]
    repair(c, action(c, "report_conflict", roots))
    c[2].change_response = lambda req: {"decision": "accept", "reason": "The limited observations are compatible.",
                                      "goal_supported": None, "scope_explanation": "Distinct saved copy versions; neither claims all versions."}
    row = repair(c, action(c, "resolve_conflict", roots, resolution="different_scope"))
    views = {i.record_id: i for i in history(c)}
    assert all(views[root].effective_version_id == root for root in roots)
    assert all(views[root].committed_version_id == row.record_id for root in roots)


@pytest.mark.parametrize("resolution", ["keep_second", "withdraw_both", "unresolved"])
def test_other_conflict_outcomes_are_explicit_and_do_not_change_fact_proofs(revision_case, resolution):
    c = revision_case
    roots = [f.fact_id for f in c[0].state.facts]
    original = c[0].state.facts
    repair(c, action(c, "report_conflict", roots))
    repair(c, action(c, "resolve_conflict", roots, resolution=resolution))
    views = {i.record_id: i for i in history(c)}
    if resolution == "keep_second":
        assert views[roots[0]].current_status == "withdrawn"
        assert views[roots[1]].current_status == "effective"
    elif resolution == "withdraw_both":
        assert all(views[root].current_status == "withdrawn" for root in roots)
    else:
        assert all(views[root].current_status == "conflicted" for root in roots)
    assert c[0].state.facts == original


@pytest.mark.parametrize("missing", ["redaction", "role"])
def test_repair_refuses_missing_configuration_before_admission(revision_case, missing):
    c = revision_case
    if missing == "redaction":
        c[0]._redaction_configured = False
    else:
        c[2].role_config = c[1].role_config
    before = c[0].store.d4_budget_usage(c[7])
    with pytest.raises(D3RecoveryRequired, match="configuration|role"):
        asyncio.run(c[0].repair_conclusions(c[8], str(uuid4()), c[5]))
    assert c[0].store.d4_budget_usage(c[7]) == before
    assert not c[1].change_calls and not c[2].change_calls


def test_revision_review_shares_eight_start_limit_and_never_gains_budget_on_reopen(revision_case):
    from agent_core.operation_accounting import D4BudgetExhausted, D4OperationStart, D4OperationCompletion
    c = revision_case
    counts = dict(c[0].store.d4_budget_usage(c[7]).category_counts)
    for _ in range(8 - counts.get("fact_review", 0)):
        identity = str(uuid4())
        c[0].store.begin_d4_operation(D4OperationStart(
            identity, c[7], "fact_review", c[4](), c[0].state.latest_checkpoint.sequence,
            "0" * 64, c[8], c[0].config.d4_budget.budget_policy_ref,
            "outer_planning", c[0].config.outer_role.role_config_ref))
        c[0].store.complete_d4_operation(D4OperationCompletion(identity, c[4](), "rejected", identity, "0" * 64))
    request_id = str(uuid4())
    with pytest.raises(D4BudgetExhausted):
        repair(c, action(c, "withdraw", [c[0].state.facts[0].fact_id]), request_id)
    assert not c[2].change_calls
    calls = len(c[1].change_calls)
    reopen(c)
    with pytest.raises(D4BudgetExhausted):
        asyncio.run(c[0].repair_conclusions(c[8], request_id, c[5]))
    assert len(c[1].change_calls) == calls
    assert dict(c[0].store.d4_budget_usage(c[7]).category_counts)["fact_review"] == 8
    assert c[0].config.d4_budget.total == 60


def test_withdraw_current_plan_support_cannot_be_repaired_by_rewriting_plan(revision_case):
    c = revision_case
    current = c[0].state.facts[1]
    plan = next(p for p in c[0].state.plans if p.intent_id == c[8])
    result = c[0].store.load_d3_plan_result(plan.plan_id)
    repair(c, action(c, "withdraw", [current.fact_id]))
    with pytest.raises(D3RecoveryRequired, match="conclusion"):
        c[0]._check_dispatch()
    assert c[0].store.load_d3_plan_result(plan.plan_id) == result
    with pytest.raises(ValueError, match="relation"):
        D8ConclusionChange.parse_action(action(c, "replace_support", [current.fact_id],
                                              replacement_ref=target(c, c[0].state.facts[0].fact_id)["ref"],
                                              relation_kind="plan_support", relation_owner_id=c[8]))


def test_old_read_snapshot_is_retained_but_not_currently_effective_after_withdrawal(revision_case):
    c = revision_case
    root = c[0].state.facts[0].fact_id
    ref = target(c, root)["ref"]
    first = c[0].handoff.read_history(refs=[ref])
    repair(c, action(c, "withdraw", [root]))
    assert c[0].handoff.resume_read(first["request_id"]) == first
    views = c[0].handoff.retrieved_for(c[8])
    matching = next(v for v in views if v["read"]["request_id"] == first["request_id"])
    assert matching["read"]["items"][0]["current_status"] == "effective"
    assert matching["current_applicability"][0]["current_effectiveness"] == "withdrawn"
