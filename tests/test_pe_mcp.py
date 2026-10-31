"""Planner checks for the PE inspection MCP."""

import pytest

from MCPs.Pe_Mcp.server import inspect_pe, pe_disassemble, pe_strings


def test_inspect_pe_builds_pefile_script_and_stages_input():
    plan = inspect_pe("/workspace/input/demo.exe")
    command = plan["sandbox_command"]

    assert command["tool"] == "python3"
    script = command["arguments"][1]
    assert "import pefile" in script
    assert "_qy_dst = '/workspace/work/.stage/" in script
    assert "mitigations" in script
    assert plan["request"]["path"].startswith("/workspace/work/.stage/")


def test_inspect_pe_uses_work_path_directly():
    plan = inspect_pe("/workspace/work/demo.exe")
    script = plan["sandbox_command"]["arguments"][1]
    assert "_qy_dst = '/workspace/work/demo.exe'" in script
    assert "shutil.copyfile" not in script


def test_strings_are_bounded():
    plan = pe_strings("/workspace/work/demo.exe", min_length=6, limit=10)
    script = plan["sandbox_command"]["arguments"][1]
    assert "{6,}" in script
    assert "[:10]" in script
    with pytest.raises(ValueError):
        pe_strings("/workspace/work/demo.exe", min_length=2)
    with pytest.raises(ValueError):
        pe_strings("/workspace/work/demo.exe", limit=9999)


def test_disassembly_validates_rva_and_count():
    plan = pe_disassemble("/workspace/work/demo.exe", rva=0x1000, count=8)
    script = plan["sandbox_command"]["arguments"][1]
    assert "get_offset_from_rva(4096)" in script
    assert "capstone" in script
    with pytest.raises(ValueError):
        pe_disassemble("/workspace/work/demo.exe", rva=-1)
    with pytest.raises(ValueError):
        pe_disassemble("/workspace/work/demo.exe", rva=0x1000, count=1000)


def test_pe_rejects_paths_outside_the_sandbox():
    with pytest.raises(ValueError):
        inspect_pe("/etc/passwd")
    with pytest.raises(ValueError):
        pe_strings("/workspace/input/../../etc/passwd")
