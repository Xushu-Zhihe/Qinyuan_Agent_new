"""Only frozen output descriptions and safe diagnostics, never model error text."""

from agent_adapters.handoff_evidence import FileD8HandoffEvidence


class FileD8OutputEvidence(FileD8HandoffEvidence):
    KINDS = {"contract", "diagnostic", "review-root"}
