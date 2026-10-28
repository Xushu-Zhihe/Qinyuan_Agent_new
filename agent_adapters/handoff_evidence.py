"""Immutable, checksummed safe D8-2 evidence; no arbitrary path reads."""

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.operation_accounting import _uuid
from agent_core.handoff_contracts import handoff_json
import json


class FileD8HandoffEvidence:
    KINDS = {"request", "result", "handoff", "read-snapshot", "read-result", "execution-context", "pause", "decision", "strategy-effect", "strategy-yield", "strategy-discard"}

    def __init__(self, directory, *, create=True):
        self.records = T6CommandRecorder(directory, create=create)

    def save(self, kind, identity, value):
        _uuid(identity, "evidence_id")
        if kind not in self.KINDS:
            raise ValueError("d8_evidence_kind_invalid")
        # Equality on replay uses the exact persisted JSON representation.
        try:
            return self.records._write("d8-handoff-" + kind, identity, json.loads(handoff_json(value)))
        except (T6EvidenceError, OSError, ValueError, TypeError) as exc:
            raise ExecutionRecoveryRequired("d8_handoff_evidence_write_recovery_required") from exc

    def capacity_block(self, task_run_id, checkpoint_sequence):
        """Project only a block still bound to this durable state, never an old pause."""
        candidates = []
        for path in self.records.directory.glob("d8-handoff-pause-*.json"):
            identity = path.name.removeprefix("d8-handoff-pause-").removesuffix(".json")
            value = self.read("pause", identity)
            if value.get("kind") not in {"complete_model_request", "initial_history", "visible_history", "evaluation_required_context"} or "scope" not in value:
                continue
            if value.get("task_run_id") != task_run_id:
                raise ExecutionRecoveryRequired("d8_capacity_block_owner_changed")
            if value.get("checkpoint_sequence") != checkpoint_sequence:
                continue
            if (value["scope"] not in {"intent", "task"} or type(value.get("organization_attempted")) is not bool
                    or value.get("next_action") not in {"continue_other_feasible_intents", "wait_for_context_change_or_deadline"}):
                raise ExecutionRecoveryRequired("d8_capacity_block_invalid")
            candidates.append((path.stat().st_mtime_ns, value))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def read(self, kind, identity):
        _uuid(identity, "evidence_id")
        if kind not in self.KINDS:
            raise ValueError("d8_evidence_kind_invalid")
        try:
            return self.records._read("d8-handoff-" + kind, identity)
        except (T6EvidenceError, OSError, ValueError, TypeError) as exc:
            raise ExecutionRecoveryRequired("d8_handoff_evidence_unavailable") from exc


class FakeUnicodeCapacity:
    """Only measures the complete Fake request envelope in Unicode code points."""

    def fits(self, request, policy):
        from agent_core.handoff_contracts import handoff_json
        if policy.policy_name != "fake-json-v1" or policy.measurement_unit != "unicode_codepoints":
            raise ValueError("capacity adapter does not implement the frozen policy")
        return len(handoff_json(request)) + policy.output_reserve_units <= policy.total_units
