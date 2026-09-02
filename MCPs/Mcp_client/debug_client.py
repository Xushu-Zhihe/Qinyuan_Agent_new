from __future__ import annotations

import asyncio
import json

from MCPs.Mcp_client import build_mcp_runtime
from Sandbox.docker_runtime import DockerRuntime
from Sandbox.service import SandboxService


AGENT_ID = "nmap-debug-agent"


def create_sandbox() -> tuple[SandboxService, str]:
    """
    在 asyncio 事件循环之外创建 Docker 客户端和沙盒。
    """

    runtime = DockerRuntime()
    sandbox_service = SandboxService(runtime)
    session = sandbox_service.start_for_agent(AGENT_ID)

    return sandbox_service, session.sandbox_id


async def run_mcp_test(
    sandbox_service: SandboxService,
) -> None:
    manager = None

    try:
        manager = await build_mcp_runtime(sandbox_service)

        client = manager.bind_agent(
            AGENT_ID,
            allowed_tools={"nmap.run_nmap_tcp_scan"},
        )

        result = await client.call_tool(
            "nmap.run_nmap_tcp_scan",
            {
                "target": "127.0.0.1",
                "ports": "80,443",
                "timeout_seconds": 30,
            },
        )

        print(
            json.dumps(
                result.to_tool_payload(),
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        if manager is not None:
            await manager.stop_all()


def main() -> None:
    sandbox_service, sandbox_id = create_sandbox()
    print(f"Agent sandbox started: {sandbox_id}")

    try:
        asyncio.run(run_mcp_test(sandbox_service))
    finally:
        # Docker SDK 清理也放在事件循环之外。
        sandbox_service.close()


if __name__ == "__main__":
    main()