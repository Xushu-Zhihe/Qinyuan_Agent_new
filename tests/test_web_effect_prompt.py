"""Prompt-level checks for irreversible target effects in web actions."""

from types import SimpleNamespace

from agent_core.stage_planning import WEB_EFFECT_GUIDANCE, web_effect_guidance


def _request(stage="stage_action", *tools):
    return SimpleNamespace(stage=stage, tools=tuple({"qualified_name": name} for name in tools))


def test_guidance_only_applies_to_target_http_actions():
    assert web_effect_guidance(_request("stage_action", "http.run_experiment")) == WEB_EFFECT_GUIDANCE
    assert web_effect_guidance(_request("stage_roads", "http.run_experiment")) == ""
    assert web_effect_guidance(_request("stage_action", "shell.run_bash")) == ""
    assert web_effect_guidance(_request("stage_action")) == ""


def test_guidance_keeps_repeatable_probes_and_requires_discriminating_checks():
    for phrase in (
        "one-shot",
        "irreversible",
        "read-only requests",
        "stays repeatable",
        "reusable primitive",
        "non-consuming",
        "discriminating",
        "identical responses",
    ):
        assert phrase in WEB_EFFECT_GUIDANCE
