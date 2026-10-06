"""Versioned Turn admission keeps legacy limits and D7's shared budget."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token
from test_d5_chain import RefinementModel

from agent_adapters.multi_turn_application import D3Application
from agent_core.contracts import TaskRequest
from agent_core.control_store import StorageError
from agent_core.dual_llm_contracts import D3RunConfig, D3Authorization
from agent_core.operation_accounting import D4BudgetPolicy, D4BudgetExhausted
from agent_core.turn_contracts import D1RoleConfig


def make_case(directory, version):
    source = directory / "source"
    source.mkdir()
    (source / "input.txt").write_bytes(b"D7 versioned admission fixture")
    outer_role = D1RoleConfig("outer_planning", "fake", "test-model", "outer-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "test-model", "inner-v1")
    config = D3RunConfig(
        outer_role, inner_role, D3Authorization(str(uuid4()), (0,)),
        outer_role.role_config_ref, contract_version=version,
        d4_budget=D4BudgetPolicy(), d5_enabled=True, d5_sensitive_requirements=(),
    )
    sandbox, clock, token = Sandbox(), Clock(), Token()
    app = D3Application(
        directory / "run", config=config, sandbox=sandbox, import_sandbox=sandbox,
        workspace_sandbox=sandbox, command_sandbox=sandbox,
        outer_model=Model(outer_role), inner_model=Model(inner_role),
        review_model=ReviewModel(outer_role), refinement_model=RefinementModel(outer_role),
        sensitive_config={}, clock=clock,
    )
    app.start(TaskRequest.from_dict({
        "goal": "exercise versioned Turn admission only",
        "target": {"attachments": [{"kind": "local_path", "path": "input.txt"}]},
    }), source)
    return app, token


async def register_next(app, token):
    ordinal = len(app.state.turns) + 1
    receipts = await app._import_all(token)
    request_id, proposal = await app._outer_proposal(ordinal, receipts, token)
    binding = app.control.register_turn(
        proposal, request_id, receipts[proposal.attachment_index],
        name=f"turn/{ordinal}/register",
    )
    app.store.mark_d3_operation_applied(request_id, binding.turn_id)


@pytest.mark.parametrize("version", [1, 2])
def test_config_roundtrip_preserves_version_and_turn_policy(tmp_path, version):
    app, _ = make_case(tmp_path, version)
    try:
        loaded = D3RunConfig.from_dict(json.loads(json.dumps(asdict(app.config))))
        assert loaded == app.config
        assert loaded.max_turns == (2 if version == 1 else None)
    finally:
        app.cancel()
        app.close()


def test_d7_config_requires_budget_and_safe_projection(tmp_path):
    app, _ = make_case(tmp_path, 2)
    try:
        with pytest.raises(ValueError, match="D7 requires"):
            replace(app.config, d4_budget=None, d5_enabled=False)
        with pytest.raises(ValueError, match="D7 requires"):
            replace(app.config, d5_enabled=False)
    finally:
        app.cancel()
        app.close()


def test_legacy_config_still_rejects_third_turn(tmp_path):
    app, token = make_case(tmp_path, 1)
    try:
        for ordinal in (1, 2):
            if ordinal > 1:
                asyncio.run(register_next(app, token))
            assert asyncio.run(app.run(token)).stop_reason == "turn_completed_sandbox_retained"
        with pytest.raises(StorageError, match="versioned limit"):
            asyncio.run(register_next(app, token))
        assert len(app.state.turns) == 2
    finally:
        app.cancel()
        app.close()


def test_d7_allows_four_turns_then_existing_planning_budget_blocks_fifth(tmp_path):
    app, token = make_case(tmp_path, 2)
    try:
        for ordinal in range(1, 4):
            if ordinal > 1:
                asyncio.run(register_next(app, token))
            result = asyncio.run(app.run(token))
            assert result.stop_reason == "turn_completed_sandbox_retained"
            assert result.overall_goal == "not_evaluated"
        assert len(app.state.turns) == 3
        asyncio.run(register_next(app, token))
        assert len(app.state.turns) == 4
        before = app.store.d4_budget_usage(app.state.task_run.task_run_id)
        with pytest.raises(D4BudgetExhausted):
            asyncio.run(register_next(app, token))
        assert len(app.state.turns) == 4
        assert app.store.d4_budget_usage(app.state.task_run.task_run_id) == before
        assert dict(before.category_counts)["planning"] == 4
    finally:
        app.cancel()
        app.close()
