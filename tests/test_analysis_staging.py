"""Planner-level checks for executable staging and the gdb run mode."""

import pytest

from MCPs.analysis_common import needs_stage
from MCPs.Gdb_Mcp.server import debug_binary
from MCPs.Pwntools_Mcp.server import run_pwntools, stage_binary


def test_only_the_work_tree_is_executable():
    assert needs_stage("/workspace/input/chal")
    assert needs_stage("/tmp/chal")
    assert not needs_stage("/workspace/work/chal")
    assert not needs_stage("/workspace/work/.stage/chal")


def test_gdb_stages_input_artifacts_and_uses_bash():
    plan = debug_binary("/workspace/input/chal", run=True)
    command = plan["sandbox_command"]

    assert command["tool"] == "bash"
    assert command["arguments"][:3] == ["--noprofile", "--norc", "-lc"]
    script = command["arguments"][3]
    assert "cp -f /workspace/input/chal" in script
    assert "chmod 0755" in script
    assert "gdb --batch --quiet" in script
    assert "starti" in script
    assert plan["request"]["staged_path"].startswith("/workspace/work/.stage/")


def test_gdb_work_tree_artifact_runs_without_a_shell():
    plan = debug_binary("/workspace/work/chal", run=False)

    assert plan["sandbox_command"]["tool"] == "gdb"
    assert plan["request"]["staged_path"] is None
    assert "info files" in plan["request"]["commands"]


def test_gdb_rejects_shell_escape_but_allows_gdb_python():
    with pytest.raises(ValueError):
        debug_binary("/workspace/work/chal", commands=["shell id"])
    plan = debug_binary("/workspace/work/chal", commands=["python print(1)"])
    assert plan["request"]["commands"] == ["python print(1)"]


def test_pwntools_exports_qinyuan_bin_for_staged_artifacts():
    plan = run_pwntools("print(1)", executable="/workspace/input/chal")
    script = plan["sandbox_command"]["arguments"][1]

    assert "_qy_dst = '/workspace/work/.stage/" in script
    assert "os.environ['QINYUAN_BIN'] = _qy_dst" in script
    assert plan["request"]["staged_path"].startswith("/workspace/work/.stage/")


def test_stage_binary_reports_the_executable_path():
    plan = stage_binary("/workspace/input/chal")

    assert plan["sandbox_command"]["tool"] == "python3"
    assert plan["request"]["staged_path"].startswith("/workspace/work/.stage/")
    script = plan["sandbox_command"]["arguments"][1]
    assert "print(json.dumps" in script
