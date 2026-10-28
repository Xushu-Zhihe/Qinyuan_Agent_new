"""Internal stage scheduling over the existing controller, MCP and ledger."""

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
from uuid import uuid4

from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_adapters.stage_evidence import StageEvidence, StageModelCalls
from agent_core.actions import MCPCall
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import ControlMutation
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import IntentStatus
from agent_core.review_policy import MAX_ACTION_TIMEOUT
from agent_core.contracts import ModelBackend
from agent_core.handoff_contracts import handoff_json
from agent_core.redaction import D5Redactor
from agent_core.runtime import RuntimeEvent, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_contracts import (
    PlanningExhausted, PlanningSession, RoadResolution, RoadSnapshot, StageFactApplication,
    StageConclusionChange, StageFactSelection, StageHistoryRead, StageFactProposal, StageIncomplete,
    StageReviewBinding, StageRunConfig, StageStateChange, StageTurnSnapshot,
    TurnAchievement, TurnCompletion, TurnPlanningContext, TurnPlanningState, TurnIntent, StagePlanningDecision, MAX_IN_FLIGHT_TOOLS,
)
from agent_core.stage_planning import StageModelRequest
from agent_core.stage_storage import StageRecoveryRequired, StageStateError, active_turn, _just_registered_roads, execution_policy
from agent_core.turn_modeling import digest, stable_id
from qinyuan_core import Category, Layer, classify, get_logger


@dataclass(frozen=True, slots=True)
class StageRunResult:
    task_run_id: str
    turn: StageTurnSnapshot | None
    reason: str


