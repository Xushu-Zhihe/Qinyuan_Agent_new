from __future__ import annotations

from dataclasses import dataclass, replace

from agent_core.control_store import (
    ControlMutation,
    UpsertFact,
    ControlCommitResult,
    ControlStateRows,
    ControlStateSnapshot,
    ControlStore,
    StorageError,
    TaskRunNotFoundError,
    TaskRunSnapshot,
    UpsertTurn,
)
from agent_core.control_candidate import project_candidate, validate_control_ownership, validate_running_transition
from agent_core.domain import (
    EXECUTION_TERMINAL_STATUSES,
    ExecutionEvidence,
    ExecutionEvidencePort,
    ExecutionStatus,
    FailureSummary,
    FailureType,
    FactStatus,
    IntentStatus,
    PlanStatus,
    PlanValidationStatus,
    TurnStatus,
)
from agent_core.modeling import FactSnapshot
from agent_core.file_type_fact import fact_content
from agent_core.file_type_verifier import (
    FileTypeProofEvidence,
    FileTypeProofEvidencePort,
)
from agent_core.observation_proof import (
    T6ObservationProofEvidencePort,
    observation_fact_content,
)
from agent_core.artifact_proofs import D3ObservationProofVerifier, d3_fact_content
from agent_core.conclusion_contracts import D8Conclusion, d8_fact_content


MAX_CONTEXT_FACTS = 64
MAX_CONTEXT_FACT_CONTENT = 16_384
MAX_CONTEXT_FAILURES = 32


