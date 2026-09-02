from __future__ import annotations

from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-binutils-command-planner",
    instructions=(
        "Prepare bounded binutils inspection commands for files in the Agent "
        "sandbox. This server never executes binutils itself."
    ),
    json_response=True,
)


_OPERATIONS: dict[str, tuple[str, list[str]]] = {
    "headers": ("readelf", ["--file-header", "--program-headers"]),
    "sections": ("readelf", ["--section-headers", "--wide"]),
    "symbols": ("readelf", ["--symbols", "--wide"]),
    "nm": ("nm", ["--demangle", "--line-numbers"]),
    "disassemble": ("objdump", ["--disassemble", "--no-show-raw-insn", "-M", "intel"]),
    "strings": ("strings", ["--all", "--bytes=4"]),
}


@mcp.tool(title="Inspect ELF With Binutils")
def inspect_binary(
    path: str,
    operation: str = "headers",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    normalized_path = validate_sandbox_path(path)
    normalized_operation = operation.strip().lower()
    try:
        executable, options = _OPERATIONS[normalized_operation]
    except (KeyError, AttributeError) as exc:
        raise ValueError(
            f"operation must be one of {sorted(_OPERATIONS)}"
        ) from exc
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": executable,
            "arguments": [*options, "--", normalized_path],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": normalized_operation,
            "path": normalized_path,
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
