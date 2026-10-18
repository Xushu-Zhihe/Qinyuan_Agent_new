from __future__ import annotations

import pytest

from agent_core.intent_goal_evaluator import IntentGoalEvaluation
from agent_core.intent_loop import IntentLoopResult


def test_loop_result_keeps_evaluation_separate_from_stop_reason() -> None:
    evaluation = IntentGoalEvaluation(
        "task", "intent", "not_satisfied", (), "no_supporting_verified_fact"
    )
    result = IntentLoopResult(
        "task", "intent", evaluation, "model_stopped", None, (), ()
    )
    assert result.evaluation.outcome == "not_satisfied"
    assert result.stop_reason == "model_stopped"


def test_loop_result_rejects_unrelated_or_unsupported_facts() -> None:
    evaluation = IntentGoalEvaluation(
        "task", "intent", "satisfied", ("fact",), "verified_file_type_fact"
    )
    with pytest.raises(ValueError, match="same Intent"):
        IntentLoopResult(
            "task", "other", evaluation, "intent_satisfied", None, (), ("fact",)
        )
    with pytest.raises(ValueError, match="exactly"):
        IntentLoopResult("task", "intent", evaluation, "intent_satisfied", None, (), ())
