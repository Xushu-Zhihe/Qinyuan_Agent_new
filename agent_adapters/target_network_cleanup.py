"""Durable confirmation that a TaskRun target network was removed."""

from __future__ import annotations

from pathlib import Path

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
        return self.network.create(task_run_id, target_url)

    def bind(
        self, task_run_id: str, binding: TargetNetworkBinding, *, terminal: bool
    ) -> None:
        self.network.bind(task_run_id, binding, terminal=terminal)

    def destroy(self, task_run_id: str, binding: TargetNetworkBinding) -> None:
        self.network.destroy(task_run_id, binding)
        immutable_write(
            self.directory / "target-network-cleanup.json",
            target_network_cleanup_receipt(task_run_id, binding),
        )
