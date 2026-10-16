"""Only frozen output descriptions and safe diagnostics, never model error text."""

from agent_adapters.handoff_evidence import FileD8HandoffEvidence


class FileD8OutputEvidence(FileD8HandoffEvidence):
    KINDS = {"contract", "diagnostic", "review-root", "business-diagnostic", "review-request", "review-binding", "review-result", "fixed-probe-binding", "planning-binding", "fact-assertion-binding", "evaluation-binding", "evaluation-diagnostic", "candidate-binding", "selection-request", "selection-binding", "answer-decision", "http-plan-binding", "http-result"}
