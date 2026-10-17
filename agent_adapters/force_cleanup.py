"""Force cleanup utility for stuck tasks - bypass confirmations and clean up everything."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_adapters.workbench import Workbench

import docker


def force_cleanup_task(workbench: Workbench, challenge_id: str) -> dict:
    """
    Force cleanup a task completely, bypassing all confirmations.

    This will:
    1. Stop any running containers (task + forwarder)
    2. Remove Docker networks
    3. Delete the run directory
    4. Remove database entries

    Returns status dict with details of what was cleaned up.
    """
    cleanup_result = {
        "challenge_id": challenge_id,
        "containers_stopped": [],
        "networks_removed": [],
        "directory_deleted": False,
        "database_cleaned": False,
        "errors": []
    }

    # Get run information
    with workbench.lock:
        runs = list(workbench._db().execute(
            "SELECT run_id FROM runs WHERE challenge_id=?", (challenge_id,)
        ))

    if not runs:
        cleanup_result["errors"].append("No runs found for challenge")
        return cleanup_result

    client = docker.from_env()

    for row in runs:
        run_id = row["run_id"]

        # Find and stop containers
        try:
            # Get all agent-state directories for this run
            run_path = workbench.root / "runs" / run_id
            if run_path.exists():
                agent_dirs = list(run_path.glob("agent-state/*"))
                agent_dirs = [d for d in agent_dirs if d.is_dir() and d.name != "requests"]

                for agent_dir in agent_dirs:
                    task_run_id = agent_dir.name

                    # Stop task container
                    try:
                        container = client.containers.get(f"qinyuan-task-{task_run_id}")
                        container.stop(timeout=1)
                        container.remove(force=True)
                        cleanup_result["containers_stopped"].append(f"qinyuan-task-{task_run_id}")
                    except docker.errors.NotFound:
                        pass
                    except Exception as e:
                        cleanup_result["errors"].append(f"Error stopping task container: {e}")

                    # Stop forwarder container
                    try:
                        forwarder = client.containers.get(f"qinyuan-t6-forwarder-{task_run_id}")
                        forwarder.stop(timeout=1)
                        forwarder.remove(force=True)
                        cleanup_result["containers_stopped"].append(f"qinyuan-t6-forwarder-{task_run_id}")
                    except docker.errors.NotFound:
                        pass
                    except Exception as e:
                        cleanup_result["errors"].append(f"Error stopping forwarder: {e}")

                    # Remove networks
                    for network_type in ["in", "out"]:
                        network_name = f"qinyuan-t6-{network_type}-{task_run_id}"
                        try:
                            network = client.networks.get(network_name)
                            network.remove()
                            cleanup_result["networks_removed"].append(network_name)
                        except docker.errors.NotFound:
                            pass
                        except Exception as e:
                            cleanup_result["errors"].append(f"Error removing network {network_name}: {e}")

                # Delete run directory
                try:
                    shutil.rmtree(run_path)
                    cleanup_result["directory_deleted"] = True
                except Exception as e:
                    cleanup_result["errors"].append(f"Error deleting directory: {e}")

        except Exception as e:
            cleanup_result["errors"].append(f"Error processing run {run_id}: {e}")

    # Clean up database
    try:
        with workbench.lock, workbench._db() as db:
            db.execute("DELETE FROM runs WHERE challenge_id=?", (challenge_id,))
            cleanup_result["database_cleaned"] = True
    except Exception as e:
        cleanup_result["errors"].append(f"Error cleaning database: {e}")

    return cleanup_result


def batch_force_cleanup(workbench: Workbench, challenge_ids: list[str]) -> list[dict]:
    """Force cleanup multiple tasks at once."""
    results = []
    for challenge_id in challenge_ids:
        result = force_cleanup_task(workbench, challenge_id)
        results.append(result)
    return results
