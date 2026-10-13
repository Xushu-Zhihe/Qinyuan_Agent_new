"""Read-only evidence gates for D3 Controller-owned mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Callable

from agent_core.actions import CompleteIntentAction, ProbeAction
from agent_core.artifact_operations import fixed_size_script, parse_size_output
from agent_core.artifact_proofs import d3_fact_content
from agent_core.conclusion_contracts import D8Conclusion
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.control_store import (
    AppendPlanValidationRecord,
    ControlMutation,
    StateChange,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertTurn,
)
from agent_core.domain import (
    ExecutionStatus,
    FactStatus,
    IntentStatus,
    PlanStatus,
    TurnStatus,
)
from agent_core.dual_llm_contracts import (
    D3PlanBinding,
    D3RecoveryRequired,
    D3ReviewApplication,
    D3TurnBinding,
    parse_d3_review_output,
)
from agent_core.dual_llm_modeling import (
    D3ActionProposal,
    D3ModelStage,
    D3OuterContext,
    D3PlanContext,
    D3PlanProposal,
    D3SharedArtifactContext,
    D3TurnProposal,
    parse_d3_action,
    parse_d3_plan_proposal,
    parse_d3_turn_proposal,
)
from agent_core.intent_postconditions import FileSha256
from agent_core.review_contracts import D2AllowedActionKinds, D2ReviewRequest
from agent_core.turn_modeling import digest, stable_id


class D3EvidenceValidator:
    def __init__(
        self,
        ledger,
        imports,
        workspace,
        commands,
        models,
        reviews,
        proofs,
        source_loader: Callable[[str], object],
    ) -> None:
        self.conclusions = None
        self.handoff = None
        self.ledger = ledger
        self.imports = imports
        self.workspace = workspace
        self.commands = commands
        self.models = models
        self.reviews = reviews
        self.proofs = proofs
        self.source_loader = source_loader

    def _known_model(self, task_run_id: str, request_id: str, stage: D3ModelStage):
        evidence = self.models.read(request_id)
        operation = self.ledger.load_d3_operation(request_id)
        config = self.ledger.load_d3_run_config(task_run_id)
        if config is None:
            raise D3RecoveryRequired("d3_run_configuration_missing")
        role = config.outer_role if stage == "outer_turn" else config.inner_role
        if (
            evidence is None
            or operation is None
            or evidence.outcome != "accepted"
            or evidence.stage != stage
            or evidence.task_run_id != task_run_id
            or evidence.role_config_ref != role.role_config_ref
            or self.models.read_rejection(request_id) is not None
            or operation.start.task_run_id != task_run_id
            or operation.start.category
            != ("outer_model" if stage == "outer_turn" else "inner_model")
            or operation.start.role != role.role
            or operation.start.role_config_ref != role.role_config_ref
            or operation.start.context_sha256 != evidence.context_sha256
            or operation.start.base_checkpoint_sequence
            != evidence.base_checkpoint_sequence
            or operation.start.resource_ref != evidence.resource_ref
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_ref != request_id
            or operation.completion.evidence_sha256 != digest(asdict(evidence))
        ):
            raise D3RecoveryRequired("d3_model_proposal_evidence_unavailable")
        if stage == "outer_turn":
            return parse_d3_turn_proposal(evidence.raw)
        if config.contract_version == 4 and operation.application_ref is None:
            self.handoff.app.revisions.validate_frozen_execution(request_id)
        if stage == "inner_plan":
            return parse_d3_plan_proposal(evidence.raw)
        return parse_d3_action(evidence.raw, stage)

    def _known_action(self, task_run_id: str, plan_id: str, stage: D3ModelStage):
        base_id = stable_id(task_run_id, f"d3/action/{plan_id}/{stage}")
        correction_id = stable_id(task_run_id, f"d3/{base_id}/correction")
        config = self.ledger.load_d3_run_config(task_run_id)
        if config is not None and config.contract_version == 4:
            operations = [r for r in self.ledger.list_d3_operations(task_run_id)
                          if r.start.resource_ref == f"{plan_id}:{stage}"
                          and (e := self.models.read(r.start.operation_id)) is not None
                          and not (isinstance(e.raw, dict) and e.raw.get("kind") == "conclusion_change")]
            applied = [r.start.operation_id for r in operations if r.application_ref is not None]
            revision = len(self.ledger.list_d8_revisions(task_run_id))
            if revision:
                base_id = stable_id(task_run_id, f"d3/action/{plan_id}/{stage}/after-revision/{revision}")
                correction_id = stable_id(task_run_id, f"d3/{base_id}/correction")
            candidates = applied or (base_id, correction_id)
        else:
            candidates = (base_id, correction_id)
        accepted = [
            request_id
            for request_id in candidates
            if (evidence := self.models.read(request_id)) is not None
            and evidence.outcome == "accepted"
            and self.models.read_rejection(request_id) is None
        ]
        if len(accepted) != 1:
            raise D3RecoveryRequired("d3_fixed_action_proposal_missing")
        request_id = accepted[0]
        proposal = self._known_model(task_run_id, request_id, stage)
        if not isinstance(proposal, D3ActionProposal):
            raise D3RecoveryRequired("d3_fixed_action_proposal_invalid")
        return request_id, proposal

    def _review_action(self, task_run_id: str, request_id: str, intent_id: str):
        if self.conclusions is not None:
            trigger = self.conclusions.review_trigger(request_id, intent_id)
            if trigger is not None:
                return trigger
        evidence = self.models.read(request_id)
        operation = self.ledger.load_d3_operation(request_id)
        if evidence is None or operation is None or ":" not in evidence.resource_ref:
            raise D3RecoveryRequired("d3_review_action_source_missing")
        plan_id, stage = evidence.resource_ref.rsplit(":", 1)
        binding = self.ledger.load_d3_plan_binding(plan_id)
        config = self.ledger.load_d3_run_config(task_run_id)
        if (
            stage not in {"inner_size", "inner_sha256", "inner_complete"}
            or evidence.stage != stage
            or evidence.model_request_id != request_id
            or evidence.task_run_id != task_run_id
            or evidence.outcome != "accepted"
            or binding is None
            or binding.intent_id != intent_id
            or binding.task_run_id != task_run_id
            or config is None
            or evidence.role_config_ref != config.inner_role.role_config_ref
            or operation.start.task_run_id != task_run_id
            or operation.start.category != "inner_model"
            or operation.start.resource_ref != evidence.resource_ref
            or operation.start.context_sha256 != evidence.context_sha256
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_sha256 != digest(asdict(evidence))
        ):
            raise D3RecoveryRequired("d3_review_action_owner_changed")
        proposal = parse_d3_action(evidence.raw, stage)
        if (
            not proposal.review_requested
            and self.models.read_rejection(request_id) is None
        ):
            raise D3RecoveryRequired("d3_action_review_not_requested")
        return stage, plan_id

    def _review_call(self, task_run_id: str, review_id: str):
        application = self.ledger.load_d3_review_application(task_run_id, review_id)
        if application is None or application.paused_gate:
            raise D3RecoveryRequired("d3_accepted_review_missing")
        root = self.reviews.read_root(application.review_root_id)
        calls = root.get("calls", ()) if isinstance(root, dict) else ()
        call = next(
            (item for item in calls if item.get("review_id") == review_id), None
        )
        if call is None or call.get("outcome") != "accepted":
            raise D3RecoveryRequired("d3_review_call_evidence_missing")
        fields = dict(call["request"])
        fields["source_versions"] = tuple(fields["source_versions"])
        request = D2ReviewRequest(**fields)
        output = parse_d3_review_output(call["response"], request)
        if (
            request.task_run_id != task_run_id
            or request.review_id != review_id
            or request.review_root_id != application.review_root_id
            or request.review_generation != application.review_generation
            or request.turn_id != application.turn_id
            or request.intent_id != application.intent_id
            or request.review_scope != application.review_scope
            or request.action_candidate_id != application.action_candidate_id
            or request.context_version != application.context_version
            or output.decision != application.decision
            or output.source_ids != application.source_ids
            or output.constraints != application.output_constraints
            or digest(asdict(output)) != application.output_sha256
        ):
            raise D3RecoveryRequired("d3_review_application_evidence_changed")
        return application, request, output

    def _stop(
        self,
        task_run_id: str,
        stop_id: str,
        review_id: str,
        intent_id: str,
        reason: str,
        state,
    ) -> None:
        receipt = self.workspace.read_stop(stop_id)
        operation = self.ledger.load_d3_operation(stop_id)
        review = self.ledger.load_d3_review_application(task_run_id, review_id)
        intent = next(
            (item for item in state.intents if item.intent_id == intent_id), None
        )
        process_refs = tuple(
            execution.execution_id
            for execution in state.executions
            if any(
                plan.plan_id == execution.plan_id and plan.intent_id == intent_id
                for plan in state.plans
            )
        )
        if (
            receipt is None
            or receipt.status != "confirmed"
            or receipt.claim.task_run_id != task_run_id
            or receipt.claim.intent_id != intent_id
            or review is None
            or intent is None
            or receipt.claim.turn_id != review.turn_id
            or receipt.claim.turn_id != intent.turn_id
            or receipt.claim.review_id != review_id
            or receipt.claim.reason != reason
            or receipt.claim.process_refs != process_refs
            or receipt.readback.unknown_refs
            or operation is None
            or operation.start.category != "intent_stop"
            or operation.start.task_run_id != task_run_id
            or operation.start.resource_ref != intent_id
            or operation.start.context_sha256 != digest(asdict(receipt.claim))
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_sha256 != digest(asdict(receipt))
            or operation.application_ref != stop_id
        ):
            raise D3RecoveryRequired("d3_intent_stop_unconfirmed")

    def _support(self, state, binding: D3PlanBinding, proof_id: str, fact_id: str):
        proof = self.proofs.proofs.read(proof_id)
        if proof is None:
            raise D3RecoveryRequired("d3_support_proof_missing")
        checked, _ = self.proofs.read_verified(proof.source_execution_id, proof_id)
        fact = next((item for item in state.facts if item.fact_id == fact_id), None)
        original = self.ledger.load_d3_plan_result(proof.plan_id)
        if (
            checked != proof
            or fact is None
            or original is None
            or original.outcome != "satisfied"
            or original.task_run_id != binding.task_run_id
            or original.artifact_version_id != proof.artifact_version_id
            or original.observation_id != proof.observation_id
            or original.proof_id != proof_id
            or original.supporting_fact_id != fact_id
            or fact.status is not FactStatus.VERIFIED
            or fact.turn_id != binding.turn_id
            or fact.source_ref != proof.source_execution_id
            or fact.verification_ref != f"d3:{proof_id}"
            or fact.content != self._fact_content(proof_id, state.task_run.task_run_id)
            or proof.task_run_id != binding.task_run_id
            or proof.turn_id != binding.turn_id
            or proof.intent_id != binding.intent_id
            or proof.artifact_version_id != binding.artifact_version_id
            or proof.rule_binding_sha256 != binding.rule_binding_sha256
            or binding.observation_mode == "new_observation"
            and proof.plan_id != binding.plan_id
            or binding.observation_mode == "reuse_saved_proof"
            and (
                binding.source_proof_id != proof_id
                or binding.source_fact_id != fact_id
                or proof.plan_id == binding.plan_id
            )
        ):
            raise D3RecoveryRequired("d3_support_binding_mismatch")
        return proof, fact

    def _turn_proposal(self, mutation: ControlMutation, state) -> None:
        metadata = mutation.d3
        if metadata is None or metadata.turn_binding is None:
            raise D3RecoveryRequired("d3_turn_proposal_binding_missing")
        self.validate_turn_proposal(
            mutation.task_run_id,
            mutation.operation,
            state,
            metadata.turn_binding,
            mutation.state_changes,
        )

    def validate_turn_proposal(
        self,
        task_run_id: str,
        operation: str,
        state,
        binding: D3TurnBinding,
        state_changes: tuple[StateChange, ...],
    ) -> None:
        proposal = self._known_model(
            task_run_id, binding.proposal_request_id, "outer_turn"
        )
        if not isinstance(proposal, D3TurnProposal):
            raise D3RecoveryRequired("d3_turn_proposal_invalid")
        new_turn = next(
            (
                item
                for item in state_changes
                if isinstance(item, UpsertTurn) and item.turn_id == binding.turn_id
            ),
            None,
        )
        children = sorted(
            (
                item
                for item in state_changes
                if isinstance(item, UpsertIntent) and item.turn_id == binding.turn_id
            ),
            key=lambda item: item.priority,
        )
        receipt = self.imports.read_receipt(binding.import_id)
        import_operation = self.ledger.load_d3_operation(binding.import_id)
        config = self.ledger.load_d3_run_config(task_run_id)
        evidence = self.models.read(binding.proposal_request_id)
        prior_turn = state.turns[-1] if state.turns else None
        prior_reason = (
            "turn_target_adjusted"
            if operation == "advance_d3_turn"
            else self.ledger.load_d3_outcome(prior_turn.turn_id).reason
            if prior_turn is not None
            and self.ledger.load_d3_outcome(prior_turn.turn_id) is not None
            else None
        )
        receipts = (
            tuple(
                self.imports.read_receipt(stable_id(task_run_id, f"d3/import/{index}"))
                for index in config.authorization.attachment_indices
            )
            if config is not None
            else ()
        )
        if (
            config is None
            or evidence is None
            or any(item is None for item in receipts)
            or binding.attachment_index not in config.authorization.attachment_indices
            or binding.import_id
            != stable_id(task_run_id, f"d3/import/{binding.attachment_index}")
            or evidence.resource_ref != f"turn:{len(state.turns) + 1}"
            or evidence.base_checkpoint_sequence != state.latest_checkpoint.sequence
            or evidence.context_sha256
            != digest(
                asdict(
                    D3OuterContext(
                        state.task_run.request.goal,
                        receipts,
                        prior_turn.turn_id if prior_turn else None,
                        prior_reason,
                    )
                )
            )
            or new_turn is None
            or new_turn.turn_target != proposal.turn_target
            or proposal.attachment_index != binding.attachment_index
            or len(children) != len(proposal.intents)
            or any(
                (child.description, child.intent_type)
                != (candidate.description, candidate.intent_type)
                for child, candidate in zip(children, proposal.intents, strict=True)
            )
            or receipt is None
            or receipt.task_run_id != task_run_id
            or receipt.sandbox_id != state.task_run.sandbox_id
            or receipt.attachment_index != binding.attachment_index
            or receipt.byte_size != binding.expected_byte_size
            or receipt.sha256 != binding.expected_sha256
            or receipt.sandbox_path
            != f"/workspace/input/d3-attachment-{binding.attachment_index}"
            or import_operation is None
            or import_operation.start.category != "attachment_import"
            or import_operation.start.task_run_id != task_run_id
            or import_operation.start.resource_ref != receipt.sandbox_path
            or import_operation.completion is None
            or import_operation.completion.result_kind != "completed"
            or import_operation.completion.evidence_sha256 != digest(asdict(receipt))
        ):
            raise D3RecoveryRequired("d3_turn_proposal_or_import_changed")

    def _plan_context_digest(self, request_id, context):
        if self.handoff is not None:
            saved = self.handoff.evidence.read("execution-context", request_id)
            if saved is None:
                raise D3RecoveryRequired("d8_execution_context_missing")
            return digest({"context": asdict(context), "process_facts": (),
                           "history_context": saved["history_context"],
                           "history_capacity_version": saved["capacity_version"]})
        return digest(asdict(context))

    def _plan_proposal(self, mutation: ControlMutation, state) -> None:
        metadata = mutation.d3
        if metadata is None or metadata.plan_binding is None:
            raise D3RecoveryRequired("d3_plan_proposal_binding_missing")
        binding = metadata.plan_binding
        proposal = self._known_model(
            mutation.task_run_id, binding.proposal_request_id, "inner_plan"
        )
        turn = self.ledger.load_d3_turn_binding(binding.turn_id)
        prior = [item for item in state.plans if item.intent_id == binding.intent_id]
        intent = next(
            (item for item in state.intents if item.intent_id == binding.intent_id),
            None,
        )
        registered_turn = next(
            (item for item in state.turns if item.turn_id == binding.turn_id), None
        )
        evidence = self.models.read(binding.proposal_request_id)
        reusable = tuple(
            result.proof_id
            for item in prior
            if (result := self.ledger.load_d3_plan_result(item.plan_id)) is not None
            and result.outcome == "satisfied"
        )
        shared = tuple(
            D3SharedArtifactContext(
                receipt.claim.share_id,
                receipt.claim.artifact_version_id,
                receipt.claim.source_turn_id,
                receipt.claim.source_intent_id,
                receipt.claim.target_path,
                receipt.claim.source_sha256,
                receipt.claim.original_verification_status,
            )
            for receipt in self.workspace.list_shares(mutation.task_run_id)
            if receipt.claim.target_intent_id == binding.intent_id
        )
        if (
            not isinstance(proposal, D3PlanProposal)
            or turn is None
            or intent is None
            or intent.turn_id != binding.turn_id
            or intent.status is not IntentStatus.RUNNING
            or registered_turn is None
            or registered_turn.status is not TurnStatus.RUNNING
            or evidence is None
            or evidence.resource_ref != f"plan:{binding.intent_id}:{len(prior) + 1}"
            or evidence.base_checkpoint_sequence != state.latest_checkpoint.sequence
            or evidence.context_sha256
            != self._plan_context_digest(binding.proposal_request_id,
                    D3PlanContext(
                        registered_turn.turn_target,
                        turn,
                        binding.intent_id,
                        tuple(item.plan_id for item in prior),
                        reusable,
                        shared,
                    )
            )
            or binding.task_run_id != mutation.task_run_id
            or binding.rule_binding_sha256 != digest(asdict(turn))
            or proposal.template != binding.template
            or proposal.observation_mode != binding.observation_mode
            or proposal.source_proof_id != binding.source_proof_id
            or (not prior and binding.template != "size_then_sha256")
            or (prior and binding.template != "sha256_only")
            or (not prior and binding.replan_review_id is not None)
            or (prior and binding.replan_review_id is None)
        ):
            raise D3RecoveryRequired("d3_plan_proposal_owner_mismatch")
        if prior:
            review_id = binding.replan_review_id
            if review_id is None:
                raise D3RecoveryRequired("d3_replan_review_unavailable")
            self._review_call(mutation.task_run_id, review_id)
            review = self.ledger.load_d3_review_application(
                mutation.task_run_id, review_id
            )
            if (
                review is None
                or review.decision != "replan_current_intent"
                or review.intent_id != binding.intent_id
                or review.review_scope != "action"
                or prior[-1].status not in {PlanStatus.RUNNING, PlanStatus.COMPLETED}
            ):
                raise D3RecoveryRequired("d3_replan_review_unavailable")
            review_stage, review_plan_id = self._review_action(
                mutation.task_run_id, review.action_candidate_id, binding.intent_id
            )
            if review_plan_id != prior[-1].plan_id:
                raise D3RecoveryRequired("d3_replan_review_plan_changed")
            if prior[-1].status is PlanStatus.RUNNING and review_stage != "fact_proposal":
                size_execution = next(
                    (
                        item
                        for item in state.executions
                        if item.plan_id == prior[-1].plan_id
                        and item.step_index == 0
                        and item.status is ExecutionStatus.SUCCEEDED
                    ),
                    None,
                )
                prior_binding = self.ledger.load_d3_plan_binding(prior[-1].plan_id)
                completed = (
                    self.commands.read_completed(size_execution.execution_id)
                    if size_execution
                    else None
                )
                size = (
                    self.workspace.read_size(size_execution.execution_id)
                    if size_execution
                    else None
                )
                observed_status, observed_size = (
                    parse_size_output(completed.record.stdout)
                    if completed
                    else (None, None)
                )
                if (
                    size_execution is None
                    or prior_binding is None
                    or completed is None
                    or size is None
                    or review_stage != "inner_size"
                    or size.task_run_id != mutation.task_run_id
                    or size.plan_id != prior[-1].plan_id
                    or size.artifact_version_id != prior_binding.artifact_version_id
                    or size.execution_id != size_execution.execution_id
                    or size.expected_byte_size != turn.expected_byte_size
                    or size.observed_status != observed_status
                    or size.observed_byte_size != observed_size
                    or size.output_sha256
                    != hashlib.sha256(completed.record.stdout).hexdigest()
                    or size.status != "confirmed"
                    or any(
                        item.plan_id == prior[-1].plan_id and item.step_index == 1
                        for item in state.executions
                    )
                ):
                    raise D3RecoveryRequired("d3_replan_first_step_not_saved")
            elif prior[-1].status is PlanStatus.COMPLETED and review_stage not in {"inner_sha256", "inner_complete"}:
                raise D3RecoveryRequired("d3_replan_final_step_review_missing")
        if binding.observation_mode == "reuse_saved_proof":
            if binding.source_proof_id is None or binding.source_fact_id is None:
                raise D3RecoveryRequired("d3_reused_support_missing")
            self._support(
                state, binding, binding.source_proof_id, binding.source_fact_id
            )
        elif any(
            self.ledger.load_d3_plan_binding(item.plan_id) is not None
            and self.ledger.load_d3_plan_binding(item.plan_id).artifact_version_id
            == binding.artifact_version_id
            for item in state.plans
        ):
            raise D3RecoveryRequired("d3_new_plan_reuses_artifact_version")

    def _fixed_action(self, state, binding: D3PlanBinding, step_index: int):
        turn = self.ledger.load_d3_turn_binding(binding.turn_id)
        artifact = self.workspace.read_artifact(binding.artifact_version_id)
        if turn is None or artifact is None or artifact.status != "confirmed":
            raise D3RecoveryRequired("d3_fixed_action_artifact_missing")
        path = artifact.claim.path
        sha_step = 1 if binding.template == "size_then_sha256" else 0
        stage: D3ModelStage = (
            "inner_size"
            if binding.template == "size_then_sha256" and step_index == 0
            else "inner_sha256"
        )
        expected_script = (
            fixed_size_script(path)
            if stage == "inner_size"
            else fixed_digest_script(path)
        )
        expected_conditions = (
            ()
            if stage == "inner_size"
            else (FileSha256(sha_step, path, turn.expected_sha256),)
        )
        request_id, proposal = self._known_action(
            binding.task_run_id, binding.plan_id, stage
        )
        action = proposal.action
        if (
            not isinstance(action, ProbeAction)
            or action.script != expected_script
            or action.stdin != b""
            or action.working_directory != path.rsplit("/", 1)[0]
            or action.conditions != expected_conditions
            or stage == "inner_sha256"
            and step_index != sha_step
        ):
            raise D3RecoveryRequired("d3_fixed_action_changed")
        return request_id, action

    def _allowed(
        self,
        task_run_id: str,
        turn_id: str,
        intent_id: str,
        kind: str,
        timeout: int | None,
    ) -> None:
        allowed = {"probe", "complete_intent"}
        maximum = 300
        for application in self.ledger.list_d3_review_applications(task_run_id):
            if application.paused_gate:
                raise D3RecoveryRequired(application.pause_reason or "d3_review_paused")
            for constraint in application.output_constraints:
                if not (
                    constraint.scope_kind == "turn"
                    and constraint.scope_id == turn_id
                    or constraint.scope_kind == "intent"
                    and constraint.scope_id == intent_id
                ):
                    continue
                if isinstance(constraint, D2AllowedActionKinds):
                    allowed &= set(constraint.allowed_action_kinds)
                else:
                    maximum = min(maximum, constraint.max_action_timeout_seconds)
        if (
            kind not in allowed
            or kind == "probe"
            and (timeout is None or timeout > maximum)
        ):
            raise D3RecoveryRequired("d3_action_rejected_by_review_constraint")

    def _tool(self, mutation: ControlMutation, state) -> None:
        change = mutation.state_changes[0]
        if isinstance(change, AppendPlanValidationRecord):
            plan_id = change.record.plan_id
            step_index = change.record.step_index
            command_ref = change.record.command_ref
            command_evidence = (
                self.commands.read_command(command_ref) if command_ref else None
            )
            request_id = command_evidence.request_id if command_evidence else None
        elif isinstance(change, UpsertExecution):
            plan_id = change.plan_id
            step_index = change.step_index
            command_ref = change.command_ref
            request_id = change.execution_id
        else:
            raise D3RecoveryRequired("d3_tool_change_invalid")
        binding = self.ledger.load_d3_plan_binding(plan_id)
        if binding is None or step_index is None or request_id is None:
            raise D3RecoveryRequired("d3_tool_plan_missing")
        expected_id = stable_id(mutation.task_run_id, f"d3/tool/{plan_id}/{step_index}")
        operation = self.ledger.load_d3_operation(request_id)
        command = self.commands.read_command(command_ref) if command_ref else None
        config = self.ledger.load_d3_run_config(mutation.task_run_id)
        _, action = self._fixed_action(state, binding, step_index)
        if (
            request_id != expected_id
            or operation is None
            or config is None
            or operation.start.category != "tool_dispatch"
            or operation.start.task_run_id != mutation.task_run_id
            or command is None
            or command.request_id != request_id
            or command.task_run_id != mutation.task_run_id
            or command.sandbox_id != state.task_run.sandbox_id
            or command.authorization_id != config.authorization.authorization_id
            or command.argv != ("bash", "-c", action.script)
            or command.working_directory != action.working_directory
        ):
            raise D3RecoveryRequired("d3_tool_declaration_unavailable")
        if mutation.operation in {
            "append_plan_validation_record",
            "create_execution",
            "start_execution",
        }:
            self._allowed(
                mutation.task_run_id,
                binding.turn_id,
                binding.intent_id,
                "probe",
                action.timeout_seconds,
            )
        if mutation.operation in {"complete_execution", "finish_d3_execution"}:
            completed = self.commands.read_completed(request_id)
            if (
                completed is None
                or operation.completion is None
                or operation.completion.evidence_ref != request_id
                or mutation.operation == "complete_execution"
                and (
                    completed.record.status != "completed"
                    or completed.record.exit_code != 0
                )
                or mutation.operation == "finish_d3_execution"
                and (
                    completed.record.status == "completed"
                    and completed.record.exit_code == 0
                )
            ):
                raise D3RecoveryRequired("d3_tool_result_unconfirmed")

    def _complete_plan(self, mutation: ControlMutation, state) -> None:
        result = mutation.d3.plan_result if mutation.d3 is not None else None
        binding = self.ledger.load_d3_plan_binding(result.plan_id) if result else None
        if result is None or binding is None or result.outcome != "satisfied":
            raise D3RecoveryRequired("d3_fixed_rule_has_no_satisfied_result")
        if result.proof_id is None or result.supporting_fact_id is None:
            raise D3RecoveryRequired("d3_plan_support_missing")
        changes = [
            item for item in mutation.state_changes if isinstance(item, UpsertFact)
        ]
        if binding.observation_mode == "new_observation":
            proof = self.proofs.proofs.read(result.proof_id)
            if proof is None:
                raise D3RecoveryRequired("d3_new_proof_missing")
            checked, _ = self.proofs.read_verified(
                proof.source_execution_id, proof.proof_id
            )
            if (
                checked != proof
                or len(changes) != 1
                or changes[0].fact_id != result.supporting_fact_id
                or changes[0].source_ref != proof.source_execution_id
                or changes[0].verification_ref != f"d3:{proof.proof_id}"
                or changes[0].content != self._fact_content(proof.proof_id, mutation.task_run_id)
                or proof.plan_id != binding.plan_id
                or proof.artifact_version_id != binding.artifact_version_id
                or proof.observation_id != result.observation_id
            ):
                raise D3RecoveryRequired("d3_new_plan_proof_mismatch")
            receipt = self.workspace.read_digest(result.observation_id)
            if receipt is None or result.evidence_sha256 != digest(asdict(receipt)):
                raise D3RecoveryRequired("d3_new_observation_receipt_mismatch")
        else:
            proof, _ = self._support(
                state, binding, result.proof_id, result.supporting_fact_id
            )
            original = self.ledger.load_d3_plan_result(proof.plan_id)
            if (
                changes
                or proof.observation_id != result.observation_id
                or original is None
                or original.outcome != "satisfied"
                or original.supporting_fact_id != result.supporting_fact_id
                or result.evidence_sha256 != original.evidence_sha256
                or any(item.plan_id == binding.plan_id for item in state.executions)
            ):
                raise D3RecoveryRequired("d3_reused_support_changed")

    def _complete_intent(self, mutation: ControlMutation, state) -> None:
        change = next(
            (item for item in mutation.state_changes if isinstance(item, UpsertIntent)),
            None,
        )
        if change is None or change.status is not IntentStatus.SUCCEEDED:
            raise D3RecoveryRequired("d3_intent_completion_change_invalid")
        plans = [item for item in state.plans if item.intent_id == change.intent_id]
        if not plans:
            raise D3RecoveryRequired("d3_intent_has_no_plan")
        binding = self.ledger.load_d3_plan_binding(plans[-1].plan_id)
        result = self.ledger.load_d3_plan_result(plans[-1].plan_id)
        if binding is None or result is None or result.outcome != "satisfied":
            raise D3RecoveryRequired("d3_latest_plan_support_missing")
        if result.proof_id is None or result.supporting_fact_id is None:
            raise D3RecoveryRequired("d3_latest_plan_support_missing")
        self._support(state, binding, result.proof_id, result.supporting_fact_id)
        request_id, proposal = self._known_action(
            mutation.task_run_id, binding.plan_id, "inner_complete"
        )
        if (
            not isinstance(proposal.action, CompleteIntentAction)
            or proposal.action.fact_ids != (result.supporting_fact_id,)
            or mutation.d3 is None
            or len(mutation.d3.outcomes) != 1
            or mutation.d3.outcomes[0].evidence_refs
            != (binding.plan_id, result.supporting_fact_id, request_id)
        ):
            raise D3RecoveryRequired("d3_complete_action_or_support_changed")
        self._allowed(
            mutation.task_run_id,
            binding.turn_id,
            binding.intent_id,
            "complete_intent",
            None,
        )
        if proposal.review_requested and not any(
            item.action_candidate_id == request_id
            and item.decision == "continue_current_intent"
            and not item.paused_gate
            for item in self.ledger.list_d3_review_applications(mutation.task_run_id)
        ):
            raise D3RecoveryRequired("d3_complete_action_review_missing")

    def _complete_turn(self, mutation: ControlMutation, state) -> None:
        change = next(
            (item for item in mutation.state_changes if isinstance(item, UpsertTurn)),
            None,
        )
        if change is None or change.status is not TurnStatus.COMPLETED:
            raise D3RecoveryRequired("d3_turn_completion_change_invalid")
        winners = [
            item
            for item in state.intents
            if item.turn_id == change.turn_id and item.status is IntentStatus.SUCCEEDED
        ]
        if len(winners) != 1:
            raise D3RecoveryRequired("d3_turn_has_no_single_verified_winner")
        plans = [item for item in state.plans if item.intent_id == winners[0].intent_id]
        binding = self.ledger.load_d3_plan_binding(plans[-1].plan_id) if plans else None
        result = self.ledger.load_d3_plan_result(plans[-1].plan_id) if plans else None
        if binding is None or result is None or result.outcome != "satisfied":
            raise D3RecoveryRequired("d3_winning_plan_support_missing")
        self._support(state, binding, result.proof_id, result.supporting_fact_id)
        if mutation.d3 is None or not any(
            item.entity_kind == "turn"
            and item.entity_id == change.turn_id
            and item.reason == "turn_goal_satisfied"
            and item.evidence_refs == (winners[0].intent_id, result.supporting_fact_id)
            for item in mutation.d3.outcomes
        ):
            raise D3RecoveryRequired("d3_turn_completion_reason_missing")
        for sibling in state.intents:
            if (
                sibling.turn_id != change.turn_id
                or sibling.status is not IntentStatus.PENDING
            ):
                continue
            skipped = next(
                (
                    item
                    for item in mutation.state_changes
                    if isinstance(item, UpsertIntent)
                    and item.intent_id == sibling.intent_id
                ),
                None,
            )
            outcome = next(
                (
                    item
                    for item in mutation.d3.outcomes
                    if item.entity_kind == "intent"
                    and item.entity_id == sibling.intent_id
                ),
                None,
            )
            if (
                skipped is None
                or skipped.status is not IntentStatus.SKIPPED
                or outcome is None
                or outcome.reason != "turn_goal_satisfied"
                or outcome.evidence_refs
                != (winners[0].intent_id, result.supporting_fact_id)
            ):
                raise D3RecoveryRequired("d3_untried_sibling_not_atomically_skipped")

    def _review_application(self, mutation: ControlMutation, state) -> None:
        application = mutation.state_changes[0]
        if not isinstance(application, D3ReviewApplication):
            raise D3RecoveryRequired("d3_review_application_missing")
        if application.paused_gate:
            if self.reviews.read_root(application.review_root_id) is None:
                raise D3RecoveryRequired("d3_review_pause_without_root")
            return
        root = self.reviews.read_root(application.review_root_id)
        calls = root.get("calls", ()) if isinstance(root, dict) else ()
        call = next(
            (item for item in calls if item.get("review_id") == application.review_id),
            None,
        )
        if call is None or call.get("outcome") != "accepted":
            raise D3RecoveryRequired("d3_review_output_not_saved")
        request_fields = dict(call["request"])
        request_fields["source_versions"] = tuple(request_fields["source_versions"])
        request = D2ReviewRequest(**request_fields)
        output = parse_d3_review_output(call["response"], request)
        turn = next(
            (item for item in state.turns if item.turn_id == application.turn_id), None
        )
        intent = next(
            (item for item in state.intents if item.intent_id == application.intent_id),
            None,
        )
        if (
            turn is None
            or intent is None
            or intent.turn_id != turn.turn_id
            or request.review_id != application.review_id
            or request.review_root_id != application.review_root_id
            or request.review_generation != application.review_generation
            or request.task_run_id != mutation.task_run_id
            or request.turn_id != application.turn_id
            or request.intent_id != application.intent_id
            or request.review_scope != application.review_scope
            or request.action_candidate_id != application.action_candidate_id
            or request.context_version != application.context_version
            or request.checkpoint_id != state.latest_checkpoint.sequence
            or output.decision != application.decision
            or output.source_ids != application.source_ids
            or output.constraints != application.output_constraints
            or digest(asdict(output)) != application.output_sha256
            or any(item not in request.source_versions for item in output.source_ids)
        ):
            raise D3RecoveryRequired("d3_review_application_context_changed")
        summary = json.loads(request.context_summary)
        versions = summary.get("source_versions", {})
        if not isinstance(versions, dict) or any(
            versions.get(source_id) != digest(self.source_loader(source_id))
            for source_id in request.source_versions
        ):
            raise D3RecoveryRequired("d3_review_source_changed")
        if any(
            item.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}
            for item in state.executions
        ):
            raise D3RecoveryRequired("d3_review_has_pending_execution")
        if request.review_scope == "turn_end":
            if turn.status not in {
                TurnStatus.COMPLETED,
                TurnStatus.FAILED,
                TurnStatus.INCONCLUSIVE,
                TurnStatus.SUPERSEDED,
            }:
                raise D3RecoveryRequired("d3_tail_review_before_turn_end")
            return
        if (
            turn.status is not TurnStatus.RUNNING
            or intent.status is not IntentStatus.RUNNING
        ):
            raise D3RecoveryRequired("d3_review_requires_active_intent")
        if request.review_scope == "turn_start" and any(
            item.intent_id == intent.intent_id for item in state.plans
        ):
            raise D3RecoveryRequired("d3_start_review_after_plan")
        if request.review_scope == "action":
            if request.action_candidate_id not in request.source_versions:
                raise D3RecoveryRequired("d3_action_review_source_missing")
            self._review_action(
                mutation.task_run_id, request.action_candidate_id, intent.intent_id
            )
        if output.decision == "switch_sibling_intent" and not any(
            item.turn_id == turn.turn_id and item.status is IntentStatus.PENDING
            for item in state.intents
        ):
            raise ValueError("d3_no_sibling_candidate")
        if output.decision == "replan_current_intent":
            plans = [item for item in state.plans if item.intent_id == intent.intent_id]
            if not plans or len(plans) >= 2:
                raise ValueError("d3_replan_not_available")
        if output.decision == "adjust_turn_target" and len(state.turns) >= 2:
            raise ValueError("d3_turn_limit_reached")

    def _fact_content(self, proof_id, task_run_id):
        config = self.ledger.load_d3_run_config(task_run_id)
        if config is not None and config.d8_enabled:
            if self.conclusions is None:
                raise D3RecoveryRequired("d8_validator_not_configured")
            return self.conclusions.observation(proof_id).fact_content()
        return d3_fact_content(proof_id)

    def __call__(self, mutation: ControlMutation, state) -> None:
        from agent_core.handoff_storage import D8_OPERATIONS
        if mutation.operation in D8_OPERATIONS:
            if self.handoff is None:
                raise D3RecoveryRequired("d8_handoff_validator_not_configured")
            self.handoff.validate_mutation(mutation, state)
            return
        if (self.handoff is not None and self.handoff.app.revisions is not None
                and mutation.operation in {"register_d3_plan", "replan_d3_plan", "complete_d3_plan",
                                           "complete_d3_intent", "complete_d3_turn"}):
            self.handoff.app.revisions.check_required()
        config = self.ledger.load_d3_run_config(mutation.task_run_id)
        records = [c for c in mutation.state_changes if isinstance(c, D8Conclusion)]
        if config is not None and config.d8_enabled:
            if self.conclusions is None:
                raise D3RecoveryRequired("d8_validator_not_configured")
            for record in records:
                self.conclusions.validate_record(record, state)
            if any(isinstance(c, UpsertFact) for c in mutation.state_changes) and len(records) != 1:
                raise D3RecoveryRequired("d8_fact_approval_missing")
        elif records:
            raise D3RecoveryRequired("d8_not_enabled")
        operation = mutation.operation
        if operation in {"register_d3_turn", "advance_d3_turn"}:
            self._turn_proposal(mutation, state)
        if operation in {"register_d3_plan", "replan_d3_plan"}:
            self._plan_proposal(mutation, state)
        if operation in {
            "append_plan_validation_record",
            "create_execution",
            "start_execution",
            "complete_execution",
            "finish_d3_execution",
        }:
            self._tool(mutation, state)
        elif operation == "apply_d3_review":
            self._review_application(mutation, state)
        elif operation == "complete_d3_plan":
            self._complete_plan(mutation, state)
        elif operation == "complete_d3_intent":
            self._complete_intent(mutation, state)
        elif operation == "complete_d3_turn":
            self._complete_turn(mutation, state)
        elif operation == "switch_d3_intent":
            outcome = (
                mutation.d3.outcomes[0]
                if mutation.d3 and mutation.d3.outcomes
                else None
            )
            if outcome is None or outcome.reason != "strategy_abandoned":
                raise D3RecoveryRequired("d3_strategy_abandonment_reason_missing")
            review_id, stop_id = outcome.evidence_refs
            review, _, _ = self._review_call(mutation.task_run_id, review_id)
            if (
                review.decision != "switch_sibling_intent"
                or review.intent_id != outcome.entity_id
            ):
                raise D3RecoveryRequired("d3_switch_review_changed")
            self._stop(
                mutation.task_run_id,
                stop_id,
                review_id,
                outcome.entity_id,
                "sibling_switch",
                state,
            )
        elif operation == "advance_d3_turn":
            metadata = mutation.d3
            if metadata is None:
                raise D3RecoveryRequired("d3_adjustment_metadata_missing")
            outcome = next(
                (item for item in metadata.outcomes if item.entity_kind == "turn"), None
            )
            if outcome is None or outcome.reason != "turn_target_adjusted":
                raise D3RecoveryRequired("d3_adjustment_reason_missing")
            review_id, stop_id, _ = outcome.evidence_refs
            review, _, _ = self._review_call(mutation.task_run_id, review_id)
            if review.decision != "adjust_turn_target":
                raise D3RecoveryRequired("d3_adjustment_review_changed")
            self._stop(
                mutation.task_run_id,
                stop_id,
                review_id,
                review.intent_id,
                "turn_target_adjusted",
                state,
            )
        elif operation not in {
            "register_d3_turn",
            "select_d3_intent",
            "register_d3_plan",
            "replan_d3_plan",
            "append_d8_conclusion",
        }:
            raise D3RecoveryRequired("d3_unvalidated_transition")
