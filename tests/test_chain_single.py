"""D1 uses two Fake roles and real durable control/evidence, with separate cleanup."""

import asyncio
import hashlib
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from agent_adapters.dual_llm_application import D1Application
from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.command_contracts import T6CommandResult
from agent_core.contracts import TaskRequest
from agent_core.control_store import StorageError
from agent_core.intent_postconditions import FileDigestObservation, FileSha256
from agent_core.turn_contracts import D1Authorization, D1RoleConfig, D1RunConfig
from agent_core.turn_modeling import stable_id


@dataclass
class Clock:
    now: float = 1_800_000_000

    def __call__(self):
        return self.now


@dataclass
class Token:
    is_cancelled: bool = False


class Sandbox:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.import_calls = 0
        self.copy_calls = 0
        self.digest_calls = 0
        self.destroyed = False
        self.unknown = None
        self.observe_status = "present"
        self.on_call = lambda stage: None

    def create(self, task_run_id):
        self.task_run_id = task_run_id
        return "fake-d1-sandbox"

    def destroy(self, sandbox_id):
        self.destroyed = True

    def quiesce(self, sandbox_id):
        pass

    def interrupt(self, sandbox_id):
        pass

    def close_admission(self, sandbox_id):
        pass

    async def create_and_readback(self, sandbox_id, path, content, timeout, token):
        self.import_calls += 1
        self.files[path] = content
        self.on_call("import")
        if self.unknown == "import":
            raise TimeoutError()
        return AttachmentImportReadback(
            len(content), hashlib.sha256(content).hexdigest()
        )

    async def copy_and_readback(self, sandbox_id, source, destination, timeout, token):
        self.copy_calls += 1
        self.files[destination] = self.files[source]
        self.on_call("copy")
        if self.unknown == "copy":
            raise TimeoutError()
        return AttachmentImportReadback(
            len(self.files[destination]),
            hashlib.sha256(self.files[destination]).hexdigest(),
        )

    async def execute(self, approved, token):
        self.calls.append(approved)
        self.on_call("command")
        return T6CommandResult(
            approved.sandbox_id,
            0,
            b"fixed check",
            b"",
            False,
            False,
            False,
            self.unknown != "command",
            True,
            True,
        )

    async def hash_file(self, task_run_id, sandbox_id, path, timeout, token):
        self.digest_calls += 1
        self.on_call("digest")
        if self.unknown == "digest":
            return FileDigestObservation("unknown")
        if self.observe_status != "present":
            return FileDigestObservation(self.observe_status)
        return FileDigestObservation(
            "present", hashlib.sha256(self.files[path]).hexdigest()
        )


class Model:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = []
        self.on_call = lambda request: None
        self.bad_outer = None
        self.bad_inner = None
        self.structural = set()
        self.unknown = False

    def invoke(self, request, timeout_seconds, token):
        self.calls.append(request)
        self.on_call(request)
        if self.unknown:
            raise TimeoutError()
        if (
            request.stage in self.structural
            and request.correction_of_model_request_id is None
        ):
            return {"unexpected": True}
        if request.stage == "outer":
            value = {
                "turn_target": "找到 flag",
                "completion_rule": {
                    "rule_id": "attachment_copy_sha256",
                    "version": 1,
                    "parameters": {"attachment_index": 0},
                },
                "intents": [
                    {
                        "description": "verify attachment copy",
                        "intent_type": "file_sha256",
                    }
                ],
            }
            if self.bad_outer:
                self.bad_outer(value)
            return value
        if request.stage == "probe":
            binding = request.context.binding
            value = action_payload(
                ProbeAction(
                    request.context.fixed_script,
                    b"",
                    binding.work_path.rsplit("/", 1)[0],
                    60,
                    (FileSha256(0, binding.work_path, binding.expected_sha256),),
                )
            )
            if self.bad_inner:
                self.bad_inner(value)
            return value
        return action_payload(CompleteIntentAction(request.context.fact_ids))


