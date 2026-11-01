"""One direct first-flag submission; later candidates keep the reviewed handoff."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from test_stage_application import case
from agent_core.goal_feedback import ExternalValidationFeedback
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.runtime import TaskRunStatus
from agent_core.stage_contracts import (
    PlanningSession, StagePlanningDecision, TurnIntent, TurnPlanningContext, TurnPlanningState, StageTurnSnapshot,
)
from agent_core.stage_planning import StageModelRequest, schema, stage_runtime


def request_for(runtime=None, *, with_observation=True):
    task, turn_id, road_id = str(uuid4()), str(uuid4()), str(uuid4())
    turn = StageTurnSnapshot(turn_id, TurnIntent("goal", "criterion"), TurnPlanningState.ACTIVE)
    history = ()
    if with_observation:
        history = (D8HistoryItem(str(uuid4()), task, "progress", turn_id, road_id, None, "observed",
            (), "0" * 64, 1, "tool_observation_not_fact", "current", "available"),)
    context = TurnPlanningContext(task, "goal", PlanningSession("road_execution", road_id),
        turn, history, goal_validation="external")
    return StageModelRequest(str(uuid4()), task, "stage_action", "inner", 1, context, (), (),
        execution_context={"stage_runtime": runtime or {}})


def direct_branches(request):
    return [branch for branch in schema(request)["anyOf"]
            if branch.get("properties", {}).get("kind", {}).get("const") == "request_final_validation"]


def test_direct_schema_requires_the_runtime_marker_and_an_original_observation():
    enabled = request_for({"direct_submission": "first_flag_v1"})
    branch, = direct_branches(enabled)
    assert set(branch["properties"]) >= {"reason", "candidate", "source_ref"}
    assert branch["properties"]["source_ref"]["enum"] == [enabled.context.history[0].ref]
    assert direct_branches(request_for()) == []
    assert direct_branches(request_for({"direct_submission": "first_flag_v1"}, with_observation=False)) == []


def test_grounds_allows_only_the_declared_submission_head_substitution():
    from agent_adapters.stage_goal import StageGoal
    def goal_for(stdout, head=""):
        goal = f"controlled challenge\n\n提交 flag 头：{head}" if head else "controlled challenge"
        result = {"payload": {"stdout": stdout}}
        app = SimpleNamespace(task_id="task", web=None,
            state=SimpleNamespace(task_run=SimpleNamespace(request=SimpleNamespace(goal=goal))),
            evidence=SimpleNamespace(
                read_history=lambda task, refs: (SimpleNamespace(record_id="op"),),
                read=lambda kind, identity: result if kind == "mcp-result" else {}),
            store=SimpleNamespace())
        return StageGoal(app)
    converted = goal_for("msg    D1  b'wectf{th1s_1s_we_b64}'", "NSSCTF")
    assert converted.grounds(SimpleNamespace(candidate="NSSCTF{th1s_1s_we_b64}", source_ref="op"))
    assert not converted.grounds(SimpleNamespace(candidate="NSSCTF{th1s_1s_we_b6}", source_ref="op"))
    assert not converted.grounds(SimpleNamespace(candidate="FLAG{th1s_1s_we_b64}", source_ref="op"))
    # A model that appends its derivation to candidate still submits only the token.
    assert converted.grounds(SimpleNamespace(
        candidate="NSSCTF{th1s_1s_we_b64}\n\nDerivation: decoded from the custom alphabet", source_ref="op"))
    numeric = goal_for("123{th1s_1s_we_b64}", "NSSCTF")
    assert not numeric.grounds(SimpleNamespace(candidate="NSSCTF{th1s_1s_we_b64}", source_ref="op"))
    exact = goal_for("candidate NSSCTF{abc}", "NSSCTF")
    assert exact.grounds(SimpleNamespace(candidate="NSSCTF{abc}", source_ref="op"))
    assert not exact.grounds(SimpleNamespace(candidate="NSSCTF{abd}", source_ref="op"))
    assert not exact.grounds(SimpleNamespace(candidate="", source_ref="op"))
    custom = goal_for("msg b'CTF{custom_body}'", "flag")
    assert custom.grounds(SimpleNamespace(candidate="flag{custom_body}", source_ref="op"))
    assert not custom.grounds(SimpleNamespace(candidate="NSSCTF{custom_body}", source_ref="op"))
    # Empty field disables head substitution; exact source text still works.
    plain = goal_for("msg b'wectf{plain_body}'")
    assert not plain.grounds(SimpleNamespace(candidate="NSSCTF{plain_body}", source_ref="op"))
    assert plain.grounds(SimpleNamespace(candidate="wectf{plain_body}", source_ref="op"))


def external_app(case, head=""):
    from dataclasses import replace
    from agent_adapters.stage_application import StageApplication
    task = case.app.state.task_run.request
    if head:
        task = replace(task, goal=task.goal + f"\n\n提交 flag 头：{head}")
    app = StageApplication(case.path / "direct-task", **case.args, goal_validation="external")
    app.start(task)
    asyncio.run(case.app.close())
    case.app = app
    case.tools.task_id = app.task_id
    return app


def direct_transform(case, candidate="observed"):
    previous = case.models.inner.transform
    def transform(request, raw):
        if (request.stage == "stage_action"
                and request.execution_context.get("stage_runtime", {}).get("direct_submission")):
            sources = [i for i in request.context.history
                       if i.record_type == "progress" and i.identity == "tool_observation_not_fact"]
            if sources:
                return {"kind": "request_final_validation", "reason": "grounded flag in the cited observation",
                        "candidate": candidate, "source_ref": sources[-1].ref}
        return previous(request, raw)
    case.models.inner.transform = transform
    return transform


def test_first_grounded_flag_is_handed_off_without_review_and_consumes_the_shot(case):
    app = external_app(case)
    direct_transform(case)
    result = asyncio.run(app.run(case.token))
    assert result.reason == "stage.awaiting_external_validation"
    assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    candidate, = app.store.list_d7_answer_candidates(app.task_id)
    assert not app.store.list_d7_validation_feedback(app.task_id, candidate.candidate_id, candidate.evidence_version)
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    protected = D7CandidateEvidenceService(app.directory, app.task_id,
        trusted_adapter_ids=("competition",), create=False)
    assert protected.read_port("competition").read(candidate.candidate_id) == "observed"
    # No Fact, completion, road closure or independent review was needed to submit.
    assert app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert not any(isinstance(r.proposal, StagePlanningDecision)
                   and r.proposal.kind == "request_final_validation" for r in case.models.outer.calls)
    turn, = app.store.list_stage_turns(app.task_id)
    assert turn.achievement is None
    assert any((op.application_ref or "").startswith("stage-direct/")
               for op in app.store.list_d4_operations(app.task_id))
    # A rejected direct answer is now history; the next candidate keeps the reviewed path.
    app.controller.apply_external_validation_feedback(ExternalValidationFeedback(app.task_id,
        candidate.candidate_id, candidate.evidence_version, "rejected", "controlled-verifier", str(uuid4())))
    assert app.state.task_run.status is TaskRunStatus.RUNNING
    road = next(r for r in turn.roads if r.status.value in {"running", "succeeded"})
    after = app._request("stage_action", PlanningSession("road_execution", road.road_id))
    assert "direct_submission" not in stage_runtime(after)
    assert direct_branches(after) == []


def test_direct_handoff_accepts_task_required_head_substitution(case):
    from MCPs.Mcp_client.client import MCPExecutionResult
    app = external_app(case, "NSSCTF")
    original = case.tools.call_tool
    async def call(name, arguments):
        result = await original(name, arguments)
        return MCPExecutionResult(result.agent_id, result.sandbox_id, name, result.executable,
            result.arguments, "completed", 0, "msg b'wectf{observed}'", "", False)
    case.tools.call_tool = call
    direct_transform(case, candidate="NSSCTF{observed}\n\nDerivation: decoded from the artifact")
    result = asyncio.run(app.run(case.token))
    assert result.reason == "stage.awaiting_external_validation"
    assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    candidate, = app.store.list_d7_answer_candidates(app.task_id)
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    protected = D7CandidateEvidenceService(app.directory, app.task_id,
        trusted_adapter_ids=("competition",), create=False)
    # The submitted value is the clean token, never the model's derivation.
    assert protected.read_port("competition").read(candidate.candidate_id) == "NSSCTF{observed}"
