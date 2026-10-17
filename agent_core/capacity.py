"""Context capacity management for D22 Intent tree.

Detects when active Intents won't fit in model context and pauses with
explicit error listing excluded Intents (strict about boundaries).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.intent_context import IntentTreeFrontier


@dataclass(frozen=True)
class CapacityCheckResult:
    """Result of capacity check for Intent frontier injection.

    Design principle: Strict about output boundaries - either all active Intents
    fit in context, or none do (with explicit error listing exclusions).
    """

    fits: bool  # True if frontier fits in available capacity
    used_tokens: int  # Estimated tokens used by frontier
    available_tokens: int  # Available capacity
    excluded_intent_ids: tuple[str, ...]  # Intent IDs that don't fit (if fits=False)
    reason: str | None  # Human-readable explanation if fits=False


def estimate_frontier_tokens(frontier: IntentTreeFrontier) -> int:
    """
    Estimate token count for active Intent frontier JSON.

    Conservative estimate using character count / 3 (rough tokens heuristic).
    Better to slightly overestimate and pause early than underestimate and
    cause runtime context overflow.

    Args:
        frontier: IntentTreeFrontier to estimate

    Returns:
        Estimated token count
    """
    # Estimate JSON overhead
    overhead = 100  # {"task_run_id": "...", "total_pending": N, ...}

    # Estimate per-Intent entry cost
    # Each entry has ~10 fields, UUID (36 chars), description (variable)
    token_per_entry = 0
    for entry in frontier.entries:
        # UUID fields: intent_id (36) + turn_id (36) + parent_intent_id (36 or null)
        uuid_chars = 36 + 36 + (36 if entry.parent_intent_id else 4)
        # Description varies but average ~50 chars
        desc_chars = len(entry.description)
        # Other fields: status (10), depth (2), flags (20), counts (10)
        fixed_chars = 42
        # JSON overhead per entry: {}, "", : (estimate 30 chars)
        json_overhead = 30

        total_chars = uuid_chars + desc_chars + fixed_chars + json_overhead
        # Conservative token estimate: 1 token per 3 chars
        token_per_entry += total_chars / 3

    return int(overhead + token_per_entry)


def check_capacity(
    frontier: IntentTreeFrontier,
    available_capacity: int,
    *,
    reserve_for_response: int = 2000,
) -> CapacityCheckResult:
    """
    Check if active Intent frontier fits in available model context capacity.

    Strict boundary: either ALL active Intents fit, or we pause with explicit
    error. Never silently omit Intents (lenient toward input - model should
    see full frontier or be told why it can't).

    Args:
        frontier: IntentTreeFrontier to check
        available_capacity: Available tokens in model context
        reserve_for_response: Tokens to reserve for model output (default 2000)

    Returns:
        CapacityCheckResult with fits=True if frontier fits, else fits=False
        with excluded Intent IDs and reason
    """
    estimated_tokens = estimate_frontier_tokens(frontier)
    usable_capacity = available_capacity - reserve_for_response

    if estimated_tokens <= usable_capacity:
        return CapacityCheckResult(
            fits=True,
            used_tokens=estimated_tokens,
            available_tokens=available_capacity,
            excluded_intent_ids=(),
            reason=None,
        )

    # Frontier doesn't fit - fail with explicit error
    excluded_ids = tuple(e.intent_id for e in frontier.entries)
    reason = (
        f"Active Intent frontier requires ~{estimated_tokens} tokens but only "
        f"{usable_capacity} available (after reserving {reserve_for_response} for response). "
        f"Total active Intents: {len(frontier.entries)} ({frontier.total_pending} pending, "
        f"{frontier.total_running} running). Consider completing or cancelling some Intents "
        f"to free capacity."
    )

    return CapacityCheckResult(
        fits=False,
        used_tokens=estimated_tokens,
        available_tokens=available_capacity,
        excluded_intent_ids=excluded_ids,
        reason=reason,
    )


def format_capacity_error(result: CapacityCheckResult) -> str:
    """
    Format capacity check failure as structured error message.

    Used when pausing tree planning due to capacity overflow.
    """
    if result.fits:
        return ""  # No error

    error_lines = [
        "D22 Capacity Overflow: Active Intent frontier exceeds context capacity",
        "",
        f"Required: ~{result.used_tokens} tokens",
        f"Available: {result.available_tokens} tokens",
        f"Excluded Intents: {len(result.excluded_intent_ids)}",
        "",
        "Excluded Intent IDs:",
    ]

    for intent_id in result.excluded_intent_ids[:10]:  # Show first 10
        error_lines.append(f"  - {intent_id}")

    if len(result.excluded_intent_ids) > 10:
        error_lines.append(f"  ... and {len(result.excluded_intent_ids) - 10} more")

    error_lines.append("")
    error_lines.append("Resolution: Complete or cancel some active Intents to free capacity.")

    return "\n".join(error_lines)
