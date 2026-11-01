"""A withdrawal recorded by the runtime must satisfy the pending-operations check."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_core.stage_contracts import StageReviewBinding
from agent_core.stage_storage import _withdrawn_by_review

TASK = "task-run"
PROPOSAL = "proposal-op"
REVIEW = "review-op"
PLANNING = "planning-op"
EXECUTED = "executed-op"


def request(conversation=PLANNING):
    message = {"role": "user", "content": json.dumps({"coverage": [[conversation, "0" * 64]]})}
    return SimpleNamespace(messages=(message,))


def operation(identity, *, task_run_id=TASK, role="inner_action", result="completed", applied=None, executed=False):
    return SimpleNamespace(
        start=SimpleNamespace(task_run_id=task_run_id, role=role, category="action",
                              operation_id=identity,
                              resource_ref=("mcp/" if executed else "stage/") + identity),
        completion=None if result is None else SimpleNamespace(result_kind=result),
        application_ref=applied)


class Store:
    def __init__(self, *, operations, interruptions, requests=None):
        self.operations = operations
        self.interruptions = interruptions
        self.requests = requests or {}
        self.stage_evidence = SimpleNamespace(
            request=lambda identity: self.requests[identity],
            review_interruptions=lambda review: self.interruptions.get(review, ()))

    def load_d4_operation(self, identity):
        return next((value for value in self.operations if value.start.operation_id == identity), None)

    def list_d4_operations(self, task_id):
        return list(self.operations)


def change(*, reviews=(StageReviewBinding(PROPOSAL, REVIEW),), discarded=(StageReviewBinding(PROPOSAL, REVIEW),)):
    return SimpleNamespace(reviews=tuple(reviews), discarded_decisions=tuple(discarded))


def mutation():
    return SimpleNamespace(task_run_id=TASK)


def store(**overrides):
    values = dict(
        operations=[operation(PROPOSAL), operation(REVIEW, role="outer_planning")],
        interruptions={REVIEW: (PLANNING,)},
        requests={PROPOSAL: request()},
    )
    values.update(overrides)
    return Store(**values)


def test_a_review_that_settles_this_commit_may_withdraw_the_decision():
    assert _withdrawn_by_review(store(), mutation(), change()) == {PROPOSAL}


def test_a_withdrawal_without_the_matching_review_is_ignored():
    assert _withdrawn_by_review(store(), mutation(), change(reviews=())) == set()


def test_a_withdrawal_outside_the_review_interruptions_is_ignored():
    assert _withdrawn_by_review(store(interruptions={REVIEW: ()}), mutation(), change()) == set()


def test_an_executed_decision_is_never_withdrawn():
    executed = operation(PROPOSAL, executed=True)
    child = SimpleNamespace(start=SimpleNamespace(category="tool_dispatch", resource_ref="mcp/" + PROPOSAL))
    assert _withdrawn_by_review(store(operations=[executed, child]), mutation(), change()) == set()


def test_an_already_applied_or_failed_decision_is_not_withdrawn():
    assert _withdrawn_by_review(store(operations=[operation(PROPOSAL, applied="ref")]), mutation(), change()) == set()
    assert _withdrawn_by_review(store(operations=[operation(PROPOSAL, result="failed")]), mutation(), change()) == set()


def test_a_foreign_or_router_operation_is_not_withdrawn():
    wrong_task = operation(PROPOSAL, task_run_id="other-task")
    assert _withdrawn_by_review(store(operations=[wrong_task]), mutation(), change()) == set()
    outer = operation(PROPOSAL, role="outer_planning")
    assert _withdrawn_by_review(store(operations=[outer]), mutation(), change()) == set()


def test_the_withdrawal_is_narrowed_to_the_exact_binding():
    other = SimpleNamespace(proposal_operation_id="another-op", review_operation_id=REVIEW)
    assert _withdrawn_by_review(store(), mutation(), change(discarded=(other,))) == set()
