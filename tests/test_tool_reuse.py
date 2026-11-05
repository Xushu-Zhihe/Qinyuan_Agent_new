"""File-backed planner inputs and category-independent action guidance."""

import asyncio
import io
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from MCPs.Pwntools_Mcp.server import run_pwntools
from MCPs.Pycryptodome_Mcp.server import run_crypto
from MCPs.Z3_Mcp.server import solve_constraints
from MCPs.Shell_Mcp.server import run_bash, run_powershell
from MCPs.Crypto_Mcp.server import pari, sympy, lll


@pytest.mark.parametrize("planner", [run_pwntools, run_crypto, solve_constraints])
def test_saved_python_executes_with_filename_and_existing_prefix(planner, monkeypatch, capsys):
    plan = planner(script_path="solver.py")
    assert plan["request"]["script_path"] == "/workspace/work/solver.py"
    reads = []
    def read(path):
        reads.append(path.as_posix())
        return b"assert __file__ == '/workspace/work/solver.py'; print('reused')"
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(plan["sandbox_command"]["arguments"][1], {})
    assert reads == ["/workspace/work/solver.py"]
    assert capsys.readouterr().out == "reused\n"
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"x" * 65537)
    with pytest.raises(ValueError, match="tool_input_invalid"):
        exec(plan["sandbox_command"]["arguments"][1], {})


@pytest.mark.parametrize("planner", [run_pwntools, run_crypto, solve_constraints, run_bash, run_powershell])
def test_file_inputs_reject_ambiguous_or_host_paths_before_dispatch(planner):
    for kwargs in ({}, {"script": "print(1)", "script_path": "s.py"},
                   {"script_path": "../s.py"}, {"script_path": "/etc/passwd"},
                   {"script_path": "C:\\secret.py"}):
        with pytest.raises(ValueError):
            planner(**kwargs)


def test_shell_files_are_argv_not_interpolated_commands():
    assert run_bash(script_path="a b;echo BAD.sh")["sandbox_command"]["arguments"] == [
        "--noprofile", "--norc", "/workspace/work/a b;echo BAD.sh"]
    assert run_powershell(script_path="saved.ps1")["sandbox_command"]["arguments"][-2:] == [
        "-File", "/workspace/work/saved.ps1"]


def test_json_bindings_are_loaded_only_by_the_execution(monkeypatch, capsys):
    backend = ModuleType("sympy")
    backend.factorint = lambda n: {n: 1}
    monkeypatch.setitem(sys.modules, "sympy", backend)
    plan = sympy("factorint(n)", data_path="parameters.json")
    reads = []
    def opened(path, **kwargs):
        reads.append(path)
        return io.StringIO(json.dumps({"n": 17}))
    monkeypatch.setattr("builtins.open", opened)
    exec(plan["sandbox_command"]["arguments"][1], {})
    assert reads == ["/workspace/work/parameters.json"]
    assert capsys.readouterr().out == "{17: 1}\n"
    for planner, kwargs in [(pari, {"expression": "n", "data_path": "/etc/p"}),
                            (lll, {"matrix_path": "../m.json"})]:
        with pytest.raises(ValueError):
            planner(**kwargs)


def test_matrix_file_checks_shape_before_reduction(monkeypatch):
    backend = ModuleType("fpylll")
    backend.IntegerMatrix = None
    backend.LLL = None
    monkeypatch.setitem(sys.modules, "fpylll", backend)
    plan = lll(matrix_path="basis.json")
    for value in ([[1, 2], [3]], [[True]], [], [[1.5]]):
        monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(json.dumps(value)))
        with pytest.raises(ValueError, match="tool_input_invalid"):
            exec(plan["sandbox_command"]["arguments"][1], {})
    with pytest.raises(ValueError, match="exactly one"):
        lll([[1]], matrix_path="basis.json")


def test_valid_file_data_reaches_pari_and_lll_backends(monkeypatch, capsys):
    # Controlled backends verify data transport, not the external math libraries.
    calls = []
    pari_module = ModuleType("cypari2")
    class Pari:
        def __call__(self, expression):
            calls.append(expression)
            return 17
    pari_module.Pari = Pari
    monkeypatch.setitem(sys.modules, "cypari2", pari_module)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO('{"n":17}'))
    exec(pari("n", data_path="p.json")["sandbox_command"]["arguments"][1], {})
    assert calls == ["n=17", "n"]
    assert capsys.readouterr().out == "17\n"
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO('{"x;quit":17}'))
    with pytest.raises(ValueError, match="tool_input_invalid"):
        exec(pari("n", data_path="p.json")["sandbox_command"]["arguments"][1], {})
    backend = ModuleType("fpylll")
    class Matrix:
        nrows = ncols = 1
        @classmethod
        def from_matrix(cls, rows):
            assert rows == [[17]]
            return cls()
        def __getitem__(self, key):
            return 17
    class Reduction:
        @staticmethod
        def reduction(matrix, delta):
            assert delta == .99
    backend.IntegerMatrix, backend.LLL = Matrix, Reduction
    monkeypatch.setitem(sys.modules, "fpylll", backend)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO('[[17]]'))
    exec(lll(matrix_path="m.json")["sandbox_command"]["arguments"][1], {})
    assert capsys.readouterr().out == "[[17]]\n"


def test_script_file_option_is_visible_in_each_mcp_schema():
    from MCPs.Pwntools_Mcp.server import mcp as pwn
    from MCPs.Pycryptodome_Mcp.server import mcp as crypto
    from MCPs.Z3_Mcp.server import mcp as z3
    from MCPs.Shell_Mcp.server import mcp as shell
    for server in (pwn, crypto, z3, shell):
        tools = asyncio.run(server.list_tools())
        for tool in tools:
            if tool.name in {"run_pwntools", "run_crypto", "solve_constraints", "run_bash", "run_powershell"}:
                assert "script_path" in tool.inputSchema["properties"]
                assert "script" not in tool.inputSchema.get("required", [])