@pytest.fixture
def scenario(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sample.txt").write_bytes(b"a real local attachment for the Fake Sandbox")
    clock, token, sandbox = Clock(), Token(), Sandbox()
    config = D1RunConfig(
        D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1"),
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D1Authorization(str(uuid4())),
    )
    outer, inner = Model(config.outer_role), Model(config.inner_role)
    app = D1Application(
        tmp_path / "run",
        config=config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        copy_sandbox=sandbox,
        command_sandbox=sandbox,
        digest_sandbox=sandbox,
        outer_model=outer,
        inner_model=inner,
        clock=clock,
    )
    app.start(
        TaskRequest.from_dict(
            {
                "goal": "solve task",
                "target": {
                    "attachments": [{"kind": "local_path", "path": "sample.txt"}]
                },
            }
        ),
        root,
    )
    case = [app, sandbox, outer, inner, token, clock]
    try:
        yield case
    finally:
        app = case[0]
        if app.controller.control_state.task_run.status.value == "running":
            app.controller.cancel(str(uuid4()))
        app.controller.cleanup_sandbox()
        assert sandbox.destroyed, "test resource cleanup is separately verified"
        app.close()


def run(s):
    return asyncio.run(s[0].run(s[4]))


def reopen(scenario):
    original, sandbox, outer, inner, _, clock = scenario
    task_id = original.controller.control_state.task_run.task_run_id
    # Simulate process loss; graceful Controller.close requires a terminal run.
    original.store.close()
    app = D1Application(
        original.directory,
        config=original.config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        copy_sandbox=sandbox,
        command_sandbox=sandbox,
        digest_sandbox=sandbox,
        outer_model=outer,
        inner_model=inner,
        clock=clock,
    )
    scenario[0] = app
    app.controller.attach(task_id)
    return app


def test_dual_role_single_turn_keeps_overall_goal_unassessed(scenario):
    app, sandbox, outer, inner, _, _ = scenario
    result = run(scenario)
    assert result.rule_outcome == "satisfied", result
    assert (result.intent_status, result.turn_status, result.task_run_status) == (
        "succeeded",
        "completed",
        "running",
    )
    assert result.overall_goal == "not_evaluated"
    assert result.dispatch_status == "stopped" and result.sandbox_status == "retained"
    assert len(result.verified_fact_ids) == 1
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert outer.calls[0].role == "outer_planning"
    assert all(item.role == "inner_action" for item in inner.calls)
    assert outer.calls[0].role_config_ref != inner.calls[0].role_config_ref
    assert (
        sandbox.import_calls
        == sandbox.copy_calls
        == sandbox.digest_calls
        == len(sandbox.calls)
        == 1
    )
    assert len(app.controller.control_state.turns) == 1
    assert (
        app.store.load_d1_turn_proof(result.turn_id).supporting_fact_ids
        == result.verified_fact_ids
    )
    sandbox.files[next(path for path in sandbox.files if "/intents/" in path)] = (
        b"changed later"
    )
    replay = run(scenario)
    assert replay == result
    assert sandbox.digest_calls == len(sandbox.calls) == 1
    assert len(outer.calls) == 1 and len(inner.calls) == 2


@pytest.mark.parametrize("stage", ["outer", "probe", "complete"])
def test_structure_correction_is_counted_within_its_own_phase(scenario, stage):
    app, _, outer, inner, _, _ = scenario
    (outer if stage == "outer" else inner).structural.add(stage)
    result = run(scenario)
    assert result.rule_outcome == "satisfied", result
    category = {
        "outer": "outer_model",
        "probe": "inner_probe_model",
        "complete": "inner_complete_model",
    }[stage]
    records = [
        item
        for item in app.store.list_d1_operations(result.task_run_id)
        if item.start.category == category
    ]
    assert len(records) == 2 and records[0].completion.result_kind == "rejected"
    assert records[0].application_ref is None


@pytest.mark.parametrize("field,value", [("rule_id", "unknown"), ("version", 2)])
def test_unregistered_rule_stops_before_registration(scenario, field, value):
    _, sandbox, outer, inner, _, _ = scenario
    outer.bad_outer = lambda raw: raw["completion_rule"].__setitem__(field, value)
    result = run(scenario)
    assert result.turn_id is None and not sandbox.calls and not inner.calls
    assert len(outer.calls) == 1
    assert result.stop_reason == "completion_rule_unregistered"


def test_fixed_check_cannot_be_replaced_by_arbitrary_bash(scenario):
    app, sandbox, _, inner, _, _ = scenario
    inner.bad_inner = lambda raw: raw.__setitem__(
        "script", "touch /workspace/work/unsafe"
    )
    result = run(scenario)
    assert result.stop_reason == "fixed_probe_rejected"
    assert not sandbox.calls and len(inner.calls) == 1 and not result.verified_fact_ids
    record = app.store.load_d1_operation(stable_id(result.task_run_id, "probe/model"))
    known = record.completion
    decision = app.model_evidence.read_application_rejection(record.start.operation_id)
    assert known.result_kind == "completed" and record.application_ref is None
    assert decision.rejection_code == "fixed_probe_rejected"
    replay = run(scenario)
    assert replay.stop_reason == result.stop_reason and len(inner.calls) == 1
    assert app.store.load_d1_operation(record.start.operation_id).completion == known
    assert (
        app.model_evidence.read_application_rejection(record.start.operation_id)
        == decision
    )


@pytest.mark.parametrize(
    "stage", ["import", "copy", "command", "digest", "outer_model", "inner_model"]
)
def test_unknown_result_is_not_redispatched(scenario, stage):
    _, sandbox, outer, inner, _, _ = scenario
    if stage.endswith("model"):
        (outer if stage == "outer_model" else inner).unknown = True
    else:
        sandbox.unknown = stage
    first = run(scenario)
    assert first.recovery_required, first
    counts = (
        sandbox.import_calls,
        sandbox.copy_calls,
        len(sandbox.calls),
        sandbox.digest_calls,
        len(outer.calls),
        len(inner.calls),
    )
    second = run(scenario)
    assert second.recovery_required and not second.verified_fact_ids
    assert counts == (
        sandbox.import_calls,
        sandbox.copy_calls,
        len(sandbox.calls),
        sandbox.digest_calls,
        len(outer.calls),
        len(inner.calls),
    )


@pytest.mark.parametrize("status", ["missing", "unsafe"])
def test_confirmed_unsatisfied_observation_is_not_unknown(scenario, status):
    _, sandbox, _, _, _, _ = scenario
    sandbox.observe_status = status
    first = run(scenario)
    assert first.rule_outcome == "not_satisfied" and not first.recovery_required
    assert not first.verified_fact_ids and first.intent_status == "running"
    second = run(scenario)
    assert second.rule_outcome == "not_satisfied" and sandbox.digest_calls == 1


def test_cancelled_late_outer_response_is_known_stale_without_registration(scenario):
    app, sandbox, outer, _, token, _ = scenario
    outer.on_call = lambda request: setattr(token, "is_cancelled", True)
    result = run(scenario)
    assert (
        result.task_run_status == "cancelled"
        and result.turn_id is None
        and not sandbox.calls
    )
    record = next(
        item
        for item in app.store.list_d1_operations(result.task_run_id)
        if item.start.category == "outer_model"
    )
    assert record.completion.result_kind == "stale" and record.application_ref is None


def test_deadline_stops_before_model_dispatch(scenario):
    _, sandbox, outer, _, _, clock = scenario
    clock.now += 100000
    result = run(scenario)
    assert (
        result.task_run_status == "deadline_exceeded"
        and not outer.calls
        and not sandbox.calls
    )


def test_missing_proof_cannot_be_recreated_on_completed_turn_replay(
    scenario, monkeypatch
):
    app, sandbox, outer, inner, _, _ = scenario
    first = run(scenario)
    assert first.rule_outcome == "satisfied", first
    with monkeypatch.context() as patch:
        patch.setattr(app.proofs.proofs, "read", lambda _proof_id: None)
        second = run(scenario)
        assert second.recovery_required
        assert second.historical_verified_fact_ids == first.verified_fact_ids
        assert second.verified_fact_ids == ()
        assert second.fact_evidence_status == "unavailable"
    assert sandbox.digest_calls == len(sandbox.calls) == 1
    assert len(outer.calls) == 1 and len(inner.calls) == 2


def test_missing_proof_after_issue_before_fact_cannot_be_reissued(
    scenario, monkeypatch
):
    app, sandbox, outer, inner, _, _ = scenario
    with monkeypatch.context() as patch:
        patch.setattr(
            app.controller,
            "commit_verified_t6_observation",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("interrupted before Fact")
            ),
        )
        first = run(scenario)
        assert first.recovery_required and not first.verified_fact_ids
    proof_id = stable_id(first.task_run_id, "proof")
    saved_proof = app.proofs.read_proof(proof_id)
    assert saved_proof is not None
    with monkeypatch.context() as patch:
        patch.setattr(app.proofs.proofs, "read", lambda _proof_id: None)

        def cannot_issue(*args, **kwargs):
            pytest.fail("a declared historical proof must never be replaced")

        patch.setattr(app.proofs, "issue", cannot_issue)
        recovered = run(scenario)
        assert (
            recovered.recovery_required
            and recovered.stop_reason == "declared_proof_missing"
        )
        assert not recovered.verified_fact_ids
    assert sandbox.digest_calls == len(sandbox.calls) == 1
    assert len(outer.calls) == len(inner.calls) == 1


