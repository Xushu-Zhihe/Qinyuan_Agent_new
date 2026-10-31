"""Planner checks for the kernel/QEMU MCP."""

import pytest

from MCPs.Kernel_Mcp.server import (
    kernel_boot,
    kernel_capture,
    kernel_gdb,
    kernel_send,
    kernel_stop,
    kernel_unpack,
)


def test_boot_uses_tcg_without_kvm_and_fixed_console():
    plan = kernel_boot("/workspace/input/bzImage", "/workspace/input/rootfs.cpio.gz")
    args = plan["sandbox_command"]["arguments"]

    assert plan["sandbox_command"]["tool"] == "tmux"
    assert "qy-kernel" in args
    assert "/usr/bin/qemu-system-x86_64" in args
    assert "-enable-kvm" not in args
    assert args[args.index("-m") + 1] == "192"
    assert args[args.index("-kernel") + 1] == "/workspace/input/bzImage"
    assert args[args.index("-append") + 1] == "console=ttyS0 nokaslr"
    assert args[args.index("-gdb") + 1] == "tcp:127.0.0.1:1234"
    assert "-S" not in args


def test_boot_stop_at_start_and_bounds():
    plan = kernel_boot("/workspace/work/bzImage", memory_mb=256, stop_at_start=True)
    assert "-S" in plan["sandbox_command"]["arguments"]
    with pytest.raises(ValueError):
        kernel_boot("/workspace/work/bzImage", memory_mb=1024)
    with pytest.raises(ValueError):
        kernel_boot("/workspace/work/bzImage", append="bad\nappend")
    with pytest.raises(ValueError):
        kernel_boot("/workspace/work/bzImage", gdb_port=80)


def test_gdb_attaches_to_loopback_gdbstub():
    plan = kernel_gdb(gdb_port=1235, vmlinux="/workspace/work/vmlinux")
    args = plan["sandbox_command"]["arguments"]
    assert args[args.index("--") + 1] == "/usr/bin/gdb-multiarch"
    assert "/workspace/work/vmlinux" in args
    assert "target remote 127.0.0.1:1235" in args


def test_unpack_is_quoted_and_stays_in_work():
    plan = kernel_unpack("/workspace/input/rootfs.cpio.gz", "/workspace/work/rootfs")
    script = plan["sandbox_command"]["arguments"][3]
    assert "cpio -idm --no-absolute-filenames" in script
    assert "/workspace/input/rootfs.cpio.gz" in script
    with pytest.raises(ValueError):
        kernel_unpack("/workspace/input/rootfs.cpio.gz", "/etc")


def test_console_helpers_delegate_to_session_rules():
    assert kernel_send(text="id")["sandbox_command"]["tool"] == "tmux"
    assert kernel_capture(lines=200)["sandbox_command"]["arguments"][-1] == "-200"
    with pytest.raises(ValueError):
        kernel_capture(lines=1)
    assert kernel_stop()["sandbox_command"]["arguments"] == ["kill-session", "-t", "qy-kernel"]
