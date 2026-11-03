"""Prompt-level checks for grounded-constraint search anchors."""

from types import SimpleNamespace

from agent_core.stage_planning import ANCHOR_SEARCH_GUIDANCE, anchor_search_guidance, model_contract
from test_stage_direct_submission import request_for


def _request(stage):
    return SimpleNamespace(stage=stage)


def test_anchor_guidance_only_enters_planning_and_action_stages():
    assert anchor_search_guidance(_request("stage_roads")) == ANCHOR_SEARCH_GUIDANCE
    assert anchor_search_guidance(_request("stage_action")) == ANCHOR_SEARCH_GUIDANCE
    assert anchor_search_guidance(_request("stage_completion")) == ""
    assert anchor_search_guidance(_request("stage_review")) == ""


def test_guidance_requires_grounding_and_a_fallback():
    for phrase in ("already grounded", "search anchor", "never evidence", "fall back to normal recon"):
        assert phrase in ANCHOR_SEARCH_GUIDANCE


def test_action_contract_injects_the_anchor_guidance():
    rules = model_contract(request_for())["rules"]
    assert "use it as a search anchor" in rules
