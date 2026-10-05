"""Immutable exact-asset authorization and trusted-action impact checks."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

from agent_core.cross_run_artifact_contracts import D6ArtifactReference

D6Impact = Literal["low_impact", "state_change", "destructive"]


@dataclass(frozen=True, slots=True)
class D6ActionDeclaration:
    """Trusted adapter declaration used by the D6 policy boundary."""

    action_id: str
    impact: D6Impact
    uses_credentials: bool
    authentication: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id_invalid")
        if self.impact not in {"low_impact", "state_change", "destructive"}:
            raise ValueError("impact_invalid")
        if type(self.uses_credentials) is not bool or type(self.authentication) is not bool:
            raise ValueError("action_classification_invalid")


def _uuid4(value: object, code: str) -> None:
    if not isinstance(value, str):
        raise TypeError(code)
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(code)


def _action_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[a-z][a-z0-9_.-]*", value) is None
    ):
        raise ValueError("d6_action_id_invalid")


def _host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("d6_network_host_invalid")
    if any(char.isspace() for char in value) or any(
        char in value for char in ("/", "\\", "*", "%", "@", "[", "]")
    ):
        raise ValueError("d6_network_host_invalid")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    labels = value.split(".")
    if any(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        is None
        for label in labels
    ):
        raise ValueError("d6_network_host_invalid")
    if len(labels) == 4 and all(label.isdigit() for label in labels):
        raise ValueError("d6_network_host_invalid")
    return value.lower()


@dataclass(frozen=True, slots=True)
class D6NetworkAsset:
    """One exact host, protocol and explicitly declared port."""

    host: str
    protocol: Literal["http", "https", "tcp"]
    port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _host(self.host))
        if self.protocol not in {"http", "https", "tcp"}:
            raise ValueError("d6_network_protocol_invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("d6_network_port_invalid")


D6Asset = D6ArtifactReference | D6NetworkAsset


@dataclass(frozen=True, slots=True)
class D6Authorization:
    """A TaskRun-owned snapshot; changes require a new TaskRun."""

    authorization_id: str
    task_run_id: str
    local_artifacts: tuple[D6ArtifactReference, ...] = ()
    network_assets: tuple[D6NetworkAsset, ...] = ()
    state_change_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _uuid4(self.authorization_id, "d6_authorization_id_invalid")
        _uuid4(self.task_run_id, "d6_authorization_task_run_invalid")
        for values, expected, code in (
            (self.local_artifacts, D6ArtifactReference, "d6_local_assets_invalid"),
            (self.network_assets, D6NetworkAsset, "d6_network_assets_invalid"),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, expected) for value in values
            ):
                raise ValueError(code)
            if len(set(values)) != len(values):
                raise ValueError(code)
        if not isinstance(self.state_change_actions, tuple):
            raise TypeError("d6_state_change_actions_invalid")
        for action in self.state_change_actions:
            _action_id(action)
        if len(set(self.state_change_actions)) != len(self.state_change_actions):
            raise ValueError("d6_state_change_actions_invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "task_run_id": self.task_run_id,
            "local_artifacts": [asdict(item) for item in self.local_artifacts],
            "network_assets": [asdict(item) for item in self.network_assets],
            "state_change_actions": list(self.state_change_actions),
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> D6Authorization:
        if not isinstance(value, dict) or set(value) != {
            "authorization_id",
            "task_run_id",
            "local_artifacts",
            "network_assets",
            "state_change_actions",
        }:
            raise ValueError("d6_authorization_invalid")
        try:
            local = value["local_artifacts"]
            network = value["network_assets"]
            actions = value["state_change_actions"]
            if not all(isinstance(items, list) for items in (local, network, actions)):
                raise ValueError("d6_authorization_invalid")
            if any(
                not isinstance(item, dict)
                or set(item) != {"source_task_run_id", "object_version_id", "sha256"}
                for item in local
            ) or any(
                not isinstance(item, dict)
                or set(item) != {"host", "protocol", "port"}
                for item in network
            ):
                raise ValueError("d6_authorization_invalid")
            return cls(
                value["authorization_id"],
                value["task_run_id"],
                tuple(D6ArtifactReference(**item) for item in local),
                tuple(D6NetworkAsset(**item) for item in network),
                tuple(actions),
            )
        except (TypeError, ValueError):
            raise ValueError("d6_authorization_invalid") from None

    def verify_frozen(self, expected: D6Authorization) -> None:
        if not isinstance(expected, D6Authorization) or self != expected:
            raise ValueError("d6_authorization_changed")


@dataclass(frozen=True, slots=True)
class D6TrustedAction:
    """A declaration registered by trusted adapters, never from model output."""

    action_id: str
    impact: D6Impact
    uses_credentials: bool
    authentication: bool

    def __post_init__(self) -> None:
        _action_id(self.action_id)
        if self.impact not in {"low_impact", "state_change", "destructive"}:
            raise ValueError("d6_impact_invalid")
        if (
            type(self.uses_credentials) is not bool
            or type(self.authentication) is not bool
        ):
            raise ValueError("d6_action_declaration_invalid")


DEFAULT_ACTION_DECLARATIONS = (
    D6TrustedAction("fixed_size", "low_impact", False, False),
    D6TrustedAction("fixed_sha256", "low_impact", False, False),
)


@dataclass(frozen=True, slots=True)
class D6ApprovedAction:
    action_id: str
    task_run_id: str
    authorization_id: str
    authorization_sha256: str
    asset: D6Asset
    impact: D6Impact


class D6Policy:
    """Check exact asset membership and registered impact before side effects."""

    def __init__(
        self,
        authorization: D6Authorization,
        declarations: tuple[D6TrustedAction | D6ActionDeclaration, ...] = (
            DEFAULT_ACTION_DECLARATIONS
        ),
    ) -> None:
        if not isinstance(authorization, D6Authorization):
            raise TypeError("d6_authorization_invalid")
        if not isinstance(declarations, tuple):
            raise TypeError("d6_action_declarations_invalid")
        registry: dict[str, D6TrustedAction] = {}
        for declaration in declarations:
            if not isinstance(declaration, (D6TrustedAction, D6ActionDeclaration)):
                raise TypeError("d6_action_declaration_invalid")
            trusted = (
                declaration
                if isinstance(declaration, D6TrustedAction)
                else D6TrustedAction(
                    declaration.action_id,
                    declaration.impact,
                    declaration.uses_credentials,
                    declaration.authentication,
                )
            )
            if trusted.action_id in registry:
                raise ValueError("d6_action_declaration_duplicate")
            registry[trusted.action_id] = trusted
        self._authorization = authorization
        self._declarations = registry

    def authorize(
        self, action_id: str, task_run_id: str, asset: D6Asset
    ) -> D6ApprovedAction:
        if task_run_id != self._authorization.task_run_id:
            raise ValueError("d6_authorization_task_mismatch")
        _action_id(action_id)
        declaration = self._declarations.get(action_id)
        if declaration is None:
            raise ValueError("d6_action_unknown")
        if declaration.authentication or declaration.uses_credentials:
            raise ValueError("d6_authentication_forbidden")
        if declaration.impact == "destructive":
            raise ValueError("d6_destructive_forbidden")
        if (
            declaration.impact == "state_change"
            and action_id not in self._authorization.state_change_actions
        ):
            raise ValueError("d6_state_change_not_authorized")
        allowed = (
            self._authorization.local_artifacts
            if isinstance(asset, D6ArtifactReference)
            else self._authorization.network_assets
            if isinstance(asset, D6NetworkAsset)
            else ()
        )
        if (
            not isinstance(asset, (D6ArtifactReference, D6NetworkAsset))
            or asset not in allowed
        ):
            raise ValueError("d6_asset_not_authorized")
        if action_id in {"fixed_size", "fixed_sha256"} and not isinstance(
            asset, D6ArtifactReference
        ):
            raise ValueError("d6_action_asset_mismatch")
        return D6ApprovedAction(
            action_id,
            task_run_id,
            self._authorization.authorization_id,
            self._authorization.sha256,
            asset,
            declaration.impact,
        )
