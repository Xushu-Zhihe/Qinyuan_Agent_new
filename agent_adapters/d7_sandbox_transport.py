"""Trusted D7 HTTP transport backed by the TaskRun's isolated Sandbox."""

from __future__ import annotations

from agent_adapters.d7_http_adapter import (
    MAX_D7_HTTP_RESPONSE_BYTES,
    D7HttpError,
    D7HttpOrigin,
    D7HttpWireResponse,
)
from agent_core.control_store import TargetNetworkBinding
from agent_core.d7_web_contracts import D7WebRunConfig
from Sandbox.task_run_sandbox import TaskRunSandboxError


class D7SandboxHttpTransport:
    """Capture one persisted configuration and dispatch only through its Sandbox."""

    def __init__(
        self,
        sandbox,
        *,
        sandbox_id: str,
        run_config: D7WebRunConfig,
        network_binding: TargetNetworkBinding,
        target_network,
    ) -> None:
        if not isinstance(run_config, D7WebRunConfig):
            raise D7HttpError("d7_http_run_config_invalid")
        if not isinstance(network_binding, TargetNetworkBinding):
            raise D7HttpError("d7_http_target_network_binding_invalid")
        if (
            network_binding.target_url != run_config.endpoint
            or sandbox is None
            or not callable(getattr(sandbox, "execute_d7_http", None))
            or not callable(getattr(target_network, "bind", None))
        ):
            raise D7HttpError("d7_http_transport_binding_invalid")
        self.sandbox = sandbox
        self.sandbox_id = sandbox_id
        self.run_config = run_config
        self.authorization = run_config.authorization
        self.network_binding = network_binding
        self.target_network = target_network

    def send(
        self,
        *,
        origin: D7HttpOrigin,
        method: str,
        path_query: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> D7HttpWireResponse:
        if (
            not isinstance(origin, D7HttpOrigin)
            or origin != D7HttpOrigin.parse(self.run_config.endpoint)
            or max_response_bytes != MAX_D7_HTTP_RESPONSE_BYTES
        ):
            raise D7HttpError("d7_http_transport_origin_or_limit_mismatch")
        try:
            response = self.sandbox.execute_d7_http(
                self.sandbox_id,
                task_run_id=self.run_config.task_run_id,
                run_config=self.run_config,
                authorization=self.authorization,
                network_binding=self.network_binding,
                target_network=self.target_network,
                method=method,
                path_query=path_query,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            )
        except TaskRunSandboxError as exc:
            if exc.outcome == "outcome_unknown":
                raise D7HttpError("outcome_unknown") from exc
            raise D7HttpError("d7_http_dispatch_rejected") from exc
        except (TypeError, ValueError) as exc:
            raise D7HttpError("d7_http_dispatch_rejected") from exc
        except D7HttpError:
            raise
        except Exception as exc:
            raise D7HttpError("outcome_unknown") from exc
        return D7HttpWireResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
        )


__all__ = ["D7SandboxHttpTransport"]
