from __future__ import annotations

from typing import Any

from MCPs.analysis_common import (
    needs_stage,
    python_command,
    stage_python_prefix,
    staged_path,
    validate_sandbox_path,
    validate_script,
    validate_timeout,
)
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-pwntools-command-planner",
    instructions=(
        "Prepare a pwntools Python script for execution inside the current "
        "Agent sandbox. When `executable` is set, the planner copies it to an "
        "executable staging path and exports QINYUAN_BIN, so scripts should "
        "use process(os.environ['QINYUAN_BIN']) instead of /workspace/input. "
        "For deterministic addresses use process([...], aslr=False) and then "
        "gdb.attach; gdb.debug() keeps ASLR for gdbserver-launched targets. "
        "For crash triage run the target under gdb (gdb --batch -ex run -ex bt) "
        "because core dumps are disabled. Start long-running services with "
        "qinyuan-exec so a later gdb -p can attach. The MCP server never "
        "executes Python itself."
    ),
    json_response=True,
)


def _stage_prefix(source: str, target: str) -> str:
    if needs_stage(source):
        return stage_python_prefix(source, target)
    return f"import os\nos.environ['QINYUAN_BIN'] = {target!r}\n"


@mcp.tool(title="Run Pwntools Script")
def run_pwntools(
    script: str,
    timeout_seconds: int = 120,
    executable: str | None = None,
) -> dict[str, Any]:
    normalized_script = validate_script(script)
    timeout = validate_timeout(timeout_seconds)
    staged: str | None = None
    prefix = ""
    if executable is not None:
        source = validate_sandbox_path(executable)
        staged = staged_path(source) if needs_stage(source) else source
        prefix = _stage_prefix(source, staged)
    combined = prefix + normalized_script
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(combined),
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "pwntools_python",
            "script_bytes": len(normalized_script.encode("utf-8")),
            "staged_path": staged,
        },
    }


@mcp.tool(title="Stage Executable In Sandbox")
def stage_binary(path: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Copy a read-only/noexec artifact to /workspace/work/.stage and print it."""

    source = validate_sandbox_path(path)
    target = staged_path(source) if needs_stage(source) else source
    timeout = validate_timeout(timeout_seconds)
    script = (
        _stage_prefix(source, target)
        + "import json, os\n"
        + "print(json.dumps({'path': os.environ['QINYUAN_BIN']}))\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "stage_binary", "path": source, "staged_path": target},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
