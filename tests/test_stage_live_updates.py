"""Live operator updates and batch claims through the real local stage boundary."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest

from test_stage_application import case
from agent_core import operator_controls
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_contracts import PlanningSession, StageFactBatch, fact_review_parts
from agent_core.stage_storage import StageStateError


def test_user_hint_is_idempotent_next_request_context_not_verified_fact(case):
    submitted = []
    def transform(request, raw):
        if request.stage == "stage_action" and not submitted:
            assert not any(i.identity == "user_hint_not_verified" for i in request.context.history)
            identity = str(uuid4())
            payload = {"content": "Check the actual dependency version; this suggestion is unverified."}
            with SqliteControlStore.open(case.path / "control.sqlite", clock=lambda: case.now.value) as store:
                before = store.load_task_run(case.app.task_id).deadline_at
                first = operator_controls.submit(store, case.app.task_id, identity, "user_hint", payload)
                assert operator_controls.submit(store, case.app.task_id, identity, "user_hint", payload) == first
                assert store.load_task_run(case.app.task_id).deadline_at == before
                with pytest.raises(ValueError, match="operator_operation_conflict"):
                    operator_controls.submit(store, case.app.task_id, identity, "user_hint", {"content": "changed"})
            submitted.append(identity)
            assert not any(i.identity == "user_hint_not_verified" for i in request.context.history)
        if raw.get("kind") == "fact_candidate":
            own = {i.ref for i in request.context.history if i.identity == "tool_observation_not_fact"}
            raw = {**raw, "support_refs": [ref for ref in raw["support_refs"] if ref in own]}
        return raw
    case.models.inner.transform = transform
    asyncio.run(case.app.run(case.token))
    subsequent = [r for r in case.models.inner.calls if r.stage == "stage_action"][1:]
    assert subsequent and all(any(i.identity == "user_hint_not_verified" for i in r.context.history) for r in subsequent)
    history = case.app.evidence.history(case.app.task_id)
    hints = [i for i in history if i.identity == "user_hint_not_verified"]
    assert len(hints) == 1 and json.loads(hints[0].content)["author"] == "local_operator"
    assert len([i for i in history if i.record_type == "verified_fact"]) == 1
    from agent_core.stage_planning import fact_support_refs
    assert all(hints[0].ref not in fact_support_refs(r, inferred=False) for r in subsequent)
    assert any(hints[0].ref == i["ref"] for i in subsequent[-1].history_catalog)


def test_task_extension_keeps_checkpoint_and_refuses_expired_new_commands(case):
    task_id = case.app.task_id
    checkpoint = case.app.state.latest_checkpoint
    before = case.app.state.task_run.deadline_at
    operation = str(uuid4())
    value = operator_controls.submit(case.app.store, task_id, operation, "extend_question", {"seconds": 1800})
    operator_controls.enable(case.app.controller)
    assert case.app.state.task_run.deadline_at == before + 1800
    assert case.app.state.latest_checkpoint == checkpoint
    assert operator_controls.submit(case.app.store, task_id, operation, "extend_question", {"seconds": 1800}) == value
    case.now.value = before + 1800
    for action, payload in (("extend_question", {"seconds": 60}), ("user_hint", {"content": "too late"})):
        with pytest.raises(ValueError, match="operator_task_expired_or_terminal"):
            operator_controls.submit(case.app.store, task_id, str(uuid4()), action, payload)
    assert operator_controls.submit(case.app.store, task_id, operation, "extend_question", {"seconds": 1800}) == value


def test_inflight_planning_survives_600_seconds_and_old_deadline_after_extension(case, monkeypatch):
    from agent_adapters import stage_evidence
    from agent_core.output_contracts import D8OutputRequest, D8ModelResponse
    from agent_core.stage_planning import model_contract
    tick = [0.0]
    monkeypatch.setattr(stage_evidence, "monotonic", lambda: tick[0])
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    received = []
    def invoke(envelope, timeout, token):
        received.append((timeout, token))
        entered.set()
        try:
            assert release.wait(3)
            assert not token.is_cancelled
            return D8ModelResponse("known response")
        finally:
            exited.set()
    case.models.inner.invoke = invoke
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    contract = model_contract(request)
    envelope = D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": contract["schema"]}, contract["instruction"], None)
    async def exercise():
        pending = asyncio.create_task(case.app.models._invoke(case.models.inner, envelope, 600,
            case.token, case.app.state.task_run.deadline_at))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert received[0][0] == 3600 and received[0][1].task_deadline_only
            tick[0] = 700; case.now.value += 700
            await asyncio.sleep(.04)
            assert not pending.done()
            operator_controls.submit(case.app.store, case.app.task_id, str(uuid4()), "extend_question", {"seconds": 300})
            tick[0] = 3700; case.now.value += 3000
            # The token itself rechecks the committed deadline, even before the
            # owner's next polling iteration. It never re-enters model.invoke.
            assert not received[0][1].is_cancelled
            release.set()
            assert (await asyncio.wait_for(pending, 2)).content == "known response"
            assert len(received) == 1
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["all", "partial", "mixed"])
@pytest.mark.parametrize("native", [False, True])
def test_batch_fact_review_is_one_call_with_independent_claim_binding(case, mode, native):
    case.models.inner.native = case.models.outer.native = native
    def inner(request, raw):
        if raw.get("kind") == "fact_candidate":
            claims = [{"content": "The controlled tool emitted observed.", "reason": "direct", "support_refs": raw["support_refs"], "inferred": False},
                {"content": "A separate supported observation." if mode != "mixed" else "A tentative explanation.",
                 "reason": "independent", "support_refs": raw["support_refs"], "inferred": mode == "mixed"}]
            return {"kind": "fact_candidates", "candidates": claims}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactBatch):
            return {"decisions": [{"decision": "accept", "reason": "supports own claim"},
                {"decision": "reject" if mode == "partial" else "accept", "reason": "per-claim judgment"}]}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    reviews = [r for r in case.models.outer.calls if isinstance(r.proposal, StageFactBatch)]
    assert len(reviews) == 1
    rows = case.app.store._connection.execute("SELECT fact_id,verification_ref FROM facts").fetchall()
    assert len(rows) == (2 if mode == "all" else 1)
    assert {fact_review_parts(row[1])[0] for row in rows} == {reviews[0].model_request_id}
    assert {fact_review_parts(row[1])[1] for row in rows} == ({0, 1} if mode == "all" else {0})
    history = case.app.evidence.history(case.app.task_id)
    assert len([i for i in history if i.record_type == "reviewed_inference"]) == (1 if mode == "mixed" else 0)
    assert not case.app.store.blocking_d4_operations(case.app.task_id)
    assert case.app.store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    case.app.models.reconcile(case.app.task_id)
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == len(rows)


def test_batch_accepted_claim_cannot_be_omitted_from_atomic_commit(case, monkeypatch):
    commit = case.app._commit
    def drop_claim(change, **kwargs):
        if change.facts:
            change = replace(change, facts=())
        return commit(change, **kwargs)
    monkeypatch.setattr(case.app, "_commit", drop_claim)
    with pytest.raises(StageStateError, match="accepted_fact_not_applied"):
        asyncio.run(case.app.run(case.token))
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_workbench_live_control_binds_run_and_retries_original_receipt(case, monkeypatch):
    from types import SimpleNamespace
    from agent_adapters.workbench_runner import Runner
    from agent_adapters.workbench_contracts import WorkbenchError
    directory = case.path / case.app.task_id
    directory.mkdir()
    with SqliteControlStore.open(directory / "control.sqlite", clock=lambda: case.now.value) as copied:
        case.app.store._connection.backup(copied._connection)
    runner = object.__new__(Runner)
    runner.workbench = SimpleNamespace(lock=threading.RLock())
    run = {"run_id": str(uuid4()), "status": "running", "directory": str(case.path)}
    monkeypatch.setattr(runner, "_current", lambda challenge: run)
    monkeypatch.setattr(runner, "_find_agent_directory", lambda parent: directory)
    value = {"run_id": run["run_id"], "operation_id": str(uuid4()), "action": "extend_question", "payload": {"seconds": 120}}
    first = runner.live_control("controlled-challenge", value)
    assert first["operator_control"]["question_deadline_at"] == case.app.state.task_run.deadline_at + 120
    run["status"] = "recovery_required"
    assert runner.live_control("controlled-challenge", value) == first
    with pytest.raises(WorkbenchError, match="operator_task_not_active"):
        runner.live_control("controlled-challenge", {**value, "operation_id": str(uuid4())})
    with pytest.raises(WorkbenchError, match="operator_run_changed"):
        runner.live_control("controlled-challenge", {**value, "run_id": str(uuid4())})


def test_frozen_history_survives_prompt_upgrade_but_cannot_dispatch_old_contract(case, monkeypatch):
    from agent_core import stage_planning
    from agent_adapters.stage_evidence import StageEvidence
    asyncio.run(case.app.run(case.token))
    original = case.models.inner.calls[0]
    record = case.app.store.load_d4_operation(original.model_request_id)
    before = case.app.evidence.read("contract", original.model_request_id)
    reader = StageEvidence(case.path / "stage-evidence", case.app.store, create=False)
    monkeypatch.setitem(stage_planning.RULES, original.stage, "A revised planning instruction for a new task.")
    assert reader.request(original.model_request_id) == original
    assert reader.read_model_result(record).result.description
    with pytest.raises(StageStateError, match="stage.model_contract_changed"):
        case.app.evidence.read_model_result(record)
    with pytest.raises(StageStateError, match="stage.model_contract_changed"):
        case.app.models._freeze(original)
    assert case.app.evidence.read("contract", original.model_request_id) == before


@pytest.mark.parametrize("native", [False, True])
def test_batch_source_error_uses_original_shared_correction_before_review(case, native):
    case.models.inner.native = case.models.outer.native = native
    def inner(request, raw):
        if raw.get("kind") == "fact_candidate":
            first = {key: raw[key] for key in ("content", "reason", "support_refs", "inferred")}
            second = {**first, "content": "Another direct observation.", "support_refs": first["support_refs"]
                if request.correction_of_model_request_id else ["invented-source"]}
            return {"kind": "fact_candidates", "candidates": [first, second]}
        return raw
    def outer(request, raw):
        return ({"decisions": [{"decision": "accept", "reason": "per-claim sources checked"} for _ in request.proposal.candidates]}
            if isinstance(request.proposal, StageFactBatch) else raw)
    case.models.inner.transform, case.models.outer.transform = inner, outer
    asyncio.run(case.app.run(case.token))
    reviews = [r for r in case.models.outer.calls if isinstance(r.proposal, StageFactBatch)]
    assert len(reviews) == 1
    corrected = case.app.evidence.request(reviews[0].proposal_operation_id)
    assert corrected.correction_of_model_request_id
    failed = case.app.store.load_d4_operation(corrected.correction_of_model_request_id)
    assert failed.completion.result_kind == "failed"
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_schema35_fact_trigger_migration_rolls_back_and_preserves_old_rows(case, monkeypatch):
    import sqlite3
    from agent_core import stage_storage
    from agent_core.control_store import StorageError
    asyncio.run(case.app.run(case.token))
    database = case.path / "old-copy.sqlite"
    with sqlite3.connect(database) as copy:
        case.app.store._connection.backup(copy)
        copy.execute("DROP TRIGGER facts_source_insert")
        copy.execute(stage_storage.FACT_SCHEMA[stage_storage.FACT_SCHEMA.index("CREATE TRIGGER facts_source_insert"):])
        copy.execute("DROP TABLE stage_plan_bindings")
        copy.execute("DROP TABLE stage_branches")
        copy.execute("DROP TABLE stage_review_queue")
        copy.execute("DELETE FROM schema_versions WHERE version>=35")
        before = copy.execute("SELECT * FROM facts").fetchall()
    with SqliteControlStore.open_readonly(database) as reader:
        assert reader._schema_version == 34 and reader._connection.execute("SELECT * FROM facts").fetchall()
        assert reader._connection.total_changes == 0
    original = stage_storage.FACT_BATCH_SCHEMA
    monkeypatch.setattr(stage_storage, "FACT_BATCH_SCHEMA", original.replace("CREATE TRIGGER", "CREATE TRIGGERR"))
    with pytest.raises(StorageError, match="schema migration failed"):
        SqliteControlStore.open(database)
    with sqlite3.connect(database) as copy:
        assert copy.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 34
        assert copy.execute("SELECT name FROM sqlite_master WHERE name='facts_source_insert'").fetchone()
        assert copy.execute("SELECT * FROM facts").fetchall() == before
    monkeypatch.setattr(stage_storage, "FACT_BATCH_SCHEMA", original)
    with SqliteControlStore.open(database) as writer:
        from agent_core.sqlite_control_store import CURRENT_SCHEMA_VERSION
        assert writer._schema_version == CURRENT_SCHEMA_VERSION
        assert [tuple(row) for row in writer._connection.execute("SELECT * FROM facts")] == before
        assert writer._connection.execute("PRAGMA foreign_key_check").fetchall() == []
