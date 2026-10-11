"""Immutable D8 request/result evidence; no ordinary raw-response export."""

from dataclasses import asdict

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.conclusion_contracts import D8ModelRequest
from agent_core.dual_llm_contracts import D3RecoveryRequired


class FileD8Evidence:
    def __init__(self, directory, *, create=True):
        self.records = T6CommandRecorder(directory, create=create)

    def save_request(self, request: D8ModelRequest) -> None:
        self.records._write("d8-request", request.model_request_id, asdict(request))

    def read_request(self, request_id: str) -> D8ModelRequest | None:
        try:
            value = self.records._read("d8-request", request_id)
            result = D8ModelRequest.from_dict(value) if value is not None else None
            if result is not None and result.model_request_id != request_id:
                raise ValueError("request identity mismatch")
            return result
        except (T6EvidenceError, ValueError, TypeError, KeyError) as exc:
            raise D3RecoveryRequired("d8_request_evidence_unavailable") from exc

    def save_result(self, request_id: str, result: dict) -> None:
        self.records._write("d8-result", request_id, result)

    def read_result(self, request_id: str) -> dict | None:
        try:
            result = self.records._read("d8-result", request_id)
            if result is not None and result.get("model_request_id") != request_id:
                raise ValueError("result identity mismatch")
            return result
        except (T6EvidenceError, ValueError, TypeError, KeyError) as exc:
            raise D3RecoveryRequired("d8_result_evidence_unavailable") from exc