def test_corrupt_import_evidence_returns_readable_recovery(scenario, monkeypatch):
    app, _, _, _, _, _ = scenario
    run(scenario)
    with monkeypatch.context() as patch:
        patch.setattr(
            app.imports,
            "read_receipt",
            lambda _reference: (_ for _ in ()).throw(OSError("import unavailable")),
        )
        result = run(scenario)
        assert result.recovery_required and result.rule_outcome == "inconclusive"
        assert result.stop_reason == "import unavailable"


def test_cancel_commit_evidence_error_does_not_claim_cleanup(scenario, monkeypatch):
    app, _, _, _, token, _ = scenario
    first = run(scenario)
    token.is_cancelled = True
    with monkeypatch.context() as patch:
        patch.setattr(app.proofs.proofs, "read", lambda _proof_id: None)
        result = run(scenario)
        assert result.recovery_required
        assert (
            result.task_run_status == "running" and result.sandbox_status == "retained"
        )
        assert result.historical_verified_fact_ids == first.verified_fact_ids
        assert not result.verified_fact_ids


def test_completed_turn_reopen_only_rechecks_saved_evidence(scenario):
    first = run(scenario)
    assert first.rule_outcome == "satisfied", first
    _, sandbox, outer, inner, _, _ = scenario
    reopen(scenario)
    assert run(scenario) == first
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert (
        sandbox.import_calls
        == sandbox.copy_calls
        == sandbox.digest_calls
        == len(sandbox.calls)
        == 1
    )


