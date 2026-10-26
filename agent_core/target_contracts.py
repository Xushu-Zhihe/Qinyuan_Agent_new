"""Exact TCP target grants and Turn provenance; HTTP keeps its own semantics."""
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from agent_core.execution_authorization import D6NetworkAsset


def canonical_tcp_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("tcp.endpoint_invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("tcp.endpoint_invalid")
        asset = D6NetworkAsset(parsed.hostname, "tcp", parsed.port)
    except (TypeError, ValueError):
        raise ValueError("tcp.endpoint_invalid") from None
    host = "[" + asset.host + "]" if ":" in asset.host else asset.host
    return f"tcp://{host}:{asset.port}"


@dataclass(frozen=True, slots=True)
class TcpAuthorization:
    authorization_id: str
    allowed_endpoint: str
    allowed_tool: str = "sandbox_tcp"
    network_allowed: bool = True

    def __post_init__(self):
        from agent_core.dual_llm_contracts import _uuid
        _uuid(self.authorization_id, "authorization_id")
        if (self.allowed_endpoint != canonical_tcp_endpoint(self.allowed_endpoint)
                or self.allowed_tool != "sandbox_tcp" or self.network_allowed is not True):
            raise ValueError("tcp.authorization_invalid")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"authorization_id", "allowed_endpoint", "allowed_tool", "network_allowed"}:
            raise ValueError("tcp.authorization_fields_invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class TcpTurnBinding:
    binding_id: str
    task_run_id: str
    turn_id: str
    endpoint: str
    authorization_id: str
    proposal_request_id: str
    kind: str = "tcp_target"

    def __post_init__(self):
        from agent_core.dual_llm_contracts import _uuid
        for field in ("binding_id", "task_run_id", "turn_id", "authorization_id", "proposal_request_id"):
            _uuid(getattr(self, field), field)
        if self.endpoint != canonical_tcp_endpoint(self.endpoint) or self.kind != "tcp_target":
            raise ValueError("tcp.turn_binding_invalid")


def turn_binding(app, turn_id, request_id):
    authorization = app.config.tcp_authorization
    return TcpTurnBinding(app._id("binding/" + turn_id), app.state.task_run.task_run_id,
                          turn_id, authorization.allowed_endpoint, authorization.authorization_id, request_id)


def valid_turn(config, binding):
    return (isinstance(binding, TcpTurnBinding) and config.tcp_authorization is not None
            and binding.endpoint == config.tcp_authorization.allowed_endpoint
            and binding.authorization_id == config.tcp_authorization.authorization_id)


def policy(app):
    from agent_core.turn_modeling import digest
    authorization = app.config.tcp_authorization
    attachments = []
    for index in app.config.authorization.attachment_indices:
        receipt = app.imports.read_receipt(app._id(f"import/{index}"))
        if receipt is None:
            from agent_core.dual_llm_contracts import D3RecoveryRequired
            raise D3RecoveryRequired("tcp.attachment_source_missing")
        attachments.append({"attachment_index": index, "import_id": receipt.import_id,
                            "path": receipt.sandbox_path, "sha256": receipt.sha256,
                            "byte_size": receipt.byte_size})
    return {"endpoint": authorization.allowed_endpoint, "protocol": "tcp",
            "authorization_sha256": digest(asdict(authorization)),
            "sandbox_host": "target.qinyuan", "port": urlsplit(authorization.allowed_endpoint).port,
            "attachments": attachments}
