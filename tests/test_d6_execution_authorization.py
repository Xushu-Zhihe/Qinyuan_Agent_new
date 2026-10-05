"""Fail-closed D6 authorization over trusted adapter declarations."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from agent_core.d4_contracts import D4BudgetPolicy
from agent_core.d6_contracts import D6ActionDeclaration, D6ArtifactReference
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig, d3_payload
from agent_core.execution_authorization import (
    DEFAULT_ACTION_DECLARATIONS,
    D6Authorization,
    D6NetworkAsset,
    D6Policy,
    D6TrustedAction,
)
from agent_core.turn_contracts import D1RoleConfig


def artifact() -> D6ArtifactReference:
    return D6ArtifactReference(str(uuid4()), str(uuid4()), "a" * 64)


def authorization(
    *,
    local: tuple[D6ArtifactReference, ...] = (),
    network: tuple[D6NetworkAsset, ...] = (),
    actions: tuple[str, ...] = (),
) -> D6Authorization:
    return D6Authorization(str(uuid4()), str(uuid4()), local, network, actions)


class FakeActionAdapter:
    """Accepts dispatches without changing any target state."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, action_id: str) -> str:
        self.calls.append(action_id)
        return "fake_accepted"


def dispatch(
    policy: D6Policy,
    adapter: FakeActionAdapter,
    action_id: str,
    frozen: D6Authorization,
    asset: D6ArtifactReference | D6NetworkAsset,
) -> str:
    policy.authorize(action_id, frozen.task_run_id, asset)
    return adapter.execute(action_id)


@pytest.mark.parametrize("action_id", ["fixed_size", "fixed_sha256"])
def test_fixed_low_impact_actions_accept_only_exact_local_versions(action_id):
    source = artifact()
    frozen = authorization(local=(source,))
    approved = D6Policy(frozen).authorize(action_id, frozen.task_run_id, source)

    assert approved.action_id == action_id
    assert approved.asset == source
    assert approved.impact == "low_impact"
    assert approved.authorization_id == frozen.authorization_id
    assert approved.authorization_sha256 == frozen.sha256
    with pytest.raises(FrozenInstanceError):
        approved.impact = "state_change"


@pytest.mark.parametrize("action_id", ["fixed_size", "fixed_sha256"])
def test_fixed_local_actions_cannot_operate_on_authorized_network_assets(action_id):
    network = D6NetworkAsset("example.test", "https", 443)
    frozen = authorization(network=(network,))
    adapter = FakeActionAdapter()

    with pytest.raises(ValueError, match="^d6_action_asset_mismatch$"):
        dispatch(D6Policy(frozen), adapter, action_id, frozen, network)
    assert adapter.calls == []


@pytest.mark.parametrize("changed", ["source_task_run_id", "object_version_id", "sha256"])
def test_local_authorization_matches_each_identity_component(changed):
    source = artifact()
    frozen = authorization(local=(source,))
    other = replace(source, **{changed: "b" * 64 if changed == "sha256" else str(uuid4())})

    with pytest.raises(ValueError, match="^d6_asset_not_authorized$"):
        D6Policy(frozen).authorize("fixed_sha256", frozen.task_run_id, other)


def test_task_run_mismatch_refuses_before_adapter_dispatch():
    source = artifact()
    frozen = authorization(local=(source,))
    adapter = FakeActionAdapter()

    with pytest.raises(ValueError, match="^d6_authorization_task_mismatch$"):
        D6Policy(frozen).authorize("fixed_size", str(uuid4()), source)
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("action_id", "declaration", "authorized", "reason"),
    [
        ("change", D6TrustedAction("change", "state_change", False, False), (), "d6_state_change_not_authorized"),
        ("change", D6TrustedAction("change", "state_change", False, False), ("other",), "d6_state_change_not_authorized"),
        ("destroy", D6TrustedAction("destroy", "destructive", False, False), ("destroy",), "d6_destructive_forbidden"),
        ("login", D6TrustedAction("login", "low_impact", False, True), ("login",), "d6_authentication_forbidden"),
        ("credential_probe", D6TrustedAction("credential_probe", "low_impact", True, False), ("credential_probe",), "d6_authentication_forbidden"),
        ("unknown", D6TrustedAction("read", "low_impact", False, False), ("unknown",), "d6_action_unknown"),
    ],
)
def test_rejected_action_has_observable_reason_and_never_calls_fake_adapter(
    action_id, declaration, authorized, reason,
):
    source = artifact()
    frozen = authorization(local=(source,), actions=authorized)
    adapter = FakeActionAdapter()
    policy = D6Policy(frozen, (declaration,))

    with pytest.raises(ValueError, match=f"^{reason}$"):
        dispatch(policy, adapter, action_id, frozen, source)
    assert adapter.calls == []