def test_known_unapplied_completion_survives_reopen(scenario, monkeypatch):
    app, sandbox, outer, inner, _, _ = scenario
    with monkeypatch.context() as patch:
        patch.setattr(
            app.controller,
            "complete_verified_t6_intent",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("interrupted before Intent")
            ),
        )
        stopped = run(scenario)
        assert stopped.recovery_required and stopped.verified_fact_ids
        assert stopped.intent_status == "running" and stopped.turn_status == "running"
    reopened = reopen(scenario)
    completed = run(scenario)
    assert completed.rule_outcome == "satisfied", completed
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert sandbox.digest_calls == len(sandbox.calls) == 1
    assert (
        reopened.store.load_d1_operation(
            stable_id(completed.task_run_id, "complete/model")
        ).application_ref
        == completed.intent_id
    )


def test_actual_adapter_config_change_is_rejected_even_on_completed_replay(scenario):
    first = run(scenario)
    assert first.rule_outcome == "satisfied", first
    _, sandbox, _, inner, _, _ = scenario
    inner.role_config = replace(inner.role_config, prompt_template="changed")
    result = run(scenario)
    assert (
        result.recovery_required
        and result.stop_reason == "adapter_role_configuration_changed"
    )
    assert sandbox.digest_calls == len(sandbox.calls) == 1


