"""Layered Fact Storage for D22.

Extends Fact storage with traceable links:
  Fact → FactCandidate(s) → Evidence

Design Principle: Strict about output boundaries - preserve full audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.domain import Fact, FactStatus


class FactLayer(StrEnum):
    """D22: Three-tier Fact categorization for flexible storage.

    Lenient toward semantics: allows exploratory positioning.
    Strict about boundaries: each layer has defined submission triggers.
    """
    EVIDENCE = "evidence"  # Direct MCP tool outputs, verified data
    BUSINESS = "business"  # Business logic inferences from observations
    PROCESS = "process"  # High-level process summaries across multiple facts


class FactOrigin(StrEnum):
    """Where a Fact came from."""

    INNER_LAYER = "inner_layer"  # Inner layer conclusion
    OUTER_REFINED = "outer_refined"  # Outer layer refined candidate text
    MERGED = "merged"  # Merged from multiple candidates
    PROCESS_ORGANIZED = "process_organized"  # ProcessRefinement summary


@dataclass(frozen=True)
class FactCandidate:
    """A proposed Fact before outer layer review.

    Links to Evidence that supports this candidate.
    Multiple candidates can be merged into one Fact.
    """

    candidate_id: str  # UUID
    intent_id: str  # Which Intent produced this
    plan_id: str | None  # Which Plan produced this (None for exploratory)
    turn_id: str
    content: str  # Proposed Fact text (max 256 chars)
    submission_reason: str | None  # Why inner layer requested submission
    evidence_refs: tuple[str, ...]  # References to supporting Evidence
    created_at: float  # Timestamp

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("FactCandidate.candidate_id required")
        if not self.intent_id:
            raise ValueError("FactCandidate.intent_id required")
        if not self.turn_id:
            raise ValueError("FactCandidate.turn_id required")
        if not self.content:
            raise ValueError("FactCandidate.content required")
        if len(self.content) > 256:
            raise ValueError("FactCandidate.content must be ≤256 chars")


@dataclass(frozen=True)
class FactProvenance:
    """Traceable links from Fact to its source candidates and Evidence.

    Strict about output boundaries: preserve full audit trail, never overwrite.
    Version history allows correction without losing original reasoning.
    """

    fact_id: str
    version: int  # Increments on correction/refinement
    origin: FactOrigin  # Where this Fact came from
    source_candidate_ids: tuple[str, ...]  # Which candidates contributed
    refined_content: str | None  # Outer layer text refinement (if any)
    merged_sources: tuple[str, ...] | None  # Merged candidate IDs (if merged)
    created_at: float  # When this version was created

    def __post_init__(self) -> None:
        if not self.fact_id:
            raise ValueError("FactProvenance.fact_id required")
        if self.version < 1:
            raise ValueError("FactProvenance.version must be ≥1")
        if not self.source_candidate_ids:
            raise ValueError("FactProvenance.source_candidate_ids required")
        if self.origin == FactOrigin.MERGED and not self.merged_sources:
            raise ValueError("FactProvenance.merged_sources required when origin=MERGED")


@dataclass(frozen=True)
class FactEvidence:
    """Link from FactCandidate to raw Evidence.

    Evidence is immutable observation data: MCP tool output, file hashes, HTTP responses.
    Multiple candidates can reference the same Evidence.
    """

    evidence_id: str  # UUID
    evidence_type: str  # "mcp_tool_output", "file_hash", "http_response", etc.
    evidence_ref: str  # Storage reference (path, URL, hash)
    content_summary: str  # Brief description for human review
    created_at: float

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("FactEvidence.evidence_id required")
        if not self.evidence_type:
            raise ValueError("FactEvidence.evidence_type required")
        if not self.evidence_ref:
            raise ValueError("FactEvidence.evidence_ref required")


class LayeredFactStore:
    """Storage layer for Facts with full provenance tracking.

    Three-tier architecture:
    1. FactEvidence: Immutable raw observations
    2. FactCandidate: Inner layer proposals linking to Evidence
    3. Fact + FactProvenance: Verified conclusions linking to candidates

    Design:
    - Lenient toward input: accept candidates without forced Fact creation
    - Strict about boundaries: preserve full audit trail, version history
    """

    def __init__(self) -> None:
        # Storage dictionaries (in-memory for now; migrate to DB later)
        self._evidence: dict[str, FactEvidence] = {}
        self._candidates: dict[str, FactCandidate] = {}
        self._provenance: dict[tuple[str, int], FactProvenance] = {}  # (fact_id, version)
        self._fact_latest_version: dict[str, int] = {}

    def store_evidence(self, evidence: FactEvidence) -> None:
        """Store immutable Evidence."""
        if evidence.evidence_id in self._evidence:
            existing = self._evidence[evidence.evidence_id]
            if existing != evidence:
                raise ValueError(
                    f"Evidence {evidence.evidence_id} already exists with different content"
                )
        self._evidence[evidence.evidence_id] = evidence

    def store_candidate(self, candidate: FactCandidate) -> None:
        """Store Fact candidate from inner layer."""
        # Validate evidence refs exist
        for ref in candidate.evidence_refs:
            if ref not in self._evidence:
                raise ValueError(f"Evidence {ref} not found (store Evidence first)")

        self._candidates[candidate.candidate_id] = candidate

    def store_fact_with_provenance(
        self,
        fact: Fact,
        provenance: FactProvenance,
    ) -> None:
        """Store verified Fact with full provenance.

        Validates:
        - All source candidates exist
        - fact_id matches provenance
        - Version number correct (current + 1 or initial 1)
        """
        if fact.fact_id != provenance.fact_id:
            raise ValueError("Fact.fact_id must match FactProvenance.fact_id")

        # Validate all source candidates exist
        for cid in provenance.source_candidate_ids:
            if cid not in self._candidates:
                raise ValueError(f"Candidate {cid} not found")

        # Validate version sequence
        current_version = self._fact_latest_version.get(fact.fact_id, 0)
        if provenance.version != current_version + 1:
            raise ValueError(
                f"Expected version {current_version + 1}, got {provenance.version}"
            )

        # Store provenance and update version tracking
        self._provenance[(fact.fact_id, provenance.version)] = provenance
        self._fact_latest_version[fact.fact_id] = provenance.version

    def get_candidate(self, candidate_id: str) -> FactCandidate | None:
        """Retrieve candidate by ID."""
        return self._candidates.get(candidate_id)

    def get_provenance(
        self,
        fact_id: str,
        version: int | None = None,
    ) -> FactProvenance | None:
        """Retrieve provenance for Fact.

        Args:
            fact_id: Fact UUID
            version: Specific version, or None for latest

        Returns:
            FactProvenance if found, else None
        """
        if version is None:
            version = self._fact_latest_version.get(fact_id)
            if version is None:
                return None
        return self._provenance.get((fact_id, version))

    def get_fact_history(self, fact_id: str) -> list[FactProvenance]:
        """Get all versions of a Fact in chronological order."""
        max_version = self._fact_latest_version.get(fact_id, 0)
        return [
            prov
            for v in range(1, max_version + 1)
            if (prov := self._provenance.get((fact_id, v))) is not None
        ]

    def get_evidence(self, evidence_id: str) -> FactEvidence | None:
        """Retrieve Evidence by ID."""
        return self._evidence.get(evidence_id)

    def get_candidates_for_intent(self, intent_id: str) -> list[FactCandidate]:
        """Get all candidates produced by an Intent."""
        return [
            c
            for c in self._candidates.values()
            if c.intent_id == intent_id
        ]


# Alias for backwards compatibility and test expectations
FactStorageManager = LayeredFactStore