@dataclass(frozen=True, slots=True)
class ModelContextSnapshot:
    facts: tuple[FactSnapshot, ...]
    failure_summaries: tuple[FailureSummary, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: ControlStateSnapshot
    commit: ControlCommitResult | None


class ControlStateRepository:
    """Validate opaque execution references before exposing control snapshots."""

    def __init__(
        self,
        store: ControlStore,
        execution_evidence: ExecutionEvidencePort,
        file_type_proofs: FileTypeProofEvidencePort | None = None,
        t6_observation_proofs: T6ObservationProofEvidencePort | None = None,
        d3_observation_proofs: D3ObservationProofVerifier | None = None,
    ) -> None:
        self._store = store
        self._execution_evidence = execution_evidence
        self._file_type_proofs = file_type_proofs
        self._t6_observation_proofs = t6_observation_proofs
        self._d3_observation_proofs = d3_observation_proofs

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        return self._store.load_task_run(task_run_id)

    def _file_type_proof(
        self, execution_id: str, proof_id: str
    ) -> FileTypeProofEvidence:
        if self._file_type_proofs is None:
            raise StorageError("file-type proof evidence is not configured")
        try:
            return self._file_type_proofs.lookup_file_type_proof(
                execution_id, proof_id
            )
        except Exception as exc:
            raise StorageError("file-type proof evidence is unavailable") from exc

    def _verified_file_type_evidence(
        self, execution_id: str, proof_id: str
    ) -> ExecutionEvidence:
        return self._file_type_proof(execution_id, proof_id).execution

    def validate_candidate(self, state: ControlStateSnapshot, mutation: ControlMutation) -> None:
        """Recheck the resulting candidate, without writing or retaining evidence."""
        try:
            rows = project_candidate(state, mutation)
            old_turns = {item.turn_id: item for item in state.turns}
            old_intents = {item.intent_id: item for item in state.intents}
            changes_by_turn = {
                item.turn_id: item
                for item in mutation.state_changes
                if isinstance(item, UpsertTurn)
            }
            normalized_turns = []
            for turn in rows.turns:
                old = old_turns.get(turn.turn_id)
                change = changes_by_turn.get(turn.turn_id)
                if old is not None:
                    if change is not None and (
                        change.parent_turn_id is not None
                        or change.triggering_intent_id is not None
                    ) and (
                        change.parent_turn_id,
                        change.triggering_intent_id,
                    ) != (old.parent_turn_id, old.triggering_intent_id):
                        raise StorageError("Turn parent metadata is immutable")
                    # Older transition call sites update Turn status without
                    # repeating its immutable parent declaration.
                    turn = replace(
                        turn,
                        parent_turn_id=old.parent_turn_id,
                        triggering_intent_id=old.triggering_intent_id,
                    )
                else:
                    if change is not None:
                        turn = replace(
                            turn,
                            parent_turn_id=change.parent_turn_id,
                            triggering_intent_id=change.triggering_intent_id,
                        )
                    if turn.parent_turn_id is not None:
                        parent = old_turns.get(turn.parent_turn_id)
                        trigger = old_intents.get(turn.triggering_intent_id)
                        if parent is None or parent.status is not TurnStatus.RUNNING:
                            raise StorageError(
                                "child Turn requires an existing running parent Turn"
                            )
                        if (
                            trigger is None
                            or trigger.turn_id != parent.turn_id
                            or trigger.status is not IntentStatus.SUCCEEDED
                        ):
                            raise StorageError(
                                "child Turn requires its already-succeeded parent Intent"
                            )
                normalized_turns.append(turn)
            rows = replace(rows, turns=tuple(normalized_turns))
            self._validate_rows(rows, tuple(c for c in mutation.state_changes if isinstance(c, D8Conclusion)))
            validate_running_transition(state, rows, mutation)
            executions = {item.execution_id: item for item in rows.executions}
            targets = {item.url for item in rows.task_run.request.target.endpoints} | {
                item.value for item in rows.task_run.request.target.attachments
            }
            # Fact creation/verification also consumes Execution evidence, not
            # just the opaque command/output references checked by _validate_rows.
            for change in mutation.state_changes:
                if not isinstance(change, UpsertFact):
                    continue
                if (change.verification_ref or "").startswith("d8e:"):
                    manager=getattr(self,"_evidence_conclusions",None)
                    if manager is None:raise StorageError("Reviewed evidence port not configured")
                    manager.validate_fact(change,rows,tuple(c for c in mutation.state_changes if isinstance(c,D8Conclusion)))
                    continue
                execution = executions[change.source_ref]
                if execution.status not in EXECUTION_TERMINAL_STATUSES:
                    raise StorageError("Fact source Execution must be terminal")
                evidence = (
                    self._verified_t6_evidence(
                        change.source_ref, change.verification_ref.removeprefix("t6:")
                    ) if change.verification_ref is not None
                    and change.verification_ref.startswith("t6:")
                    else self._verified_d3_evidence(
                        change.source_ref, change.verification_ref.removeprefix("d3:")
                    ) if change.verification_ref is not None
                    and change.verification_ref.startswith("d3:")
                    else self._verified_file_type_evidence(
                        change.source_ref, change.verification_ref
                    )
                    if change.verification_ref is not None
                    else self._execution_evidence.lookup(change.source_ref)
                )
                if evidence is None:
                    raise StorageError("Fact source Execution evidence is unavailable")
                if change.status is FactStatus.VERIFIED and (
                    evidence.task_run_id != rows.task_run.task_run_id
                    or (evidence.target_ref not in targets
                        and not (change.verification_ref or "").startswith(("t6:", "d3:")))
                    or not rows.task_run.started_at <= evidence.observed_at <= rows.task_run.deadline_at
                ):
                    raise StorageError("verified Fact evidence is outside its TaskRun scope or time range")
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"candidate validation failed before commit: {exc}") from exc

    def _verified_t6_evidence(
        self, execution_id: str, proof_id: str
    ) -> ExecutionEvidence:
        if self._t6_observation_proofs is None:
            raise StorageError("T6 observation proof evidence is not configured")
        try:
            return self._t6_observation_proofs.lookup(execution_id, proof_id)
        except Exception as exc:
            raise StorageError("T6 observation proof evidence is unavailable") from exc

    def _verified_d3_evidence(
        self, execution_id: str, proof_id: str
    ) -> ExecutionEvidence:
        if self._d3_observation_proofs is None:
            raise StorageError("D3 observation proof evidence is not configured")
        try:
            return self._d3_observation_proofs.read_verified(execution_id, proof_id)[1]
        except Exception as exc:
            raise StorageError("D3 observation proof evidence is unavailable") from exc

    def load_control_state(
        self,
        task_run_id: str,
    ) -> ControlStateSnapshot | None:
        from qinyuan_core.logging import timed_phase
        with timed_phase("store", "state_read", run=task_run_id):
            rows = self._store.load_control_state_rows(task_run_id)
        if rows is None:
            return None
        try:
            with timed_phase("store", "state_and_evidence_validation", run=task_run_id):
                self._validate_rows(rows)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"persisted control state is invalid: {exc}") from exc
        return ControlStateSnapshot(
            task_run=rows.task_run,
            latest_checkpoint=rows.latest_checkpoint,
            turns=rows.turns,
            intents=rows.intents,
            plans=rows.plans,
            executions=rows.executions,
            facts=rows.facts,
            failure_summaries=rows.failure_summaries,
            plan_validation_records=rows.plan_validation_records,
        )

    def query_model_context(self, task_run_id: str) -> ModelContextSnapshot:
        state = self.load_control_state(task_run_id)
        if state is None:
            raise TaskRunNotFoundError(f"TaskRun does not exist: {task_run_id}")

        selected_facts: list[FactSnapshot] = []
        content_length = 0
        for fact in reversed(state.facts):
            if fact.status is not FactStatus.VERIFIED:
                continue
            if len(selected_facts) >= MAX_CONTEXT_FACTS:
                break
            next_length = content_length + len(fact.content)
            if next_length > MAX_CONTEXT_FACT_CONTENT:
                break
            selected_facts.append(
                FactSnapshot(
                    fact_id=fact.fact_id,
                    content=fact.content,
                    source_ref=fact.source_ref,
                )
            )
            content_length = next_length
        selected_facts.reverse()

        selected_failures = [
            item
            for item in reversed(state.failure_summaries)
            if item.failure_type is FailureType.TARGET_MISMATCH
        ][:MAX_CONTEXT_FAILURES]
        selected_failures.reverse()
        return ModelContextSnapshot(
            facts=tuple(selected_facts),
            failure_summaries=tuple(item.to_domain() for item in selected_failures),
        )

    def _validate_rows(self, rows: ControlStateRows, d8_pending: tuple[D8Conclusion, ...] = ()) -> None:
        validate_control_ownership(rows)
        task_run_id = rows.task_run.task_run_id
        turns = {item.turn_id: item for item in rows.turns}
        intents = {item.intent_id: item for item in rows.intents}
        plans = {item.plan_id: item for item in rows.plans}
        executions = {item.execution_id: item for item in rows.executions}
        validations = {
            item.plan_validation_id: item
            for item in rows.plan_validation_records
        }

        if len(turns) != len(rows.turns):
            raise StorageError("control state contains duplicate Turn IDs")
        if len(intents) != len(rows.intents):
            raise StorageError("control state contains duplicate Intent IDs")
        if len(plans) != len(rows.plans):
            raise StorageError("control state contains duplicate Plan IDs")
        if len(executions) != len(rows.executions):
            raise StorageError("control state contains duplicate Execution IDs")
        if len(validations) != len(rows.plan_validation_records):
            raise StorageError("control state contains duplicate PlanValidationRecord IDs")

        running_turns = [
            item.turn_id for item in rows.turns if item.status is TurnStatus.RUNNING
        ]
        if len(running_turns) > 1:
            config = self._store.load_d3_run_config(rows.task_run.task_run_id)
            if config is None or config.contract_version not in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                raise StorageError("control state contains multiple running Turns")
            if config.intent_execution_policy == "continuous_v1":
                # A parked old root is retained for audit; it has no dispatchable Intent.
                active = {i.turn_id for i in rows.intents if i.status in {IntentStatus.RUNNING, IntentStatus.PENDING}}
                running_turns = [i for i in running_turns if i in active]
            leaf = max((turns[i] for i in running_turns), key=lambda t: t.sequence, default=None)
            chain = set()
            while leaf is not None and leaf.turn_id not in chain:
                chain.add(leaf.turn_id)
                leaf = turns.get(leaf.parent_turn_id) if leaf.parent_turn_id else None
            if set(running_turns) - chain:
                raise StorageError("D8-2 running Turns must form one ancestor chain")
        running_executions = [
            item.execution_id
            for item in rows.executions
            if item.status is ExecutionStatus.RUNNING
        ]
        if len(running_executions) > 1:
            raise StorageError("control state contains multiple running Executions")

        intents_by_turn: dict[str, list[str]] = {item.turn_id: [] for item in rows.turns}
        for intent in rows.intents:
            if intent.turn_id not in turns:
                raise StorageError("Intent references an unknown Turn")
            intents_by_turn[intent.turn_id].append(intent.intent_id)
            intent.to_domain()
        for registered_turn in rows.turns:
            if registered_turn.task_run_id != task_run_id:
                raise StorageError("Turn belongs to another TaskRun")
            registered_turn.to_domain()
            if tuple(intents_by_turn[registered_turn.turn_id]) != registered_turn.intent_ids:
                raise StorageError("Turn.intent_ids do not match its persisted Intents")

        child_by_intent: dict[str, str] = {}
        for registered_turn in rows.turns:
            parent_turn_id = registered_turn.parent_turn_id
            triggering_intent_id = registered_turn.triggering_intent_id
            if (parent_turn_id is None) != (triggering_intent_id is None):
                raise StorageError("Turn parent metadata must be set together")
            if parent_turn_id is None:
                continue
            parent = turns.get(parent_turn_id)
            trigger = intents.get(triggering_intent_id)
            if (
                parent is None
                or parent.task_run_id != registered_turn.task_run_id
                or parent.turn_id == registered_turn.turn_id
            ):
                raise StorageError("child Turn references an invalid parent Turn")
            if (
                trigger is None
                or trigger.turn_id != parent.turn_id
                or trigger.status is not IntentStatus.SUCCEEDED
            ):
                raise StorageError(
                    "child Turn trigger must be a succeeded Intent in its parent Turn"
                )
            if triggering_intent_id in child_by_intent:
                raise StorageError("one Intent cannot trigger multiple child Turns")
            child_by_intent[triggering_intent_id] = registered_turn.turn_id

        for registered_turn in rows.turns:
            seen: set[str] = set()
            current = registered_turn
            while current.parent_turn_id is not None:
                if current.turn_id in seen:
                    raise StorageError("Turn parent relationships contain a cycle")
                seen.add(current.turn_id)
                parent = turns.get(current.parent_turn_id)
                if parent is None:
                    break
                current = parent

        plan_by_intent: dict[str, PlanStatus] = {}
        for registered_plan in rows.plans:
            if registered_plan.intent_id not in intents:
                raise StorageError("Plan references an unknown Intent")
            previous_status = plan_by_intent.get(registered_plan.intent_id)
            if previous_status is not None and previous_status not in {
                PlanStatus.COMPLETED,
                PlanStatus.SUPERSEDED,
            }:
                raise StorageError("a later Plan follows an unfinished Plan")
            plan_by_intent[registered_plan.intent_id] = registered_plan.status
            registered_plan.to_domain()

        command_results: dict[str, bool] = {}
        output_results: dict[str, bool] = {}
        execution_commands: dict[tuple[str, int], set[str]] = {}
        for stored_execution in rows.executions:
            plan = plans.get(stored_execution.plan_id)
            if plan is None:
                raise StorageError("Execution references an unknown Plan")
            if not 0 <= stored_execution.step_index < len(plan.steps):
                raise StorageError("Execution step_index is outside its Plan")
            execution_commands.setdefault(
                (stored_execution.plan_id, stored_execution.step_index), set()
            ).add(stored_execution.command_ref)
            stored_execution.to_domain()
            self._require_reference(
                "command",
                stored_execution.command_ref,
                command_results,
            )
            if stored_execution.status in EXECUTION_TERMINAL_STATUSES:
                assert stored_execution.output_ref is not None
                self._require_reference(
                    "output",
                    stored_execution.output_ref,
                    output_results,
                )

        successful_validations: dict[tuple[str, int], list[str]] = {}
        attempts: dict[tuple[str, int | None], list[int]] = {}
        for stored_validation in rows.plan_validation_records:
            plan = plans.get(stored_validation.plan_id)
            if plan is None or stored_validation.task_run_id != task_run_id:
                raise StorageError(
                    "PlanValidationRecord references a Plan outside its TaskRun"
                )
            if stored_validation.step_index is not None and not (
                0 <= stored_validation.step_index < len(plan.steps)
            ):
                raise StorageError(
                    "PlanValidationRecord step_index is outside its Plan"
                )
            attempt_key = (stored_validation.plan_id, stored_validation.step_index)
            attempts.setdefault(attempt_key, []).append(stored_validation.attempt)
            if (
                stored_validation.status is PlanValidationStatus.SUCCEEDED
                and stored_validation.step_index is not None
            ):
                success_key = (stored_validation.plan_id, stored_validation.step_index)
                assert stored_validation.command_ref is not None
                successful_validations.setdefault(success_key, []).append(
                    stored_validation.command_ref
                )
                self._require_reference(
                    "command",
                    stored_validation.command_ref,
                    command_results,
                )
        for attempt_values in attempts.values():
            if attempt_values != list(range(1, len(attempt_values) + 1)):
                raise StorageError(
                    "PlanValidationRecord attempts must be contiguous from one"
                )

        for key, commands in successful_validations.items():
            if len(commands) > 2 or len(commands) != len(set(commands)):
                raise StorageError("Plan step contains invalid command retry records")
            if len(commands) == 2:
                first = [
                    item for item in rows.executions
                    if (item.plan_id, item.step_index, item.command_ref)
                    == (*key, commands[0])
                ]
                if (
                    len(first) != 1
                    or first[0].status is not ExecutionStatus.TIMED_OUT
                    or not any(
                        failure.evidence_ref == first[0].execution_id
                        and failure.failure_type is FailureType.STEP_TIMEOUT
                        for failure in rows.failure_summaries
                    )
                ):
                    raise StorageError("changed command lacks a confirmed step timeout")
        for execution_key, command_refs in execution_commands.items():
            if not command_refs.issubset(set(successful_validations.get(execution_key, ()))):
                raise StorageError(
                    "Execution does not match its successful command generation record"
                )

        for fact in rows.facts:
            if (fact.verification_ref or "").startswith("d8e:"):
                manager=getattr(self,"_evidence_conclusions",None)
                if manager is None:raise StorageError("Reviewed evidence port not configured")
                manager.validate_fact(fact,rows,d8_pending)
                continue
            source = executions.get(fact.source_ref)
            turn = turns.get(fact.turn_id)
            if source is None or turn is None:
                raise StorageError("Fact references missing control state")
            source_plan = plans[source.plan_id]
            source_intent = intents[source_plan.intent_id]
            if source_intent.turn_id != turn.turn_id:
                raise StorageError("Fact source Execution belongs to another Turn")
            fact.to_domain()
            if fact.verification_ref is not None:
                if fact.verification_ref.startswith("d3:"):
                    if self._d3_observation_proofs is None:
                        raise StorageError("D3 observation proof evidence is not configured")
                    # Re-validating an already committed Fact must not require
                    # its source Plan to still be active: replanning supersedes
                    # the old Plan while its verified Facts stay valid.
                    proof, evidence = self._d3_observation_proofs.read_verified(
                        fact.source_ref, fact.verification_ref.removeprefix("d3:"),
                        allow_inactive_plan=True,
                    )
                    config = self._store.load_d3_run_config(task_run_id)
                    expected_content = d3_fact_content(proof.proof_id)
                    if config is not None and config.d8_enabled:
                        expected_content = d8_fact_content(proof.artifact_version_id, proof.observed_sha256, proof.proof_id)
                        if getattr(proof, 'rule_id', None) == 'http_response_observed':
                            from agent_core.http_contracts import is_http
                            if not is_http(config):
                                raise StorageError('HTTP proof requires its frozen contract20 authorization')
                            expected_content = proof.fact_content()
                        approvals = (*self._store.list_d8_conclusions(task_run_id), *d8_pending)
                        if not any(r.outcome == "verified_fact" and r.fact_id == fact.fact_id
                                   and r.plan_id == source_plan.plan_id for r in approvals):
                            raise StorageError("D8 observation Fact has no approval record")
                        # Contracts 21/22 let the accepted review replace the
                        # observation template with its refined content; the
                        # stored verified_fact approval is the authority for the
                        # committed content there, so the template equality is
                        # relaxed for exactly that pair of contracts.
                        refined_ok = config.contract_version in {21, 22}
                    if (
                        fact.status is not FactStatus.VERIFIED
                        or proof.intent_id != source_intent.intent_id
                        or proof.turn_id != fact.turn_id
                        or proof.task_run_id != task_run_id
                        or (fact.content != expected_content
                            and not (config is not None and config.d8_enabled
                                     and config.contract_version in {21, 22}))
                        or evidence.task_run_id != task_run_id
                    ):
                        raise StorageError("D3 observation Fact proof does not match control state")
                    continue
                if fact.verification_ref.startswith("t6:"):
                    if self._t6_observation_proofs is None:
                        raise StorageError("T6 observation proof evidence is not configured")
                    proof, evidence = self._t6_observation_proofs.read_verified(
                        fact.source_ref, fact.verification_ref.removeprefix("t6:")
                    )
                    if (
                        fact.status is not FactStatus.VERIFIED
                        or proof.intent_id != source_intent.intent_id
                        or proof.task_run_id != task_run_id
                        or fact.content != observation_fact_content(
                            proof.proof_id, len(proof.execution_ids)
                        )
                        or evidence.task_run_id != task_run_id
                    ):
                        raise StorageError("T6 observation Fact proof does not match control state")
                    continue
                verified = self._file_type_proof(
                    fact.source_ref, fact.verification_ref
                )
                binding = self._store.load_intent_goal(source_intent.intent_id)
                if (
                    fact.status is not FactStatus.VERIFIED
                    or binding is None
                    or verified.proof.intent_id != source_intent.intent_id
                    or verified.proof.path != binding.goal.path
                    or fact.content != fact_content(verified.proof.observed_type)
                    or verified.execution.task_run_id != task_run_id
                    or verified.execution.target_ref not in {
                        item.value for item in rows.task_run.request.target.attachments
                    }
                    or not rows.task_run.started_at
                    <= verified.execution.observed_at
                    <= rows.task_run.deadline_at
                ):
                    raise StorageError("file-type Fact proof does not match control state")

        for summary in rows.failure_summaries:
            execution = executions.get(summary.evidence_ref)
            validation = validations.get(summary.evidence_ref)
            model_error_for_intent = (
                summary.failure_type is FailureType.MODEL_ERROR
                and any(item.intent_id == summary.evidence_ref for item in rows.intents)
            )
            if execution is None and validation is None and not model_error_for_intent:
                raise StorageError("FailureSummary references missing evidence")
            summary.to_domain()

    def _require_reference(
        self,
        kind: str,
        reference: str,
        cache: dict[str, bool],
    ) -> None:
        if reference in cache:
            exists = cache[reference]
        else:
            try:
                if kind == "command":
                    exists = self._execution_evidence.command_exists(reference)
                else:
                    exists = self._execution_evidence.output_exists(reference)
            except Exception as exc:
                raise StorageError(
                    f"{kind} evidence validation is unavailable: {exc}"
                ) from exc
            cache[reference] = exists
        if not exists:
            raise StorageError(
                f"persisted control state contains an invalid {kind}_ref: {reference}"
            )