def test_first_observation_digest_mismatch_stays_deterministically_unsatisfied(
    scenario,
):
    _, sandbox, _, _, _, _ = scenario

    def change_copy(stage):
        if stage == "command":
            path = next(path for path in sandbox.files if "/intents/" in path)
            sandbox.files[path] = b"changed before first observation"

    sandbox.on_call = change_copy
    first = run(scenario)
    assert first.rule_outcome == "not_satisfied" and not first.recovery_required
    assert (
        not first.verified_fact_ids
        and first.turn_status == first.intent_status == "running"
    )
    assert run(scenario) == first
    assert sandbox.digest_calls == len(sandbox.calls) == 1


def test_committed_state_reload_error_is_observable_and_can_resume(
    scenario, monkeypatch
):
    app, sandbox, outer, inner, _, _ = scenario

    def failed_reload(*args, **kwargs):
        app.controller._cache_invalid = True
        raise StorageError("committed state reload unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app.controller, "_reload_after_commit", failed_reload)
        result = run(scenario)
        assert result.recovery_required and result.rule_outcome == "inconclusive"
        assert result.control_state_status == "recovery_required"
        assert result.turn_id is not None and not result.verified_fact_ids
        assert len(outer.calls) == 1 and not inner.calls and not sandbox.calls
    app.controller.reload_state()
    resumed = run(scenario)
    assert resumed.rule_outcome == "satisfied", resumed
    assert len(outer.calls) == 1 and len(inner.calls) == 2


def test_result_projection_failure_preserves_history_without_claiming_satisfied(
    scenario, monkeypatch
):
    app, _, _, _, _, _ = scenario
    original = app.evaluator.evaluate

    def projection_unavailable(binding, state, facts):
        if state.turns[0].status.value == "completed":
            raise OSError("result evidence unavailable")
        return original(binding, state, facts)

    with monkeypatch.context() as patch:
        patch.setattr(app.evaluator, "evaluate", projection_unavailable)
        result = run(scenario)
        assert result.turn_status == "completed" and result.intent_status == "succeeded"
        assert result.rule_outcome == "inconclusive" and result.recovery_required
        assert result.historical_verified_fact_ids and not result.verified_fact_ids
        assert result.fact_evidence_status == "unavailable"
    assert run(scenario).rule_outcome == "satisfied"


@pytest.mark.parametrize("stage", ["outer", "probe", "complete"])
def test_model_application_marker_is_repaired_from_exact_committed_mutation(
    scenario, monkeypatch, stage
):
    app, sandbox, outer, inner, _, _ = scenario
    request_id = stable_id(
        app.controller.control_state.task_run.task_run_id, stage + "/model"
    )
    original = app.store.mark_d1_operation_applied

    def interrupted_marker(operation_id, application_ref):
        if operation_id == request_id:
            raise RuntimeError("interrupted after committed application")
        return original(operation_id, application_ref)

    with monkeypatch.context() as patch:
        patch.setattr(app.store, "mark_d1_operation_applied", interrupted_marker)
        interrupted = run(scenario)
        assert interrupted.recovery_required
        record = app.store.load_d1_operation(request_id)
        assert (
            record.completion.result_kind == "completed"
            and record.application_ref is None
        )
    result = run(scenario)
    assert result.rule_outcome == "satisfied", result
    assert app.store.load_d1_operation(request_id).application_ref is not None
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert sandbox.digest_calls == len(sandbox.calls) == 1


def test_correction_replay_requires_original_structure_rejection(scenario, monkeypatch):
    app, sandbox, _, inner, _, _ = scenario
    inner.structural.add("probe")
    with monkeypatch.context() as patch:
        patch.setattr(
            app.controller,
            "commit_verified_t6_observation",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("before Fact")),
        )
        first = run(scenario)
        assert first.recovery_required and not first.verified_fact_ids
    original = app.model_evidence.read
    parent_id = stable_id(first.task_run_id, "probe/model")
    with monkeypatch.context() as patch:
        patch.setattr(
            app.model_evidence,
            "read",
            lambda reference: None if reference == parent_id else original(reference),
        )
        result = run(scenario)
        assert result.recovery_required and not result.verified_fact_ids
        assert len(inner.calls) == 2 and sandbox.digest_calls == len(sandbox.calls) == 1
    assert run(scenario).rule_outcome == "satisfied"
