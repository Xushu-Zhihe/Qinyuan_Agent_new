"""D3 internal chain with Fake roles and Sandbox, without a task evaluator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from agent_adapters.multi_turn_application import D3Application
from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.artifact_operations import fixed_size_script
from agent_core.artifact_workspace import (
    D3ShareReadback,
    D3StopReadback,
    D3StopReceipt,
    artifact_path,
)
from agent_core.attachment_import import (
    AttachmentImportReadback,
    AttachmentImportStorageError,
)
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.command_contracts import T6CommandResult
from agent_core.contracts import TaskRequest
from agent_core.control_store import StorageError, UpsertIntent, UpsertTurn
from agent_core.domain import IntentStatus, TurnStatus
from agent_core.dual_llm_contracts import (
    D3Authorization,
    D3BudgetExhausted,
    D3CommitMetadata,
    D3OperationCompletion,
    D3OperationStart,
    D3Outcome,
    D3PlanResult,
    D3RecoveryRequired,
    D3RunConfig,
)
from agent_core.dual_llm_modeling import parse_d3_plan_proposal
from agent_core.intent_postconditions import FileDigestObservation, FileSha256
from agent_core.turn_contracts import D1RoleConfig
from agent_core.turn_modeling import digest, stable_id


@dataclass
class Clock:
    now: float = 1_800_000_000

    def __call__(self):
        return self.now


@dataclass
class Token:
    is_cancelled: bool = False


class ProcessLoss(BaseException):
    pass


class Sandbox:
    def __init__(self):
        self.files = {}
        self.import_calls = 0
        self.copy_calls = 0
        self.share_calls = 0
        self.stop_calls = []
        self.command_calls = []
        self.digest_calls = 0
        self.readback_calls = []
        self.destroyed = False
        self.unknown = None
        self.on_execute = lambda request, path: None
        self.hash_status = "present"

    def create(self, task_run_id):
        self.task_run_id = task_run_id
        return "fake-d3-sandbox"

    def destroy(self, sandbox_id):
        self.destroyed = True

    def quiesce(self, sandbox_id):
        raise AssertionError("D3 must not quiesce the whole TaskRun")

    def interrupt(self, sandbox_id):
        pass

    def close_admission(self, sandbox_id):
        pass

    async def create_and_readback(self, sandbox_id, path, content, timeout, token):
        self.import_calls += 1
        self.files[path] = content
        if self.unknown == "import":
            raise TimeoutError("import result was lost")
        return self._readback(path)

    def _readback(self, path):
        content = self.files[path]
        return AttachmentImportReadback(
            len(content), hashlib.sha256(content).hexdigest()
        )

    async def copy_exclusive_and_readback(
        self, sandbox_id, source, destination, timeout, token
    ):
        self.copy_calls += 1
        assert destination not in self.files
        self.files[destination] = self.files[source]
        if self.unknown == "copy":
            raise TimeoutError("copy result was lost")
        return self._readback(destination)

    async def readback(self, sandbox_id, path, timeout, token):
        self.readback_calls.append(path)
        return self._readback(path)

    async def share_exclusive_and_readback(
        self, sandbox_id, source, destination, timeout, token
    ):
        self.share_calls += 1
        assert destination not in self.files
        source_readback = self._readback(source)
        self.files[destination] = self.files[source]
        if self.unknown == "share":
            raise TimeoutError("share result was lost")
        return D3ShareReadback(source_readback, self._readback(destination))

    async def stop_intent_processes(self, claim, sandbox_id, timeout, token):
        self.stop_calls.append(claim)
        if self.unknown == "stop":
            raise TimeoutError("stop result was lost")
        if self.unknown == "stop_refs":
            return D3StopReadback((), (), claim.process_refs)
        return D3StopReadback((), claim.process_refs, ())

    async def query_intent_processes(self, claim, sandbox_id, timeout, token):
        return D3StopReadback((), claim.process_refs, ())

    async def execute(self, approved, token):
        request = approved.request
        self.command_calls.append(request)
        path = request.script.rsplit(" ", 1)[-1]
        self.on_execute(request, path)
        if path not in self.files:
            output = {"status": "missing"}
        elif self.hash_status == "unsafe" and request.script == fixed_digest_script(
            path
        ):
            output = {"status": "unsafe"}
        elif request.script == fixed_size_script(path):
            output = {"status": "present", "byte_size": len(self.files[path])}
        elif request.script == fixed_digest_script(path):
            output = {
                "status": "present",
                "sha256": hashlib.sha256(self.files[path]).hexdigest(),
            }
        else:
            raise AssertionError("non-fixed script was dispatched")
        return T6CommandResult(
            approved.sandbox_id,
            0,
            json.dumps(output).encode(),
            b"",
            False,
            False,
            False,
            True,
            True,
            True,
        )

    async def hash_file(self, task_run_id, sandbox_id, path, timeout, token):
        self.digest_calls += 1
        if path not in self.files:
            return FileDigestObservation("missing")
        if self.hash_status == "unsafe":
            return FileDigestObservation("unsafe")
        return FileDigestObservation(
            "present", hashlib.sha256(self.files[path]).hexdigest()
        )


class Model:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = []
        self.review_when = lambda request: False
        self.reuse_saved_proof = False
        self.turn_index = 0
        self.candidates = 2
        self.fail_stage = None
        self.proof_override = None
        self.modify_action = lambda request, value: value

    def invoke(self, request, timeout_seconds, token):
        self.calls.append(request)
        if request.stage == self.fail_stage:
            raise TimeoutError("model result was lost")
        if request.stage == "outer_turn":
            return {
                "turn_target": f"verify attachment {self.turn_index}",
                "completion_rule": {
                    "rule_id": "attachment_copy_sha256",
                    "version": 1,
                    "parameters": {"attachment_index": self.turn_index},
                },
                "intents": [
                    {"description": "first strategy", "intent_type": "file_sha256"},
                    {"description": "second strategy", "intent_type": "file_sha256"},
                ][: self.candidates],
            }
        if request.stage == "inner_plan":
            context = request.context
            return {
                "template": "sha256_only"
                if context.prior_plan_ids
                else "size_then_sha256",
                "observation_mode": "reuse_saved_proof"
                if self.reuse_saved_proof and context.prior_plan_ids
                else "new_observation",
                "source_proof_id": (
                    self.proof_override or context.reusable_proof_ids[0]
                )
                if self.reuse_saved_proof and context.prior_plan_ids
                else None,
            }
        context = request.context
        if request.stage == "inner_complete":
            action = CompleteIntentAction(context.supporting_fact_ids)
        else:
            conditions = (
                ()
                if request.stage == "inner_size"
                else (
                    FileSha256(
                        context.step_index,
                        context.fixed_script.rsplit(" ", 1)[-1],
                        context.turn_binding.expected_sha256,
                    ),
                )
            )
            action = ProbeAction(
                context.fixed_script,
                b"",
                context.fixed_working_directory,
                60,
                conditions,
            )
        value = {
            "action": action_payload(action),
            "review_requested": self.review_when(request),
        }
        return self.modify_action(request, value)


class ReviewModel:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = []
        self.decision_for = lambda request: "continue_current_intent"
        self.constraints_for = lambda request: []

    def invoke(self, request):
        self.calls.append(request)
        return {
            "decision": "acknowledge_turn_end"
            if request.review_scope == "turn_end"
            else self.decision_for(request),
            "rationale": "use the saved source evidence",
            "source_ids": list(request.source_versions),
            "constraints": self.constraints_for(request),
        }


@pytest.fixture
def scenario(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_bytes(b"D3 first attachment")
    (source / "second.txt").write_bytes(b"D3 second attachment")
    config = D3RunConfig(
        D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1"),
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D3Authorization(str(uuid4()), (0, 1)),
        D1RoleConfig(
            "outer_planning", "fake", "same-model", "outer-v1"
        ).role_config_ref,
    )
    sandbox, clock, token = Sandbox(), Clock(), Token()
    outer, inner = Model(config.outer_role), Model(config.inner_role)
    review = ReviewModel(config.outer_role)
    case = [None, sandbox, outer, inner, review, clock, token]
    case[0] = _application(tmp_path / "run", config, case)
    case[0].start(
        TaskRequest.from_dict(
            {
                "goal": "verify authorized local copies",
                "target": {
                    "attachments": [
                        {"kind": "local_path", "path": "first.txt"},
                        {"kind": "local_path", "path": "second.txt"},
                    ]
                },
            }
        ),
        source,
    )
    try:
        yield case
    finally:
        app = case[0]
        if app.state.task_run.status.value == "running":
            app.controller.cancel(str(uuid4()))
        app.controller.cleanup_sandbox()
        assert sandbox.destroyed
        app.close()


def _application(directory, config, case):
    _, sandbox, outer, inner, review, clock, _ = case
    return D3Application(
        directory,
        config=config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        workspace_sandbox=sandbox,
        command_sandbox=sandbox,
        outer_model=outer,
        inner_model=inner,
        review_model=review,
        clock=clock,
    )


def run(case):
    return asyncio.run(case[0].run(case[6]))


def reopen(case):
    original = case[0]
    task_run_id = original.state.task_run.task_run_id
    original.store.close()
    case[0] = _application(original.directory, original.config, case)
    case[0].controller.attach(task_run_id)
    return case[0]


def interrupt_after_artifact(case):
    app = case[0]
    prepare = app.workspace.prepare_artifact

    async def interrupted(*args):
        await prepare(*args)
        raise ProcessLoss()

    app.workspace.prepare_artifact = interrupted
    with pytest.raises(ProcessLoss):
        run(case)
    app.workspace.prepare_artifact = prepare


def test_two_candidates_first_success_skips_sibling_without_task_success(scenario):
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert [status for _, status in result.turn_statuses] == ["completed"]
    assert [status for _, status in result.intent_statuses] == ["succeeded", "skipped"]
    assert [status for _, status in result.plan_statuses] == ["completed"]
    assert len(result.verified_fact_ids) == 1
    assert (
        result.task_run_status == "running" and result.overall_goal == "not_evaluated"
    )
    assert len(scenario[1].command_calls) == 2 and scenario[1].digest_calls == 1
    assert [request.review_scope for request in scenario[4].calls] == [
        "turn_start",
        "turn_end",
    ]
    counts = (len(scenario[2].calls), len(scenario[3].calls), len(scenario[4].calls))
    reopen(scenario)
    replay = run(scenario)
    assert replay == result
    assert (
        len(scenario[2].calls),
        len(scenario[3].calls),
        len(scenario[4].calls),
    ) == counts


def test_reviewed_sibling_switch_stops_old_intent_and_preserves_size_evidence(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        and sum(item.review_scope == "action" for item in review.calls) == 1
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert [status for _, status in result.intent_statuses] == [
        "abandoned",
        "succeeded",
    ]
    assert [status for _, status in result.plan_statuses] == ["superseded", "completed"]
    assert len(sandbox.stop_calls) == 1
    old_plan, new_plan = app.state.plans
    old_execution = next(
        item for item in app.state.executions if item.plan_id == old_plan.plan_id
    )
    assert old_execution.step_index == 0 and old_execution.status.value == "succeeded"
    assert sandbox.stop_calls[0].process_refs == (old_execution.execution_id,)
    assert len(sandbox.command_calls) == 3 and sandbox.digest_calls == 1
    assert (
        app.store.load_d3_outcome(app.state.intents[0].intent_id).reason
        == "strategy_abandoned"
    )
    assert app.store.load_d3_outcome(old_plan.plan_id).reason == "sibling_switch"
    artifacts = app.workspace_evidence.list_artifacts(result.task_run_id)
    assert {item.claim.intent_id for item in artifacts} == {
        old_plan.intent_id,
        new_plan.intent_id,
    }
    assert all(
        item.claim.path.startswith(f"/workspace/work/intents/{item.claim.intent_id}/")
        for item in artifacts
    )


def test_size_step_review_replans_without_running_old_sha_step(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "replan_current_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    old_plan, new_plan = app.state.plans
    assert [old_plan.status.value, new_plan.status.value] == ["superseded", "completed"]
    assert app.store.load_d3_outcome(old_plan.plan_id).reason == "replanned"
    assert [(item.plan_id, item.step_index) for item in app.state.executions] == [
        (old_plan.plan_id, 0),
        (new_plan.plan_id, 0),
    ]
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1
    artifacts = app.workspace_evidence.list_artifacts(result.task_run_id)
    assert len(artifacts) == 2
    assert (
        artifacts[0].claim.artifact_version_id != artifacts[1].claim.artifact_version_id
    )
    assert {item.claim.import_id for item in artifacts} == {
        app.store.load_d3_turn_binding(app.state.turns[0].turn_id).import_id,
    }


def test_new_plan_reuses_old_proof_and_fact_without_new_execution(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.reuse_saved_proof = True
    inner.review_when = lambda request: request.stage == "inner_sha256"
    review.decision_for = lambda request: (
        "replan_current_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    old_plan, new_plan = app.state.plans
    old_result = app.store.load_d3_plan_result(old_plan.plan_id)
    new_result = app.store.load_d3_plan_result(new_plan.plan_id)
    new_binding = app.store.load_d3_plan_binding(new_plan.plan_id)
    assert old_plan.status.value == new_plan.status.value == "completed"
    assert new_binding.source_proof_id == old_result.proof_id
    assert new_binding.source_fact_id == old_result.supporting_fact_id
    assert new_result.proof_id == old_result.proof_id
    assert new_result.supporting_fact_id == old_result.supporting_fact_id
    assert len(result.verified_fact_ids) == 1
    assert len(app.state.executions) == 2
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1
    assert len(app.workspace_evidence.list_artifacts(result.task_run_id)) == 1
    assert (
        sum(
            item.start.category == "sha_observation"
            for item in app.store.list_d3_operations(result.task_run_id)
        )
        == 1
    )


def test_adjust_turn_atomically_preserves_old_goal_and_targets_second_attachment(
    scenario,
):
    app, sandbox, outer, _, review, _, _ = scenario

    def decide(request):
        if request.review_scope == "turn_start" and outer.turn_index == 0:
            outer.turn_index = 1
            return "adjust_turn_target"
        return "continue_current_intent"

    review.decision_for = decide
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert [status for _, status in result.turn_statuses] == ["superseded", "completed"]
    assert [status for _, status in result.intent_statuses] == [
        "abandoned",
        "skipped",
        "succeeded",
        "skipped",
    ]
    old_turn, new_turn = app.state.turns
    assert old_turn.turn_target == "verify attachment 0"
    assert new_turn.turn_target == "verify attachment 1"
    assert (
        app.store.load_d3_outcome(old_turn.turn_id).successor_turn_id
        == new_turn.turn_id
    )
    assert app.store.load_d3_turn_binding(old_turn.turn_id).attachment_index == 0
    assert app.store.load_d3_turn_binding(new_turn.turn_id).attachment_index == 1
    assert len(sandbox.stop_calls) == 1 and sandbox.stop_calls[0].process_refs == ()
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1
    assert [request.review_scope for request in review.calls].count("turn_end") == 2


def test_adjust_turn_rechecks_new_proposal_before_stopping_old_intent(
    scenario, monkeypatch
):
    app, sandbox, outer, _, review, _, _ = scenario

    def decide(request):
        if request.review_scope == "turn_start" and outer.turn_index == 0:
            outer.turn_index = 1
            return "adjust_turn_target"
        return "continue_current_intent"

    review.decision_for = decide
    original_proposal = app._outer_proposal

    async def changed_import_after_proposal(*args, **kwargs):
        result = await original_proposal(*args, **kwargs)
        if args[0] == 2:
            original_read = app.imports.read_receipt
            second_import_id = app._id("import/1")

            def changed_read(import_id):
                receipt = original_read(import_id)
                return (
                    replace(receipt, sha256="0" * 64)
                    if import_id == second_import_id
                    else receipt
                )

            monkeypatch.setattr(app.imports, "read_receipt", changed_read)
        return result

    monkeypatch.setattr(app, "_outer_proposal", changed_import_after_proposal)
    result = run(scenario)
    assert result.recovery_required
    assert sandbox.stop_calls == []
    assert [status for _, status in result.turn_statuses] == ["running"]
    assert result.intent_statuses[0][1] == "running"


def test_unknown_copy_readback_does_not_sign_receipt_or_dispatch_again(scenario):
    app, sandbox, _, _, _, _, token = scenario
    sandbox.unknown = "copy"
    result = run(scenario)
    assert result.recovery_required and "copy_outcome_unknown" in result.stop_reason
    assert sandbox.copy_calls == 1
    version_id = app.state.plans[0].plan_id
    binding = app.store.load_d3_plan_binding(version_id)
    operation = app.store.load_d3_operation(binding.artifact_version_id)
    assert operation.completion is None
    assert app.workspace_evidence.read_artifact(binding.artifact_version_id) is None
    audit = asyncio.run(app.audit_unknown_operation(binding.artifact_version_id, token))
    assert audit.source == audit.target
    assert app.workspace_evidence.read_artifact(binding.artifact_version_id) is None
    sandbox.unknown = None
    reopen(scenario)
    replay = run(scenario)
    assert replay.recovery_required and sandbox.copy_calls == 1
    assert replay.intent_statuses[0][1] == "running"


def test_unknown_stop_keeps_old_intent_as_anchor_after_readonly_query(scenario):
    app, sandbox, _, inner, review, _, token = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    sandbox.unknown = "stop"
    result = run(scenario)
    assert result.recovery_required and result.intent_statuses[0][1] == "running"
    assert result.intent_statuses[1][1] == "pending"
    assert len(sandbox.stop_calls) == 1
    stop_id = sandbox.stop_calls[0].stop_id
    assert app.store.load_d3_operation(stop_id).completion is None
    assert app.workspace_evidence.read_stop(stop_id) is None
    audit = asyncio.run(app.audit_unknown_operation(stop_id, token))
    assert audit.process_readback.absent_refs == sandbox.stop_calls[0].process_refs
    assert app.workspace_evidence.read_stop(stop_id) is None
    sandbox.unknown = None
    reopen(scenario)
    replay = run(scenario)
    assert replay.recovery_required and len(sandbox.stop_calls) == 1
    assert replay.intent_statuses[0][1] == "running"
    assert replay.intent_statuses[1][1] == "pending"


def test_share_explicit_version_is_context_not_fact_and_detects_source_change(scenario):
    app, sandbox, _, inner, review, _, token = scenario
    interrupt_after_artifact(scenario)
    source_plan = app.state.plans[0]
    source_binding = app.store.load_d3_plan_binding(source_plan.plan_id)
    target_intent = app.state.intents[1]
    receipt = asyncio.run(
        app.share_artifact(
            source_binding.artifact_version_id,
            target_intent.intent_id,
            token,
        )
    )
    assert receipt.status == "confirmed"
    assert receipt.claim.original_verification_status == "unverified"
    assert receipt.claim.source_intent_id != receipt.claim.target_intent_id
    assert receipt.claim.source_path != receipt.claim.target_path
    assert app.state.facts == ()
    assert sandbox.share_calls == 1
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        and sum(item.review_scope == "action" for item in review.calls) == 1
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    target_plan_call = next(
        item
        for item in inner.calls
        if item.stage == "inner_plan"
        and item.context.intent_id == target_intent.intent_id
    )
    assert (
        target_plan_call.context.shared_artifacts[0].share_id == receipt.claim.share_id
    )
    assert len(result.verified_fact_ids) == 1
    sandbox.files[receipt.claim.source_path] = b"changed after sharing"
    with pytest.raises(D3RecoveryRequired, match="d3_share_content_changed"):
        asyncio.run(app.workspace.verify_share(receipt.claim.share_id, token))


def test_unknown_share_readback_keeps_one_share_id_without_completion(scenario):
    app, sandbox, _, inner, review, _, token = scenario
    interrupt_after_artifact(scenario)
    binding = app.store.load_d3_plan_binding(app.state.plans[0].plan_id)
    target_intent_id = app.state.intents[1].intent_id
    sandbox.unknown = "share"
    with pytest.raises(D3RecoveryRequired, match="d3_share_outcome_unknown"):
        asyncio.run(
            app.share_artifact(binding.artifact_version_id, target_intent_id, token)
        )
    assert sandbox.share_calls == 1
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.recovery_required and "unknown" in result.stop_reason
    assert [status for _, status in result.intent_statuses] == ["running", "pending"]
    assert sandbox.stop_calls == []
    shares = [
        item
        for item in app.store.list_d3_operations(app.state.task_run.task_run_id)
        if item.start.category == "artifact_share"
    ]
    assert len(shares) == 1 and shares[0].completion is None
    share_id = shares[0].start.operation_id
    audit = asyncio.run(app.audit_unknown_operation(share_id, token))
    assert audit.source == audit.target
    assert app.workspace_evidence.read_share(share_id) is None
    sandbox.unknown = None
    with pytest.raises(D3RecoveryRequired, match="d3_share_outcome_unknown"):
        asyncio.run(
            app.share_artifact(binding.artifact_version_id, target_intent_id, token)
        )
    assert sandbox.share_calls == 1


def test_size_change_after_confirmed_copy_pauses_without_strategy_failure(scenario):
    app, sandbox, _, _, _, _, _ = scenario

    def mutate(request, path):
        if request.script == fixed_size_script(path):
            sandbox.files[path] += b"unexpected bytes"

    sandbox.on_execute = mutate
    result = run(scenario)
    assert (
        result.recovery_required and result.stop_reason == "d3_size_integrity_exception"
    )
    assert result.intent_statuses[0][1] == "running"
    assert not result.verified_fact_ids and not sandbox.stop_calls
    observation = app.workspace_evidence.read_size(app.state.executions[0].execution_id)
    assert observation.status == "integrity_exception"
    assert app.store.load_d3_plan_result(app.state.plans[0].plan_id) is None


@pytest.mark.parametrize("change", ["different_digest", "missing", "unsafe"])
def test_sha_change_after_confirmed_copy_pauses_without_not_satisfied(scenario, change):
    app, sandbox, _, _, _, _, _ = scenario

    def mutate(request, path):
        if request.script == fixed_digest_script(path):
            if change == "missing":
                del sandbox.files[path]
            elif change == "unsafe":
                sandbox.hash_status = "unsafe"
            else:
                sandbox.files[path] = b"D3 other attachment"

    sandbox.on_execute = mutate
    result = run(scenario)
    assert (
        result.recovery_required
        and result.stop_reason == "d3_digest_integrity_exception"
    )
    assert result.intent_statuses[0][1] == "running"
    assert not result.verified_fact_ids and not sandbox.stop_calls
    plan = app.state.plans[0]
    assert app.store.load_d3_plan_result(plan.plan_id) is None
    observation_id = app._id(f"observation/{plan.plan_id}")
    observation = app.workspace_evidence.read_digest(observation_id)
    assert observation.status == "integrity_exception"
    assert observation.observed.status == (
        "present" if change == "different_digest" else change
    )


def test_switch_without_sibling_is_rejected_before_stop(scenario):
    _, sandbox, outer, inner, review, _, _ = scenario
    outer.candidates = 1
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.recovery_required and "review_retry_exhausted" in result.stop_reason
    assert result.intent_statuses[0][1] == "running"
    assert not sandbox.stop_calls
    assert len([item for item in review.calls if item.review_scope == "action"]) == 2
    assert not scenario[0].store.load_d3_outcome(scenario[0].state.intents[0].intent_id)


def test_intent_scoped_probe_constraint_does_not_block_sibling(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    review.constraints_for = lambda request: (
        [
            {
                "kind": "allowed_action_kinds",
                "scope_kind": "intent",
                "scope_id": request.intent_id,
                "allowed_action_kinds": ["complete_intent"],
            }
        ]
        if request.review_scope == "turn_start"
        and len([item for item in review.calls if item.review_scope == "turn_start"])
        == 1
        else []
    )
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert [status for _, status in result.intent_statuses] == [
        "abandoned",
        "succeeded",
    ]
    assert len(sandbox.command_calls) == 2
    assert sandbox.command_calls[0].plan_id == app.state.plans[1].plan_id
    assert (
        app.store.load_d3_outcome(app.state.intents[0].intent_id).reason
        == "strategy_abandoned"
    )


def test_confirmed_copy_receipt_repairs_database_without_second_copy(scenario):
    app, sandbox, _, _, _, _, _ = scenario
    finish = app.workspace._finish

    def interrupted(operation_id, receipt, *, confirmed):
        if operation_id == receipt.claim.artifact_version_id:
            raise ProcessLoss()
        return finish(operation_id, receipt, confirmed=confirmed)

    app.workspace._finish = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    version_id = app.store.load_d3_plan_binding(
        app.state.plans[0].plan_id
    ).artifact_version_id
    assert app.workspace_evidence.read_artifact(version_id) is not None
    assert app.store.load_d3_operation(version_id).completion is None
    app.workspace._finish = finish
    reopen(scenario)
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert scenario[0].store.load_d3_operation(version_id).completion is not None
    assert sandbox.copy_calls == 1


def test_confirmed_share_receipt_repairs_without_second_share(scenario):
    app, sandbox, _, _, _, _, token = scenario
    interrupt_after_artifact(scenario)
    version_id = app.store.load_d3_plan_binding(
        app.state.plans[0].plan_id
    ).artifact_version_id
    target_id = app.state.intents[1].intent_id
    finish = app.workspace._finish

    def interrupted(operation_id, receipt, *, confirmed):
        if operation_id == receipt.claim.share_id:
            raise ProcessLoss()
        return finish(operation_id, receipt, confirmed=confirmed)

    app.workspace._finish = interrupted
    with pytest.raises(ProcessLoss):
        asyncio.run(app.share_artifact(version_id, target_id, token))
    app.workspace._finish = finish
    shares = [
        item
        for item in app.store.list_d3_operations(app.state.task_run.task_run_id)
        if item.start.category == "artifact_share"
    ]
    assert len(shares) == 1 and shares[0].completion is None
    assert app.workspace_evidence.read_share(shares[0].start.operation_id) is not None
    reopen(scenario)
    receipt = asyncio.run(scenario[0].share_artifact(version_id, target_id, token))
    assert receipt.status == "confirmed" and sandbox.share_calls == 1
    assert (
        scenario[0].store.load_d3_operation(receipt.claim.share_id).completion
        is not None
    )


def test_changed_share_source_is_rejected_without_verified_fact(scenario):
    app, sandbox, _, _, _, _, token = scenario
    interrupt_after_artifact(scenario)
    binding = app.store.load_d3_plan_binding(app.state.plans[0].plan_id)
    artifact = app.workspace_evidence.read_artifact(binding.artifact_version_id)
    sandbox.files[artifact.claim.path] = b"changed source before share"
    with pytest.raises(D3RecoveryRequired, match="d3_share_integrity_exception"):
        asyncio.run(
            app.share_artifact(
                binding.artifact_version_id, app.state.intents[1].intent_id, token
            )
        )
    assert sandbox.share_calls == 1 and not app.state.facts
    share = next(
        item
        for item in app.store.list_d3_operations(app.state.task_run.task_run_id)
        if item.start.category == "artifact_share"
    )
    receipt = app.workspace_evidence.read_share(share.start.operation_id)
    assert receipt.status == "rejected" and share.completion.result_kind == "failed"


@pytest.mark.parametrize(
    "boundary", ["before_controller", "before_application_receipt"]
)
def test_known_review_response_recovers_without_another_model_call(
    scenario, monkeypatch, boundary
):
    app, _, _, _, review, _, _ = scenario
    if boundary == "before_controller":
        original = app.controller.commit_d3_transition

        def interrupted(operation, *args, **kwargs):
            if operation == "apply_d3_review":
                raise RuntimeError("Controller application interrupted")
            return original(operation, *args, **kwargs)

        owner, method = app.controller, "commit_d3_transition"
    else:
        original = app.review_ledger.record_application

        def interrupted(*args):
            raise RuntimeError("review application receipt interrupted")

        owner, method = app.review_ledger, "record_application"
    with monkeypatch.context() as patch:
        patch.setattr(owner, method, interrupted)
        stopped = run(scenario)
    assert (
        stopped.recovery_required
        and stopped.stop_reason == "review_application_uncommitted"
    )
    assert len(review.calls) == 1
    root_id = review.calls[0].review_root_id
    root = app.review_ledger.read_root(root_id)
    assert root["last_outcome"] == "accepted" and not root["applied"]
    reopen(scenario)
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert len(review.calls) == 2
    assert scenario[0].review_ledger.read_root(root_id)["applied"]


def test_proof_saved_before_fact_reuses_original_timestamp_on_resume(scenario):
    app, sandbox, _, _, _, clock, _ = scenario
    issue = app.proofs.issue

    def interrupted(*args):
        issue(*args)
        raise ProcessLoss()

    app.proofs.issue = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    plan = app.state.plans[0]
    proof_id = app._id(f"proof/{plan.plan_id}")
    saved = app.proof_store.read(proof_id)
    assert saved is not None and app.state.facts == ()
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1
    clock.now += 1
    reopen(scenario)
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert scenario[0].proof_store.read(proof_id) == saved
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1


def test_confirmed_stop_receipt_repairs_without_second_stop(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        and sum(item.review_scope == "action" for item in review.calls) == 1
        else "continue_current_intent"
    )
    finish = app.workspace._finish

    def interrupted(operation_id, receipt, *, confirmed):
        if isinstance(receipt, D3StopReceipt):
            raise ProcessLoss()
        return finish(operation_id, receipt, confirmed=confirmed)

    app.workspace._finish = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    assert len(sandbox.stop_calls) == 1
    stop_id = sandbox.stop_calls[0].stop_id
    assert app.workspace_evidence.read_stop(stop_id).status == "confirmed"
    assert app.store.load_d3_operation(stop_id).completion is None
    app.workspace._finish = finish
    reopen(scenario)
    result = run(scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert len(sandbox.stop_calls) == 1
    assert scenario[0].store.load_d3_operation(stop_id).completion is not None


def test_explicit_unknown_stop_refs_remain_unknown_after_readonly_query(scenario):
    app, sandbox, _, inner, review, _, token = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    sandbox.unknown = "stop_refs"
    result = run(scenario)
    assert result.recovery_required and result.intent_statuses[1][1] == "pending"
    stop_id = sandbox.stop_calls[0].stop_id
    assert app.workspace_evidence.read_stop(stop_id).status == "unknown"
    assert app.store.load_d3_operation(stop_id).completion is None
    sandbox.unknown = None
    audit = asyncio.run(app.audit_unknown_operation(stop_id, token))
    assert audit.process_readback.absent_refs == sandbox.stop_calls[0].process_refs
    assert app.workspace_evidence.read_stop(stop_id).status == "unknown"
    assert app.store.load_d3_operation(stop_id).completion is None
    assert len(sandbox.stop_calls) == 1


def test_verified_share_manifest_binds_original_fact_and_proof(scenario):
    app, sandbox, _, _, _, _, token = scenario
    complete = app.control.complete_plan

    def interrupted(*args):
        complete(*args)
        raise ProcessLoss()

    app.control.complete_plan = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    plan = app.state.plans[0]
    result = app.store.load_d3_plan_result(plan.plan_id)
    assert plan.status.value == "completed" and len(app.state.facts) == 1
    version_id = app.store.load_d3_plan_binding(plan.plan_id).artifact_version_id
    target_intent_id = app.state.intents[1].intent_id
    share = asyncio.run(app.share_artifact(version_id, target_intent_id, token))
    assert share.claim.original_verification_status == "verified"
    assert share.claim.source_proof_id == result.proof_id
    assert share.claim.source_fact_id == result.supporting_fact_id
    assert len(app.state.facts) == 1 and sandbox.share_calls == 1
    reopen(scenario)
    asyncio.run(scenario[0].workspace.verify_share(share.claim.share_id, token))
    assert scenario[0].workspace_evidence.read_share(share.claim.share_id) == share


def test_share_rejects_wrong_owner_and_cross_intent_path(scenario):
    app, sandbox, _, _, _, _, token = scenario
    interrupt_after_artifact(scenario)
    version_id = app.store.load_d3_plan_binding(
        app.state.plans[0].plan_id
    ).artifact_version_id
    with pytest.raises(D3RecoveryRequired, match="d3_share_target_mismatch"):
        asyncio.run(app.share_artifact(version_id, str(uuid4()), token))
    assert sandbox.share_calls == 0
    receipt = asyncio.run(
        app.share_artifact(version_id, app.state.intents[1].intent_id, token)
    )
    with pytest.raises(ValueError, match="target must remain"):
        replace(
            receipt.claim,
            target_path="/workspace/work/intents/another/shared-elsewhere",
        )


@pytest.mark.parametrize(
    "category,limit",
    [
        ("outer_model", 4),
        ("inner_model", 16),
        ("tool_dispatch", 6),
        ("sha_observation", 4),
        ("artifact_share", 4),
    ],
)
def test_d3_operation_limits_survive_reopen_without_goal_failure(
    scenario, category, limit
):
    app, _, _, _, _, clock, _ = scenario
    task_run_id = app.state.task_run.task_run_id

    def start(index):
        role = (
            app.config.outer_role
            if category == "outer_model"
            else app.config.inner_role
            if category == "inner_model"
            else None
        )
        return D3OperationStart(
            stable_id(task_run_id, f"d3-budget/{category}/{index}"),
            task_run_id,
            category,
            clock.now,
            app.state.latest_checkpoint.sequence,
            digest({"category": category, "index": index}),
            f"budget-resource-{category}-{index}",
            role.role if role else None,
            role.role_config_ref if role else None,
        )

    for index in range(limit):
        operation = start(index)
        app.store.begin_d3_operation(operation)
        app.store.complete_d3_operation(
            D3OperationCompletion(
                operation.operation_id,
                clock.now,
                "failed" if index == 0 else "completed",
                operation.operation_id,
                digest({"operation_id": operation.operation_id, "known": True}),
            )
        )
    reopen(scenario)
    with pytest.raises(D3BudgetExhausted):
        scenario[0].store.begin_d3_operation(start(limit))
    assert len(scenario[0].store.list_d3_operations(task_run_id)) == limit
    assert scenario[0].state.task_run.status.value == "running"


@pytest.mark.parametrize("stage", ["outer_turn", "inner_plan"])
def test_unknown_model_start_is_not_reinvoked_after_reopen(scenario, stage):
    _, _, outer, inner, _, _, _ = scenario
    model = outer if stage == "outer_turn" else inner
    model.fail_stage = stage
    first = run(scenario)
    assert first.recovery_required and first.task_run_status == "running"
    first_calls = len(model.calls)
    assert first_calls == 1
    model.fail_stage = None
    reopen(scenario)
    replay = run(scenario)
    assert replay.recovery_required and len(model.calls) == first_calls
    assert (
        replay.task_run_status == "running" and replay.overall_goal == "not_evaluated"
    )


def test_arbitrary_script_is_rejected_before_tool_dispatch(scenario):
    app, sandbox, _, inner, _, _, _ = scenario

    def modify(request, value):
        if request.stage == "inner_size":
            value["action"]["script"] = "echo untrusted"
        return value

    inner.modify_action = modify
    result = run(scenario)
    assert result.recovery_required and result.stop_reason == "d3_fixed_probe_rejected"
    assert not sandbox.command_calls and not app.state.facts
    assert app.state.intents[0].status.value == "running"


def test_wrong_complete_fact_does_not_promote_verified_observation(scenario):
    app, sandbox, _, inner, _, _, _ = scenario

    def modify(request, value):
        if request.stage == "inner_complete":
            value["action"]["fact_ids"] = [str(uuid4())]
        return value

    inner.modify_action = modify
    result = run(scenario)
    assert (
        result.recovery_required and result.stop_reason == "d3_completion_fact_rejected"
    )
    assert len(app.state.facts) == 1 and app.state.facts[0].status.value == "verified"
    assert app.state.intents[0].status.value == "running"
    assert app.state.turns[0].status.value == "running"
    assert len(sandbox.command_calls) == 2


def test_unlisted_reusable_proof_is_rejected_without_new_plan(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.reuse_saved_proof = True
    inner.proof_override = str(uuid4())
    inner.review_when = lambda request: request.stage == "inner_sha256"
    review.decision_for = lambda request: (
        "replan_current_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    result = run(scenario)
    assert (
        result.recovery_required
        and result.stop_reason == "d3_plan_template_or_source_unavailable"
    )
    assert len(app.state.plans) == 1 and app.state.plans[0].status.value == "completed"
    assert len(app.state.facts) == 1 and app.state.intents[0].status.value == "running"
    assert len(sandbox.command_calls) == 2 and sandbox.digest_calls == 1


def test_v1_not_satisfied_result_cannot_be_promoted_to_intent_failure(scenario):
    app, _, _, _, _, _, _ = scenario
    interrupt_after_artifact(scenario)
    plan = app.state.plans[0]
    binding = app.store.load_d3_plan_binding(plan.plan_id)
    result = D3PlanResult(
        binding.task_run_id,
        plan.plan_id,
        "not_satisfied",
        binding.artifact_version_id,
        str(uuid4()),
        None,
        digest({"unsupported": "not_satisfied"}),
    )
    with pytest.raises(
        D3RecoveryRequired, match="d3_fixed_rule_has_no_satisfied_result"
    ):
        app.control.complete_plan(binding, result)
    assert app.store.load_d3_plan_result(plan.plan_id) is None
    assert app.store.load_d3_outcome(plan.intent_id) is None
    assert app.state.intents[0].status.value == "running"


def test_pending_sibling_cannot_reuse_active_intent_plan_proposal(scenario):
    app, _, _, _, _, _, _ = scenario
    interrupt_after_artifact(scenario)
    first_plan = app.state.plans[0]
    first_binding = app.store.load_d3_plan_binding(first_plan.plan_id)
    proposal = parse_d3_plan_proposal(
        app.model_evidence.read(first_binding.proposal_request_id).raw
    )
    pending = app.state.intents[1]
    with pytest.raises(D3RecoveryRequired, match="d3_plan_proposal_owner_mismatch"):
        app.control.register_plan(
            pending.intent_id,
            proposal,
            first_binding.proposal_request_id,
            str(uuid4()),
        )
    assert len(app.state.plans) == 1
    assert pending.status is IntentStatus.PENDING


def test_pending_sibling_cannot_prepare_target_copy_before_switch(scenario):
    app, sandbox, _, _, _, _, token = scenario
    interrupt_after_artifact(scenario)
    pending = app.state.intents[1]
    turn = app.store.load_d3_turn_binding(pending.turn_id)
    with pytest.raises(D3RecoveryRequired, match="d3_artifact_owner_mismatch"):
        asyncio.run(
            app.workspace.prepare_artifact(
                turn,
                pending.intent_id,
                str(uuid4()),
                str(uuid4()),
                token,
            )
        )
    assert sandbox.copy_calls == 1


def test_sibling_switch_cannot_leave_old_plan_running(scenario):
    app, _, _, inner, review, _, _ = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )
    captured = {}

    def interrupted(intent_id, review_id, stop_id):
        captured.update(intent_id=intent_id, review_id=review_id, stop_id=stop_id)
        raise ProcessLoss()

    app.control.switch_sibling = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    old, sibling = app.state.intents
    changes = (
        UpsertIntent(
            old.intent_id,
            old.turn_id,
            old.description,
            old.priority,
            old.intent_type,
            IntentStatus.ABANDONED,
        ),
        UpsertIntent(
            sibling.intent_id,
            sibling.turn_id,
            sibling.description,
            sibling.priority,
            sibling.intent_type,
            IntentStatus.RUNNING,
        ),
    )
    metadata = D3CommitMetadata(
        outcomes=(
            D3Outcome(
                "intent",
                old.intent_id,
                "abandoned",
                "strategy_abandoned",
                (captured["review_id"], captured["stop_id"]),
            ),
        )
    )
    with pytest.raises(
        StorageError, match="D3 sibling switch must close old running Plans"
    ):
        app.controller.commit_d3_transition(
            "switch_d3_intent",
            changes,
            (old.intent_id, sibling.intent_id),
            sibling.intent_id,
            str(uuid4()),
            metadata=metadata,
        )
    assert [item.status for item in app.state.plans] == [app.state.plans[0].status]
    assert app.state.plans[0].status.value == "running"
    assert [item.status for item in app.state.intents] == [
        IntentStatus.RUNNING,
        IntentStatus.PENDING,
    ]


def test_adjust_turn_cannot_leave_untried_candidate_pending(scenario):
    app, _, outer, _, review, _, _ = scenario
    captured = {}

    def decide(request):
        if request.review_scope == "turn_start":
            outer.turn_index = 1
            return "adjust_turn_target"
        return "continue_current_intent"

    review.decision_for = decide

    def interrupted(*args):
        captured["args"] = args
        raise ProcessLoss()

    app.control.adjust_turn = interrupted
    with pytest.raises(ProcessLoss):
        run(scenario)
    intent_id, review_id, stop_id, proposal, proposal_id, receipt = captured["args"]
    registration = app.control._registration(f"adjust/{review_id}", proposal)
    binding, new_intents = app.control._turn_metadata(
        registration,
        proposal,
        proposal_id,
        receipt,
    )
    old_turn = app.state.turns[0]
    active = app.state.intents[0]
    changes = (
        UpsertTurn(
            old_turn.turn_id,
            old_turn.task_run_id,
            old_turn.sequence,
            old_turn.turn_target,
            old_turn.intent_ids,
            TurnStatus.SUPERSEDED,
        ),
        UpsertIntent(
            active.intent_id,
            active.turn_id,
            active.description,
            active.priority,
            active.intent_type,
            IntentStatus.ABANDONED,
        ),
        *registration.state_changes,
    )
    metadata = D3CommitMetadata(
        turn_binding=binding,
        intent_bindings=new_intents,
        outcomes=(
            D3Outcome(
                "turn",
                old_turn.turn_id,
                "superseded",
                "turn_target_adjusted",
                (review_id, stop_id, proposal_id),
                registration.turn_id,
            ),
            D3Outcome(
                "intent",
                intent_id,
                "abandoned",
                "turn_target_adjusted",
                (review_id, stop_id),
            ),
        ),
    )
    with pytest.raises(
        StorageError, match="D3 adjusted Turn must close remaining candidates"
    ):
        app.controller.commit_d3_transition(
            "advance_d3_turn",
            changes,
            (old_turn.turn_id, active.intent_id, *registration.entity_ids),
            registration.turn_id,
            str(uuid4()),
            metadata=metadata,
        )
    assert len(app.state.turns) == 1
    assert [item.status for item in app.state.intents] == [
        IntentStatus.RUNNING,
        IntentStatus.PENDING,
    ]


def test_import_outcome_unknown_pauses_without_outer_dispatch_or_retry(scenario):
    app, sandbox, outer, _, _, _, _ = scenario
    sandbox.unknown = "import"
    first = run(scenario)
    assert first.recovery_required and first.stop_reason == "d3_import_outcome_unknown"
    assert sandbox.import_calls == 1 and not outer.calls and not app.state.turns
    sandbox.unknown = None
    reopen(scenario)
    replay = run(scenario)
    assert (
        replay.recovery_required and replay.stop_reason == "d3_import_outcome_unknown"
    )
    assert sandbox.import_calls == 1 and not outer.calls


def test_import_evidence_storage_fault_pauses_before_model_call(scenario):
    app, _, outer, _, _, _, _ = scenario

    def unavailable(_import_id):
        raise AttachmentImportStorageError("receipt damaged")

    app.imports.read_receipt = unavailable
    result = run(scenario)
    assert (
        result.recovery_required
        and result.stop_reason == "d3_import_evidence_unavailable"
    )
    assert not outer.calls and not app.state.turns


def test_unknown_copy_audit_rejects_changed_claim_owner_before_readback(scenario):
    app, sandbox, _, _, _, _, token = scenario
    sandbox.unknown = "copy"
    result = run(scenario)
    assert result.recovery_required
    version_id = app.store.load_d3_plan_binding(
        app.state.plans[0].plan_id
    ).artifact_version_id
    claim = app.workspace_evidence.read_artifact_claim(version_id)
    pending_id = app.state.intents[1].intent_id
    forged = replace(
        claim,
        intent_id=pending_id,
        path=artifact_path(pending_id, version_id),
    )
    app.workspace_evidence.read_artifact_claim = lambda _version_id: forged
    with pytest.raises(D3RecoveryRequired, match="d3_unknown_artifact_claim_changed"):
        asyncio.run(app.audit_unknown_operation(version_id, token))
    assert not sandbox.readback_calls
    assert app.store.load_d3_operation(version_id).completion is None


def test_malformed_stop_result_pauses_without_switch_or_retry(scenario):
    app, sandbox, _, inner, review, _, _ = scenario
    inner.review_when = lambda request: request.stage == "inner_size"
    review.decision_for = lambda request: (
        "switch_sibling_intent"
        if request.review_scope == "action"
        else "continue_current_intent"
    )

    async def incomplete(claim, sandbox_id, timeout, token):
        sandbox.stop_calls.append(claim)
        return D3StopReadback((), (), ())

    sandbox.stop_intent_processes = incomplete
    first = run(scenario)
    assert first.recovery_required and first.stop_reason == "d3_stop_outcome_unknown"
    assert [item.status for item in app.state.intents] == [
        IntentStatus.RUNNING,
        IntentStatus.PENDING,
    ]
    stop_id = sandbox.stop_calls[0].stop_id
    assert app.store.load_d3_operation(stop_id).completion is None
    reopen(scenario)
    second = run(scenario)
    assert second.recovery_required and len(sandbox.stop_calls) == 1


def test_schema_12_upgrade_adds_d3_tables_without_changing_readonly_history(
    tmp_path,
):
    import sqlite3

    import agent_core.sqlite_control_store as module
    from agent_core.sqlite_control_store import SqliteControlStore

    path = tmp_path / "schema12.sqlite3"
    connection = sqlite3.connect(path)
    for version in range(1, 13):
        connection.executescript(getattr(module, f"MIGRATION_{version}"))
        connection.execute(
            "INSERT INTO schema_versions VALUES (?, ?)", (version, 1800000000.0)
        )
    connection.commit()
    connection.close()

    before = path.read_bytes()
    with SqliteControlStore.open_readonly(path) as store:
        assert store.load_d3_run_config(str(uuid4())) is None
    assert path.read_bytes() == before

    with SqliteControlStore.open(path) as store:
        assert (
            store._connection.execute(
                "SELECT MAX(version) FROM schema_versions"
            ).fetchone()[0]
            == module.CURRENT_SCHEMA_VERSION
        )
        assert (
            store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'd3_runs'"
            ).fetchone()[0]
            == 1
        )
