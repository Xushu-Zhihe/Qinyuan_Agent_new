"""Test version-aware rules for D22 unlimited Intent support."""

import pytest
from agent_adapters.deepseek_roles import get_rules


def test_version_22_removes_hard_limits():
    """Version 22 rules should not contain hard candidate limits like '1-2' or '0-2'."""
    rules = get_rules(contract_version=22)

    # Version 22 should say "one or more" instead of "1-2"
    assert "1-2" not in rules["tree_planning"]
    assert "1-2" not in rules["outer_turn"]
    assert "0-2" not in rules["inner_complete"]

    # Should allow flexible wording
    assert "one or more" in rules["tree_planning"] or "Propose" in rules["tree_planning"]
    assert "one or more" in rules["outer_turn"] or "propose" in rules["outer_turn"]


def test_legacy_version_retains_limits():
    """Legacy versions should retain original 1-2 / 0-2 limits."""
    rules = get_rules(contract_version=21)

    # Legacy should have hard limits
    assert "1-2" in rules["tree_planning"]
    assert "1-2" in rules["outer_turn"]
    assert "0-2" in rules["inner_complete"]


def test_no_version_uses_legacy():
    """None contract_version should default to legacy rules."""
    rules = get_rules(contract_version=None)

    # Should behave like legacy
    assert "1-2" in rules["tree_planning"]
    assert "1-2" in rules["outer_turn"]
    assert "0-2" in rules["inner_complete"]


def test_all_stages_present():
    """Both version sets should contain all required stages."""
    required_stages = [
        "tree_planning", "outer_turn", "inner_plan", "inner_size", "inner_sha256",
        "inner_complete", "inner_candidate", "fact_proposal", "fact_review",
        "conclusion_repair", "change_review", "intent_evaluation", "handoff_summary",
        "review", "progress_review", "refinement"
    ]

    v22_rules = get_rules(contract_version=22)
    legacy_rules = get_rules(contract_version=None)

    for stage in required_stages:
        assert stage in v22_rules, f"Version 22 missing stage: {stage}"
        assert stage in legacy_rules, f"Legacy missing stage: {stage}"
        assert isinstance(v22_rules[stage], str), f"Version 22 {stage} not a string"
        assert isinstance(legacy_rules[stage], str), f"Legacy {stage} not a string"


def test_unchanged_stages_identical():
    """Stages without limit language should be identical across versions."""
    unchanged_stages = [
        "inner_plan", "inner_size", "inner_sha256", "inner_candidate",
        "fact_proposal", "fact_review", "conclusion_repair", "change_review",
        "intent_evaluation", "handoff_summary", "review", "progress_review", "refinement"
    ]

    v22_rules = get_rules(contract_version=22)
    legacy_rules = get_rules(contract_version=None)

    for stage in unchanged_stages:
        assert v22_rules[stage] == legacy_rules[stage], f"Stage {stage} should be identical"