def test_named_state_change_can_reach_fake_adapter_without_target_side_effects():
    source = artifact()
    frozen = authorization(local=(source,), actions=("test_change",))
    adapter = FakeActionAdapter()
    policy = D6Policy(frozen, (D6TrustedAction("test_change", "state_change", False, False),))

    assert dispatch(policy, adapter, "test_change", frozen, source) == "fake_accepted"
    assert adapter.calls == ["test_change"]


def test_unknown_impact_and_model_supplied_declaration_fail_closed():
    frozen = authorization()

    with pytest.raises(ValueError, match="^d6_impact_invalid$"):
        D6TrustedAction("probe", "unknown", False, False)
    with pytest.raises(TypeError):
        D6TrustedAction("unclassified", "low_impact")
    with pytest.raises(TypeError, match="^d6_action_declaration_invalid$"):
        D6Policy(frozen, ({"action_id": "probe", "impact": "low_impact"},))
    with pytest.raises(ValueError, match="^d6_action_declaration_duplicate$"):
        D6Policy(frozen, DEFAULT_ACTION_DECLARATIONS + DEFAULT_ACTION_DECLARATIONS)


def test_contract_declarations_are_registered_only_through_trusted_constructor():
    source = artifact()
    frozen = authorization(local=(source,))
    policy = D6Policy(frozen, (D6ActionDeclaration("inspect", "low_impact", False, False),))

    assert policy.authorize("inspect", frozen.task_run_id, source).impact == "low_impact"


def test_contract_declaration_cannot_misclassify_credential_use_as_low_impact():
    source = artifact()
    frozen = authorization(local=(source,))
    policy = D6Policy(
        frozen,
        (D6ActionDeclaration("credential_login", "low_impact", True, True),),
    )

    with pytest.raises(ValueError, match="^d6_authentication_forbidden$"):
        policy.authorize("credential_login", frozen.task_run_id, source)


@pytest.mark.parametrize(
    "other",
    [
        D6NetworkAsset("child.example.test", "https", 443),
        D6NetworkAsset("other.test", "https", 443),
        D6NetworkAsset("example.test", "http", 443),
        D6NetworkAsset("example.test", "https", 444),
        D6NetworkAsset("203.0.113.1", "https", 443),
    ],
)
def test_network_authorization_does_not_expand_host_protocol_or_port(other):
    exact = D6NetworkAsset("example.test", "https", 443)
    frozen = authorization(network=(exact,))
    policy = D6Policy(frozen, (D6TrustedAction("observe_network", "low_impact", False, False),))

    with pytest.raises(ValueError, match="^d6_asset_not_authorized$"):
        policy.authorize("observe_network", frozen.task_run_id, other)


@pytest.mark.parametrize(
    ("host", "canonical"),
    [("ExAmPlE.TEST", "example.test"), ("203.0.113.1", "203.0.113.1"), ("2001:0db8:0:0::1", "2001:db8::1")],
)
def test_explicit_network_host_is_canonical_without_resolution(host, canonical):
    exact = D6NetworkAsset(host, "tcp", 8080)
    frozen = authorization(network=(exact,))
    policy = D6Policy(frozen, (D6TrustedAction("observe_network", "low_impact", False, False),))

    assert exact.host == canonical
    assert policy.authorize("observe_network", frozen.task_run_id, exact).asset == exact


@pytest.mark.parametrize(
    "host",
    ["", "*.test", "example.test/next", "example.test.", "example.test:443", "203.0.113.0/24", "203.0.113.999", "[::1]", "fe80::1%eth0", "user@example.test", " bad.test", "bad-.test", "-bad.test", "bad..test", "bad\\test"],
)
def test_network_host_rejects_implicit_ranges_urls_and_ambiguous_hosts(host):
    with pytest.raises(ValueError, match="^d6_network_host_invalid$"):
        D6NetworkAsset(host, "https", 443)


@pytest.mark.parametrize("port", [0, 65536, True, "443", None])
def test_network_requires_explicit_integer_port(port):
    with pytest.raises(ValueError, match="^d6_network_port_invalid$"):
        D6NetworkAsset("example.test", "https", port)


