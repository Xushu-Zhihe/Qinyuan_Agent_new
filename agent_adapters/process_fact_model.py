"""Model-boundary redaction for explicitly enabled D5 runs."""

from __future__ import annotations

from dataclasses import replace

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.modeling import ModelError
from agent_core.review_contracts import D2ModelFailure, D2ModelResponse


class D5RedactingModel:
    def __init__(
        self,
        adapter,
        redactor,
        *,
        enabled: bool = True,
        configured: bool = True,
        redact_response: bool = True,
    ) -> None:
        self.adapter = adapter
        self.redactor = redactor
        self.enabled = enabled
        self.configured = configured
        self.redact_response = redact_response

    @property
    def role_config(self):
        return self.adapter.role_config

    def invoke(self, request, *args):
        if not self.enabled:
            return self.adapter.invoke(request, *args)
        if not self.configured:
            raise D3RecoveryRequired("d5_redaction_configuration_unavailable")
        safe_request = self.redactor.redact_model_input(request)
        result = self.adapter.invoke(safe_request, *args)
        if self.redact_response:
            if isinstance(result, ModelError):
                return replace(
                    result,
                    short_reason=self.redactor.redact(result.short_reason),
                    evidence_ref=self.redactor.redact(result.evidence_ref)
                    if result.evidence_ref
                    else None,
                    validation_code=self.redactor.redact(result.validation_code)
                    if result.validation_code
                    else None,
                )
            if isinstance(result, D2ModelFailure):
                return replace(
                    result,
                    reason=self.redactor.redact(result.reason),
                    provider_request_id=self.redactor.redact(result.provider_request_id)
                    if result.provider_request_id
                    else None,
                )
            if isinstance(result, D2ModelResponse):
                return replace(
                    result,
                    response=self.redactor.redact_value(result.response),
                    provider_request_id=self.redactor.redact(result.provider_request_id)
                    if result.provider_request_id
                    else None,
                )
            return self.redactor.redact_value(result)
        return result
