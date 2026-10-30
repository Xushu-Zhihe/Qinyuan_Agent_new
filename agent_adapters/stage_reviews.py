"""One Outer queue consumer, using the original Stage model/evidence owners."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace

from agent_adapters.stage_evidence import StageModelSuperseded, StageModelTransportFailure
from agent_adapters.output_evidence import safe_failure_details
from agent_core.command_contracts import T6EvidenceCapacityError
from agent_core.stage_contracts import (
    StageBranchFork, StageFactApplication, StageFactBatch, StageReviewBinding, StageStateChange,
    fact_candidates, reviewed_fact, TurnIntent,
)
from agent_core.stage_planning import review_members, stage_runtime, conversation_operation_ids
from agent_core.stage_storage import (
    branch_prefix, load_branch, review_groups, review_rejections, StageRecoveryRequired,
)
from agent_core.turn_modeling import digest
from qinyuan_core import get_logger


class _GenerationToken:
    def __init__(self, parent):
        self.parent = parent
        self.superseded_by = None
        self.stopped = False

    @property
    def is_cancelled(self):
        return self.parent.is_cancelled or self.stopped or self.superseded_by is not None

    @property
    def is_user_cancelled(self):
        return self.parent.is_cancelled


class StageReviews:
    def __init__(self, app):
        self.app = app
        self.job = self.foreground = None

    def _root(self, request):
        while request.correction_of_model_request_id or request.capacity_recovery_of:
            request = self.app.evidence.request(request.correction_of_model_request_id or request.capacity_recovery_of)
        return request.model_request_id

    def _owned(self, identity, entry):
        if entry is None or entry.task.done() or identity not in self.app.models.live_operations:
            return False
        request = self.app.evidence.request(identity)
        return request.task_run_id == self.app.task_id and self._root(request) == entry.request.model_request_id

    def live(self, identity):
        return self._owned(identity, self.job)

    def live_foreground(self, identity):
        return self._owned(identity, self.foreground)

    def admits(self, start):
        if self.job is None or self.job.task.done() or start.role != "outer_planning":
            return False
        request = self.app.evidence.request(start.operation_id)
        return (request.task_run_id == self.app.task_id and self._root(request) == self.job.request.model_request_id
            and request.stage in {"stage_review", "stage_summary"}
            and start.context_sha256 == digest(asdict(request)))

    def eligible(self, identity):
        request = self.app.evidence.request(identity)
        operation = self.app.store.load_d4_operation(identity)
        value = self.app.evidence.read_model_result(operation).result
        from agent_core.stage_plans import admitted
        if isinstance(value, (TurnIntent, tuple)) and not admitted(self.app.store, identity):
            return False
        return (request.stage == "stage_action" and request.context.session.scope == "road_execution" and bool(fact_candidates(value))
            or request.context.session.scope == "turn_planning" and isinstance(value, (TurnIntent, tuple)))

    def _make_request(self, members):
        app = self.app
        first = app.evidence.request(members[0])
        sources = [app.evidence.read_model_result(app.store.load_d4_operation(identity)) for identity in members]
        value = sources[0].result if len(members) == 1 else StageFactBatch(tuple(
            claim for source in sources for _, claim in fact_candidates(source.result)))
        request = app._request("stage_review", first.context.session, proposal_id=members[0], proposal=value)
        if isinstance(value, TurnIntent) and first.context.turn is None:
            request = replace(request, context=replace(request.context, turn=None,
                effective_support_refs=first.context.effective_support_refs))
        identity = app._id("stage/review/" + members[0]) if len(members) == 1 else app._id(
            "stage/review-batch/" + "/".join(members))
        if app.evidence.read("request", identity) is not None:
            return app.evidence.request(identity)
        runtime = {**stage_runtime(request), "review_members": list(members)}
        return replace(request, model_request_id=identity, execution_context={**request.execution_context, "stage_runtime": runtime})

    async def start(self, token):
        if self.job is not None:
            return
        groups = review_groups(self.app.store, self.app.task_id)
        if not groups or not self.eligible(groups[0][0]):
            return
        members = groups[0]
        saved = self.app.evidence.read("review-group", members[0])
        if saved is not None:
            members = tuple(saved["members"])
            request = self.app.evidence.request(saved["model_request_id"])
            if saved["request_sha256"] != digest(asdict(request)) or members != review_members(request):
                raise StageRecoveryRequired("stage.review_group_binding_changed")
        else:
            # Never wait to accumulate work, or send an oversized aggregation.
            from agent_adapters.stage_evidence import _envelope
            while True:
                request = self._make_request(members)
                contract = self.app.evidence.contract(request)
                request = replace(request, frozen_contract_ref="sha256:" + digest(contract))
                envelope = _envelope(request, contract)
                fits_record = True
                if len(members) > 1:
                    try:
                        self.app.evidence.records.encode("stage-request", request.model_request_id,
                            {"request": asdict(request), "proposal_kind": type(request.proposal).__name__}, limit=1024 * 1024)
                    except T6EvidenceCapacityError:
                        fits_record = False
                if len(members) == 1 or fits_record and self.app.models.capacity_port.fits(envelope, self.app.config.handoff_config.outer_capacity):
                    break
                members = members[:-1]
            self.app.evidence.review_sources(request)
            request = self.app.models.prepare(request)
            self.app.evidence.save("review-group", members[0], {
                "members": list(members), "model_request_id": request.model_request_id, "request_sha256": digest(asdict(request))})
        pending = {entry.proposal_operation_id for entry in self.app.store.list_stage_reviews(self.app.task_id) if entry.state == "pending"}
        if not set(members).issubset(pending):
            raise StageRecoveryRequired("stage.review_group_binding_changed")
        self.app.evidence.review_sources(request)
        self.job = SimpleNamespace(request=request, members=members, token=_GenerationToken(token), task=None)
        self.job.task = asyncio.create_task(self.app.models.call(request, self.job.token))
        await asyncio.sleep(0)  # Register the original operation before allowing Inner to proceed.
        get_logger("ctrl").info("stage.review_resumed" if saved is not None else "stage.review_dequeued", run=self.app.task_id,
            operation=request.model_request_id, phase="stage_review", proposal_count=len(members))

    def _correction(self, identity):
        request = self.app.evidence.request(identity)
        branch = load_branch(self.app.store, self.app.task_id, request.context.session)
        visible = set(branch_prefix(self.app.store, branch)) if branch else set()
        return next((source for source in review_rejections(self.app.store, identity) if source in visible), None)

    def _result(self):
        try:
            identity, result = self.job.task.result()
            return identity, result, None
        except StageModelTransportFailure as error:
            if error.context["model_call"]["stage"] != "stage_review":
                raise
            return error.context["model_call"]["operation_id"], None, error

    def dispatch_fence(self, proposal_id):
        if self.job is None or not self.job.task.done():
            return None
        identity, _, error = self._result()
        if error:
            return None
        request = self.app.evidence.request(proposal_id)
        return identity if set(self.app.evidence.review_interruptions(identity)).intersection(conversation_operation_ids(request)) else None

    async def collect(self, token, *, wait=False, unused=None):
        await self.start(token)
        if self.job is None:
            return False
        if wait:
            while not self.job.task.done():
                self.app._check_dispatch()
                await self.app._collect_tools()
                await asyncio.wait({self.job.task}, timeout=.1)
        if not self.job.task.done():
            return False
        identity, result, error = self._result()
        request = self.app.evidence.request(identity)
        repair = None if error else self._correction(identity)
        from agent_core import stage_plans
        updates = () if error else stage_plans.feedback_turns(self.app.store, self.app.task_id, identity)
        if repair or updates:
            await self.app._collect_tools(drain=True)
        if error:
            change = StageStateChange(blocked_reviews=tuple(StageReviewBinding(member, identity) for member in self.job.members))
        else:
            claims = self.app.evidence.review_claims(request) if fact_candidates(request.proposal) else ()
            facts = []
            for index, (source_id, local_index, claim) in enumerate(claims):
                slot = index if isinstance(request.proposal, StageFactBatch) else None
                if not claim.inferred and reviewed_fact(request.proposal, result, slot)[1].decision == "accept":
                    facts.append(StageFactApplication(self.app._id("stage/fact/" + source_id
                        + ("" if local_index is None else "/" + str(local_index))), identity, slot))
            change = StageStateChange(turns=updates, reviews=(StageReviewBinding(self.job.members[0], identity),), facts=tuple(facts),
                branches=(StageBranchFork(repair, identity),) if repair else (),
                discarded_decisions=(StageReviewBinding(unused, identity),) if unused else ())
        self.app._commit(change)
        if error:
            get_logger("ctrl").warning("stage.review_unavailable", run=self.app.task_id,
                operation=identity, phase="stage_review", category="dependency", proposal_count=len(self.job.members),
                **safe_failure_details(error))
        if repair:
            get_logger("ctrl").info("stage.branch_restored", run=self.app.task_id,
                operation=identity, source_operation=repair, phase=self.app.evidence.request(repair).stage)
        self.job = None
        return bool(repair)

    async def drain(self, token, *, before=None):
        repaired = False
        while True:
            pending = [entry.proposal_operation_id for entry in self.app.store.list_stage_reviews(self.app.task_id)
                if entry.state == "pending"]
            if self.job is None and pending and pending[0] == before:
                return repaired
            await self.start(token)
            if self.job is None:
                return repaired
            repaired = await self.collect(token, wait=True) or repaired

    async def action(self, request, token):
        await self.start(token)
        self.foreground = SimpleNamespace(request=request, token=_GenerationToken(token), task=None)
        self.foreground.task = asyncio.create_task(self.app.models.call(request, self.foreground.token))
        while not self.foreground.task.done():
            waiting = {self.foreground.task}
            if self.job is not None and not self.job.task.done():
                waiting.add(self.job.task)
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if self.job is not None and self.job.task.done():
                identity, _, error = self._result()
                interrupt = () if error else self.app.evidence.review_interruptions(identity)
                if set(interrupt).intersection(conversation_operation_ids(request)):
                    self.foreground.token.superseded_by = identity
                    unused = None
                    try:
                        unused = (await self.foreground.task)[0]
                    except StageModelSuperseded:
                        pass
                    await self.collect(token, unused=unused)
                    self.foreground = None
                    return None
        # If both tasks finished in the same scheduler tick, still fence the
        # unused decision before it reaches any tool dispatcher.
        if self.job is not None and self.job.task.done():
            identity, _, error = self._result()
            if not error and set(self.app.evidence.review_interruptions(identity)).intersection(conversation_operation_ids(request)):
                unused = self.foreground.task.result()[0]
                await self.collect(token, unused=unused)
                self.foreground = None
                return None
        result = self.foreground.task.result()
        self.foreground = None
        return result

    async def stop(self):
        from agent_core.dual_llm_modeling import MODEL_CANCEL_GRACE_SECONDS
        entries = [entry for entry in (self.foreground, self.job) if entry is not None]
        for entry in entries:
            entry.token.stopped = True
        tasks = [entry.task for entry in entries]
        if tasks:
            await asyncio.wait(tasks, timeout=MODEL_CANCEL_GRACE_SECONDS)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.foreground = self.job = None
