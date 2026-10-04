"""The D1 single-Turn control chain, with historical evidence-only completion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.attachment_import import AttachmentImportEvidencePort
from agent_core.attachment_workspace import D1ProofClaim, D1ProofClaimsPort
from agent_core.command_contracts import (
    SandboxCommandAuthorization,
    SandboxCommandRequest,
    T6CommandRecoveryRequired,
    T6EvidenceError,
)
from agent_core.control_store import StorageError
from agent_core.domain import (
    FactStatus,
    IntentStatus,
    Plan,
    PlanStatus,
    PlanStep,
    TurnStatus,
)
from agent_core.intent_postconditions import (
    CheckStatus,
    FileSha256,
    IntentPostconditions,
    check_postconditions,
)
from agent_core.modeling import CancellationToken
from agent_core.observation_proof import ObservationProofError
from agent_core.review import D2ReviewPaused, D2ReviewSession
from agent_core.review_contracts import D2ReviewModel, D2ReviewRequest, D2ReviewScope
from agent_core.runtime import TaskRunStatus
from agent_core.turn_contracts import (
    D1BudgetExhausted,
    D1OperationCompletion,
    D1OperationStart,
    D1RecoveryRequired,
    D1RunConfig,
    D1TurnBinding,
    D1TurnEvaluation,
)
from agent_core.turn_manager import TurnManager
from agent_core.turn_modeling import (
    D1AuditedModel,
    D1InnerContext,
    D1ModelRequest,
    D1OuterContext,
    D1ProposalRejected,
    digest,
    stable_id,
)
from agent_core.turn_working_copy import TurnWorkingCopy


class D1TurnRuleEvaluator:
    """Recheck the frozen rule against the saved observation, never the Sandbox."""

    def __init__(
        self,
        ledger,
        imports: AttachmentImportEvidencePort,
        postconditions,
        commands,
        proofs,
        workspace,
    ) -> None:
        self.ledger = ledger
        self.imports = imports
        self.postconditions = postconditions
        self.commands = commands
        self.proofs = proofs
        self.workspace = workspace

    def evaluate(
        self, binding: D1TurnBinding, state, fact_ids: tuple[str, ...]
    ) -> D1TurnEvaluation:
        try:
            if (
                self.ledger.load_d1_turn_binding(binding.turn_id) != binding
                or state.task_run.task_run_id != binding.task_run_id
            ):
                raise D1RecoveryRequired("turn_binding_mismatch")
            receipt = self.imports.read_receipt(binding.import_id)
            if receipt is None or (
                receipt.task_run_id,
                receipt.sandbox_id,
                receipt.attachment_index,
                receipt.sha256,
            ) != (
                binding.task_run_id,
                state.task_run.sandbox_id,
                binding.attachment_index,
                binding.expected_sha256,
            ):
                raise D1RecoveryRequired("import_binding_mismatch")
            copy = self.workspace.read_copy(binding.preparation_ref)
            operation = self.ledger.load_d1_operation(binding.preparation_ref)
            if copy is None or operation is None or operation.completion is None:
                raise D1RecoveryRequired("copy_evidence_missing")
            claim = copy.claim
            if (
                (
                    claim.binding_id,
                    claim.task_run_id,
                    claim.intent_id,
                    claim.import_id,
                    claim.work_path,
                    claim.source_path,
                    claim.expected_sha256,
                    copy.sha256,
                )
                != (
                    binding.binding_id,
                    binding.task_run_id,
                    binding.intent_id,
                    binding.import_id,
                    binding.work_path,
                    receipt.sandbox_path,
                    binding.expected_sha256,
                    binding.expected_sha256,
                )
                or operation.completion.result_kind != "completed"
                or operation.completion.evidence_sha256 != digest(asdict(copy))
            ):
                raise D1RecoveryRequired("copy_evidence_mismatch")
            if not fact_ids or len(fact_ids) != len(set(fact_ids)):
                return D1TurnEvaluation(False, None, ())
            refs = []
            for fact_id in fact_ids:
                fact = next(
                    (item for item in state.facts if item.fact_id == fact_id), None
                )
                if (
                    fact is None
                    or fact.status is not FactStatus.VERIFIED
                    or fact.turn_id != binding.turn_id
                    or fact.verification_ref is None
                    or not fact.verification_ref.startswith("t6:")
                ):
                    return D1TurnEvaluation(False, None, ())
                proof, _ = self.proofs.read_verified(
                    fact.source_ref, fact.verification_ref[3:]
                )
                claim = self.workspace.read_proof_claim(proof.proof_id)
                operation = (
                    None
                    if claim is None
                    else self.ledger.load_d1_operation(claim.observation_id)
                )
                if (
                    claim is None
                    or (
                        claim.task_run_id,
                        claim.binding_id,
                        claim.intent_id,
                        claim.plan_id,
                    )
                    != (
                        binding.task_run_id,
                        binding.binding_id,
                        binding.intent_id,
                        proof.plan_id,
                    )
                    or operation is None
                    or operation.start.category != "first_digest"
                    or operation.start.task_run_id != binding.task_run_id
                    or operation.application_ref != proof.proof_id
                ):
                    raise D1RecoveryRequired("proof_issuance_unavailable")
                conditions = self.postconditions.read(proof.plan_id, binding.intent_id)
                if (
                    conditions is None
                    or proof.intent_id != binding.intent_id
                    or proof.task_run_id != binding.task_run_id
                    or conditions.conditions
                    != (FileSha256(0, binding.work_path, binding.expected_sha256),)
                ):
                    raise D1RecoveryRequired("turn_rule_proof_mismatch")
                outcome, _ = check_postconditions(
                    state,
                    conditions,
                    self.commands,
                    lambda index, path: self.postconditions.read_file(
                        conditions, index, path
                    ),
                )
                if outcome is not CheckStatus.SATISFIED:
                    raise D1RecoveryRequired("turn_observation_unavailable")
                refs.append(fact.verification_ref)
            if len(set(refs)) != 1:
                raise D1RecoveryRequired("turn_rule_proof_mismatch")
            return D1TurnEvaluation(True, refs[0], fact_ids)
        except D1RecoveryRequired:
            raise
        except Exception as exc:
            raise D1RecoveryRequired("turn_evidence_unavailable") from exc


@dataclass(frozen=True, slots=True)
class D1Result:
    task_run_id: str
    turn_id: str | None
    intent_id: str | None
    verified_fact_ids: tuple[str, ...]
    intent_status: str | None
    turn_status: str | None
    rule_outcome: str
    rule_id: str
    rule_version: int
    task_run_status: str
    overall_goal: str
    dispatch_status: str
    stop_reason: str
    recovery_required: bool
    sandbox_status: str
    historical_verified_fact_ids: tuple[str, ...]
    fact_evidence_status: str
    control_state_status: str


class D1SingleTurnRunner:
    def __init__(
        self,
        *,
        controller,
        ledger,
        config: D1RunConfig,
        imports,
        import_id: str,
        outer: D1AuditedModel,
        inner: D1AuditedModel,
        copy_preparer,
        observer_factory,
        gateway_factory,
        postconditions,
        commands,
        proofs,
        evaluator: D1TurnRuleEvaluator,
        proof_claims: D1ProofClaimsPort,
        clock: Callable[[], float],
        d2_session: D2ReviewSession | None = None,
        d2_model: D2ReviewModel | None = None,
        d2_coordinator=None,
        d2_request_builder=None,
        d2_gate=None,
        d2_pause=None,
    ) -> None:
        self.controller = controller
        self.ledger = ledger
        self.config = config
        self.imports = imports
        self.import_id = import_id
        self.outer = outer
        self.inner = inner
        self.copy_preparer = copy_preparer
        self.observer_factory = observer_factory
        self.gateway_factory = gateway_factory
        self.postconditions = postconditions
        self.commands = commands
        self.proofs = proofs
        self.evaluator = evaluator
        self.proof_claims = proof_claims
        self.clock = clock
        self.d2_session = d2_session
        self.d2_model = d2_model
        self.d2_coordinator = d2_coordinator
        self._d2_root_ids: list[str] = []
        self.d2_request_builder = d2_request_builder
        self.d2_gate = d2_gate
        self.d2_pause = d2_pause
        self._last_state = controller.control_state
        self._token: CancellationToken | None = None

    @property
    def state(self):
        state = self.controller.control_state
        self._last_state = state
        return state

    def _id(self, name: str) -> str:
        return stable_id(self.state.task_run.task_run_id, name)

    def _work(self):
        return TurnWorkingCopy(self.state, self.commands)

    def _commit(self, name: str, prepare):
        mutation_id = self._id(name)
        saved = self.ledger.load_committed_mutation(
            self.state.task_run.task_run_id, mutation_id
        )
        if saved is not None:
            return saved
        return self.controller.commit_turn_operation(prepare(), mutation_id)

    def _interrupt(self, token: CancellationToken) -> bool:
        if self.state.task_run.status is not TaskRunStatus.RUNNING:
            return True
        if self.clock() >= self.state.task_run.deadline_at:
            self.controller.advance_time(self._id("deadline"))
            return True
        if token.is_cancelled:
            self.controller.cancel(self._id("cancel"))
            return True
        return False

    def _request(self, stage, context) -> D1ModelRequest:
        role = self.config.outer_role if stage == "outer" else self.config.inner_role
        return D1ModelRequest(
            self._id(stage + "/model"),
            self.state.task_run.task_run_id,
            self.state.latest_checkpoint.sequence,
            digest(asdict(context)),
            role.role,
            role.role_config_ref,
            self._id("register_turn"),
            stage,
            context,
        )

    def _d2_request(
        self,
        binding: D1TurnBinding,
        scope: D2ReviewScope,
        *,
        action_candidate_id: str | None,
        source_versions: tuple[str, ...],
    ) -> D2ReviewRequest:
        if self.d2_request_builder is not None:
            return self.d2_request_builder(
                binding, scope, action_candidate_id, source_versions
            )
        task_id = self.state.task_run.task_run_id
        suffix = action_candidate_id or "turn"
        root_id = self._id(f"d2/{scope}/root/{suffix}")
        return D2ReviewRequest(
            self._id(f"d2/{scope}/request/{suffix}"),
            root_id,
            1,
            1,
            scope,
            task_id,
            binding.turn_id,
            binding.intent_id,
            action_candidate_id,
            self.state.latest_checkpoint.sequence,
            digest(
                {
                    "scope": scope,
                    "task_run_id": task_id,
                    "turn_id": binding.turn_id,
                    "intent_id": binding.intent_id,
                    "action_candidate_id": action_candidate_id,
                    "facts": source_versions,
                }
            ),
            source_versions,
            0,
            f"D2 {scope} review for the committed D1 snapshot",
        )

    def _d2_start(self, binding: D1TurnBinding) -> None:
        if self.d2_session is None or self.d2_model is None:
            return
        request = self._d2_request(
            binding,
            "turn_start",
            action_candidate_id=None,
            source_versions=(binding.import_id,),
        )
        result = self.d2_session.turn_start(
            request,
            self.d2_model,
        )
        if result.output is not None:
            self._d2_root_ids.append(request.review_root_id)

    def _d2_validate(self, action_kind: str, timeout_seconds: int | None) -> None:
        if self.d2_gate is not None:
            self.d2_gate(action_kind, timeout_seconds)
            return
        if self.d2_coordinator is None or not self._d2_root_ids:
            return
        self.d2_coordinator.validate_future_action(
            tuple(self._d2_root_ids),
            action_kind=action_kind,
            timeout_seconds=timeout_seconds,
        )

    def _d2_action(
        self,
        binding: D1TurnBinding,
        *,
        action_candidate_id: str,
        source_versions: tuple[str, ...],
        triggered: bool,
        deterministic_rejection: bool = False,
        trigger_reasons: tuple[str, ...] = (),
    ):
        if self.d2_session is None or self.d2_model is None:
            return
        reasons = list(trigger_reasons)
        if deterministic_rejection:
            reasons.append("trusted_action_rejection")
        if (
            "fixed_probe_rejected" in source_versions
            or "completion_fact_rejected" in source_versions
        ):
            reasons.append("trusted_action_rejection")
        candidate_evidence = self.inner.evidence.read(action_candidate_id)
        if (
            candidate_evidence is not None
            and candidate_evidence.outcome == "invalid_structure"
        ):
            stage = (
                "probe"
                if action_candidate_id == self._id("probe/model")
                else "complete"
            )
            candidate_evidence = self.inner.evidence.read(
                self._id(stage + "/correction")
            )
        if candidate_evidence is not None and self.inner.review_requested(
            candidate_evidence.model_request_id
        ):
            reasons.append("inner_request_review")
        if not reasons and triggered:
            reasons.append("trusted_not_satisfied")
        request = self._d2_request(
            binding,
            "action",
            action_candidate_id=action_candidate_id,
            source_versions=source_versions,
        )
        result = self.d2_session.action(
            request,
            self.d2_model,
            action_finished=not deterministic_rejection,
            evidence_saved=True,
            triggered=triggered,
            trigger_reasons=tuple(dict.fromkeys(reasons)),
            deterministic_rejection=deterministic_rejection,
        )
        if hasattr(result, "output") and result.output is not None:
            self._d2_root_ids.append(request.review_root_id)
        return result

    def _d2_end(self, binding: D1TurnBinding, fact_ids: tuple[str, ...]) -> None:
        if self.d2_session is None or self.d2_model is None:
            return
        request = self._d2_request(
            binding,
            "turn_end",
            action_candidate_id=None,
            source_versions=fact_ids or (binding.import_id,),
        )
        self.d2_session.turn_end(
            request,
            self.d2_model,
            turn_completed=True,
            cancelled=(
                self._token is not None
                and self._token.is_cancelled
                or self.state.task_run.status is TaskRunStatus.CANCELLED
            ),
            deadline_exceeded=self.clock() >= self.state.task_run.deadline_at,
        )
        if self._token is not None:
            self._interrupt(self._token)

    def result(
        self, reason: str, outcome: str = "not_evaluated", recovery: bool = False
    ) -> D1Result:
        control_state_status = "validated"
        try:
            state = self.state
        except Exception:
            control_state_status = "recovery_required"
            recovery, outcome = True, "inconclusive"
            try:
                state = self.ledger.load_control_state_rows(
                    self._last_state.task_run.task_run_id
                )
            except Exception:
                state = None
            if state is None:
                state = self._last_state
                control_state_status = "last_known"
        turn = state.turns[0] if state.turns else None
        intent = state.intents[0] if state.intents else None
        facts = tuple(
            item.fact_id for item in state.facts if item.status is FactStatus.VERIFIED
        )
        verified = ()
        evidence_status = "not_present"
        if facts and turn is not None:
            try:
                binding = self.ledger.load_d1_turn_binding(turn.turn_id)
                if (
                    binding is None
                    or not self.evaluator.evaluate(binding, state, facts).satisfied
                ):
                    raise D1RecoveryRequired("fact_evidence_unavailable")
                verified = facts
                evidence_status = "verified"
            except Exception:
                evidence_status = "unavailable"
                if outcome == "satisfied":
                    reason = "fact_evidence_unavailable"
                outcome, recovery = "inconclusive", True
        return D1Result(
            state.task_run.task_run_id,
            turn.turn_id if turn else None,
            intent.intent_id if intent else None,
            verified,
            intent.status.value if intent else None,
            turn.status.value if turn else None,
            outcome,
            "attachment_copy_sha256",
            1,
            state.task_run.status.value,
            "not_evaluated",
            "stopped",
            reason,
            recovery,
            "retained"
            if state.task_run.status is TaskRunStatus.RUNNING
            else "not_confirmed",
            facts,
            evidence_status,
            control_state_status,
        )

    def _issue_proof(
        self, binding: D1TurnBinding, plan_id: str, observation_id: str
    ) -> None:
        claim = D1ProofClaim(
            self._id("proof"),
            binding.task_run_id,
            binding.binding_id,
            binding.intent_id,
            plan_id,
            observation_id,
        )
        operation = self.ledger.load_d1_operation(observation_id)
        if (
            operation is None
            or operation.start.category != "first_digest"
            or operation.start.task_run_id != binding.task_run_id
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.application_ref not in {None, claim.proof_id}
        ):
            raise D1RecoveryRequired("proof_observation_mismatch")
        existing = self.proof_claims.read_proof_claim(claim.proof_id)
        if existing is None:
            if (
                operation.application_ref is not None
                or self.proofs.read_proof(claim.proof_id) is not None
            ):
                raise D1RecoveryRequired("proof_claim_missing")
            self.proof_claims.begin_proof(claim)
            self.ledger.mark_d1_operation_applied(observation_id, claim.proof_id)
            self.proofs.issue(binding.intent_id, claim.proof_id, plan_id)
        elif existing != claim:
            raise D1RecoveryRequired("proof_claim_mismatch")
        proof = self.proofs.read_proof(claim.proof_id)
        if proof is None:
            raise D1RecoveryRequired("declared_proof_missing")
        if (proof.task_run_id, proof.intent_id, proof.plan_id) != (
            claim.task_run_id,
            claim.intent_id,
            claim.plan_id,
        ):
            raise D1RecoveryRequired("declared_proof_mismatch")
        self.proofs.read_verified(proof.source_execution_id, claim.proof_id)
        self.ledger.mark_d1_operation_applied(observation_id, claim.proof_id)

    def _reconcile_inner(self, stage, binding, facts, token):
        evidence = self.inner.evidence.read(self._id(stage + "/model"))
        if evidence is None or evidence.outcome == "invalid_structure":
            evidence = self.inner.evidence.read(self._id(stage + "/correction"))
        if evidence is None:
            raise D1RecoveryRequired("inner_model_evidence_missing")
        from agent_core.attachment_workspace import fixed_digest_script

        context = D1InnerContext(
            self.state.turns[0].turn_target,
            binding,
            fixed_digest_script(binding.work_path),
            facts,
        )
        return self.inner.reconcile_application(
            evidence.model_request_id,
            stage,
            context,
            self._id("register_turn"),
            self._id("plan") if stage == "probe" else binding.intent_id,
            self._id("register_plan")
            if stage == "probe"
            else self._id("complete_intent"),
            token,
        )

    async def run(self, token: CancellationToken) -> D1Result:
        self._token = token
        try:
            return await self._run(token)
        except D1ProposalRejected as exc:
            self._interrupt(token)
            return self.result(exc.code)
        except D1BudgetExhausted:
            return self.result("budget_exhausted")
        except D2ReviewPaused as exc:
            if self.d2_pause is not None:
                self.d2_pause(exc)
            self._interrupt(token)
            outcome = (
                "satisfied"
                if self.state.turns
                and self.state.turns[0].status is TurnStatus.COMPLETED
                else "not_satisfied"
                if exc.reason == "no_safe_continuation_in_d2"
                else "inconclusive"
            )
            return self.result(str(exc), outcome, exc.outcome == "unknown")
        except (
            D1RecoveryRequired,
            T6CommandRecoveryRequired,
            T6EvidenceError,
            StorageError,
            ObservationProofError,
        ) as exc:
            self._interrupt(token)
            if self.state.turns and self.state.turns[0].status is TurnStatus.COMPLETED:
                return self.result(str(exc), "satisfied", True)
            return self.result(str(exc), "inconclusive", True)

    async def _run(self, token: CancellationToken) -> D1Result:
        if (
            self.ledger.load_d1_run_config(self.state.task_run.task_run_id)
            != self.config
        ):
            raise D1RecoveryRequired("run_configuration_changed")
        if self._interrupt(token):
            return self.result("cancelled_or_deadline")
        receipt = self.imports.read_receipt(self.import_id)
        if receipt is None or receipt.task_run_id != self.state.task_run.task_run_id:
            raise D1RecoveryRequired("attachment_import_unavailable")
        if self.state.turns:
            if len(self.state.turns) != 1:
                raise D1RecoveryRequired("single_turn_required")
            turn = self.state.turns[0]
            binding = self.ledger.load_d1_turn_binding(turn.turn_id)
            if binding is None or binding.import_id != self.import_id:
                raise D1RecoveryRequired("turn_binding_unavailable")
        else:
            request_id, proposal = await self.outer.propose_async(
                self._request(
                    "outer", D1OuterContext(self.state.task_run.request.goal, receipt)
                ),
                token,
            )
            if self._interrupt(token):
                return self.result("cancelled_or_deadline")
            registration = TurnManager.prepare_turn_registration(
                state=self.state,
                mutation_id=self._id("register_turn"),
                turn_target=proposal.turn_target,
                candidates=proposal.intents,
            )
            intent_id = registration.entity_ids[1]
            binding = D1TurnBinding(
                self._id("binding"),
                self.state.task_run.task_run_id,
                registration.turn_id,
                intent_id,
                0,
                self.import_id,
                receipt.sha256,
                f"/workspace/work/intents/{intent_id}/attachment",
                self._id("copy"),
                request_id,
            )
            self.controller.commit_turn_registration(
                registration, self._id("register_turn"), d1_binding=binding
            )
            self.ledger.mark_d1_operation_applied(request_id, binding.turn_id)
        self.outer.reconcile_application(
            binding.proposal_request_id,
            "outer",
            D1OuterContext(self.state.task_run.request.goal, receipt),
            self._id("register_turn"),
            binding.turn_id,
            self._id("register_turn"),
            token,
        )
        if any(plan.plan_id == self._id("plan") for plan in self.state.plans):
            self._reconcile_inner("probe", binding, (), token)
        if self.state.intents[0].status is IntentStatus.SUCCEEDED:
            saved_facts = tuple(
                fact.fact_id
                for fact in self.state.facts
                if fact.status is FactStatus.VERIFIED
            )
            self._reconcile_inner("complete", binding, saved_facts, token)
        if self.state.turns[0].status is TurnStatus.COMPLETED:
            proof = self.ledger.load_d1_turn_proof(binding.turn_id)
            if proof is None:
                raise D1RecoveryRequired("turn_proof_missing")
            evaluation = self.evaluator.evaluate(
                binding, self.state, proof.supporting_fact_ids
            )
            if not evaluation.satisfied or evaluation.proof_ref != proof.proof_ref:
                raise D1RecoveryRequired("turn_proof_mismatch")
            self._d2_end(binding, proof.supporting_fact_ids)
            return self.result("turn_completed_sandbox_retained", "satisfied")
        if self.state.intents[0].status is IntentStatus.PENDING:
            self._commit(
                "select_intent",
                lambda: self._work().select_next_intent(binding.turn_id),
            )
        self._d2_start(binding)
        await self.copy_preparer.prepare(binding, token)
        if self._interrupt(token):
            return self.result("cancelled_or_deadline")
        from agent_core.attachment_workspace import fixed_digest_script

        plan_id = self._id("plan")
        plan = next(
            (item for item in self.state.plans if item.plan_id == plan_id), None
        )
        facts = tuple(
            item.fact_id
            for item in self.state.facts
            if item.status is FactStatus.VERIFIED
        )
        if not facts:
            context = D1InnerContext(
                self.state.turns[0].turn_target,
                binding,
                fixed_digest_script(binding.work_path),
                (),
            )
            probe_id = self._id("probe/model")
            previous = self.ledger.load_d1_operation(probe_id)
            request = self._request("probe", context)
            if previous is not None and plan is not None:
                from dataclasses import replace

                request = replace(
                    request,
                    base_checkpoint_sequence=previous.start.base_checkpoint_sequence,
                )
            review_requested = False
            if plan is None:
                request_id, action = await self.inner.propose_async(request, token)
                review_requested = self.inner.review_requested(request_id)
                expected = (FileSha256(0, binding.work_path, binding.expected_sha256),)
                if (
                    not isinstance(action, ProbeAction)
                    or action.script != context.fixed_script
                    or action.stdin != b""
                    or action.working_directory != binding.work_path.rsplit("/", 1)[0]
                    or action.conditions != expected
                ):
                    self.inner.reject_application(request_id, "fixed_probe_rejected")
                    self._d2_action(
                        binding,
                        action_candidate_id=self._id("probe/model"),
                        source_versions=(request_id, "fixed_probe_rejected"),
                        triggered=True,
                        deterministic_rejection=True,
                    )
                    raise D1ProposalRejected("fixed_probe_rejected")
                self.postconditions.bind(
                    IntentPostconditions(
                        binding.task_run_id, binding.intent_id, plan_id, expected
                    )
                )
                self._commit(
                    "register_plan",
                    lambda: self._work().register_plan(
                        Plan(
                            plan_id,
                            binding.intent_id,
                            (PlanStep("fixed read-only SHA-256 check"),),
                        )
                    ),
                )
                self.ledger.mark_d1_operation_applied(request_id, plan_id)
                self._commit("start_plan", lambda: self._work().start_plan(plan_id))
            else:
                evidence = self.inner.evidence.read(probe_id)
                if evidence is None or evidence.outcome == "invalid_structure":
                    evidence = self.inner.evidence.read(self._id("probe/correction"))
                if evidence is None or evidence.outcome != "accepted":
                    raise D1RecoveryRequired("probe_evidence_missing")
                request_id = evidence.model_request_id
                self.inner.reconcile_application(
                    request_id,
                    "probe",
                    context,
                    self._id("register_turn"),
                    plan_id,
                    self._id("register_plan"),
                    token,
                )
                request_id, action = self.inner.read_saved(
                    evidence.model_request_id,
                    "probe",
                    context,
                    self._id("register_turn"),
                    token,
                )
                review_requested = self.inner.review_requested(request_id)
                saved_conditions = self.postconditions.read(plan_id, binding.intent_id)
                if saved_conditions is None or saved_conditions.conditions != (
                    FileSha256(0, binding.work_path, binding.expected_sha256),
                ):
                    raise D1RecoveryRequired("probe_postconditions_missing")
                if plan.status is PlanStatus.PENDING:
                    self._commit("start_plan", lambda: self._work().start_plan(plan_id))
            if self._interrupt(token):
                return self.result("cancelled_or_deadline")
            command_id = self._id("command")
            tool_record = self.ledger.load_d1_operation(command_id)
            completed = self.commands.read_completed(command_id)
            if tool_record is not None and completed is None:
                raise D1RecoveryRequired("command_outcome_unknown")
            if completed is None:
                try:
                    self._d2_validate("probe", action.timeout_seconds)
                except D2ReviewPaused as exc:
                    if self.d2_session is None or exc.outcome != "rejected":
                        raise
                    self.inner.reject_dispatch(request_id, exc.reason)
                    self._d2_action(
                        binding,
                        action_candidate_id=self._id("probe/model"),
                        source_versions=(request_id,),
                        triggered=True,
                        deterministic_rejection=True,
                        trigger_reasons=("trusted_action_rejection",),
                    )
                    raise
                self.ledger.begin_d1_operation(
                    D1OperationStart(
                        command_id,
                        binding.task_run_id,
                        "inner_tool",
                        self.clock(),
                        self.state.latest_checkpoint.sequence,
                        digest(action_payload(action)),
                        resource_ref=plan_id,
                    )
                )
                authorization = SandboxCommandAuthorization(
                    self.config.authorization.authorization_id,
                    binding.task_run_id,
                    ("sandbox.command",),
                )
                completed = await self.gateway_factory(binding).call(
                    SandboxCommandRequest(
                        command_id,
                        binding.task_run_id,
                        plan_id,
                        0,
                        action.script,
                        action.stdin,
                        action.working_directory,
                        action.timeout_seconds,
                    ),
                    authorization,
                    token,
                )
            if completed.record.status == "outcome_unknown":
                raise D1RecoveryRequired("command_outcome_unknown")
            self.ledger.complete_d1_operation(
                D1OperationCompletion(
                    command_id,
                    completed.record.finished_at,
                    "completed" if completed.record.status == "completed" else "failed",
                    command_id,
                    digest(
                        {
                            "request_id": command_id,
                            "command_ref": completed.record.command_ref,
                            "status": completed.record.status,
                        }
                    ),
                )
            )
            if self._interrupt(token):
                return self.result("cancelled_or_deadline")
            if completed.record.status != "completed":
                return self.result("command_" + completed.record.status, "inconclusive")
            self._commit("complete_plan", lambda: self._work().complete_plan(plan_id))
            postcondition = self.postconditions.read(plan_id, binding.intent_id)
            observer = self.observer_factory(binding)
            observation = await observer.observe(postcondition, token)
            if self._interrupt(token):
                return self.result("cancelled_or_deadline")
            if observation.status == "unknown":
                raise D1RecoveryRequired("digest_outcome_unknown")
            if (
                observation.status != "present"
                or observation.sha256 != binding.expected_sha256
            ):
                self._d2_action(
                    binding,
                    action_candidate_id=self._id("probe/model"),
                    source_versions=(command_id, observer.operation_id(postcondition)),
                    triggered=True,
                    trigger_reasons=("trusted_not_satisfied",),
                )
                if self.d2_session is not None:
                    raise D2ReviewPaused("no_safe_continuation_in_d2")
                return self.result("first_observation_not_satisfied", "not_satisfied")
            self._issue_proof(binding, plan_id, observer.operation_id(postcondition))
            self.controller.commit_verified_t6_observation(
                binding.intent_id,
                self._id("proof"),
                self._id("fact"),
                self._id("commit_fact"),
                complete_intent=False,
            )
            self.ledger.mark_d1_operation_applied(request_id, plan_id)
            self.ledger.mark_d1_operation_applied(command_id, plan_id)
        facts = tuple(
            item.fact_id
            for item in self.state.facts
            if item.status is FactStatus.VERIFIED
        )
        if self.d2_session is not None and facts:
            probe_evidence = self.inner.evidence.read(self._id("probe/model"))
            if (
                probe_evidence is not None
                and probe_evidence.outcome == "invalid_structure"
            ):
                probe_evidence = self.inner.evidence.read(self._id("probe/correction"))
            if probe_evidence is None:
                raise D1RecoveryRequired("probe_evidence_missing")
            if self.inner.review_requested(probe_evidence.model_request_id):
                self._d2_action(
                    binding,
                    action_candidate_id=self._id("probe/model"),
                    source_versions=(self._id("command"), self._id("fact")),
                    triggered=True,
                )
        if self.state.intents[0].status is not IntentStatus.SUCCEEDED:
            context = D1InnerContext(
                self.state.turns[0].turn_target,
                binding,
                fixed_digest_script(binding.work_path),
                facts,
            )
            request_id, action = await self.inner.propose_async(
                self._request("complete", context), token
            )
            review_requested = self.inner.review_requested(request_id)
            if not isinstance(action, CompleteIntentAction) or action.fact_ids != facts:
                self.inner.reject_application(request_id, "completion_fact_rejected")
                self._d2_action(
                    binding,
                    action_candidate_id=self._id("complete/model"),
                    source_versions=(request_id, "completion_fact_rejected"),
                    triggered=True,
                    deterministic_rejection=True,
                )
                raise D1ProposalRejected("completion_fact_rejected")
            if review_requested:
                self._d2_action(
                    binding,
                    action_candidate_id=self._id("complete/model"),
                    source_versions=(request_id,) + facts,
                    triggered=True,
                )
            try:
                self._d2_validate("complete_intent", None)
            except D2ReviewPaused as exc:
                if self.d2_session is None or exc.outcome != "rejected":
                    raise
                self.inner.reject_dispatch(request_id, exc.reason)
                self._d2_action(
                    binding,
                    action_candidate_id=self._id("complete/model"),
                    source_versions=(request_id,) + facts,
                    triggered=True,
                    deterministic_rejection=True,
                    trigger_reasons=("trusted_action_rejection",),
                )
                raise
            self.controller.complete_verified_t6_intent(
                binding.intent_id, action.fact_ids, self._id("complete_intent")
            )
            self.ledger.mark_d1_operation_applied(request_id, binding.intent_id)
        self.controller.complete_verified_d1_turn(
            binding.turn_id, facts, self._id("complete_turn")
        )
        self._d2_end(binding, facts)
        return self.result("turn_completed_sandbox_retained", "satisfied")