class StageApplication:
    def __init__(self, directory, *, config, sandbox, workspace_sandbox, inner_model, outer_model,
                 clock, capacity_port, mcp_manager=None, target_network_adapter=None, sensitive_values=(), goal_validation=None, http_sandbox=None, import_sandbox=None):
        self.directory, self.config, self.clock = directory, config, clock
        self.goal_validation = goal_validation
        self.store = SqliteControlStore.open(directory / "control.sqlite", clock=clock)
        self.evidence = StageEvidence(directory / "stage-evidence", self.store)
        self.store.stage_evidence = self.evidence
        self.models = StageModelCalls(self.store, self.evidence, inner_model, outer_model, clock,
            capacity_port, config.handoff_config)
        self.redactor = D5Redactor(tuple(sensitive_values))
        self.controller = TaskRunController(sandbox, clock=clock, store=self.store,
            repository=ControlStateRepository(self.store, SimpleNamespace()),
            registry=TaskRunControllerRegistry(), target_network_adapter=target_network_adapter)
        self.workspace = SimpleNamespace(sandbox=workspace_sandbox)
        self.output_boundary = SimpleNamespace(evidence=self.evidence)
        self.web = None  # Native HTTP experiments remain in their existing owner.
        self.http_sandbox = http_sandbox
        self.mcp = MCPPlanExecutor(self, mcp_manager)
        if http_sandbox is not None and config.web_authorization is not None:
            from agent_adapters.http_capability import HttpCapability
            self.web = HttpCapability(self)
            self.evidence.http_validator = self.mcp.experiments.validate_receipt
        self.validator = SimpleNamespace(action_limits=self._action_limits)
        self.task_id, self._token, self._supervision = None, None, None
        self._tools_in_flight = {}
        self._tools_stopping = False
        self.store.live_strategy_operation = self._live_supervision
        self.store.live_tool_request = self._live_tool_request
        self.store.live_tool_operation = self._live_tool_operation
        self._target_network = target_network_adapter
        self.models.messages = self._conversation
        self.models.recover_capacity = self._recover_capacity
        from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
        from agent_core.attachment_importer import AttachmentImporter
        self.imports = FileAttachmentImportRecorder(directory / "attachment-imports") if import_sandbox is not None else None
        self.importer = AttachmentImporter(controller=self.controller, recorder=self.imports,
            sandbox=import_sandbox, clock=clock) if import_sandbox is not None else None
        from agent_adapters.stage_goal import StageGoal
        self.goal = StageGoal(self) if goal_validation is not None else None

    @property
    def state(self):
        rows = self.store.load_control_state_rows(self.task_id)
        return SimpleNamespace(task_run=rows.task_run, latest_checkpoint=rows.latest_checkpoint)

    def _id(self, name):
        return stable_id(self.task_id, name)

    def _policy(self):
        return execution_policy(self.config)

    def start(self, request, *, source_root=None, effective_model_backend=ModelBackend.DEEPSEEK_OPENAI, task_run_id=None):
        from agent_core.http_validation import valid_target
        if request.target.endpoints and self._target_network is None:
            raise StageStateError("stage.target_network_required", code="stage.target_network_required")
        if request.target.endpoints and not valid_target(self.config, request):
            raise StageStateError("stage.target_not_authorized", code="stage.target_not_authorized")
        if len(request.target.attachments) != len(self.config.authorization.attachment_indices):
            raise StageStateError("stage.attachments_not_authorized", code="stage.attachments_not_authorized")
        if request.target.attachments and self.importer is None:
            raise StageStateError("stage.attachment_import_not_connected", code="stage.attachment_import_not_connected")
        task_id = str(uuid4()) if task_run_id is None else task_run_id
        policy = self._policy()
        config = StageRunConfig(self.config.outer_role, self.config.inner_role, self.config.d4_budget,
            "sha256:" + digest(policy), self.goal_validation)
        self.evidence.save("policy", task_id, policy)
        self.controller.start(request, effective_model_backend, str(uuid4()), source_root=source_root,
            task_run_id=task_id, stage_config=config)
        self.task_id = task_id
        self._freeze_models()
        self.mcp.authorize(task_id)
        return task_id

    def bind(self, task_id):
        config = self.store.load_stage_run_config(task_id)
        if config is None:
            code = "stage.task_run_not_found" if self.store.load_task_run(task_id) is None else "stage.legacy_task_not_supported"
            raise StageRecoveryRequired(code, category=Category.POLICY, layer=Layer.CTRL)
        if (config.outer_role != self.config.outer_role or config.inner_role != self.config.inner_role
                or config.goal_validation != self.goal_validation
                or config.budget != self.config.d4_budget or config.execution_policy_ref != "sha256:" + digest(self._policy())
                or self.evidence.read("policy", task_id) != self._policy()):
            raise StageStateError("stage.execution_configuration_changed", code="stage.execution_configuration_changed")
        self.task_id = task_id
        self._freeze_models()
        self.controller.attach(task_id)

    def _freeze_models(self):
        configured = [model for model in (self.models.inner, self.models.outer) if hasattr(model, "settings")]
        if configured:
            from agent_adapters.deepseek_roles import freeze_timeout_policy
            freeze_timeout_policy(self.evidence, self.task_id, *configured)
            for model in configured:
                model.bind_diagnostics(self.evidence)

    def _check_dispatch(self):
        task = self.state.task_run
        if self._token is not None and self._token.is_cancelled or self.clock() >= task.deadline_at or task.status is not TaskRunStatus.RUNNING:
            raise StageRecoveryRequired("stage.dispatch_stopped", category=Category.POLICY, layer=Layer.CTRL)
        from agent_core.operator_controls import check_admission
        check_admission(self.store, self.task_id)
        if self.store.blocking_d4_operations(self.task_id):
            raise StageRecoveryRequired("stage.operation_outcome_unknown", layer=Layer.CTRL)

    def _action_limits(self, task_id, turn_id, road_id):
        self._check_dispatch()
        turn = self.store.load_turn(task_id, turn_id)
        if turn is None or not any(r.road_id == road_id and r.status is IntentStatus.RUNNING for r in turn.roads):
            raise StageStateError("stage.tool_scope_changed", code="stage.tool_scope_changed")
        return {"probe"}, MAX_ACTION_TIMEOUT

    def _commit(self, change):
        self._check_dispatch()
        state = self.state
        identity = self._id("stage/commit/" + str(state.latest_checkpoint.sequence) + "/" + digest(asdict(change)))
        mutation = ControlMutation.create(task_run_id=self.task_id, mutation_id=identity,
            operation="apply_stage_state", request=state.task_run.request,
            effective_model_backend=state.task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence, state_changes=(change,),
            event=RuntimeEvent("control_state_committed", self.task_id, self.clock(), TaskRunStatus.RUNNING,
                {"operation": "apply_stage_state", "entity_ids": [t.turn_id for t in change.turns], "result": "applied"}))
        return self.store.commit(mutation)

    def _conversation_sources(self, session):
        sources = []
        for op in self.store.list_d4_operations(self.task_id):
            if op.application_ref is None or op.start.category == "tool_dispatch":
                continue
            saved = self.evidence.read("request", op.start.operation_id)
            if saved is None:
                continue
            request = self.evidence.request(op.start.operation_id)
            if request.context.session != session:
                continue
            result = self.evidence.read("result", op.start.operation_id)
            if result is None or result["outcome"] != "valid":
                continue
            self.evidence.read_model_result(op)
            sources.append((op, request, result))
        return sources

    def _render_conversation(self, sources):
        messages = []
        for op, request, result in sources:
            content = result["content"]
            if "tool_calls" in result:
                from agent_core.stage_planning import decode_stage_response
                from agent_core.output_contracts import D8ModelResponse
                value, _, _, _ = decode_stage_response(request,
                    D8ModelResponse(content, result["finish_reason"], result["tool_calls"]))
                content = json.dumps(value, ensure_ascii=False)
            if request.stage in {"stage_review", "stage_supervision"}:
                messages.append({"role": "user", "content": json.dumps({"review_feedback": content}, ensure_ascii=False)})
            else:
                messages.extend([{"role": "user", "content": handoff_json({"stage": request.stage, "context": asdict(request.context)})},
                    {"role": "assistant", "content": content if isinstance(content, str)
                     else json.dumps(content, ensure_ascii=False)}])
                if isinstance(self.evidence.read_model_result(op).result, StageHistoryRead):
                    items = self.evidence.read_history_result(op)
                    messages.append({"role": "user", "content": json.dumps({"history_read": [asdict(i) for i in items]}, ensure_ascii=False)})
        return tuple(messages)

    def _summary_view(self, sources):
        summaries = [(op, req, result) for op, req, result in sources if req.stage == "stage_summary"]
        if not summaries:
            return set(), ()
        operation, request, result = summaries[-1]
        available = {op.start.operation_id: digest({"request": asdict(req), "result": data})
                     for op, req, data in sources}
        if any(available.get(identity) != version for identity, version in request.summary_coverage):
            raise StageStateError("stage.summary_coverage_changed", code="stage.summary_coverage_changed")
        summary = {"identity": "unverified_session_summary", "summary_request_id": request.model_request_id,
            "coverage": request.summary_coverage, "content": self.evidence.read_model_result(operation).result.content}
        return {identity for identity, _ in request.summary_coverage}, ({"role": "user", "content": json.dumps(summary, ensure_ascii=False)},)

    def _conversation(self, session, sources=None):
        sources = self._conversation_sources(session) if sources is None else sources
        covered, prefix = self._summary_view(sources)
        current = [s for s in sources if s[0].start.operation_id not in covered and s[1].stage != "stage_summary"]
        return (*prefix, *self._render_conversation(current))

    async def _recover_capacity(self, request, token):
        sources = self._conversation_sources(request.context.session)
        covered, prefix = self._summary_view(sources)
        current = [s for s in sources if s[0].start.operation_id not in covered and s[1].stage != "stage_summary"]
        decisions = [i for i, (_, req, _) in enumerate(current) if req.stage not in {"stage_review", "stage_supervision"}]
        if len(decisions) <= 4:
            return None
        old = current[:decisions[-4]]
        coverage_ids = covered | {op.start.operation_id for op, _, _ in old}
        coverage = tuple((op.start.operation_id, digest({"request": asdict(req), "result": result}))
            for op, req, result in sources if op.start.operation_id in coverage_ids)
        identity = self._id("stage/summary/" + digest(coverage))
        summary_request = replace(request, model_request_id=identity, stage="stage_summary",
            role_config_ref=self.config.outer_role.role_config_ref, context=replace(request.context, history=()),
            messages=(*prefix, *self._render_conversation(old)), tools=(), proposal_operation_id=None,
            proposal=None, correction_of_model_request_id=None, summary_coverage=coverage, capacity_recovery_of=request.model_request_id)
        summary_request = replace(summary_request, frozen_contract_ref=None)
        summary_id, _ = await self.models.call(summary_request, token)
        self.store.mark_d4_operation_applied(summary_id, "stage-summary/" + summary_id)
        messages = self._conversation(request.context.session)
        self.evidence.save("capacity-recovery", request.model_request_id, {
            "source_request_sha256": digest(asdict(request)), "summary_operation_id": summary_id})
        return replace(request, model_request_id=self._id("stage/capacity/" + request.model_request_id + "/" + summary_id),
            messages=messages, capacity_recovery_of=request.model_request_id)

    def _request(self, stage, session, *, proposal_id=None, proposal=None):
        state = self.state
        identity = self._id("stage/model/" + session.scope + "/" + session.owner_id + "/" + stage
            + "/" + str(state.latest_checkpoint.sequence) + "/" + (proposal_id or ""))
        if self.evidence.read("request", identity) is not None:
            return self.evidence.request(identity)
        turn = next((t for t in self.store.list_stage_turns(self.task_id)
            if t.turn_id == session.owner_id or any(r.road_id == session.owner_id for r in t.roads)), None)
        all_history = self.evidence.history(self.task_id)
        sources = self._conversation_sources(session)
        reads = [self.evidence.read_model_result(op).result for op, _, _ in sources]
        retrieved = next((value.refs for value in reversed(reads) if isinstance(value, StageHistoryRead)), ())
        if turn is None:
            history, premises = all_history, ()
        else:
            history, premises = self.evidence.knowledge(self.task_id).context(session, retrieved)
        required = set(getattr(proposal, "support_refs", ())) | set(getattr(proposal, "evidence_refs", ()))
        if isinstance(proposal, StageConclusionChange):
            required.add(proposal.replacement_ref)
            required.update(i.ref for i in all_history if getattr(i, "logical_root_id", None) in {t.root_id for t in proposal.targets})
        refs = {i.ref for i in history} | required
        history = tuple(i for i in all_history if i.ref in refs)
        children = {t.turn_id for t in self.store.list_stage_turns(self.task_id) if turn and t.parent_turn_id == turn.turn_id}
        context = TurnPlanningContext(self.task_id, state.task_run.request.goal, session, turn, history,
            feedback_refs=tuple(i.ref for i in history if i.record_id.startswith("feedback/") and i.turn_id in children
                or i.record_id.startswith("validation/") and turn and i.turn_id == turn.turn_id),
            pending_operation_ids=tuple(entry.operation_id for entry in self._tools_in_flight.values()),
            effective_support_refs=premises, goal_validation=self.goal_validation)
        inner = stage != "stage_review" and stage != "stage_supervision"
        role = self.config.inner_role if inner else self.config.outer_role
        messages = self._conversation(session, sources)
        if stage == "stage_action" and self._tools_in_flight:
            messages += ({"role": "user", "content": json.dumps({"pending_tools": [
                {"operation_id": entry.operation_id, **asdict(entry.call)} for entry in self._tools_in_flight.values()]},
                ensure_ascii=False)},)
        return StageModelRequest(identity, self.task_id, stage, role.role_config_ref,
            state.latest_checkpoint.sequence, context, messages,
            self._available_tools() if stage == "stage_action" else (), proposal_id, proposal,
            history_catalog=tuple(i.visible(include_content=False) for i in all_history))

    async def _review(self, request, token):
        proposal_id, proposal = await self.models.call(request, token)
        if await self._knowledge_action(request, proposal_id, proposal, token):
            return proposal, None, None
        review_request = self._request("stage_review", request.context.session, proposal_id=proposal_id, proposal=proposal)
        review_id, review = await self.models.call(review_request, token)
        return proposal, review, StageReviewBinding(proposal_id, review_id)

    async def _knowledge_action(self, request, identity, value, token):
        knowledge = self.evidence.knowledge(self.task_id)
        if isinstance(value, StageFactSelection):
            turn = knowledge.turn_for(request.context.session)
            self._commit(StageStateChange(turns=(replace(turn, fact_refs=value.support_refs),), applications=(identity,)))
        elif isinstance(value, StageHistoryRead):
            self.evidence.perform_history_read(self.store.load_d4_operation(identity))
            self._commit(StageStateChange(applications=(identity,)))
        elif isinstance(value, (StageConclusionChange, StageFactProposal)):
            review_request = self._request("stage_review", request.context.session, proposal_id=identity, proposal=value)
            review_id, review = await self.models.call(review_request, token)
            binding = StageReviewBinding(identity, review_id)
            if isinstance(value, StageConclusionChange):
                record = knowledge.prepare_revision(binding)
                self._commit(StageStateChange(reviews=(binding,), revisions=(record,)))
            else:
                facts = (() if review.decision != "accept" or value.inferred else
                         (StageFactApplication(self._id("stage/fact/" + identity), review_id),))
                self._commit(StageStateChange(reviews=(binding,), facts=facts))
        else:
            return False
        return True

    def _live_supervision(self, identity):
        return self._supervision is not None and self._supervision[0] == identity

    def _live_tool_request(self, identity):
        entry = self._tools_in_flight.get(identity)
        return entry is not None and not entry.task.done()

    def _live_tool_operation(self, identity):
        operation = self.store.load_d4_operation(identity)
        if operation is None or operation.start.task_run_id != self.task_id or operation.start.category != "tool_dispatch":
            return False
        parts = operation.start.resource_ref.split("/")
        if len(parts) == 3 and parts[0] == "mcp":
            road, request = parts[1:]
        elif len(parts) == 5 and parts[0] == "mcp" and parts[2] == "experiment":
            road, request = parts[1], parts[3]
        else:
            return False
        entry = self._tools_in_flight.get(request)
        return self._live_tool_request(request) and entry.road_id == road

    def _available_tools(self):
        from agent_adapters.public_web import NAMES
        sandbox_busy = any(entry.call.tool_name not in NAMES for entry in self._tools_in_flight.values())
        return tuple(tool for tool in self.mcp.tools if not sandbox_busy or tool["qualified_name"] in NAMES)

    async def _collect_tools(self, *, wait=False, drain=False):
        while self._tools_in_flight:
            if wait or drain:
                while not any(entry.task.done() for entry in self._tools_in_flight.values()):
                    self._check_dispatch()
                    await asyncio.wait({entry.task for entry in self._tools_in_flight.values()},
                        timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
            completed = []
            for identity, entry in tuple(self._tools_in_flight.items()):
                if entry.task.done():
                    entry.task.result()  # Unknown/failed settlement is never hidden by a later success.
                    del self._tools_in_flight[identity]
                    completed.append(entry.operation_id)
            if completed:
                get_logger("ctrl").info("stage.tools_settled", run=self.task_id, phase="tool_dispatch",
                    operations=completed, in_flight=len(self._tools_in_flight))
            if not drain:
                return

    async def _start_tool(self, identity, road_id, call, token):
        if (len(self._tools_in_flight) >= MAX_IN_FLIGHT_TOOLS
                or call.tool_name not in {tool["qualified_name"] for tool in self._available_tools()}):
            raise StageStateError("stage.tool_slot_unavailable", code="stage.tool_slot_unavailable", category=Category.POLICY)
        operation_id = identity if call.tool_name == "http.run_experiment" else self._id("mcp/" + identity)
        app = self
        class ToolToken:
            @property
            def is_cancelled(self):
                return app._tools_stopping or token.is_cancelled
            @property
            def is_timed_out(self):
                return getattr(token, "is_timed_out", False)
        entry = SimpleNamespace(road_id=road_id, call=call, operation_id=operation_id,
            task=asyncio.create_task(self.mcp.execute(identity, road_id, call, ToolToken())))
        self._tools_in_flight[identity] = entry
        await asyncio.sleep(0)  # Admit the original operation before advancing the decision checkpoint.
        await self._collect_tools()
        self.store.mark_d4_operation_applied(identity, operation_id)
        self._commit(StageStateChange())
        get_logger("ctrl").info("stage.tool_dispatched", run=self.task_id, operation=operation_id,
            phase="tool_dispatch", in_flight=len(self._tools_in_flight))

    async def _stop_tools(self):
        from agent_core.dual_llm_modeling import MODEL_CANCEL_GRACE_SECONDS
        self._tools_stopping = True
        tasks = [entry.task for entry in self._tools_in_flight.values()]
        if tasks:
            # Let the original executors settle confirmed stops/results first.
            await asyncio.wait(tasks, timeout=MODEL_CANCEL_GRACE_SECONDS)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tools_in_flight.clear()

    async def _supervise(self, road_id, token):
        if self._supervision is not None:
            identity, pending = self._supervision
            if not pending.done():
                return
            try:
                _, advice = pending.result()
                self.store.mark_d4_operation_applied(identity, "stage-advice/" + identity)
                get_logger("ctrl").info("stage.strategy_reviewed", run=self.task_id, operation=identity,
                    code=advice.decision, category="strategy_signal", phase="stage_supervision")
            finally:
                self._supervision = None
        decisions = []
        for op in self.store.list_d4_operations(self.task_id):
            if op.start.category != "action" or op.application_ref is None:
                continue
            req = self.evidence.request(op.start.operation_id)
            value = self.evidence.read_model_result(op).result
            if isinstance(value, StagePlanningDecision) and value.kind == "wait_for_tools":
                continue
            if req.context.session == PlanningSession("road_execution", road_id) and req.correction_of_model_request_id is None:
                decisions.append(op)
        if decisions and len(decisions) % 4 == 0:
            request = self._request("stage_supervision", PlanningSession("road_execution", road_id))
            if self.store.load_d4_operation(request.model_request_id) is None:
                self._supervision = request.model_request_id, asyncio.create_task(self.models.call(request, token))
                await asyncio.sleep(0)  # Admit the read-only call on the same SQLite owner thread.

    async def run(self, token):
        from agent_adapters.run_files import run_lock
        with run_lock(self.directory):
            self._tools_stopping = False
            try:
                return await self._run(token)
            except Exception as exc:
                category, code, _ = classify(exc)
                get_logger("ctrl").warning("stage.run_stopped", run=self.task_id,
                    category=str(category), code=code, phase="stage_loop")
                raise
            finally:
                await self._stop_tools()

    def _handoff_result(self):
        for request, review, _ in reversed(self.evidence.applied_reviews(self.task_id)):
            if review.result.decision != "accept":
                continue
            value = request.proposal
            if isinstance(value, StagePlanningDecision) and value.kind in {"request_final_validation", "return_to_parent"}:
                turn = next(t for t in self.store.list_stage_turns(self.task_id)
                    if t.turn_id == request.context.session.owner_id or any(r.road_id == request.context.session.owner_id for r in t.roads))
                if value.kind == "request_final_validation" or turn.parent_turn_id is None:
                    if value.kind == "request_final_validation" and self.goal is not None:
                        if not self.goal.handoff(request, value):
                            continue
                        return StageRunResult(self.task_id, turn, "stage.awaiting_" + self.goal_validation + "_validation")
                    return StageRunResult(self.task_id, turn,
                        "stage.final_validation_requested" if value.kind == "request_final_validation" else "stage.root_blocked")
            if isinstance(value, PlanningExhausted) and not self.store.list_stage_turns(self.task_id):
                return StageRunResult(self.task_id, None, "stage.root_planning_exhausted")
        return None

    def _apply_planning(self, turn, proposal, review, binding):
        if review.decision != "accept":
            self._commit(StageStateChange(reviews=(binding,)))
            return
        changes = []
        if isinstance(proposal, TurnIntent):
            child = StageTurnSnapshot(self._id("stage/turn/" + binding.proposal_operation_id), proposal,
                TurnPlanningState.ACTIVE, turn.turn_id)
            changes = [replace(turn, planning_state=TurnPlanningState.WAITING_CHILD), child]
        elif isinstance(proposal, tuple):
            available = [r for r in turn.roads if r.status is IntentStatus.PENDING]
            pending = []
            for index in review.accepted_road_indices:
                candidate = proposal[index]
                existing = next((r for r in available if r.candidate == candidate), None)
                if existing:
                    available.remove(existing)
                pending.append(existing or RoadSnapshot(self._id("stage/road/" + binding.proposal_operation_id + "/" + str(index)),
                    candidate, IntentStatus.PENDING))
            if available:
                raise StageStateError("stage.pending_candidates_omitted", code="stage.pending_candidates_omitted")
            ordered = iter(pending)
            roads = [next(ordered) if r.status is IntentStatus.PENDING else r for r in turn.roads]
            changes = [replace(turn, roads=(*roads, *ordered))]
        elif isinstance(proposal, PlanningExhausted):
            changes = [replace(turn, planning_state=TurnPlanningState.EXHAUSTED)]
        elif proposal.kind == "select_road":
            changes = [replace(turn, roads=tuple(replace(r, status=IntentStatus.RUNNING)
                if r.road_id == proposal.road_id else r for r in turn.roads))]
        elif proposal.kind == "return_to_parent":
            changes = [replace(turn, roads=tuple(replace(r, status=IntentStatus.SUSPENDED)
                if r.status is IntentStatus.RUNNING else r for r in turn.roads))]
        if (isinstance(proposal, PlanningExhausted) or isinstance(proposal, StagePlanningDecision)
                and proposal.kind == "return_to_parent") and turn.parent_turn_id:
            parent = self.store.load_turn(self.task_id, turn.parent_turn_id)
            changes.append(replace(parent, planning_state=TurnPlanningState.ACTIVE))
        self._commit(StageStateChange(turns=tuple(changes), reviews=(binding,)))
        get_logger("ctrl").info("stage.planning_applied", run=self.task_id,
            operation=binding.review_operation_id, phase="stage_planning",
            code=proposal.kind if isinstance(proposal, StagePlanningDecision) else type(proposal).__name__,
            turn=turn.turn_id, parent=turn.parent_turn_id)

    async def _run(self, token):
        self._token = token
        await self._prepare_inputs(token)
        self.models.reconcile(self.task_id)
        self.mcp.reconcile()
        root_id = self._id("stage/root")
        while True:
            await self._collect_tools()
            task = self.state.task_run
            if self.goal is not None and task.status is not TaskRunStatus.RUNNING:
                self.controller._reload_after_commit()
                self.controller.advance_time(str(uuid4()))
                task = self.state.task_run
                return StageRunResult(self.task_id, active_turn(self.store.list_stage_turns(self.task_id)), "stage." + task.status.value)
            self._check_dispatch()
            handoff = self._handoff_result()
            if handoff:
                return handoff
            turn = active_turn(self.store.list_stage_turns(self.task_id))
            if turn is None:
                request = self._request("stage_intent", PlanningSession("turn_planning", root_id))
                proposal, review, binding = await self._review(request, token)
                changes = () if review.decision != "accept" or isinstance(proposal, PlanningExhausted) else (
                    StageTurnSnapshot(root_id, proposal, TurnPlanningState.ACTIVE),)
                self._commit(StageStateChange(turns=changes, reviews=(binding,)))
                if isinstance(proposal, PlanningExhausted) and review.decision == "accept":
                    return StageRunResult(self.task_id, None, "stage.root_planning_exhausted")
                continue
            running = next((r for r in turn.roads if r.status is IntentStatus.RUNNING), None)
            if turn.achievement is not None and running is None:
                if turn.planning_state is TurnPlanningState.EXHAUSTED:
                    return StageRunResult(self.task_id, turn, "stage.exhausted")
                if not _just_registered_roads(self.store, self.task_id, turn.turn_id):
                    proposal, review, binding = await self._review(self._request("stage_continuation",
                        PlanningSession("turn_planning", turn.turn_id)), token)
                    if binding is not None:
                        self._apply_planning(turn, proposal, review, binding)
                    continue
            if turn.planning_state is TurnPlanningState.EXHAUSTED:
                return StageRunResult(self.task_id, turn, "stage.exhausted")
            if running is None:
                pending = next((r for r in turn.roads if r.status is IntentStatus.PENDING), None)
                if pending is not None:
                    self._commit(StageStateChange(turns=(replace(turn, roads=tuple(
                        replace(r, status=IntentStatus.RUNNING) if r.road_id == pending.road_id else r for r in turn.roads)),)))
                    continue
                proposal, review, binding = await self._review(self._request("stage_roads", PlanningSession("turn_planning", turn.turn_id)), token)
                if binding is None:
                    continue
                self._apply_planning(turn, proposal, review, binding)
                continue
            await self._supervise(running.road_id, token)
            await self.mcp.open()
            if self._tools_in_flight and (len(self._tools_in_flight) >= MAX_IN_FLIGHT_TOOLS or not self._available_tools()):
                await self._collect_tools(wait=True)
                continue
            request = self._request("stage_action", PlanningSession("road_execution", running.road_id))
            identity, action = await self.models.call(request, token)
            await self._collect_tools()
            self._check_dispatch()
            if isinstance(action, StagePlanningDecision) and action.kind == "wait_for_tools":
                self._commit(StageStateChange(applications=(identity,)))
                await self._collect_tools(wait=True)
                continue
            if not isinstance(action, MCPCall):
                await self._collect_tools(drain=True)
            if await self._knowledge_action(request, identity, action, token):
                continue
            if isinstance(action, MCPCall):
                self.mcp.validate(SimpleNamespace(history_context=json.dumps({"mcp_tools": list(request.tools)})), action)
                await self._start_tool(identity, running.road_id, action, token)
                continue
            review_request = self._request("stage_review", request.context.session, proposal_id=identity, proposal=action)
            review_id, review = await self.models.call(review_request, token)
            binding = StageReviewBinding(identity, review_id)
            if review.decision != "accept":
                self._commit(StageStateChange(reviews=(binding,)))
                continue
            if isinstance(action, StagePlanningDecision):
                self._apply_planning(turn, action, review, binding)
                continue
            if isinstance(action, TurnCompletion):
                turn = replace(turn, achievement=TurnAchievement(action, review_id),
                    fact_refs=tuple(dict.fromkeys((*turn.fact_refs, *action.support_refs))))
            elif isinstance(action, RoadResolution):
                turn = replace(turn, roads=tuple(replace(r, status=action.outcome) if r.road_id == running.road_id else r for r in turn.roads))
            self._commit(StageStateChange(turns=(turn,), reviews=(binding,)))
            if isinstance(action, RoadResolution) and turn.achievement is None:
                request = self._request("stage_completion", PlanningSession("turn_planning", turn.turn_id))
                identity, completion = await self.models.call(request, token)
                if isinstance(completion, StageIncomplete):
                    self.store.mark_d4_operation_applied(identity, "stage-incomplete/" + identity)
                    self._commit(StageStateChange())
                else:
                    review_request = self._request("stage_review", request.context.session, proposal_id=identity, proposal=completion)
                    review_id, review = await self.models.call(review_request, token)
                    binding = StageReviewBinding(identity, review_id)
                    if review.decision == "accept":
                        turn = replace(turn, achievement=TurnAchievement(completion, review_id),
                            fact_refs=tuple(dict.fromkeys((*turn.fact_refs, *completion.support_refs))))
                    self._commit(StageStateChange(turns=(turn,), reviews=(binding,)))

    async def _prepare_inputs(self, token):
        from agent_core.attachment_import import AttachmentImportRequest
        task = self.state.task_run
        if not task.request.target.attachments:
            return
        if self.importer is None:
            raise StageStateError("stage.attachment_import_not_connected", code="stage.attachment_import_not_connected")
        records = self.evidence.read("imports", self.task_id)
        if records is None:
            records = {"items": [{"import_id": str(uuid4()), "index": index,
                "sandbox_path": "/workspace/input/d3-attachment-" + str(index)} for index in self.config.authorization.attachment_indices]}
            self.evidence.save("imports", self.task_id, records)
        for item in records["items"]:
            if self.imports.read_receipt(item["import_id"]) is not None:
                continue
            self._check_dispatch()
            if self.imports.read_claim(item["import_id"]) is None:
                request = AttachmentImportRequest(item["import_id"], self.task_id, task.sandbox_id,
                    item["index"], task.source_root, item["sandbox_path"])
                await self.importer.import_attachment(request, token)
            else:
                await self.importer.recover_import(item["import_id"], token)

    async def close(self):
        await self._stop_tools()
        if self._supervision:
            _, pending = self._supervision
            if not pending.done():
                pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            self._supervision = None
        await self.mcp.close()
        self.store.close()
