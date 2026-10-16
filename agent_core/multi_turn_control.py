"""Typed Controller transactions for the bounded D3 control graph."""

from __future__ import annotations

from dataclasses import asdict, replace

from agent_core.artifact_proofs import d3_fact_content
from agent_core.conclusion_contracts import D8Conclusion
from agent_core.control_store import (
    StateChange,
    UpsertFact,
    UpsertIntent,
    UpsertPlan,
    UpsertTurn,
)
from agent_core.domain import FactStatus, IntentStatus, PlanStatus, PlanStep, TurnStatus
from agent_core.dual_llm_contracts import (
    D3_SHA_STEP,
    D3_SIZE_STEP,
    D3CommitMetadata,
    D3IntentBinding,
    D3Outcome,
    D3PlanBinding,
    D3PlanResult,
    D3RecoveryRequired,
    D3TurnBinding,
)
from agent_core.turn_manager import TurnManager
from agent_core.turn_modeling import digest, stable_id


class D3Control:
    def __init__(self, controller, ledger) -> None:
        self.controller = controller
        self.ledger = ledger

    @property
    def state(self):
        return self.controller.control_state

    def _id(self, name: str) -> str:
        return stable_id(self.state.task_run.task_run_id, f"d3/{name}")

    def _commit(self, name, operation, changes, result, metadata=None):
        mutation_id = self._id(name)
        entity_ids = tuple(
            dict.fromkeys(
                change.turn_id
                if isinstance(change, UpsertTurn)
                else change.intent_id
                if isinstance(change, UpsertIntent)
                else change.plan_id
                if isinstance(change, UpsertPlan)
                else change.record_id
                if isinstance(change, D8Conclusion)
                else change.fact_id
                for change in changes
            )
        )
        return self.controller.commit_d3_transition(
            operation,
            tuple(changes),
            entity_ids,
            result,
            mutation_id,
            metadata=metadata,
        )

    def _registration(self, name, proposal):
        mutation_id = self._id(name)
        state = self.state
        if name.startswith("adjust/"):
            old_turn = state.turns[-1]
            state = replace(
                state,
                turns=state.turns[:-1]
                + (replace(old_turn, status=TurnStatus.SUPERSEDED),),
            )
        return TurnManager.prepare_turn_registration(
            state=state,
            mutation_id=mutation_id,
            turn_target=proposal.turn_target,
            candidates=proposal.intents,
        )

    def _turn_metadata(self, registration, proposal, proposal_request_id, receipt):
        task_id = self.state.task_run.task_run_id
        binding = D3TurnBinding(
            self._id(f"binding/{registration.turn_id}"),
            task_id,
            registration.turn_id,
            proposal.attachment_index,
            receipt.import_id,
            receipt.byte_size,
            receipt.sha256,
            proposal_request_id,
        )
        intent_bindings = tuple(
            D3IntentBinding(
                task_id,
                registration.turn_id,
                intent_id,
                f"/workspace/work/intents/{intent_id}",
            )
            for intent_id in registration.entity_ids[1:]
        )
        return binding, intent_bindings

    def register_turn(
        self, proposal, proposal_request_id, receipt, name="turn/1/register"
    ):
        registration = self._registration(name, proposal)
        binding, intents = self._turn_metadata(
            registration, proposal, proposal_request_id, receipt
        )
        self._commit(
            name,
            "register_d3_turn",
            registration.state_changes,
            registration.turn_id,
            D3CommitMetadata(turn_binding=binding, intent_bindings=intents),
        )
        return binding

    def select_first(self, turn_id: str):
        state = self.state
        turn = next(item for item in state.turns if item.turn_id == turn_id)
        first = min(
            (item for item in state.intents if item.turn_id == turn_id),
            key=lambda item: item.priority,
        )
        changes = (
            UpsertTurn(
                turn.turn_id,
                turn.task_run_id,
                turn.sequence,
                turn.turn_target,
                turn.intent_ids,
                TurnStatus.RUNNING,
            ),
            UpsertIntent(
                first.intent_id,
                first.turn_id,
                first.description,
                first.priority,
                first.intent_type,
                IntentStatus.RUNNING,
            ),
        )
        self._commit(
            f"select/{turn_id}",
            "select_d3_intent",
            changes,
            first.intent_id,
        )
        return first.intent_id

    def register_plan(
        self,
        intent_id: str,
        proposal,
        proposal_request_id: str,
        artifact_version_id: str,
        *,
        source_fact_id: str | None = None,
        review_id: str | None = None,
    ) -> D3PlanBinding:
        state = self.state
        intent = next(item for item in state.intents if item.intent_id == intent_id)
        turn = self.ledger.load_d3_turn_binding(intent.turn_id)
        from agent_core.hybrid_contracts import AttachmentPlanProposal,AttachmentPlanBinding,attachment_binding
        hybrid=isinstance(proposal,AttachmentPlanProposal)
        if hybrid:turn=attachment_binding(turn,proposal.attachment_index)
        if turn is None:
            raise D3RecoveryRequired("d3_plan_turn_binding_missing")
        previous = [item for item in state.plans if item.intent_id == intent_id]
        ordinal = len(previous) + 1
        if (ordinal == 1 and review_id is not None) or (
            ordinal > 1 and review_id is None
        ):
            raise D3RecoveryRequired("d3_plan_requires_its_replanning_review")
        plan_id = self._id(f"plan/{intent_id}/{ordinal}")
        steps = (
            (PlanStep(D3_SIZE_STEP), PlanStep(D3_SHA_STEP))
            if proposal.template == "size_then_sha256"
            else (PlanStep(D3_SHA_STEP),)
        )
        binding = D3PlanBinding(
            state.task_run.task_run_id,
            intent.turn_id,
            intent_id,
            plan_id,
            proposal.template,
            proposal.observation_mode,
            artifact_version_id,
            proposal_request_id,
            digest(asdict(turn)),
            review_id,
            proposal.source_proof_id,
            source_fact_id,
        )
        if hybrid:
            binding=AttachmentPlanBinding(**asdict(binding),attachment_index=proposal.attachment_index)
        new_plan = UpsertPlan(plan_id, intent_id, steps, PlanStatus.RUNNING)
        if previous and previous[-1].status is PlanStatus.RUNNING:
            if review_id is None:
                raise D3RecoveryRequired("d3_replan_review_missing")
            old = previous[-1]
            old_change = UpsertPlan(
                old.plan_id,
                old.intent_id,
                old.steps,
                PlanStatus.SUPERSEDED,
            )
            outcome = D3Outcome(
                "plan",
                old.plan_id,
                "superseded",
                "replanned",
                (review_id, proposal_request_id),
            )
            self._commit(
                f"plan/{intent_id}/{ordinal}/register",
                "replan_d3_plan",
                (old_change, new_plan),
                plan_id,
                D3CommitMetadata(plan_binding=binding, outcomes=(outcome,)),
            )
        elif not previous or previous[-1].status is PlanStatus.COMPLETED:
            self._commit(
                f"plan/{intent_id}/{ordinal}/register",
                "register_d3_plan",
                (new_plan,),
                plan_id,
                D3CommitMetadata(plan_binding=binding),
            )
        else:
            raise D3RecoveryRequired("d3_previous_plan_not_closed")
        return binding

    def complete_plan(
        self,
        binding: D3PlanBinding,
        result: D3PlanResult,
        source_execution_id: str | None = None,
        *, conclusion: D8Conclusion | None = None, content: str | None = None,
    ):
        state = self.state
        plan = next(item for item in state.plans if item.plan_id == binding.plan_id)
        changes: list[StateChange] = [
            UpsertPlan(
                plan.plan_id,
                plan.intent_id,
                plan.steps,
                PlanStatus.COMPLETED,
            )
        ]
        if (
            binding.observation_mode == "new_observation"
            and result.outcome == "satisfied"
        ):
            if (
                source_execution_id is None
                or result.proof_id is None
                or result.supporting_fact_id is None
            ):
                raise D3RecoveryRequired("d3_observed_plan_support_missing")
            changes.append(
                UpsertFact(
                    result.supporting_fact_id,
                    binding.turn_id,
                    content if content is not None else d3_fact_content(result.proof_id),
                    source_execution_id,
                    FactStatus.VERIFIED,
                    f"d3:{result.proof_id}",
                )
            )
        elif source_execution_id is not None:
            raise D3RecoveryRequired("d3_reused_plan_cannot_create_fact")
        if conclusion is not None:
            changes.append(conclusion)
        return self._commit(
            f"plan/{plan.plan_id}/result",
            "complete_d3_plan",
            tuple(changes),
            result.outcome,
            D3CommitMetadata(plan_result=result),
        )

    def complete_intent(
        self,
        intent_id: str,
        plan_id: str,
        fact_id: str,
        action_request_id: str,
    ):
        intent = next(
            item for item in self.state.intents if item.intent_id == intent_id
        )
        change = UpsertIntent(
            intent.intent_id,
            intent.turn_id,
            intent.description,
            intent.priority,
            intent.intent_type,
            IntentStatus.SUCCEEDED,
        )
        outcome = D3Outcome(
            "intent",
            intent_id,
            "succeeded",
            "rule_satisfied",
            (plan_id, fact_id, action_request_id),
        )
        return self._commit(
            f"intent/{intent_id}/complete",
            "complete_d3_intent",
            (change,),
            "succeeded",
            D3CommitMetadata(outcomes=(outcome,)),
        )

    def complete_turn(self, turn_id: str, winner_id: str, fact_id: str):
        state = self.state
        turn = next(item for item in state.turns if item.turn_id == turn_id)
        changes: list[StateChange] = [
            UpsertTurn(
                turn.turn_id,
                turn.task_run_id,
                turn.sequence,
                turn.turn_target,
                turn.intent_ids,
                TurnStatus.COMPLETED,
            )
        ]
        outcomes = [
            D3Outcome(
                "turn",
                turn_id,
                "completed",
                "turn_goal_satisfied",
                (winner_id, fact_id),
            )
        ]
        for intent in state.intents:
            if intent.turn_id != turn_id or intent.status is not IntentStatus.PENDING:
                continue
            changes.append(
                UpsertIntent(
                    intent.intent_id,
                    intent.turn_id,
                    intent.description,
                    intent.priority,
                    intent.intent_type,
                    IntentStatus.SKIPPED,
                )
            )
            outcomes.append(
                D3Outcome(
                    "intent",
                    intent.intent_id,
                    "skipped",
                    "turn_goal_satisfied",
                    (winner_id, fact_id),
                )
            )
        return self._commit(
            f"turn/{turn_id}/complete",
            "complete_d3_turn",
            changes,
            "completed",
            D3CommitMetadata(outcomes=tuple(outcomes)),
        )

    def switch_sibling(self, intent_id: str, review_id: str, stop_id: str):
        state = self.state
        current = next(item for item in state.intents if item.intent_id == intent_id)
        siblings = [
            item
            for item in state.intents
            if item.turn_id == current.turn_id and item.status is IntentStatus.PENDING
        ]
        config = self.ledger.load_d3_run_config(state.task_run.task_run_id)
        if not siblings and config.contract_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}:
            raise D3RecoveryRequired("d3_no_sibling_candidate")
        next_intent = min(siblings, key=lambda item: item.priority) if siblings else None
        changes: list[StateChange] = [
            UpsertIntent(
                current.intent_id,
                current.turn_id,
                current.description,
                current.priority,
                current.intent_type,
                IntentStatus.ABANDONED,
            ),
        ]
        if next_intent is not None:
            changes.append(UpsertIntent(next_intent.intent_id, next_intent.turn_id,
                                       next_intent.description, next_intent.priority,
                                       next_intent.intent_type, IntentStatus.RUNNING))
        outcomes = [
            D3Outcome(
                "intent",
                current.intent_id,
                "abandoned",
                "strategy_abandoned",
                (review_id, stop_id),
            )
        ]
        for plan in state.plans:
            if plan.intent_id == intent_id and plan.status is PlanStatus.RUNNING:
                changes.append(
                    UpsertPlan(
                        plan.plan_id,
                        plan.intent_id,
                        plan.steps,
                        PlanStatus.SUPERSEDED,
                    )
                )
                outcomes.append(
                    D3Outcome(
                        "plan",
                        plan.plan_id,
                        "superseded",
                        "sibling_switch",
                        (review_id, stop_id),
                    )
                )
        self._commit(
            f"switch/{review_id}",
            "switch_d3_intent" if next_intent is not None else "abandon_d8_intent",
            changes,
            next_intent.intent_id if next_intent is not None else "abandoned",
            D3CommitMetadata(outcomes=tuple(outcomes)),
        )
        return next_intent.intent_id if next_intent is not None else None

    def adjust_turn(
        self,
        current_intent_id: str,
        review_id: str,
        stop_id: str,
        proposal,
        proposal_request_id: str,
        receipt,
    ) -> D3TurnBinding:
        state = self.state
        old_turn = state.turns[-1]
        current = next(
            item for item in state.intents if item.intent_id == current_intent_id
        )
        registration, binding, new_intents = self.prepare_adjust_turn(
            review_id,
            proposal,
            proposal_request_id,
            receipt,
        )
        changes: list[StateChange] = [
            UpsertTurn(
                old_turn.turn_id,
                old_turn.task_run_id,
                old_turn.sequence,
                old_turn.turn_target,
                old_turn.intent_ids,
                TurnStatus.SUPERSEDED,
            ),
            UpsertIntent(
                current.intent_id,
                current.turn_id,
                current.description,
                current.priority,
                current.intent_type,
                IntentStatus.ABANDONED,
            ),
        ]
        outcomes = [
            D3Outcome(
                "turn",
                old_turn.turn_id,
                "superseded",
                "turn_target_adjusted",
                (review_id, stop_id, proposal_request_id),
                registration.turn_id,
            ),
            D3Outcome(
                "intent",
                current.intent_id,
                "abandoned",
                "turn_target_adjusted",
                (review_id, stop_id),
            ),
        ]
        for intent in state.intents:
            if (
                intent.turn_id != old_turn.turn_id
                or intent.status is not IntentStatus.PENDING
            ):
                continue
            changes.append(
                UpsertIntent(
                    intent.intent_id,
                    intent.turn_id,
                    intent.description,
                    intent.priority,
                    intent.intent_type,
                    IntentStatus.SKIPPED,
                )
            )
            outcomes.append(
                D3Outcome(
                    "intent",
                    intent.intent_id,
                    "skipped",
                    "turn_target_adjusted",
                    (review_id, proposal_request_id),
                )
            )
        for plan in state.plans:
            if (
                plan.intent_id == current_intent_id
                and plan.status is PlanStatus.RUNNING
            ):
                changes.append(
                    UpsertPlan(
                        plan.plan_id,
                        plan.intent_id,
                        plan.steps,
                        PlanStatus.SUPERSEDED,
                    )
                )
                outcomes.append(
                    D3Outcome(
                        "plan",
                        plan.plan_id,
                        "superseded",
                        "turn_target_adjusted",
                        (review_id, stop_id),
                    )
                )
        changes.extend(registration.state_changes)
        self._commit(
            f"adjust/{review_id}",
            "advance_d3_turn",
            changes,
            registration.turn_id,
            D3CommitMetadata(
                turn_binding=binding,
                intent_bindings=new_intents,
                outcomes=tuple(outcomes),
            ),
        )
        return binding

    def prepare_adjust_turn(self, review_id, proposal, proposal_request_id, receipt):
        registration = self._registration(f"adjust/{review_id}", proposal)
        binding, new_intents = self._turn_metadata(
            registration,
            proposal,
            proposal_request_id,
            receipt,
        )
        return registration, binding, new_intents
