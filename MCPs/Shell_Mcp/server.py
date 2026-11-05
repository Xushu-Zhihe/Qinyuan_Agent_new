from __future__ import annotations

from typing import Any

from MCPs.fastmcp_compat import FastMCP
from MCPs.analysis_common import validate_sandbox_path


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
def run_bash(script: str | None = None, timeout_seconds: int = 60, script_path: str | None = None) -> dict[str, Any]:
    """Run inline Bash or reuse a sandbox script_path; provide exactly one."""

    arguments, metadata = _script_arguments(script, script_path, powershell=False)
    timeout = _validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "bash",
            "arguments": arguments,
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "bash",
            **metadata,
        },
    }


@mcp.tool(title="Run PowerShell In Sandbox")
def run_powershell(script: str | None = None, timeout_seconds: int = 60, script_path: str | None = None) -> dict[str, Any]:
    """Run inline PowerShell or reuse a sandbox script_path; provide exactly one."""

    arguments, metadata = _script_arguments(script, script_path, powershell=True)
    timeout = _validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "pwsh",
            "arguments": arguments,
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "powershell",
            **metadata,
        },
    }


def _script_arguments(script, script_path, *, powershell):
    if (script is None) == (script_path is None):
        raise ValueError("tool_input_invalid: provide exactly one of script or script_path")
    if script_path is not None:
        path = validate_sandbox_path(script_path, label="script_path")
        prefix = ["-NoLogo", "-NoProfile", "-NonInteractive", "-File"] if powershell else ["--noprofile", "--norc"]
        return [*prefix, path], {"script_path": path}
    source = _validate_script(script)
    prefix = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] if powershell else ["--noprofile", "--norc", "-lc"]
    return [*prefix, source], {"script_bytes": len(source.encode("utf-8"))}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
