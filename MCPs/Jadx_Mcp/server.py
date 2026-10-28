from __future__ import annotations

from typing import Any

from MCPs.analysis_common import SANDBOX_WORK_ROOT, validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-jadx-command-planner",
    instructions=(
        "Prepare JADX decompilation commands for Android files in the current "
        "Agent sandbox. Decompiled sources and resources are written under "
        "/workspace/work or /tmp. This server never executes JADX itself."
    ),
    json_response=True,
)


def _validate_output_directory(value: str) -> str:
    return validate_sandbox_path(value, label="output_dir", writable=True)


@mcp.tool(title="Decompile Android File With JADX")
def decompile_android(
    input_path: str,
    output_dir: str = SANDBOX_WORK_ROOT + "/jadx-output",
    deobfuscate: bool = True,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Decompile an APK, DEX, AAB, AAR, JAR, ZIP, or class file with JADX.

    Read /workspace/input, /workspace/work or /tmp; write only /workspace/work or /tmp.
    Relative paths resolve below /workspace/work.
    """

    normalized_input = validate_sandbox_path(input_path, label="input_path")
    normalized_output = _validate_output_directory(output_dir)
    if normalized_input == normalized_output:
        raise ValueError("input_path and output_dir must be different")
    if not isinstance(deobfuscate, bool):
        raise ValueError("deobfuscate must be a boolean")

    arguments = ["--output-dir", normalized_output]
    if deobfuscate:
        arguments.append("--deobf")
    arguments.append(normalized_input)

    return {
        "sandbox_command": {
            "tool": "jadx",
            "arguments": arguments,
            "timeout_seconds": validate_timeout(timeout_seconds),
        },
        "request": {
            "operation": "android_decompile",
            "input_path": normalized_input,
            "output_dir": normalized_output,
            "deobfuscate": deobfuscate,
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
