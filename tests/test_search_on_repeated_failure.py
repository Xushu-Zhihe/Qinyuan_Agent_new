"""Repeated same-shape failure should trigger one limitations search, not a blind retry."""

from agent_core.stage_planning import EXPLORATION_GUIDANCE, PUBLIC_REFERENCE_GUIDANCE, model_contract
from test_stage_direct_submission import request_for


def test_guidance_turns_two_same_failures_into_one_limitations_search():
    assert "fails twice in a row" in PUBLIC_REFERENCE_GUIDANCE
    assert "known limitations" in PUBLIC_REFERENCE_GUIDANCE
    assert "when a declared public-reference tool is available" in PUBLIC_REFERENCE_GUIDANCE
    assert "missing binary" in PUBLIC_REFERENCE_GUIDANCE
    assert "do not repeat the unchanged request" in PUBLIC_REFERENCE_GUIDANCE
    assert "same failure signature" in EXPLORATION_GUIDANCE
    assert "known limitations" in EXPLORATION_GUIDANCE


def test_action_prompt_contains_the_repeated_failure_rule():
    rules = model_contract(request_for())["rules"]
    assert "fails twice in a row" in rules
    assert "do not repeat the unchanged request" in rules
