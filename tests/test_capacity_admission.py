"""SCNET admission uses confirmed limits without changing the execution flow."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
import pytest
from agent_adapters import deepseek_roles as roles
from agent_adapters.simulation import real_config, parse_request
from agent_core.output_contracts import D8OutputRequest
from test_models import real_request


def test_confirmed_profile_is_new22_only_and_legacy_is_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(roles, "MODEL", "DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    assert roles.default_capacity_policy(22) == roles.SCNET_V41_CAPACITY
    assert roles.default_capacity_policy(21) == roles.CAPACITY_POLICY
    assert real_config(contract_version=22)["handoff_config"]["inner_capacity"] == asdict(roles.SCNET_V41_CAPACITY)
    raw = real_request(tmp_path)
    raw["execution_profile"] = "d8_v22"
    raw["d8_config"] = real_config(contract_version=22)
    raw["dual_llm_config"]["outer_role"] = asdict(roles.role_config("outer_planning", version=17))
    raw["dual_llm_config"]["inner_role"] = asdict(roles.role_config("inner_action", version=17))
    # Docker schema is validated locally; no Docker process is started.
    raw = json.loads(json.dumps(raw))
    request, config = parse_request(raw)
    assert config.handoff_config.inner_capacity == roles.SCNET_V41_CAPACITY
    for name in ("inner_capacity", "outer_capacity"):
        raw["d8_config"]["handoff_config"][name] = asdict(roles.CAPACITY_POLICY)
    assert parse_request(raw)[1].handoff_config.inner_capacity == roles.CAPACITY_POLICY


@pytest.mark.parametrize("overshoot", [-1, 0, 1])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_request_boundary_accounts_for_output_and_margin(monkeypatch, overshoot, explicit):
    role = roles.role_config("inner_action")
    role = replace(role, model="DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    policy = roles.SCNET_V41_CAPACITY
    payload = dict(model=role.model, messages=[dict(role="system", content="rule"),
        dict(role="user", content="{}")])
    if explicit:payload["max_tokens"] = 32768
    limit = policy.total_units - policy.output_reserve_units - 4096
    payload["padding"] = "x" * (limit - len(roles.payload_bytes({**payload, "padding": ""})) + overshoot)
    monkeypatch.setattr(roles, "build_payload", lambda *a, **k: payload)
    envelope = D8OutputRequest(SimpleNamespace(model_request_id="id"), {"stage": "inner_plan"}, "", None)
    capacity = roles.EstimatedCapacity(role, role)
    assert capacity.fits(envelope, policy) is (overshoot <= 0)
    assert capacity.last_measurement["input_tokens"] is None
    assert capacity.last_measurement["estimated_input_tokens"] == len(roles.payload_bytes(payload)) + 4096
    assert capacity.last_measurement["output_reserve_tokens"] == roles.SCNET_V41_MAX_OUTPUT
    assert capacity.last_measurement["requested_output_tokens"] == (32768 if explicit else None)
    assert ("max_tokens" in payload) is explicit


def test_previously_paused_request_size_can_pass_without_dropping_context(monkeypatch):
    role = replace(roles.role_config("inner_action"), model="DeepSeek-V4.1-Flash")
    monkeypatch.setitem(roles.TRANSPORT, "base_url", "https://api.scnet.cn/api/llm/v1")
    payload = dict(model=role.model, messages=[dict(role="system", content="rules"), dict(role="user", content="{}")], max_tokens=32768)
    payload["padding"] = "x" * (246904 - len(roles.payload_bytes({**payload, "padding": ""})))
    monkeypatch.setattr(roles, "build_payload", lambda *a, **k: payload)
    env = D8OutputRequest(SimpleNamespace(), {"stage": "inner_plan"}, "", None)
    capacity = roles.EstimatedCapacity(role, role)
    assert not capacity.fits(env, roles.CAPACITY_POLICY)
    assert capacity.fits(env, roles.SCNET_V41_CAPACITY)
    assert len(roles.payload_bytes(payload)) == 246904
    payload["max_tokens"] = 384001
    with pytest.raises(ValueError, match="capacity_binding"):
        capacity.fits(env, roles.SCNET_V41_CAPACITY)
