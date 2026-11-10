"""Internal stage scheduling over the existing controller, MCP and ledger."""

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
from uuid import uuid4

from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_adapters.stage_evidence import StageEvidence, StageModelCalls, StageModelTransportFailure, StageModelSuperseded, StageActionStalled
from agent_adapters.stage_reviews import StageReviews
from agent_adapters.output_evidence import safe_failure_details, trace_frames
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
    StageMethodRegistration, TurnCriterionRevision, StageFactClosure,
    PlanningExhausted, PlanningSession, RoadResolution, RoadSnapshot, RoadTurnClosure, RoadTurnClosureReview, StageFactApplication,
    StageConclusionChange, StageFactSelection, StageHistoryRead, StageToolDetails, StageFactProposal, StageIncomplete,
    StageReviewBinding, StageRunConfig, StageStateChange, StageTurnSnapshot,
    TurnAchievement, TurnCompletion, TurnPlanningContext, TurnPlanningState, TurnIntent, StagePlanningDecision, MAX_IN_FLIGHT_TOOLS,
    TOOL_BATCH_WAIT_SECONDS,
    StageToolBatch, tool_calls, tool_call_key, StageFactBatch, fact_candidates, reviewed_fact, StageBranchFork, StageFactRevision,
)
from agent_core.stage_planning import StageModelRequest, stage_runtime, review_members, formal_support_refs
from agent_core.stage_storage import StageRecoveryRequired, StageStateError, active_turn, _just_registered_roads, execution_policy, load_branch, branch_prefix, duplicate_fact
from agent_core import stage_plans, stage_storage
from agent_core.turn_modeling import digest, stable_id
from qinyuan_core import Category, Layer, classify, get_logger

# A recovered request keeps this many newest history entries inline; the rest
# stay addressable by ref, so recovery can shrink below the bound that failed.
HISTORY_AFTER_RECOVERY = 8
# Small original results stay readable as an index; larger bodies require an exact read.
ACTION_OBSERVATION_BYTES = 4096


def _compact_context(context):
    """A past decision's snapshot without a second copy of every observation.

    The refs stay, and ``history_details`` resolves them to the original text,
    so nothing becomes unreachable; only the duplicated payload goes away.
    """
    value = asdict(context)
    value.pop("history", None)
    value["history_refs"] = [item.ref for item in context.history]
    return value


def _action_observation_index(item):
    # Command arguments are available through history_details. This result view
    # preserves literal output and error/truncation labels without another script copy.
    result = json.loads(item.content)
    result.pop("command", None)
    size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return {**item.visible(include_content=False), "result_bytes": size,
        **({"result": result} if size <= ACTION_OBSERVATION_BYTES else {})}


@dataclass(frozen=True, slots=True)
class StageRunResult:
    task_run_id: str
    turn: StageTurnSnapshot | None
    reason: str


def catalog_with_completion_support(catalog, eligible_refs):
    """Mark which catalogued facts may support turn_completion.

    ``history_catalog`` is deliberately wider than the live context window, so a
    readable ref can still be ineligible for completion.  Keeping the marker on
    the entry tells the model which of them to drop instead of letting it cite
    an ineligible ref and then receive an opaque ``allowed_choice`` rejection.
    """
    eligible = set(eligible_refs)
    return tuple({**entry, "completion_eligible": entry["ref"] in eligible} for entry in catalog)