@pytest.mark.parametrize("protocol", ["ssh", "ftp", "HTTPS", ""])
def test_network_rejects_undeclared_protocol(protocol):
    with pytest.raises(ValueError, match="^d6_network_protocol_invalid$"):
        D6NetworkAsset("example.test", protocol, 443)


def test_authorization_serialization_and_frozen_comparison_are_exact():
    frozen = authorization(
        local=(artifact(),),
        network=(D6NetworkAsset("example.test", "https", 443),),
        actions=("test_change",),
    )
    restored = D6Authorization.from_dict(json.loads(json.dumps(frozen.to_payload())))

    assert restored == frozen
    assert restored.sha256 == frozen.sha256
    frozen.verify_frozen(restored)
    changed = replace(frozen, network_assets=(D6NetworkAsset("example.test", "https", 444),))
    assert changed.sha256 != frozen.sha256
    with pytest.raises(ValueError, match="^d6_authorization_changed$"):
        restored.verify_frozen(changed)
    with pytest.raises(FrozenInstanceError):
        frozen.task_run_id = str(uuid4())


@pytest.mark.parametrize("field", ["authorization_id", "task_run_id"])
def test_authorization_requires_canonical_task_and_authorization_ids(field):
    frozen = authorization()
    with pytest.raises(ValueError):
        replace(frozen, **{field: "not-an-id"})


def test_authorization_rejects_mutable_duplicate_and_untyped_assets():
    source = artifact()
    network = D6NetworkAsset("example.test", "tcp", 80)
    frozen = authorization()

    for changes in (
        {"local_artifacts": [source]},
        {"local_artifacts": (source, source)},
        {"local_artifacts": (network,)},
        {"network_assets": (network, network)},
        {"network_assets": (source,)},
        {"state_change_actions": ["change"]},
        {"state_change_actions": ("change", "change")},
        {"state_change_actions": ("*",)},
    ):
        with pytest.raises((TypeError, ValueError)):
            replace(frozen, **changes)


@pytest.mark.parametrize("mutation", ["missing", "extra", "asset_extra", "asset_missing", "tuple", "credential_fields"])
def test_authorization_persisted_payload_rejects_unfrozen_extensions(mutation):
    frozen = authorization(local=(artifact(),), network=(D6NetworkAsset("example.test", "tcp", 80),))
    payload = frozen.to_payload()
    if mutation == "missing":
        payload.pop("task_run_id")
    elif mutation == "extra":
        payload["wildcard"] = True
    elif mutation == "asset_extra":
        payload["network_assets"][0]["redirects_allowed"] = True
    elif mutation == "asset_missing":
        payload["network_assets"][0].pop("port")
    elif mutation == "tuple":
        payload["local_artifacts"] = frozen.local_artifacts
    else:
        payload["credentials"] = []

    with pytest.raises(ValueError, match="^d6_authorization_invalid$"):
        D6Authorization.from_dict(payload)


def run_config(stage: int, frozen: D6Authorization | None = None) -> D3RunConfig:
    outer = D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1")
    return D3RunConfig(
        outer,
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D3Authorization(str(uuid4()), (0,)),
        outer.role_config_ref,
        d4_budget=D4BudgetPolicy() if stage >= 4 else None,
        d5_enabled=stage >= 5,
        d5_sensitive_requirements=() if stage >= 5 else None,
        d6_authorization=frozen,
    )


@pytest.mark.parametrize("stage", [3, 4, 5])
def test_legacy_d3_to_d5_payload_round_trips_without_d6_fields(stage):
    config = run_config(stage)
    payload = json.loads(json.dumps(d3_payload(config)))

    assert "d6_authorization" not in payload
    assert D3RunConfig.from_dict(payload) == config
    assert D3RunConfig.from_dict(payload).d6_authorization is None


def test_d6_config_freezes_exact_authorization_in_payload():
    frozen = authorization(local=(artifact(),))
    config = run_config(6, frozen)
    payload = json.loads(json.dumps(d3_payload(config)))
    restored = D3RunConfig.from_dict(payload)

    assert restored == config
    assert restored.d6_authorization == frozen
    restored.d6_authorization.verify_frozen(frozen)


def test_d6_config_rejects_changed_frozen_local_input_count():
    frozen = authorization(local=(artifact(),))
    config = run_config(6, frozen)
    payload = json.loads(json.dumps(d3_payload(config)))
    payload["d6_authorization"]["local_artifacts"] = []

    with pytest.raises(ValueError, match="D6 requires D5 and exact frozen local inputs"):
        D3RunConfig.from_dict(payload)
