"""Immutable, checksummed safe D8-2 evidence; no arbitrary path reads."""

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.operation_accounting import _uuid
from agent_core.handoff_contracts import handoff_json
import json


class FileD8HandoffEvidence:
    KINDS = {"request", "result", "handoff", "read-snapshot", "read-result", "execution-context", "pause", "decision"}

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
            raise D3RecoveryRequired("d8_handoff_evidence_write_recovery_required") from exc

    def read(self, kind, identity):
        _uuid(identity, "evidence_id")
        if kind not in self.KINDS:
            raise ValueError("d8_evidence_kind_invalid")
        try:
            return self.records._read("d8-handoff-" + kind, identity)
        except (T6EvidenceError, OSError, ValueError, TypeError) as exc:
            raise D3RecoveryRequired("d8_handoff_evidence_unavailable") from exc


class FakeUnicodeCapacity:
    """Only measures the complete Fake request envelope in Unicode code points."""

    def fits(self, request, policy):
        from agent_core.handoff_contracts import handoff_json
        if policy.policy_name != "fake-json-v1" or policy.measurement_unit != "unicode_codepoints":
            raise ValueError("capacity adapter does not implement the frozen policy")
        return len(handoff_json(request)) + policy.output_reserve_units <= policy.total_units
