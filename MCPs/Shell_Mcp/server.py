from __future__ import annotations

from typing import Any

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-sandbox-shell-planner",
    instructions=(
        "Prepare Bash or PowerShell scripts for execution inside the current "
        "Agent sandbox. This server never executes commands on the host."
    ),
    json_response=True,
)


def _validate_script(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("script must be a string")
    script = value.strip()
    if not script:
        raise ValueError("script must not be empty")
    if "\0" in script:
        raise ValueError("script must not contain null bytes")
    if len(script.encode("utf-8")) > 64 * 1024:
        raise ValueError("script must not exceed 64 KiB")
    return script


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


@mcp.tool(title="Run Bash In Sandbox")
def run_bash(script: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a Bash script in the Agent sandbox working directory."""

    normalized_script = _validate_script(script)
    timeout = _validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "bash",
            "arguments": ["--noprofile", "--norc", "-lc", normalized_script],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "bash",
            "script_bytes": len(normalized_script.encode("utf-8")),
        },
    }


@mcp.tool(title="Run PowerShell In Sandbox")
def run_powershell(script: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a PowerShell script in the Agent sandbox working directory."""

    normalized_script = _validate_script(script)
    timeout = _validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "pwsh",
            "arguments": [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                normalized_script,
            ],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "powershell",
            "script_bytes": len(normalized_script.encode("utf-8")),
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