class StageApplication:
    def __init__(self, directory, *, config, sandbox, workspace_sandbox, inner_model, outer_model,
                 clock, capacity_port, mcp_manager=None, target_network_adapter=None, sensitive_values=(), goal_validation=None, http_sandbox=None, import_sandbox=None, successor_drafts=False):
        self.directory, self.config, self.clock = directory, config, clock
        self.goal_validation = goal_validation
        self.successor_drafts = successor_drafts
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
        self.task_id, self._token = None, None
        self._tools_in_flight = {}
        self._tools_stopping = False
        self.reviews = StageReviews(self)
        self.store.live_stage_review_operation = self.reviews.live
        self.store.live_stage_foreground_operation = self.reviews.live_foreground
        self.store.admit_stage_review = self.reviews.admits
        self.store.live_tool_request = self._live_tool_request
        self.store.live_tool_operation = self._live_tool_operation
        self.store.live_http_model = self._live_http_model
        self.store.live_tool_calls = lambda: tuple((entry.parent, entry.call) for entry in self._tools_in_flight.values()
            if not entry.task.done())
        self._target_network = target_network_adapter
        self.models.messages = lambda session, stage=None: self._conversation(session,
            action_history=self.evidence.history(self.task_id) if stage == "stage_action" else None)
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
        policy = execution_policy(self.config)
        if self.successor_drafts:
            policy["successor_drafts"] = "isolated_v1"
        return policy

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
        # Calls that completed without their application commit are invisible to
        # the conversation while still counting as pending for every later commit.
        # Detection only: reconciliation is deliberately not automatic here, since
        # the restart path already resumes some of these operations itself.
        orphans = stage_storage.orphaned_operations(self.store, task_id)
        if orphans:
            get_logger("ctrl").warning("stage.orphan_operations_detected", category="recovery",
                operations=[record.start.operation_id for record in orphans],
                categories=[record.start.category for record in orphans])

    def _freeze_models(self):
        from agent_adapters.image_observations import ImageObservations
        images = ImageObservations(self.directory, self.task_id, self.evidence)
        for model in (self.models.inner, self.models.outer):
            model.image_loader = images.load
        self.models.capacity_port.image_loader = images.load
        configured = [model for model in (self.models.inner, self.models.outer) if hasattr(model, "settings")]
        if configured:
            from agent_adapters.deepseek_roles import freeze_timeout_policy
            freeze_timeout_policy(self.evidence, self.task_id, *configured)
            for model in configured:
                model.bind_diagnostics(self.evidence)

    def _check_dispatch(self, *, tool_resource=None):
        task = self.state.task_run
        if self._token is not None and self._token.is_cancelled or self.clock() >= task.deadline_at or task.status is not TaskRunStatus.RUNNING:
            raise StageRecoveryRequired("stage.dispatch_stopped", category=Category.POLICY, layer=Layer.CTRL)
        from agent_core.operator_controls import check_admission
        check_admission(self.store, self.task_id)
        from agent_core.stage_isolation import isolated_http_operations
        if tool_resource is not None and '/experiment/' in tool_resource and isolated_http_operations(self.store, self.task_id):
            raise StageRecoveryRequired('stage.resource_outcome_unknown', layer=Layer.CTRL)
        blocking = self.store.blocking_d4_operations(self.task_id, tool_resource=tool_resource)
        if blocking:
            raise StageRecoveryRequired("stage.operation_outcome_unknown", layer=Layer.CTRL,
                operation_admission=dict(blocking_count=len(blocking), blocking_operations=[dict(
                    operation_id=r.start.operation_id, category=r.start.category, role=r.start.role,
                    resource_ref=r.start.resource_ref) for r in blocking[:6]]))

    def _action_limits(self, task_id, turn_id, road_id, *, tool_resource=None):
        self._check_dispatch(tool_resource=tool_resource)
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
        result = self.store.commit(mutation)
        for identity in change.review_submissions:
            get_logger("ctrl").info("stage.review_queued", run=self.task_id, operation=identity, phase="stage_review")
        for binding in (*change.reviews, *change.blocked_reviews):
            get_logger("ctrl").info("stage.review_settled" if binding in change.reviews else "stage.review_blocked",
                run=self.task_id, operation=binding.review_operation_id,
                source_operation=binding.proposal_operation_id, phase="stage_review")
            request = self.evidence.request(binding.review_operation_id)
            if binding in change.reviews and fact_candidates(request.proposal) and stage_runtime(request).get("method_reuse") == "reviewed_same_task_v3":
                from agent_core.stage_methods import StageMethods, method_review_counts
                verdict = self.evidence.read_model_result(self.store.load_d4_operation(binding.review_operation_id)).result
                collected = sum(m["review_operation_id"] == binding.review_operation_id
                    for m in StageMethods(self.evidence, self.task_id).catalog())
                get_logger("ctrl").info("stage.method_reviewed", run=self.task_id,
                    operation=binding.review_operation_id, phase="stage_review",
                    **method_review_counts(request, verdict, collected))
        return result

    def _conversation_sources(self, session):
        sources = []
        branch = load_branch(self.store, self.task_id, session)
        visible = set(branch_prefix(self.store, branch)) if branch else set()
        queued = {entry.proposal_operation_id for entry in self.store.list_stage_reviews(self.task_id)
            if entry.state in {"pending", "blocked"}}
        for op in self.store.list_d4_operations(self.task_id):
            if op.application_ref is None and op.start.operation_id not in queued or op.start.category == "tool_dispatch":
                continue
            saved = self.evidence.read("request", op.start.operation_id)
            if saved is None:
                continue
            request = self.evidence.request(op.start.operation_id)
            if request.context.session != session and op.start.operation_id not in visible:
                continue
            if (branch is not None and op.start.operation_id not in visible
                    and stage_runtime(request).get("branch_id") != branch.branch_id
                    and not (request.stage == "stage_review" and set(review_members(request)).intersection(visible))):
                continue
            result = self.evidence.read("result", op.start.operation_id)
            if result is None or result["outcome"] != "valid":
                continue
            proof = self.evidence.read_model_result(op)
            if op.application_ref is None and not self.reviews.eligible(op.start.operation_id):
                continue
            sources.append((op, request, result))
            visible.add(op.start.operation_id)
        # Preserve the order already shown to this session. A late background
        # review is appended, never inserted ahead of previously visible work.
        order = []
        for message in sources[-1][1].messages if sources else ():
            if message['role'] != 'user':
                continue
            value = json.loads(message['content'])
            if isinstance(value, dict):
                order.extend(identity for identity, _ in value.get('coverage', ()))
                if 'source_operation_id' in value:
                    order.append(value['source_operation_id'])
        ranks = {identity: n for n, identity in enumerate(order)}
        sources.sort(key=lambda row: ranks.get(row[0].start.operation_id, len(ranks)))
        return sources

    def _render_conversation(self, sources, *, action_history=None):
        # The live action context carries current evidence. Its archive uses
        # refs instead of repeating old evidence snapshots; other stages keep
        # the two most recent full snapshots.
        contextual = [index for index, (_, request, _) in enumerate(sources)
                      if request.stage not in {"stage_review", "stage_supervision"}]
        keep = set(contextual[-2:])
        receipts = {}
        owners = {request.context.session.owner_id for _, request, _ in sources if request.stage == "stage_action"}
        for item in action_history or ():
            if (item.identity not in {"tool_observation_not_fact", "public_reference_not_target_observation"}
                    or item.source_status != "available" or item.intent_id not in owners):
                continue
            claim = self.evidence.read("mcp-call", item.record_id)
            if claim is not None:
                receipts.setdefault((claim["model_request_id"], digest(claim["call"])), []).append(item)
        messages = []
        for index, (op, request, result) in enumerate(sources):
            content = result["content"]
            if "tool_calls" in result:
                from agent_core.stage_planning import decode_stage_response
                from agent_core.output_contracts import D8ModelResponse
                value, _, _, _ = decode_stage_response(request,
                    D8ModelResponse(content, result["finish_reason"], result["tool_calls"]))
                content = json.dumps(value, ensure_ascii=False)
            if request.stage in {"stage_review", "stage_supervision"}:
                feedback = {"source_operation_id": op.start.operation_id, "review_feedback": content}
                if request.stage == "stage_review" and len(review_members(request)) > 1:
                    feedback["reviewed_candidates"] = [{"proposal_operation_id": identity, "candidate_index": index,
                        "candidate": asdict(claim)} for identity, index, claim in self.evidence.review_claims(request)]
                messages.append({"role": "user", "content": json.dumps(feedback, ensure_ascii=False)})
            else:
                proof = self.evidence.read_model_result(op).result
                calls = tool_calls(proof)
                from agent_core.stage_experiments import StageToolSequence
                if isinstance(proof, StageToolSequence) and op.application_ref is not None:
                    calls = tuple(MCPCall(t["tool_name"], t["arguments"]) for t in proof.steps)
                if action_history is not None and index not in keep and calls:
                    indexed = []
                    for call in calls:
                        items = receipts.get((op.start.operation_id, digest(asdict(call))), ())
                        indexed.append({"tool_name": call.tool_name, "observations": [
                            _action_observation_index(item) for item in items]}
                            if items else asdict(call))
                    # Unsettled calls retain exact arguments. A recorded result
                    # is an observation, never an automatic Fact or execution cache.
                    content = json.dumps({"kind": "recorded_tool_calls", "calls": indexed}, ensure_ascii=False)
                messages.extend([{"role": "user", "content": handoff_json({"source_operation_id": op.start.operation_id,
                    "stage": request.stage,
                    "context": asdict(request.context) if action_history is None and index in keep else _compact_context(request.context)})},
                    {"role": "assistant", "content": content if isinstance(content, str)
                     else json.dumps(content, ensure_ascii=False)}])
                if isinstance(proof, StageHistoryRead):
                    items = self.evidence.read_history_result(op)
                    body = ({"history_refs": [i.ref for i in items]}
                        if action_history is not None and index not in keep else
                        {"history_read": [asdict(i) for i in items],
                         "tool_requests": self.evidence.history_tool_requests(items)})
                    messages.append({"role": "user", "content": json.dumps(body, ensure_ascii=False)})
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

    def _conversation(self, session, sources=None, *, action_history=None):
        sources = self._conversation_sources(session) if sources is None else sources
        covered, prefix = self._summary_view(sources)
        current = [s for s in sources if s[0].start.operation_id not in covered and s[1].stage != "stage_summary"]
        return (*prefix, *self._render_conversation(current, action_history=action_history))

    def _planning_history(self, history, sources, *, required=(), retrieved=()):
        """Only an exact session summary can replace old raw observations by catalog refs."""
        covered, _ = self._summary_view(sources)
        if not covered:
            return history
        versions, retained = set(), set(required) | set(retrieved)
        for operation, request, _ in sources:
            if request.stage == "stage_summary":
                continue
            refs = {i.ref for i in request.context.history}
            if operation.start.operation_id in covered:
                versions.update(digest(asdict(i)) for i in request.context.history)
                value = self.evidence.read_model_result(operation).result
                if isinstance(value, StageHistoryRead):
                    versions.update(digest(asdict(i)) for i in self.evidence.read_history_result(operation))
            else:
                # Uncovered decisions (including the recent four) keep their full context.
                retained.update(refs)
                value = self.evidence.read_model_result(operation).result
                if isinstance(value, StageHistoryRead):
                    retained.update(value.refs)
        # Keep direct evidence for selected/current conclusions and all necessary
        # premises. Revision/feedback records and raw observations never seen by
        # the summary remain full; no semantic success/failure classifier is used.
        retained.update(ref for item in history if item.record_type != "progress" for ref in item.source_refs)
        return tuple(i for i in history if i.identity != "tool_observation_not_fact"
            or i.record_type != "progress" or digest(asdict(i)) not in versions or i.ref in retained)

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
        if self.reviews.job is not None and asyncio.current_task() is not self.reviews.job.task:
            await asyncio.wait({self.reviews.job.task})
            await asyncio.sleep(0)
            if getattr(token, "superseded_by", None) is not None:
                raise StageModelSuperseded(request.model_request_id)
        summary_id, _ = await self.models.call(summary_request, token)
        self.store.mark_d4_operation_applied(summary_id, "stage-summary/" + summary_id)
        messages = self._conversation(request.context.session,
            action_history=self.evidence.history(self.task_id) if request.stage == "stage_action" else None)
        self.evidence.save("capacity-recovery", request.model_request_id, {
            "source_request_sha256": digest(asdict(request)), "summary_operation_id": summary_id})
        # The recovered request has to be able to shrink past the bound that
        # triggered it.  Conversation is already summarized; the live history is
        # the remaining fixed cost, so keep its newest entries and leave the rest
        # readable by ref through history_catalog / history_details.
        history = request.context.history[-HISTORY_AFTER_RECOVERY:]
        # The trimmed history is part of the output schema, so this request has a
        # different contract than its source. Clear the inherited hash exactly as
        # the summary and correction paths do; call() freezes the new contract.
        return replace(request, model_request_id=self._id("stage/capacity/" + request.model_request_id + "/" + summary_id),
            messages=messages, capacity_recovery_of=request.model_request_id,
            context=replace(request.context, history=history), frozen_contract_ref=None)

    def _intent_revision(self, stage, session, branch, proposal_id, proposal):
        """Project the exact author correction; never infer ancestry from text."""
        if session.scope != "turn_planning":
            return None
        if stage == "stage_review" and isinstance(proposal, TurnIntent):
            source = self.evidence.request(proposal_id)
            prior = stage_runtime(source).get("intent_revision")
            if prior is None:
                return None
            old_id, review_id = prior["proposal_operation_id"], prior["review_operation_id"]
        elif stage in {"stage_intent", "stage_continuation"} and branch and branch.review_operation_id:
            old_id, review_id = branch.proposal_operation_id, branch.review_operation_id
        else:
            return None
        original = self.evidence.read_model_result(self.store.load_d4_operation(old_id))
        if not isinstance(original.result, TurnIntent):
            return None
        if (self.evidence.request(old_id).context.session != session
                or old_id not in self.evidence.review_rejections(review_id)):
            raise StageRecoveryRequired("stage.branch_correction_unbound")
        review = self.evidence.read_model_result(self.store.load_d4_operation(review_id)).result
        if review.decision != "revise":
            return None
        previous = asdict(original.result)
        value = {"proposal_operation_id": old_id, "review_operation_id": review_id,
            "previous_proposal": previous, "feedback": asdict(review)}
        if isinstance(proposal, TurnIntent):
            current = asdict(proposal)
            value["changed_fields"] = [key for key in previous if previous[key] != current[key]]
        return value

    def _request(self, stage, session, *, proposal_id=None, proposal=None, closure_source=None):
        # Only synchronous stages resume an outstanding proposal directly.
        if stage not in {"stage_review", "stage_supervision", "stage_summary"}:
            for entry in self.store.list_stage_reviews(self.task_id):
                if entry.state != "pending":
                    continue
                source = self.evidence.request(entry.proposal_operation_id)
                if source.stage == stage and source.context.session == session:
                    if self.reviews.eligible(entry.proposal_operation_id):
                        continue
                    return source
        state = self.state
        identity = self._id("stage/model/" + session.scope + "/" + session.owner_id + "/" + stage
            + "/" + str(state.latest_checkpoint.sequence) + "/" + (proposal_id or ""))
        if self.evidence.read("request", identity) is not None:
            return self.evidence.request(identity)
        turn = next((t for t in self.store.list_stage_turns(self.task_id)
            if t.turn_id == session.owner_id or any(r.road_id == session.owner_id for r in t.roads)), None)
        if stage == "stage_intent":
            turn = None
        if isinstance(proposal, TurnCriterionRevision):
            origin = self.evidence.request(proposal_id)
            target_id = stage_runtime(origin).get("criterion_target")
            target = self.store.load_turn(self.task_id, target_id) if target_id else origin.context.turn
            if target is not None:
                turn = self.store.load_turn(self.task_id, target.turn_id)
        if stage == "stage_action":
            queued = {entry.proposal_operation_id for entry in self.store.list_stage_reviews(self.task_id)}
            resource = "stage/" + session.scope + "/" + session.owner_id
            for operation in reversed(self.store.list_d4_operations(self.task_id)):
                if (operation.start.category != "action" or operation.application_ref is not None
                        or operation.start.resource_ref != resource
                        or operation.completion is None or operation.completion.result_kind != "completed"
                        or operation.start.operation_id in queued):
                    continue
                original = self.evidence.request(operation.start.operation_id)
                if original.stage != stage or original.context.session != session:
                    continue
                if stage_storage.unchanged_action(self.store, operation):
                    get_logger("ctrl").info("stage.decision_resumed", run=self.task_id,
                        operation=operation.start.operation_id, phase=stage)
                    return original
                self.store.mark_d4_operation_applied(operation.start.operation_id, "stage-discarded/state_changed")
                get_logger("ctrl").info("stage.decision_discarded", run=self.task_id,
                    operation=operation.start.operation_id, phase=stage, code="state_changed")
        all_history = self.evidence.history(self.task_id)
        sources = self._conversation_sources(session)
        reads = [self.evidence.read_model_result(op).result for op, _, _ in sources]
        retrieved = next((value.refs for value in reversed(reads) if isinstance(value, StageHistoryRead)), ())
        if turn is None:
            history, premises = all_history, ()
        else:
            history, premises = self.evidence.knowledge(self.task_id).context(session, retrieved)
        required = {ref for _, claim in fact_candidates(proposal) for ref in claim.support_refs} | set(getattr(proposal, "support_refs", ())) | set(getattr(proposal, "evidence_refs", ()))
        closing = stage == "stage_completion" or stage == "stage_review" and isinstance(proposal, (RoadResolution, TurnCompletion))
        if isinstance(proposal, RoadTurnClosure):
            required.update(proposal.completion.support_refs)
        if isinstance(proposal, StageConclusionChange):
            required.add(proposal.replacement_ref)
            required.update(i.ref for i in all_history if getattr(i, "logical_root_id", None) in {t.root_id for t in proposal.targets})
        refs = {i.ref for i in history} | required
        history = tuple(i for i in all_history if i.ref in refs)
        if stage == "stage_continuation" and session.scope == "turn_planning":
            required.update(premises)
            if turn is not None:
                required.update(turn.intent.support_refs)
                if turn.achievement:
                    required.update(turn.achievement.completion.support_refs)
                required.update(ref for road in turn.roads for ref in road.candidate.support_refs)
            history = self._planning_history(history, sources, required=required, retrieved=retrieved)
        if closing:
            history = tuple(i for i in history if i.ref in required or i.ref in premises or i.ref in retrieved
                or stage == "stage_completion" and i.record_type == "verified_fact")
        children = {t.turn_id for t in self.store.list_stage_turns(self.task_id) if turn and t.parent_turn_id == turn.turn_id}
        context = TurnPlanningContext(self.task_id, state.task_run.request.goal, session, turn, history,
            feedback_refs=tuple(i.ref for i in history if i.record_id.startswith("feedback/") and i.turn_id in children
                or i.record_id.startswith("validation/") and turn and i.turn_id == turn.turn_id),
            pending_operation_ids=tuple(dict.fromkeys((*(entry.operation_id for entry in self._tools_in_flight.values()),
                *(r.start.operation_id for r in self._isolated_operations())))),
            effective_support_refs=premises, goal_validation=self.goal_validation)
        inner = stage != "stage_review" and stage != "stage_supervision"
        role = self.config.inner_role if inner else self.config.outer_role
        if closing:
            messages = self._render_conversation(tuple(s for s in sources if s[1].context.session == session
                and (s[1].stage == "stage_review" and isinstance(s[1].proposal, (TurnCompletion, RoadResolution))
                     or s[1].stage == "stage_completion"))[-2:], action_history=())
            if closure_source is not None:
                reviewed, review_request = closure_source
                messages += ({"role": "user", "content": json.dumps({
                    "source_operation_id": review_request.model_request_id,
                    "settled_road": asdict(review_request.proposal),
                    "review": asdict(reviewed),
                }, ensure_ascii=False)},)
                if (stage_runtime(review_request).get("joint_fact_closure")
                        and not isinstance(review_request.proposal, RoadTurnClosure)):
                    original = self.evidence.read_model_result(self.store.load_d4_operation(review_request.proposal_operation_id)).result
                    if isinstance(original.closure, RoadTurnClosure):
                        messages += ({"role": "user", "content": json.dumps({
                            "source_operation_id": review_request.proposal_operation_id,
                            "code": "stage.joint_turn_support_missing",
                            "reason": "The conditional Turn completion was not submitted: its selected candidate supports were not accepted. Propose a new completion with valid formal support or report incomplete.",
                        })},)
        else:
            messages = self._conversation(session, sources, action_history=all_history if stage == "stage_action" else None)
        if (stage == "stage_review" and isinstance(proposal, StagePlanningDecision)
                and proposal.kind == "request_final_validation" and session.scope == "road_execution"):
            items = stage_storage.candidate_validation_sources(self.store, self.task_id, proposal, session)
            messages += ({"role": "user", "content": json.dumps({"validation_tool_requests":
                self.evidence.history_tool_requests(items)}, ensure_ascii=False)},)
        if stage == "stage_review" and (fact_candidates(proposal) or isinstance(proposal, StageConclusionChange)):
            # A Fact judgement reads the claim and its own frozen evidence, not
            # the author's argument for it. User turns (review feedback, tool
            # observations) stay; only the author's own prose is withheld.
            messages = tuple(message for message in messages if message.get("role") == "user")
        from agent_core.stage_methods import StageMethods, relevant_methods, method_review_inputs
        method_service = StageMethods(self.evidence, self.task_id)
        method_policy = self.evidence.read("policy", self.task_id).get("method_reuse")
        method_candidates, method_diagnostics = {}, {}
        if method_policy == "reviewed_same_task_v3" and stage == "stage_review" and fact_candidates(proposal):
            method_candidates, bodies, method_diagnostics = method_service.candidates(proposal, all_history)
            if bodies:
                messages += ({"role": "user", "content": json.dumps({"method_tool_requests": bodies}, ensure_ascii=False)},)
        if stage == "stage_review" and (fact_candidates(proposal) or isinstance(proposal, StageMethodRegistration)):
            from agent_core.stage_contracts import StageMethodFact
            method_sources = (proposal.method["source_ref"],) if isinstance(proposal, StageMethodRegistration) else tuple(dict.fromkeys(claim.method["source_ref"]
                for _, claim in fact_candidates(proposal) if isinstance(claim, StageMethodFact)))
            if method_sources:
                items = self.evidence.read_history(self.task_id, method_sources, observations_only=True)
                bodies = []
                for item in items:
                    for body in self.evidence.history_tool_requests((item,)):
                        captured = self.evidence.read("mcp-method-inputs", item.record_id)
                        if captured is not None:
                            body["captured_inputs"] = method_review_inputs(body["call"], captured)
                        bodies.append(body)
                messages += ({"role": "user", "content": json.dumps({"method_tool_requests": bodies}, ensure_ascii=False)},)
        if stage == "stage_action" and self._tools_in_flight:
            messages += ({"role": "user", "content": json.dumps({"pending_tools": [
                {"operation_id": entry.operation_id, **asdict(entry.call)} for entry in self._tools_in_flight.values()]},
                ensure_ascii=False)},)
        feedback = []
        for operation, _, _ in (() if closing else sources):
            if any(call.tool_name == "http.run_experiment" for call in tool_calls(
                    self.evidence.read_model_result(operation).result)):
                result = self.mcp.experiments.result(operation.start.operation_id)
                if result is not None:
                    feedback.append({"operation_id": operation.start.operation_id, "result": result})
        if feedback:
            messages += ({"role": "user", "content": json.dumps({"tool_feedback": feedback}, ensure_ascii=False)},)
        branch = load_branch(self.store, self.task_id, session)
        visible = set(branch_prefix(self.store, branch)) if branch else set()
        runtime = {"remaining_seconds": max(0, state.task_run.deadline_at - self.clock()),
            "review_mode": "async_planning_v1", "branch_id": branch.branch_id if branch else None,
            "pending_reviews": [
                {"proposal_operation_id": entry.proposal_operation_id, "state": entry.state}
                for entry in self.store.list_stage_reviews(self.task_id)
                if entry.state in {"pending", "blocked"}
                and (self.evidence.request(entry.proposal_operation_id).context.session == session
                    or entry.proposal_operation_id in visible)]}
        runtime["methods_enabled"] = method_policy in {"reviewed_same_task_v2", "reviewed_same_task_v3"}
        runtime["methods"] = relevant_methods(method_service.catalog(history=all_history,
            diagnostics=method_diagnostics), context.history)
        if method_policy == "reviewed_same_task_v3":
            runtime["method_reuse"] = method_policy
            if method_candidates:
                runtime["method_candidates"] = method_candidates
            if stage == "stage_action":
                runtime["working_scripts"] = method_service.working_scripts(all_history, preferred=retrieved)
            if method_candidates or method_diagnostics:
                get_logger("ctrl").info("stage.method_availability", run=self.task_id, operation=identity,
                    phase=stage, candidates=sum(map(len, method_candidates.values())),
                    available=len(runtime["methods"]), reasons=method_diagnostics)
        if closing:
            runtime["closure_context"] = True
        if stage == "stage_action":
            runtime["road_turn_closure"] = turn is not None and turn.achievement is None
            if self.goal_validation == "external":
                rejected = next(self._direct_rejections(road_id=session.owner_id), None)
                if rejected is not None:
                    operation, value = rejected
                    runtime["direct_candidate_rejection"] = {
                        "operation_id": operation.start.operation_id, "source_ref": value.source_ref,
                        "reason": "candidate_not_grounded_in_cited_original",
                    }
            from agent_core.stage_experiments import StageToolSequence
            applied = [(operation.start.operation_id, value)
                for (operation, source, _), value in zip(reversed(sources), reversed(reads))
                if source.stage == stage and source.context.session == session and operation.application_ref is not None
                and not operation.application_ref.startswith("stage-discarded/")]
            actions = [value for _, value in applied]
            runtime["action_checkpoint"] = None
            for operation_id, value in applied:
                if isinstance(value, StagePlanningDecision) and value.kind == "action_checkpoint":
                    runtime["action_checkpoint"] = {"operation_id": operation_id,
                        "reason": value.reason, "support_refs": list(value.support_refs)}
                    break
                if isinstance(value, StageToolSequence) or tool_calls(value):
                    break

            def tool_names(value):
                if isinstance(value, StageToolDetails):
                    return value.tool_names if value.query is None else ()
                if isinstance(value, StageToolSequence):
                    return tuple(step["tool_name"] for step in value.steps)
                return tuple(call.tool_name for call in tool_calls(value))

            names = (name for value in actions for name in tool_names(value))
            runtime["tool_details"] = {"paged_search": True, "recent": list(dict.fromkeys(names))[:8],
                "use_category_defaults": not any(isinstance(value, StageToolDetails) and value.tool_names for value in actions)}
            if actions and isinstance(actions[0], StageToolDetails) and actions[0].query is not None:
                search = actions[0]
                runtime["tool_details"]["last_search"] = {"query": search.query,
                    "matches": list(search.tool_names), "remaining_tools": list(search.remaining_tools)}
            for operation, _, _ in reversed(sources):
                result = self.evidence.read("sequence-result", operation.start.operation_id)
                if result is not None:
                    runtime["last_experiment"] = {"operation_id": operation.start.operation_id, **result}
                    break
        if turn is not None:
            from agent_core.stage_storage import criterion_version
            runtime["criterion_version"] = criterion_version(self.store, self.task_id, turn)
            fact_revisions = self.evidence.knowledge(self.task_id).visible_fact_revisions(turn.turn_id)
            if fact_revisions:
                runtime["fact_revisions"] = fact_revisions
        revision = self._intent_revision(stage, session, branch, proposal_id, proposal)
        if revision is not None:
            runtime["intent_revision"] = revision
            if stage in {"stage_intent", "stage_continuation"}:
                target = next((scope.owner_id for scope, identity in stage_plans.bindings(self.store, self.task_id)
                    if scope.scope == "turn_planning" and identity == revision["proposal_operation_id"]), None)
                target = self.store.load_turn(self.task_id, target) if target else None
                if target is not None and target.planning_state is TurnPlanningState.SUPERSEDED and target.achievement is None:
                    from agent_core.stage_storage import criterion_version
                    runtime["criterion_version"] = criterion_version(self.store, self.task_id, target)
                    runtime["criterion_target"] = target.turn_id
        if (self.goal is not None and self.goal_validation == "external"
                and not self.store.list_d7_answer_candidates(self.task_id)
                and any(i.record_type == "progress" and i.identity == "tool_observation_not_fact"
                        and i.source_status == "available" for i in context.history)):
            runtime["direct_submission"] = "first_flag_v1"
        if stage == "stage_roads":
            advice = stage_plans.route_advice(self.store, self.task_id)
            if advice and self.evidence.request(advice).context.turn.turn_id == session.owner_id:
                runtime["route_advice"] = advice
                value = self.evidence.read_model_result(self.store.load_d4_operation(advice)).result
                runtime["route_reassessment"] = {"operation_id": advice, "reason": value.reason,
                    "support_refs": list(value.support_refs)}
        if stage == "stage_action" and session.scope == "road_execution":
            runtime["fact_closure"] = True
            for operation, source, _ in reversed(sources):
                if source.context.session != session:
                    continue
                if source.stage == "stage_action":
                    elapsed = operation.completion.finished_at - operation.start.started_at
                    threshold = self.evidence.read("contract", operation.start.operation_id).get("action_slow_seconds")
                    if threshold and elapsed >= threshold:
                        runtime["slow_action"] = {"operation_id": operation.start.operation_id,
                            "elapsed_seconds": elapsed, "reminder_seconds": threshold}
                    break
                if source.stage == "stage_supervision" and stage_runtime(source).get("action_stall"):
                    runtime["action_stall"] = {**stage_runtime(source)["action_stall"],
                        "supervision_operation_id": operation.start.operation_id}
                    break
            # The original planner decides whether to close this Road; the
            # suggestion only says which available sibling would start next.
            suggestion = stage_plans.road_suggestion(self.store, self.task_id, turn, session.owner_id)
            if suggestion is not None:
                runtime["road_suggestion"] = suggestion
        if stage == "stage_action" and self.evidence.read("policy", self.task_id).get("tool_execution", {}).get("fact_sources") == "catalog_readback_v1":
            runtime["fact_sources"] = "catalog_readback_v1"
        isolated = self._isolated_operations()
        if isolated:
            runtime['execution_unknown'] = {'scope': 'authorized_target',
                'operations': [r.start.operation_id for r in isolated],
                'diagnostics': [dict(operation_id=r.start.operation_id,
                    **{key: value for key, value in self.evidence.read('experiment-failure', r.start.operation_id)
                        .get('http_diagnostic', {}).items() if key in {'phase', 'branch', 'worker_phase', 'error_type'}},
                    local_workers_stopped=True, remote_effect='unknown') for r in isolated],
                'guidance': 'Remote effects remain unknown. Use saved evidence and independent networkless analysis. '
                    'Do not infer failure, replay requests, or use affected mutable state as a proven premise. '
                    'These workers have stopped; no result is in flight. Continue useful independent work. '
                    'wait_for_tools without live work preserves the task as blocked pending recovery.'}
        from agent_adapters.image_observations import ImageObservations
        images = ImageObservations(self.directory, self.task_id, self.evidence).select(history, proposal)
        if images:
            runtime['images'] = images
            from agent_adapters.image_observations import VISION_MODEL
            runtime['image_model'] = VISION_MODEL
        request = StageModelRequest(identity, self.task_id, stage, role.role_config_ref,
            state.latest_checkpoint.sequence, context, messages,
            self._available_tools() if stage == "stage_action" else (), proposal_id, proposal,
            history_catalog=tuple(i.visible(include_content=False) for i in all_history),
            execution_context={**self.mcp.execution_context(), "stage_runtime": runtime})
        return replace(request, history_catalog=catalog_with_completion_support(
            request.history_catalog, formal_support_refs(request)))

    async def _review(self, request, token):
        decision = await self.reviews.action(request, token)
        if decision is None:
            return None, None, None
        proposal_id, proposal = decision
        if await self._knowledge_action(request, proposal_id, proposal, token):
            return proposal, None, None
        if isinstance(proposal, StagePlanningDecision) and proposal.kind == "wait_for_review":
            self._commit(StageStateChange(applications=(proposal_id,)))
            await self.reviews.collect(token, wait=True)
            return proposal, None, None
        if isinstance(proposal, (TurnIntent, tuple)):
            self._submit_planning(request, proposal_id, proposal)
            await self.reviews.start(token)
            return proposal, None, None
        try:
            review_id, review = await self._queued_review(request.context.session, proposal_id, proposal, token)
        except StageModelSuperseded:
            return proposal, None, None
        return proposal, review, StageReviewBinding(proposal_id, review_id)

    def _submit_planning(self, request, identity, proposal):
        author = request.context.session
        forks = [StageBranchFork(identity)]
        if isinstance(proposal, TurnIntent):
            parent = request.context.turn
            turn_id = (author.owner_id if parent is None and self.store.load_turn(self.task_id, author.owner_id) is None
                else self._id("stage/turn/" + identity))
            child = StageTurnSnapshot(turn_id, proposal, TurnPlanningState.ACTIVE, parent.turn_id if parent else None)
            turns = [child]
            if parent is not None:
                current = self.store.load_turn(self.task_id, parent.turn_id)
                if current.achievement is not None:
                    turns.insert(0, replace(current, planning_state=TurnPlanningState.WAITING_CHILD))
            target = PlanningSession("turn_planning", turn_id)
            if target != author:
                forks.append(StageBranchFork(identity, target_session=target))
        else:
            turn = self.store.load_turn(self.task_id, author.owner_id)
            available = [road for road in turn.roads if road.status is IntentStatus.PENDING]
            pending = []
            for index, candidate in enumerate(proposal):
                existing = next((road for road in available if road.candidate == candidate), None)
                if existing:
                    available.remove(existing)
                road = existing or RoadSnapshot(self._id("stage/road/" + identity + "/" + str(index)), candidate, IntentStatus.PENDING)
                pending.append(road)
                if existing is None:
                    forks.append(StageBranchFork(identity, target_session=PlanningSession("road_execution", road.road_id)))
            if available:
                raise StageStateError("stage.pending_candidates_omitted", code="stage.pending_candidates_omitted")
            ordered = iter(pending)
            roads = [next(ordered) if road.status is IntentStatus.PENDING else road for road in turn.roads]
            if stage_runtime(request).get("route_advice"):
                roads = [replace(road, status=IntentStatus.SUSPENDED) if road.status is IntentStatus.RUNNING else road for road in roads]
            turns = [replace(turn, roads=(*roads, *ordered))]
        self._commit(StageStateChange(turns=tuple(turns), review_submissions=(identity,),
            planning_submissions=(identity,), branches=tuple(forks)))

    async def _decide_route(self, review_id, token):
        await self._collect_tools(drain=True)
        source = self.evidence.request(review_id)
        session = PlanningSession("turn_planning", source.context.turn.turn_id)
        turn = self.store.load_turn(self.task_id, session.owner_id)
        value = self.evidence.read_model_result(self.store.load_d4_operation(review_id)).result
        continuation = (turn.achievement is not None and isinstance(value, StagePlanningDecision)
            and value.kind == "reassess_roads")
        if continuation and any(road.status is IntentStatus.RUNNING for road in turn.roads):
            self._commit(StageStateChange(turns=(replace(turn, roads=tuple(
                replace(road, status=IntentStatus.SUSPENDED) if road.status is IntentStatus.RUNNING else road
                for road in turn.roads)),)))
        request = self._request("stage_continuation" if continuation else "stage_roads", session)
        decision = await self.reviews.action(request, token)
        if decision is None:
            return
        identity, proposal = decision
        turn = self.store.load_turn(self.task_id, session.owner_id)
        if continuation and await self._knowledge_action(request, identity, proposal, token):
            return
        if continuation and isinstance(proposal, StagePlanningDecision) and proposal.kind == "wait_for_review":
            self._commit(StageStateChange(applications=(identity,)))
            await self.reviews.collect(token, wait=True)
            return
        if continuation and isinstance(proposal, (TurnIntent, tuple)):
            self._submit_planning(request, identity, proposal)
            await self.reviews.start(token)
            return
        if continuation and (not isinstance(proposal, StagePlanningDecision) or proposal.kind != "select_road"):
            review_id, review = await self._queued_review(session, identity, proposal, token)
            self._apply_planning(turn, proposal, review, StageReviewBinding(identity, review_id))
            return
        if isinstance(proposal, tuple) and any(candidate not in {r.candidate for r in turn.roads} for candidate in proposal):
            self._submit_planning(request, identity, proposal)
            updated = self.store.load_turn(self.task_id, turn.turn_id)
            chosen = next(r.road_id for r in updated.roads if r.candidate == proposal[0] and r.status is IntentStatus.PENDING)
            self._commit(StageStateChange(turns=(replace(updated, roads=tuple(
                replace(r, status=IntentStatus.RUNNING) if r.road_id == chosen else r for r in updated.roads)),)))
            await self.reviews.start(token)
            return
        if isinstance(proposal, tuple):
            live = [road for road in turn.roads if road.status in {IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED}]
            ordered = []
            for candidate in proposal:
                road = next((road for road in live if road.candidate == candidate), None)
                if road is None:
                    raise StageStateError("stage.route_order_changed_candidates", code="stage.route_order_changed_candidates")
                live.remove(road)
                ordered.append(road)
            if live:
                raise StageStateError("stage.route_order_omitted_candidates", code="stage.route_order_omitted_candidates")
            chosen = ordered[0].road_id
            iterator = iter(ordered)
            roads = tuple(next(iterator) if road.status in {IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED}
                else road for road in turn.roads)
        else:
            roads, chosen = turn.roads, proposal.road_id
        roads = tuple(replace(road, status=IntentStatus.RUNNING) if road.road_id == chosen
            else replace(road, status=IntentStatus.SUSPENDED) if road.status is IntentStatus.RUNNING else road for road in roads)
        self._commit(StageStateChange(turns=(replace(turn, roads=roads),), applications=(identity,)))
        previous = next((road.road_id for road in turn.roads if road.status is IntentStatus.RUNNING), None)
        get_logger("ctrl").info("stage.route_decided", run=self.task_id, operation=identity,
            source_operation=review_id, phase="stage_roads", turn=turn.turn_id, road=chosen,
            code="roads_reordered" if isinstance(proposal, tuple) else "road_selected" if previous is None
                else "road_kept" if previous == chosen else "road_switched")

    def _completion_due(self, turn):
        """Derive the follow-up from settled review and planning operations."""
        if turn.achievement is not None:
            return None
        planner = "stage/turn_planning/" + turn.turn_id
        roads = {"stage/road_execution/" + road.road_id for road in turn.roads}
        for op in reversed(self.store.list_d4_operations(self.task_id)):
            if not op.application_ref or op.application_ref.startswith("stage-discarded/"):
                continue
            if op.start.resource_ref == planner and op.start.role == "inner_action":
                if (self.evidence.request(op.start.operation_id).stage == "stage_completion"
                        and isinstance(self.evidence.read_model_result(op).result, (TurnCompletion, StageIncomplete))):
                    return None
            elif op.start.category == "review" and op.start.resource_ref in roads:
                request = self.evidence.request(op.start.operation_id)
                if not isinstance(request.proposal, RoadResolution):
                    continue
                review = self.evidence.read_model_result(op).result
                verdict = review.road if isinstance(review, RoadTurnClosureReview) else review
                if verdict.decision == "accept":
                    return review, request
        return None

    async def _check_completion(self, turn, token, closure_source):
        request = self._request("stage_completion", PlanningSession("turn_planning", turn.turn_id),
            closure_source=closure_source)
        decision = await self.reviews.action(request, token)
        if decision is None:
            return
        identity, completion = decision
        if await self._knowledge_action(request, identity, completion, token):
            return
        if isinstance(completion, StageIncomplete):
            self.store.mark_d4_operation_applied(identity, "stage-incomplete/" + identity)
            self._commit(StageStateChange())
        else:
            try:
                review_id, review = await self._queued_review(request.context.session, identity, completion, token)
            except StageModelSuperseded:
                return
            self._apply_planning(self.store.load_turn(self.task_id, turn.turn_id), completion, review,
                StageReviewBinding(identity, review_id))

    async def _queued_review(self, session, proposal_id, proposal, token):
        entry = next((entry for entry in self.store.list_stage_reviews(self.task_id)
            if entry.proposal_operation_id == proposal_id), None)
        if entry is None:
            self._commit(StageStateChange(review_submissions=(proposal_id,)))
        elif entry.state != "pending":
            raise StageStateError("stage.review_already_closed", code="stage.review_already_closed")
        await self.reviews.drain(token, before=proposal_id)
        entry = next(entry for entry in self.store.list_stage_reviews(self.task_id) if entry.proposal_operation_id == proposal_id)
        if entry.state == "superseded":
            raise StageModelSuperseded(proposal_id)
        advice = stage_plans.route_advice(self.store, self.task_id)
        if advice and self.store.load_d4_operation(advice).start.category == "review":
            from agent_core.stage_planning import conversation_operation_ids
            if set(self.evidence.review_interruptions(advice)).intersection(
                    conversation_operation_ids(self.evidence.request(proposal_id))):
                self._commit(StageStateChange(discarded_decisions=(StageReviewBinding(proposal_id, advice),)))
                raise StageModelSuperseded(proposal_id)
        # The queue's proposal identity survives unrelated checkpoint advances.
        identity = self._id("stage/review/" + proposal_id)
        request = self.evidence.request(identity) if self.evidence.read("request", identity) is not None else replace(
            self._request("stage_review", session, proposal_id=proposal_id, proposal=proposal), model_request_id=identity)
        return await self._closure_review(request, token)

    async def _knowledge_action(self, request, identity, value, token):
        knowledge = self.evidence.knowledge(self.task_id)
        if isinstance(value, StageFactSelection):
            turn = knowledge.turn_for(request.context.session)
            self._commit(StageStateChange(turns=(replace(turn, fact_refs=value.support_refs),), applications=(identity,)))
        elif isinstance(value, StageMethodRegistration):
            await self._collect_tools(drain=True)
            review_id, _ = await self._queued_review(request.context.session, identity, value, token)
            self._commit(StageStateChange(reviews=(StageReviewBinding(identity, review_id),)))
        elif isinstance(value, StageHistoryRead):
            self.evidence.perform_history_read(self.store.load_d4_operation(identity))
            self._commit(StageStateChange(applications=(identity,)))
        elif isinstance(value, StageFactRevision) and value.decision != "resubmit":
            self._commit(StageStateChange(applications=(identity,)))
            get_logger("ctrl").info("stage.fact_revision_disposed", run=self.task_id, operation=identity,
                phase=request.stage, decision=value.decision, revision_ref=value.revision_ref)
        elif isinstance(value, (StageConclusionChange, StageFactProposal, StageFactBatch, StageFactRevision)):
            if fact_candidates(value) and request.stage == "stage_action" and request.context.session.scope == "road_execution":
                existing = duplicate_fact(self.store, self.task_id, request.context.session, value)
                if existing is not None and existing.proposal_operation_id != identity:
                    self._commit(StageStateChange(applications=(identity,)))
                    get_logger("ctrl").info("stage.review_submission_reused", run=self.task_id,
                        operation=identity, source_operation=existing.proposal_operation_id, phase="stage_action")
                    if existing.state == "pending":
                        await self.reviews.collect(token, wait=True)
                    return True
                if existing is None:
                    self._commit(StageStateChange(review_submissions=(identity,), branches=(StageBranchFork(identity),)))
                await self.reviews.start(token)
                if isinstance(value, StageFactClosure):
                    await self.reviews.collect(token, wait=True)
                return True
            try:
                review_id, review = await self._queued_review(request.context.session, identity, value, token)
            except StageModelSuperseded:
                return True
            binding = StageReviewBinding(identity, review_id)
            if isinstance(value, StageConclusionChange):
                record = knowledge.prepare_revision(binding)
                self._commit(StageStateChange(reviews=(binding,), revisions=(record,)))
            else:
                facts = tuple(StageFactApplication(self._id("stage/fact/" + identity
                    + ("" if index is None else "/" + str(index))), review_id, index)
                    for index, claim in fact_candidates(value) if not claim.inferred
                    and reviewed_fact(value, review, index)[1].decision == "accept")
                self._commit(StageStateChange(reviews=(binding,), facts=facts))
        else:
            return False
        return True

    def _live_tool_request(self, identity):
        return any(entry.parent == identity and not entry.task.done() for entry in self._tools_in_flight.values())

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
        return any(entry.parent == request and entry.road_id == road and not entry.task.done()
            and (entry.operation_id == identity or len(parts) == 5 and entry.call.tool_name == "http.run_experiment")
            for entry in self._tools_in_flight.values())

    def _available_tools(self):
        from agent_adapters.public_web import NAMES
        sandbox_busy = any(entry.call.tool_name not in NAMES for entry in self._tools_in_flight.values())
        from agent_core.stage_isolation import tool_allowed
        isolated = self._isolated_operations()
        return tuple(tool for tool in self.mcp.tools
            if (not sandbox_busy or tool["qualified_name"] in NAMES)
            and tool_allowed(self.store, self.task_id, MCPCall(tool['qualified_name'], {}), isolated=isolated))

    def _isolated_operations(self):
        from agent_core.stage_isolation import isolated_http_operations
        return isolated_http_operations(self.store, self.task_id)

    async def _wait_isolated(self):
        """Collect real work; preserve stopped unknowns as an explicit recovery block."""
        await self._collect_tools(all_pending=True)
        self._check_dispatch()
        self.mcp.reconcile()
        isolated = self._isolated_operations()
        if not isolated:
            return
        await self.reviews.start(self._token)
        if self.reviews.job is not None:
            await self.reviews.collect(self._token, wait=True)
            return
        operations = sorted(r.start.operation_id for r in isolated)
        through = self.store.list_d4_operations(self.task_id)[-1].start.operation_id
        identity = through
        self.evidence.save("isolation-blocked", identity, dict(task_run_id=self.task_id,
            operations=operations, reason="no_recovery_channel"))
        get_logger("ctrl").warning("stage.recovery_blocked", run=self.task_id,
            category="unknown", code="stage.isolated_no_recovery_channel", operations=operations)
        raise StageRecoveryRequired("stage.isolated_no_recovery_channel", layer=Layer.CTRL)

    def _live_http_model(self, identity, resource):
        parts = resource.split("/")
        if (len(parts) != 5 or parts[0] != "mcp" or parts[2] != "experiment"
                or identity not in self.models.live_operations or self._tools_stopping
                or self._token is not None and self._token.is_cancelled):
            return False
        road, parent = parts[1], parts[3]
        if not any(entry.parent == parent and entry.road_id == road
                and entry.call.tool_name == "http.run_experiment" and not entry.task.done()
                for entry in self._tools_in_flight.values()):
            return False
        request = self.evidence.request(identity)
        return (request.task_run_id == self.task_id and request.stage == "stage_action"
            and request.context.session == PlanningSession("road_execution", road))

    async def _collect_tools(self, *, wait=False, drain=False, all_pending=False, timeout=None, ids=None):
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        def targets():
            return tuple(entry for entry in self._tools_in_flight.values()
                if ids is None or entry.operation_id in ids)
        while self._tools_in_flight:
            if wait or drain or all_pending:
                if all_pending:
                    while any(not entry.task.done() for entry in targets()):
                        self._check_dispatch()
                        remaining = None if deadline is None else deadline - loop.time()
                        if remaining is not None and remaining <= 0:
                            pending = [entry.operation_id for entry in targets() if not entry.task.done()]
                            get_logger("ctrl").info("stage.tools_wait_timeout", run=self.task_id,
                                phase="tool_dispatch", pending=pending, in_flight=len(self._tools_in_flight))
                            break
                        await asyncio.wait({entry.task for entry in targets() if not entry.task.done()},
                            timeout=0.1 if remaining is None else min(0.1, remaining),
                            return_when=asyncio.FIRST_COMPLETED)
                else:
                    while not any(entry.task.done() for entry in self._tools_in_flight.values()):
                        self._check_dispatch()
                        await asyncio.wait({entry.task for entry in self._tools_in_flight.values()},
                            timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
            completed = []
            for identity, entry in tuple(self._tools_in_flight.items()):
                if entry.task.done():
                    dispatched = entry.task.result()  # Unknown/failed settlement is never hidden by a later success.
                    del self._tools_in_flight[identity]
                    if dispatched is not False:
                        completed.append(entry.operation_id)
            if completed:
                get_logger("ctrl").info("stage.tools_settled", run=self.task_id, phase="tool_dispatch",
                    operations=completed, in_flight=len(self._tools_in_flight))
            if not drain:
                return

    async def _start_tools(self, identity, road_id, proposal, token, *, sequence_step=None):
        calls = tool_calls(proposal)
        from agent_core.actions import PUBLIC_REFERENCE_TOOLS
        from agent_core.stage_isolation import tool_allowed
        if calls and any(not tool_allowed(self.store, self.task_id, call) for call in calls):
            self.evidence.save('tool-scope-stop', identity, dict(reason='stage.resource_outcome_unknown',
                proposal_sha256=digest(asdict(proposal)),
                blocking_operations=[r.start.operation_id for r in self._isolated_operations()]))
            self.store.mark_d4_operation_applied(identity, 'stage-isolated/' + identity)
            get_logger('ctrl').info('stage.tool_scope_deferred', run=self.task_id, operation=identity,
                phase='tool_dispatch', category='unknown', code='stage.resource_outcome_unknown')
            return ()
        if (not calls or len(self._tools_in_flight) + len(calls) > MAX_IN_FLIGHT_TOOLS
                or sum(call.tool_name not in PUBLIC_REFERENCE_TOOLS for call in calls) > 1
                or any(call.tool_name not in {tool["qualified_name"] for tool in self._available_tools()} for call in calls)):
            raise StageStateError("stage.tool_slot_unavailable", code="stage.tool_slot_unavailable", category=Category.POLICY)
        app = self
        class ToolToken:
            @property
            def is_cancelled(self):
                return app._tools_stopping or token.is_cancelled
            @property
            def is_timed_out(self):
                return getattr(token, "is_timed_out", False)
        async def dispatch(entry):
            review_id = self.reviews.dispatch_fence(identity)
            entry.admitted = review_id is None
            if review_id is not None:
                self.evidence.save("tool-dispatch-stop", entry.operation_id, {"model_request_id": identity,
                    "review_operation_id": review_id, "outcome": "not_dispatched"})
                get_logger("ctrl").info("stage.tool_dispatch_fenced", run=self.task_id, operation=identity,
                    source_operation=review_id, phase="tool_dispatch", tool=entry.call.tool_name)
                return False
            if sequence_step is None:
                await self.mcp.execute(identity, road_id, entry.call, ToolToken())
            else:
                await self.mcp.execute(identity, road_id, entry.call, ToolToken(), sequence_step=sequence_step)
            return True
        entries = []
        for call in calls:
            key = tool_call_key(identity, proposal, call)
            operation_id = identity if call.tool_name == "http.run_experiment" else self._id("mcp/" + key)
            if sequence_step is not None:
                from agent_core.stage_experiments import step_id
                key = identity + "/step/" + str(sequence_step)
                operation_id = step_id(self.task_id, identity, sequence_step)
            entry = SimpleNamespace(parent=identity, road_id=road_id, call=call, operation_id=operation_id,
                task=None, admitted=None)
            entry.task = asyncio.create_task(dispatch(entry))
            self._tools_in_flight[key] = entry
            entries.append(entry)
        await asyncio.sleep(0)  # Admit the original operation before advancing the decision checkpoint.
        while any(entry.admitted is None for entry in entries):
            self._check_dispatch()
            await self._collect_tools()
            if any(entry.admitted is None for entry in entries):
                await asyncio.wait({entry.task for entry in entries if entry.admitted is None}, timeout=.01, return_when=asyncio.FIRST_COMPLETED)
        await self._collect_tools()
        if not any(entry.admitted for entry in entries):
            await self.reviews.collect(token, unused=identity)
            return ()
        if sequence_step is None:
            self.store.mark_d4_operation_applied(identity, identity if isinstance(proposal, StageToolBatch) else operation_id)
        self._commit(StageStateChange())
        get_logger("ctrl").info("stage.tool_dispatched", run=self.task_id, operation=identity,
            phase="tool_dispatch", in_flight=len(self._tools_in_flight), action_count=sum(entry.admitted for entry in entries))
        if self.reviews.dispatch_fence(identity) is not None:
            await self.reviews.collect(token)
        return tuple(entry.operation_id for entry in entries if entry.admitted)

    async def _execute_sequence(self, identity, road_id, sequence, token):
        from agent_core.stage_experiments import resolve_step, step_id, settled_payload
        await self._collect_tools(drain=True)
        request = self.evidence.request(identity)
        outcome = "completed"
        for index in range(len(sequence.steps)):
            self._check_dispatch()
            if self.reviews.dispatch_fence(identity) is not None:
                outcome = "review_blocked"
                break
            operation = self.store.load_d4_operation(step_id(self.task_id, identity, index))
            if operation is not None:
                # Reconcile only known receipts; an unknown dispatch remains a barrier.
                self.mcp.reconcile()
                payload = settled_payload(self.evidence, self.task_id, identity, index)
                continue
            try:
                call = resolve_step(self.evidence, self.task_id, identity, sequence, index)
                if call is None:
                    outcome = "condition_not_met"
                    break
                from jsonschema import Draft202012Validator
                from agent_core.stage_planning import _closed_schema
                tool_schema = next(t["input_schema"] for t in request.tools if t["qualified_name"] == call.tool_name)
                if not Draft202012Validator(_closed_schema(tool_schema)).is_valid(call.arguments):
                    raise ValueError("stage.sequence_arguments_invalid")
                self.mcp.validate(SimpleNamespace(history_context=json.dumps({"mcp_tools": list(request.tools)})), call)
            except ValueError as error:
                outcome = str(error)
                get_logger("ctrl").warning("stage.sequence_stopped", run=self.task_id, operation=identity,
                    phase="tool_dispatch", category="input", code=outcome, step=index)
                break
            get_logger("ctrl").info("stage.sequence_step", run=self.task_id, operation=identity,
                phase="tool_dispatch", step=index, tool=call.tool_name)
            admitted = await self._start_tools(identity, road_id, call, token, sequence_step=index)
            if not admitted:
                outcome = "review_blocked"
                break
            await self._collect_tools(all_pending=True, ids=admitted)
            payload = settled_payload(self.evidence, self.task_id, identity, index)
        if outcome == "completed" and payload["status"] != "completed":
            outcome = "tool_failed"
        self.evidence.save("sequence-result", identity, {"outcome": outcome, "step": index})
        self.store.mark_d4_operation_applied(identity, identity)
        self._commit(StageStateChange())

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

    async def _supervise(self, road_id, token, *, stalled_operation=None):
        # HTTP subrequests may arrive during a foreground model invocation.
        # Keep the existing supervision cadence at the next safe boundary.
        if self.reviews.foreground is not None:
            return
        decisions = []
        for op in self.store.list_d4_operations(self.task_id):
            if op.start.category != "action" or op.application_ref is None or op.application_ref.startswith(("stage-discarded/", "stage-reused/")):
                continue
            req = self.evidence.request(op.start.operation_id)
            value = self.evidence.read_model_result(op).result
            if isinstance(value, StageToolDetails):
                continue
            if isinstance(value, StagePlanningDecision) and value.kind in {"wait_for_tools", "wait_for_review", "action_checkpoint"}:
                continue
            if req.context.session == PlanningSession("road_execution", road_id):
                decisions.append(op)
        if stalled_operation or decisions and len(decisions) % 4 == 0:
            # Only this foreground safe point may start supervision. Settle
            # existing tools first; HTTP callbacks cannot share an Outer call.
            if await self.reviews.drain(token):
                return
            if stage_plans.route_advice(self.store, self.task_id):
                return
            await self._collect_tools(drain=True)
            self._check_dispatch()
            request = self._request("stage_supervision", PlanningSession("road_execution", road_id))
            identity = self._id("stage/action-stall/" + stalled_operation if stalled_operation else
                "stage/supervision/" + road_id + "/" + decisions[-1].start.operation_id)
            if stalled_operation:
                source = self.evidence.request(stalled_operation)
                operation = self.store.load_d4_operation(stalled_operation)
                if (source.stage != "stage_action" or source.context.session != request.context.session
                        or operation.completion is None or operation.completion.result_kind != "failed"
                        or self.evidence.action_generation_failure(source, self.evidence.read("result", stalled_operation)) is None):
                    raise StageRecoveryRequired("stage.action_stall_binding_changed")
                request = replace(request, frozen_contract_ref=None, execution_context={**request.execution_context,
                    "stage_runtime": {**stage_runtime(request), "action_stall": {
                        "operation_id": stalled_operation,
                        "reason": self.evidence.action_generation_failure(source,
                            self.evidence.read("result", stalled_operation)),
                        "correction": self.evidence.read("contract", stalled_operation).get("correction")}}})
            request = self.evidence.request(identity) if self.evidence.read("request", identity) is not None else replace(
                request, model_request_id=identity)
            try:
                identity, advice = await self.models.call(request, token)
            except StageModelTransportFailure as error:
                if stalled_operation:
                    raise
                if error.context["model_call"]["stage"] != "stage_supervision":
                    raise
                self._check_dispatch()
                get_logger("ctrl").warning("stage.supervision_unavailable", run=self.task_id,
                    phase="stage_supervision", category="dependency", **safe_failure_details(error))
                return
            if self.store.load_d4_operation(identity).application_ref is None:
                self.store.mark_d4_operation_applied(identity, "stage-advice/" + identity)
                get_logger("ctrl").info("stage.strategy_reviewed", run=self.task_id, operation=identity,
                    code=advice.decision, category="strategy_signal", phase="stage_supervision")
            if stalled_operation:
                self._commit(StageStateChange())

    async def run(self, token):
        from agent_adapters.run_files import run_lock
        from agent_core.operator_controls import OperatorPaused
        with run_lock(self.directory):
            self._tools_stopping = False
            try:
                return await self._run(token)
            except OperatorPaused:
                # Pausing is a normal operator command, not an unknown failure.
                get_logger("ctrl").info("stage.operator_paused", run=self.task_id, phase="stage_loop")
                raise
            except Exception as exc:
                category, code, _ = classify(exc)
                from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
                from agent_adapters.output_evidence import SAFE_MODEL_ERROR_CODES
                if isinstance(exc, ExecutionRecoveryRequired) and len(exc.args) == 1 and exc.args[0] in SAFE_MODEL_ERROR_CODES:
                    code = exc.args[0]
                get_logger("ctrl").warning("stage.run_stopped", run=self.task_id,
                    category=str(category), code=code, phase="stage_loop",
                    trace=trace_frames(exc), **safe_failure_details(exc))
                raise
            finally:
                await self.reviews.stop()
                await self._stop_tools()

    def _direct_rejections(self, *, road_id=None):
        for operation in reversed(self.store.list_d4_operations(self.task_id)):
            if stage_storage.direct_rejection_applied(operation) and (
                    road_id is None or operation.start.resource_ref == "stage/road_execution/" + road_id):
                yield operation, self.evidence.read_model_result(operation).result

    def _reject_direct_candidate(self, identity, value):
        operation = self.store.load_d4_operation(identity)
        receipt = stage_storage.direct_rejection_receipt(self.store, operation)
        if receipt is None:
            key = self.goal.submission_token(value.candidate) or value.candidate
            duplicate = next((old.start.operation_id for old, rejected in self._direct_rejections()
                if rejected.source_ref == value.source_ref
                and (self.goal.submission_token(rejected.candidate) or rejected.candidate) == key), None)
            if duplicate is None and self.goal.grounds(value):
                return False
            receipt = {"result_sha256": operation.completion.evidence_sha256, "duplicate_of": duplicate}
            self.evidence.save("direct-rejection", identity, receipt)
        self._commit(StageStateChange(applications=(identity,)))
        logger = get_logger("ctrl")
        if receipt["duplicate_of"] is None:
            logger.warning("stage.direct_candidate_ungrounded", run=self.task_id, operation=identity,
                phase="stage_action", category="policy")
        else:
            logger.info("stage.direct_candidate_rejection_reused", run=self.task_id, operation=identity,
                source_operation=receipt["duplicate_of"], phase="stage_action", category="policy")
        return True

    def _recover_direct_rejection(self, road_id):
        if self.goal_validation != "external":
            return
        sequence = self.state.latest_checkpoint.sequence
        for operation in reversed(self.store.list_d4_operations(self.task_id)):
            if (operation.start.resource_ref != "stage/road_execution/" + road_id
                    or operation.start.category != "action" or operation.completion is None
                    or operation.completion.result_kind != "completed"):
                continue
            identity = operation.start.operation_id
            legacy = (operation.application_ref == "stage-direct-ungrounded/" + identity
                and operation.start.base_checkpoint_sequence == sequence)
            if legacy or operation.application_ref is None and stage_storage.direct_rejection_receipt(self.store, operation):
                self._commit(StageStateChange(applications=(identity,)))
                get_logger("ctrl").info("stage.direct_candidate_rejection_recovered", run=self.task_id,
                    operation=identity, phase="stage_action", category="recovery")
                return

    def _recover_direct_submission(self):
        """Finish a saved one-shot handoff whose process stopped before registering."""
        if self.goal is None or self.goal_validation != "external":
            return None
        if (self.state.task_run.status is not TaskRunStatus.RUNNING
                or self.store.list_d7_answer_candidates(self.task_id)):
            return None
        for operation in self.store.list_d4_operations(self.task_id):
            if (operation.start.category != "action"
                    or not (operation.application_ref or "").startswith("stage-direct/")):
                continue
            request = self.evidence.request(operation.start.operation_id)
            if request is None or request.stage != "stage_action":
                continue
            value = self.evidence.read_model_result(operation).result
            if not (isinstance(value, StagePlanningDecision) and value.kind == "request_final_validation"):
                continue
            if self.goal.handoff(SimpleNamespace(proposal_operation_id=operation.start.operation_id,
                    model_request_id=operation.start.operation_id), value):
                return StageRunResult(self.task_id,
                    active_turn(self.store.list_stage_turns(self.task_id)),
                    "stage.awaiting_" + self.goal_validation + "_validation")
        return None

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
        if isinstance(proposal, TurnCriterionRevision):
            source = self.evidence.request(binding.proposal_operation_id)
            target_id = stage_runtime(source).get("criterion_target")
            origin = self.store.load_turn(self.task_id, target_id) if target_id else source.context.turn
            turn = self.store.load_turn(self.task_id, origin.turn_id)
            intent = replace(turn.intent, completion_criterion=proposal.completion_criterion,
                support_refs=tuple(dict.fromkeys((*turn.intent.support_refs, *proposal.support_refs))))
            changes = [replace(turn, intent=intent, planning_state=TurnPlanningState.ACTIVE)]
            if turn.parent_turn_id:
                parent = self.store.load_turn(self.task_id, turn.parent_turn_id)
                changes.append(replace(parent, planning_state=TurnPlanningState.WAITING_CHILD))
        elif isinstance(proposal, TurnIntent):
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
        elif isinstance(proposal, TurnCompletion):
            changes = [replace(turn, achievement=TurnAchievement(proposal, binding.review_operation_id),
                fact_refs=tuple(dict.fromkeys((*turn.fact_refs, *proposal.support_refs))))]
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
        forks = tuple(StageBranchFork(binding.proposal_operation_id,
            target_session=PlanningSession("road_execution", road.road_id))
            for updated in changes for road in updated.roads
            if isinstance(proposal, tuple) and not any(old.road_id == road.road_id for old in turn.roads))
        self._commit(StageStateChange(turns=tuple(changes), reviews=(binding,), branches=forks))
        get_logger("ctrl").info("stage.planning_applied", run=self.task_id,
            operation=binding.review_operation_id, phase="stage_planning",
            code=proposal.kind if isinstance(proposal, StagePlanningDecision) else type(proposal).__name__,
            turn=turn.turn_id, parent=turn.parent_turn_id)

    def _promote_successor_draft(self, turn):
        if not self.successor_drafts or turn.achievement is None:
            return False
        for operation in self.store.list_d4_operations(self.task_id):
            if (operation.start.role != "inner_action" or operation.application_ref is not None
                    or operation.completion is None or operation.completion.result_kind != "completed"):
                continue
            request = self.evidence.request(operation.start.operation_id)
            if request.stage != "stage_draft" or stage_runtime(request).get("draft_for") != self.evidence.model_root(turn.achievement.review_operation_id):
                continue
            if (self.evidence.read("result", operation.start.operation_id) or {}).get("outcome") != "valid":
                continue
            if request.context.turn.intent != turn.intent:
                continue
            value = self.evidence.read_model_result(operation).result
            from agent_core.stage_planning import frozen_history
            current = {i.ref: i for i in self.evidence.history(self.task_id)}
            original = frozen_history(request)
            if any(ref not in current or ref not in original or current[ref] != original[ref] for ref in value.support_refs):
                continue
            self._submit_planning(request, operation.start.operation_id, value)
            return True
        return False

    async def _closure_review(self, request, token):
        if not self.successor_drafts or not isinstance(request.proposal, (RoadTurnClosure, TurnCompletion)) or self.reviews.job is not None:
            return await self.models.call(request, token)
        from agent_adapters.stage_reviews import _GenerationToken
        saved_draft = self._id("stage/draft/" + request.model_request_id)
        if self.evidence.read("request", saved_draft) is not None:
            return await self.models.call(request, token)
        turn = request.context.turn
        draft = self._request("stage_draft", PlanningSession("turn_planning", turn.turn_id))
        draft = replace(draft, model_request_id=saved_draft, execution_context={**draft.execution_context,
            "stage_runtime": {**stage_runtime(draft), "draft_for": request.model_request_id}})
        self.reviews.job = SimpleNamespace(request=request, members=(request.proposal_operation_id,),
            token=_GenerationToken(token), task=None)
        self.reviews.job.task = asyncio.create_task(self.models.call(request, self.reviews.job.token))
        await asyncio.sleep(0)
        self.reviews.foreground = SimpleNamespace(request=draft, token=_GenerationToken(token), task=None)
        self.reviews.foreground.task = asyncio.create_task(self.models.call(draft, self.reviews.foreground.token))
        try:
            result = await self.reviews.job.task
            verdict = result[1].turn if isinstance(result[1], RoadTurnClosureReview) else result[1]
            if verdict.decision != "accept" or not self.reviews.foreground.task.done():
                self.reviews.foreground.token.superseded_by = result[0]
            try:
                await self.reviews.foreground.task
            except StageModelSuperseded:
                pass
            except StageRecoveryRequired as error:
                if (error.code != "stage.dispatch_stopped" or token.is_cancelled
                        or self.clock() >= self.state.task_run.deadline_at
                        or self.store.load_d4_operation(saved_draft) is not None):
                    raise
            except StageModelTransportFailure as error:
                if error.context["model_call"]["stage"] != "stage_draft":
                    raise
                get_logger("ctrl").warning("stage.successor_draft_unavailable", run=self.task_id,
                    operation=saved_draft, phase="stage_draft", category="dependency",
                    code="stage.draft_transport_unavailable")
            return result
        finally:
            if any(not entry.task.done() for entry in (self.reviews.job, self.reviews.foreground)):
                await self.reviews.stop()
            self.reviews.foreground = self.reviews.job = None

    async def _continue_joint_closure(self, turn, token):
        from agent_core.stage_storage import resolved_fact_closure
        from agent_core.stage_planning import blocking_fact_revisions
        running = next((r for r in turn.roads if r.status is IntentStatus.RUNNING), None)
        if running is None:
            return False
        for entry in self.store.list_stage_reviews(self.task_id):
            if entry.state != "settled":
                continue
            identity = entry.proposal_operation_id
            source = self.evidence.read_model_result(self.store.load_d4_operation(identity))
            if not isinstance(source.result, StageFactClosure) or source.session.owner_id != running.road_id:
                continue
            original = self.evidence.request(identity)
            if original.context.turn.intent != turn.intent:
                continue
            review_id = self._id("stage/review-closure/" + identity)
            operation = self.store.load_d4_operation(review_id)
            if operation is not None and operation.application_ref is not None:
                continue
            await self._collect_tools(drain=True)
            history = self.evidence.history(self.task_id)
            closure = resolved_fact_closure(self.store, self.task_id, identity, history)
            if closure is None:
                continue
            if isinstance(source.result.closure, RoadTurnClosure) and not isinstance(closure, RoadTurnClosure):
                get_logger("ctrl").warning("stage.joint_closure_partial", run=self.task_id, operation=identity,
                    phase="stage_completion", category="input", code="stage.joint_turn_support_missing",
                    turn=turn.turn_id, road=running.road_id)
            request = self.evidence.request(review_id) if self.evidence.read("request", review_id) else replace(
                self._request("stage_review", source.session, proposal_id=identity, proposal=closure), model_request_id=review_id)
            if blocking_fact_revisions(request):
                continue
            runtime = {**stage_runtime(request), "joint_fact_closure": identity}
            request = replace(request, execution_context={**request.execution_context, "stage_runtime": runtime})
            completed_id, review = await self._closure_review(request, token)
            if self.store.load_d4_operation(completed_id).application_ref is not None:
                continue
            binding = StageReviewBinding(identity, completed_id)
            parts = review.parts(closure) if isinstance(closure, RoadTurnClosure) else ((closure, review),)
            self._apply_closure(self.store.load_turn(self.task_id, turn.turn_id), running, binding, parts)
            return True
        return False

    async def _run(self, token):
        self._token = token
        await self._prepare_inputs(token)
        self.models.reconcile(self.task_id)
        self.mcp.reconcile()
        recovered = self._recover_direct_submission()
        if recovered is not None:
            return recovered
        root_id = self._id("stage/root")
        recover_rejection = True
        while True:
            await self._collect_tools()
            await self.reviews.collect(token)
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
            advice = stage_plans.route_advice(self.store, self.task_id)
            if advice:
                await self._decide_route(advice, token)
                continue
            turn = active_turn(self.store.list_stage_turns(self.task_id))
            if turn is not None and await self._continue_joint_closure(turn, token):
                continue
            if turn is None:
                request = self._request("stage_intent", PlanningSession("turn_planning", root_id))
                proposal, review, binding = await self._review(request, token)
                if binding is None:
                    continue
                if isinstance(proposal, TurnCriterionRevision):
                    self._apply_planning(None, proposal, review, binding)
                    continue
                changes = () if review.decision != "accept" or isinstance(proposal, PlanningExhausted) else (
                    StageTurnSnapshot(root_id, proposal, TurnPlanningState.ACTIVE),)
                self._commit(StageStateChange(turns=changes, reviews=(binding,)))
                if isinstance(proposal, PlanningExhausted) and review.decision == "accept":
                    return StageRunResult(self.task_id, None, "stage.root_planning_exhausted")
                continue
            running = next((r for r in turn.roads if r.status is IntentStatus.RUNNING), None)
            if running is not None and recover_rejection:
                self._recover_direct_rejection(running.road_id)
                recover_rejection = False
            if running is None and (closure_source := self._completion_due(turn)) is not None:
                await self._check_completion(turn, token, closure_source)
                continue
            if turn.achievement is not None and running is None:
                if turn.planning_state is TurnPlanningState.EXHAUSTED:
                    await self._wait_isolated()
                    return StageRunResult(self.task_id, turn, "stage.exhausted")
                if not _just_registered_roads(self.store, self.task_id, turn.turn_id):
                    if self._promote_successor_draft(turn):
                        await self.reviews.start(token)
                        continue
                    proposal, review, binding = await self._review(self._request("stage_continuation",
                        PlanningSession("turn_planning", turn.turn_id)), token)
                    if binding is not None:
                        self._apply_planning(turn, proposal, review, binding)
                    continue
            if turn.planning_state is TurnPlanningState.EXHAUSTED:
                await self._wait_isolated()
                return StageRunResult(self.task_id, turn, "stage.exhausted")
            if running is None:
                # An accepted suggestion only chooses which available sibling starts;
                # the Inner still decided to close the Road that just ended.
                suggestion = (stage_plans.road_suggestion(self.store, self.task_id, turn, None)
                              if turn.achievement is None else None)
                pending = next((r for r in turn.roads if suggestion is not None
                                and r.road_id == suggestion["road_id"]
                                and r.status in {IntentStatus.PENDING, IntentStatus.SUSPENDED}), None)
                pending = pending or next((r for r in turn.roads
                                           if r.status in {IntentStatus.PENDING, IntentStatus.SUSPENDED}), None)
                if pending is not None:
                    self._commit(StageStateChange(turns=(replace(turn, roads=tuple(
                        replace(r, status=IntentStatus.RUNNING) if r.road_id == pending.road_id else r for r in turn.roads)),)))
                    continue
                proposal, review, binding = await self._review(self._request("stage_roads", PlanningSession("turn_planning", turn.turn_id)), token)
                if binding is None:
                    continue
                self._apply_planning(turn, proposal, review, binding)
                continue
            pending_closure = any(entry.state == "pending" and isinstance(self.evidence.read_model_result(
                self.store.load_d4_operation(entry.proposal_operation_id)).result, (RoadResolution, TurnCompletion))
                for entry in self.store.list_stage_reviews(self.task_id))
            if not pending_closure:
                await self._supervise(running.road_id, token)
            current = active_turn(self.store.list_stage_turns(self.task_id))
            if (stage_plans.route_advice(self.store, self.task_id) or current is None or current.turn_id != turn.turn_id
                    or not any(road.road_id == running.road_id and road.status is IntentStatus.RUNNING for road in current.roads)):
                continue
            await self.mcp.open()
            from agent_core.stage_experiments import StageToolSequence
            unfinished = next((op for op in self.store.list_d4_operations(self.task_id)
                if op.application_ref is None and op.completion is not None and op.completion.result_kind == "completed"
                and op.start.category == "action" and op.start.resource_ref == "stage/road_execution/" + running.road_id
                and isinstance(self.evidence.read_model_result(op).result, StageToolSequence)), None)
            if unfinished is not None:
                await self._execute_sequence(unfinished.start.operation_id, running.road_id,
                    self.evidence.read_model_result(unfinished).result, token)
                continue
            if self._tools_in_flight and (len(self._tools_in_flight) >= MAX_IN_FLIGHT_TOOLS or not self._available_tools()):
                await self._collect_tools(wait=True)
                continue
            request = self._request("stage_action", PlanningSession("road_execution", running.road_id))
            try:
                decision = await self.reviews.action(request, token)
            except StageActionStalled as error:
                if stage_runtime(request).get("action_stall"):
                    raise
                await self._supervise(running.road_id, token,
                    stalled_operation=error.context["model_call"]["operation_id"])
                continue
            if decision is None:
                continue
            identity, action = decision
            await self._collect_tools()
            self._check_dispatch()
            if isinstance(action, StageToolSequence):
                await self._execute_sequence(identity, running.road_id, action, token)
                continue
            if isinstance(action, StageToolDetails):
                self._commit(StageStateChange(applications=(identity,)))
                get_logger("ctrl").info("stage.tool_details_read", run=self.task_id, operation=identity,
                    phase="stage_action", tool_count=len(action.tool_names),
                    lookup="capability" if action.query is not None else "names")
                continue
            if isinstance(action, StagePlanningDecision) and action.kind == "action_checkpoint":
                self._commit(StageStateChange(applications=(identity,)))
                get_logger("ctrl").info("stage.action_checkpoint_saved", run=self.task_id,
                    operation=identity, phase="stage_action", support_count=len(action.support_refs))
                continue
            if isinstance(action, StagePlanningDecision) and action.kind == "reassess_roads":
                await self._collect_tools(drain=True)
                self._commit(StageStateChange(applications=(identity,)))
                continue
            if isinstance(action, StagePlanningDecision) and action.kind == "wait_for_tools":
                self._commit(StageStateChange(applications=(identity,)))
                await self._collect_tools(all_pending=True)
                await self._wait_isolated()
                continue
            if isinstance(action, StagePlanningDecision) and action.kind == "wait_for_review":
                self._commit(StageStateChange(applications=(identity,)))
                await self.reviews.collect(token, wait=True)
                continue
            if not tool_calls(action) and not fact_candidates(action):
                await self._collect_tools(drain=True)
            if (isinstance(action, StagePlanningDecision) and action.kind == "request_final_validation"
                    and self.goal is not None and stage_runtime(request).get("direct_submission")
                    and not action.support_refs):
                if self._reject_direct_candidate(identity, action):
                    continue
                # The applied marker is written before the handoff so a restart
                # can finish the same handoff instead of losing the saved decision.
                self.store.mark_d4_operation_applied(identity, "stage-direct/" + identity)
                if self.goal.handoff(SimpleNamespace(proposal_operation_id=identity,
                        model_request_id=identity), action):
                    return StageRunResult(self.task_id,
                        active_turn(self.store.list_stage_turns(self.task_id)),
                        "stage.awaiting_" + self.goal_validation + "_validation")
                continue
            if await self._knowledge_action(request, identity, action, token):
                continue
            if tool_calls(action):
                for call in tool_calls(action):
                    self.mcp.validate(SimpleNamespace(history_context=json.dumps({"mcp_tools": list(request.tools)})), call)
                admitted = await self._start_tools(identity, running.road_id, action, token)
                if not admitted:
                    continue
                await self._collect_tools(all_pending=True, ids=admitted,
                    timeout=TOOL_BATCH_WAIT_SECONDS if len(admitted) > 1 else None)
                continue
            try:
                review_id, review = await self._queued_review(request.context.session, identity, action, token)
            except StageModelSuperseded:
                continue
            binding = StageReviewBinding(identity, review_id)
            turn = self.store.load_turn(self.task_id, turn.turn_id)
            if isinstance(action, RoadTurnClosure):
                self._apply_closure(turn, running, binding, review.parts(action))
                continue
            if review.decision != "accept":
                self._commit(StageStateChange(reviews=(binding,)))
                continue
            if isinstance(action, StagePlanningDecision):
                if action.kind == "request_final_validation":
                    self._commit(StageStateChange(reviews=(binding,)))
                    if self.goal.handoff(self.evidence.request(review_id), action):
                        return StageRunResult(self.task_id, turn,
                            "stage.awaiting_" + self.goal_validation + "_validation")
                    continue
                self._apply_planning(turn, action, review, binding)
                continue
            self._apply_closure(turn, running, binding, ((action, review),))

    def _apply_closure(self, turn, road, binding, parts):
        before = turn
        for action, review in parts:
            if review.decision != "accept":
                continue
            if isinstance(action, TurnCompletion):
                turn = replace(turn, achievement=TurnAchievement(action, binding.review_operation_id),
                    fact_refs=tuple(dict.fromkeys((*turn.fact_refs, *action.support_refs))))
            elif isinstance(action, RoadResolution):
                turn = replace(turn, roads=tuple(replace(r, status=action.outcome) if r.road_id == road.road_id else r for r in turn.roads))
        self._commit(StageStateChange(turns=(turn,) if turn != before else (), reviews=(binding,)))
        if len(parts) == 2:
            submitted = self.store.load_d4_operation(binding.proposal_operation_id)
            reviewed = self.store.load_d4_operation(binding.review_operation_id)
            get_logger("ctrl").info("stage.road_turn_closure_reviewed", run=self.task_id,
                operation=binding.review_operation_id, phase="stage_completion", turn=turn.turn_id,
                road=road.road_id, road_decision=parts[0][1].decision, turn_decision=parts[1][1].decision,
                proposal_seconds=submitted.completion.finished_at - submitted.start.started_at,
                between_calls_seconds=reviewed.start.started_at - submitted.completion.finished_at,
                review_seconds=reviewed.completion.finished_at - reviewed.start.started_at)

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
        from agent_adapters.output_evidence import safe_failure_details
        errors = []
        try:
            await self.reviews.stop()
        except BaseException as error:
            errors.append(("reviews", error))
        try:
            await self._stop_tools()
        except BaseException as error:
            errors.append(("tools", error))
        try:
            await self.mcp.close()
        except BaseException as error:
            errors.append(("mcp", error))
        try:
            self.store.close()
        except BaseException as error:
            errors.append(("store", error))
        for phase, error in errors:
            get_logger("ctrl").error("stage.cleanup_failed", phase=phase,
                **safe_failure_details(error, include_chain=True))
        if errors:
            for phase, _ in errors[1:]:
                errors[0][1].add_note("stage.cleanup_failed:" + phase)
            raise errors[0][1]
