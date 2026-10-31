from __future__ import annotations

import re
import shlex
from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP
from MCPs.Session_Mcp.server import session_capture, session_send, session_stop


mcp = FastMCP(
    "qinyuan-kernel-command-planner",
    instructions=(
        "Prepare QEMU system-emulation plans for Linux kernel challenges in "
        "the current Agent sandbox. Boot a kernel into a tmux session, attach "
        "gdb-multiarch to the QEMU gdbstub, and drive the guest console with "
        "session_send/session_capture. KVM is never used; everything runs under "
        "TCG with no capabilities. This server never executes QEMU itself."
    ),
    json_response=True,
)

QEMU = "/usr/bin/qemu-system-x86_64"
GDB = "/usr/bin/gdb-multiarch"
SESSION_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
APPEND = re.compile(r"[\x20-\x7e]{0,512}")
DEFAULT_APPEND = "console=ttyS0 nokaslr"


def _session(value: str) -> str:
    if not isinstance(value, str) or SESSION_NAME.fullmatch(value) is None:
        raise ValueError("name must match [a-z][a-z0-9_-]{0,31}")
    return value


def _readable(value: str, label: str) -> str:
    path = validate_sandbox_path(value)
    if "\n" in path or "\r" in path:
        raise ValueError(f"{label} must not contain newlines")
    return path


def _writable(value: str, label: str) -> str:
    path = validate_sandbox_path(value, writable=True)
    if "\n" in path or "\r" in path:
        raise ValueError(f"{label} must not contain newlines")
    return path


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError("gdb_port must be an integer between 1024 and 65535")
    return value


@mcp.tool(title="Boot Kernel Under QEMU")
def kernel_boot(
    kernel: str,
    initramfs: str | None = None,
    append: str = DEFAULT_APPEND,
    memory_mb: int = 192,
    gdb_port: int = 1234,
    stop_at_start: bool = False,
    name: str = "kernel",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Start qemu-system-x86_64 (TCG, no KVM) in a detached tmux session."""

    session = _session(name)
    kernel_path = _readable(kernel, "kernel")
    initrd_path = _readable(initramfs, "initramfs") if initramfs is not None else None
    if not isinstance(append, str) or APPEND.fullmatch(append) is None:
        raise ValueError("append must be printable ASCII, at most 512 characters")
    if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or not 128 <= memory_mb <= 384:
        raise ValueError("memory_mb must be between 128 and 384 (512 MiB cgroup)")
    port = _port(gdb_port)
    timeout = validate_timeout(timeout_seconds)
    arguments = [
        "-m",
        str(memory_mb),
        "-kernel",
        kernel_path,
    ]
    if initrd_path is not None:
        arguments.extend(["-initrd", initrd_path])
    arguments.extend(
        [
            "-append",
            append,
            # Debian ships qemu UI backends as separate modules; `-nographic`
            # asks for ui-none and fails when qemu-system-gui is absent. An
            # explicit `-display none` plus stdio serial works headlessly.
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-gdb",
            f"tcp:127.0.0.1:{port}",
            "-no-reboot",
        ]
    )
    if stop_at_start:
        arguments.append("-S")
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": [
                "new-session",
                "-d",
                "-s",
                f"qy-{session}",
                "-c",
                "/workspace/work",
                "--",
                QEMU,
                *arguments,
            ],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "kernel_boot",
            "session": f"qy-{session}",
            "kernel": kernel_path,
            "initramfs": initrd_path,
            "memory_mb": memory_mb,
            "gdb_port": port,
            "stop_at_start": stop_at_start,
        },
    }


@mcp.tool(title="Attach GDB To Kernel")
def kernel_gdb(
    gdb_port: int = 1234,
    vmlinux: str | None = None,
    name: str = "gdb",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Start gdb-multiarch connected to the QEMU gdbstub (127.0.0.1:port)."""

    session = _session(name)
    port = _port(gdb_port)
    timeout = validate_timeout(timeout_seconds)
    executable = _readable(vmlinux, "vmlinux") if vmlinux is not None else None
    arguments = ["-q"]
    if executable is not None:
        arguments.append(executable)
    arguments.extend(
        [
            "-ex",
            "set pagination off",
            "-ex",
            "set confirm off",
            "-ex",
            f"target remote 127.0.0.1:{port}",
        ]
    )
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": [
                "new-session",
                "-d",
                "-s",
                f"qy-{session}",
                "-c",
                "/workspace/work",
                "--",
                GDB,
                *arguments,
            ],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "kernel_gdb",
            "session": f"qy-{session}",
            "gdb_port": port,
            "vmlinux": executable,
        },
    }


@mcp.tool(title="Extract Initramfs")
def kernel_unpack(
    archive: str,
    destination: str = "/workspace/work/rootfs",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Extract a gzip/xz/raw cpio initramfs below /workspace/work."""

    source = _readable(archive, "archive")
    target = _writable(destination, "destination")
    timeout = validate_timeout(timeout_seconds)
    script = (
        "set -eu; "
        f"mkdir -p -- {shlex.quote(target)}; "
        f"cd -- {shlex.quote(target)}; "
        f"( gzip -dc -- {shlex.quote(source)} 2>/dev/null "
        f"|| xz -dc -- {shlex.quote(source)} 2>/dev/null "
        f"|| cat -- {shlex.quote(source)} ) "
        "| cpio -idm --no-absolute-filenames"
    )
    return {
        "sandbox_command": {
            "tool": "bash",
            "arguments": ["--noprofile", "--norc", "-lc", script],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "kernel_unpack",
            "archive": source,
            "destination": target,
        },
    }


@mcp.tool(title="Send To Kernel Console")
def kernel_send(
    text: str | None = None,
    key: str | None = None,
    name: str = "kernel",
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Drive the guest console through the session MCP rules."""

    return session_send(name=name, text=text, key=key, timeout_seconds=timeout_seconds)


@mcp.tool(title="Capture Kernel Console")
def kernel_capture(
    lines: int = 200,
    name: str = "kernel",
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Read the last N lines of the guest console."""

    return session_capture(name=name, lines=lines, timeout_seconds=timeout_seconds)


@mcp.tool(title="Stop Kernel Session")
def kernel_stop(name: str = "kernel", timeout_seconds: int = 10) -> dict[str, Any]:
    """Kill the kernel (or gdb) session."""

    return session_stop(name=name, timeout_seconds=timeout_seconds)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
