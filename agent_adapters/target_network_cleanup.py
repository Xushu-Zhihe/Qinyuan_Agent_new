"""Durable confirmation that a TaskRun target network was removed."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agent_adapters.run_files import immutable_write
from agent_core.control_store import TargetNetworkBinding


def target_network_cleanup_receipt(
    task_run_id: str, binding: TargetNetworkBinding
) -> dict[str, str]:
    return {
        "status": "succeeded",
        "task_run_id": task_run_id,
        "private_network_id": binding.private_network_id,
        "egress_network_id": binding.egress_network_id,
        "forwarder_id": binding.forwarder_id,
        "configuration_sha256": binding.configuration_sha256,
    }


class RecordedTargetNetwork:
    """Write a receipt only after the bound forwarder and networks are gone."""

    def __init__(self, network, directory: Path) -> None:
        self.network = network
        self.directory = directory

    def create(self, task_run_id: str, target_url: str) -> TargetNetworkBinding:
        try:
            return self.network.create(task_run_id, target_url)
        except Exception as exc:
            self._failure("create", task_run_id, exc)
            raise

    def _failure(self, phase, task_run_id, error):
        from agent_adapters.output_evidence import safe_failure_details
        from qinyuan_core import get_logger
        details = {"phase": phase, "task_run_id": task_run_id,
                   "status": "unconfirmed", "outcome": getattr(error, "outcome", None),
                   "resource_ids": list(getattr(error, "resource_ids", ())),
                   **safe_failure_details(error, include_chain=True)}
        get_logger("exec").error("target_network.failed", run=task_run_id,
            phase=phase, code=getattr(error, "code", "docker_resource_outcome_unknown"))
        immutable_write(self.directory / f"target-network-{phase}-failure-{uuid4()}.json", details)

    def cleanup_uncommitted(self, task_run_id):
        try:
            self.network.cleanup_uncommitted(task_run_id)
            immutable_write(self.directory / "target-network-uncommitted-cleanup.json",
                            {"task_run_id": task_run_id, "status": "succeeded"})
        except Exception as exc:
            self._failure("uncommitted-cleanup", task_run_id, exc)
            raise

    def bind(
        self, task_run_id: str, binding: TargetNetworkBinding, *, terminal: bool
    ) -> None:
        self.network.bind(task_run_id, binding, terminal=terminal)

    def destroy(self, task_run_id: str, binding: TargetNetworkBinding) -> None:
        from qinyuan_core import get_logger
        log = get_logger("exec")
        log.info("target_network.cleanup_started", run=task_run_id)
        try:
            self.network.destroy(task_run_id, binding)
        except Exception as exc:
            self._failure("cleanup", task_run_id, exc)
            raise
        immutable_write(
            self.directory / "target-network-cleanup.json",
            target_network_cleanup_receipt(task_run_id, binding),
        )
        log.info("target_network.cleanup_completed", run=task_run_id)
