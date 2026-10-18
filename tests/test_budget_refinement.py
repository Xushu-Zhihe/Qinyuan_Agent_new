"""Refinement budget exhaustion creates no orphan request evidence."""

from __future__ import annotations

import asyncio

import pytest
from test_budget_operation import _completion, _start
from test_chain_process_fact import close_scenario, make_d5_scenario

from agent_core.operation_accounting import D4_CATEGORY_LIMITS, D4_TOTAL


def test_refinement_limit_skips_repeated_reviews_without_saving_request(tmp_path):
    case = make_d5_scenario(tmp_path)
    app = case[0]
    task_id = app.state.task_run.task_run_id
    try:
        for index in range(app.config.d4_budget.refinement):
            start = _start(case, "refinement", index)
            app.store.begin_d4_operation(start)
            app.store.complete_d4_operation(_completion(start, result="failed"))

        result = asyncio.run(app.run(case[6]))
        assert not result.recovery_required, result
        reviews = app.store.list_d3_review_applications(task_id)
        assert reviews
        checkpoint = app.state.latest_checkpoint.sequence
        case[5].now += 1
        for _ in range(2):
            for review in reviews:
                app._after_review(review.review_id)

        assert app.state.latest_checkpoint.sequence == checkpoint
        assert app.store.list_d5_attempts(task_id) == ()
        assert app.store.list_process_facts(task_id) == ()
        assert not case[7].calls
        assert not tuple(app.refinement.evidence.directory.glob("request-*.json"))
        assert (
            dict(app.store.d4_budget_usage(task_id).category_counts)["refinement"] == D4_CATEGORY_LIMITS["refinement"]
        )
    finally:
        close_scenario(case)


@pytest.mark.parametrize("exhausted", ["total", "refinement"])
def test_exhausted_budget_short_circuits_before_source_or_request_writes(exhausted):
    from types import SimpleNamespace

    from agent_core.process_refinement import ProcessRefinement

    class Store:
        def list_d5_attempts(self, task_id):
            return ()

        def d4_budget_usage(self, task_id):
            return SimpleNamespace(
                total_used=D4_TOTAL if exhausted == "total" else 4,
                category_counts=(
                    ("refinement", D4_CATEGORY_LIMITS["refinement"] if exhausted == "refinement" else 0),
                ),
            )

        def load_d3_review_application(self, *args):
            raise AssertionError("exhausted budget must not prepare a new refinement")

    controller = SimpleNamespace(
        control_state=SimpleNamespace(
            task_run=SimpleNamespace(task_run_id="owned-task")
        )
    )
    refinement = ProcessRefinement(
        controller,
        Store(),
        None,
        None,
        None,
        SimpleNamespace(
            d5_enabled=True, d5_sensitive_requirements=(), d4_budget=SimpleNamespace(total=D4_TOTAL, refinement=D4_CATEGORY_LIMITS["refinement"])
        ),
        None,
        lambda: False,
        SimpleNamespace(configured=True),
    )
    for _ in range(2):
        refinement.after_review("committed-review")
