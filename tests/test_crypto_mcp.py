"""Planner checks for the PARI/SymPy/fpylll crypto MCP."""

import pytest

from MCPs.Crypto_Mcp.server import lll, pari, sympy


def test_pari_uses_cypari2_and_prints_the_result():
    plan = pari("factor(2^64-1)")
    command = plan["sandbox_command"]

    assert command["tool"] == "python3"
    script = command["arguments"][1]
    assert "from cypari2 import Pari" in script
    assert "factor(2^64-1)" in script
    assert "print(Pari()(_expr))" in script


def test_sympy_evaluates_in_the_sympy_namespace():
    plan = sympy("factorint(2**64 - 1)")
    script = plan["sandbox_command"]["arguments"][1]

    assert "import sympy" in script
    assert "factorint(2**64 - 1)" in script
    assert "vars(sympy)" in script


def test_lll_reduces_a_rectangular_matrix():
    plan = lll([[1, 0, 0], [0, 1, 0], [1, 1, 1]], delta=0.75)
    script = plan["sandbox_command"]["arguments"][1]

    assert "from fpylll import IntegerMatrix, LLL" in script
    assert "[[1,0,0],[0,1,0],[1,1,1]]" in script
    assert "delta=0.75" in script
    assert plan["request"]["rows"] == 3


def test_crypto_plans_reject_invalid_inputs():
    with pytest.raises(ValueError):
        pari("")
    with pytest.raises(ValueError):
        sympy("x" * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        lll([[1, 2], [3]])
    with pytest.raises(ValueError):
        lll([[1, 2]], delta=1.5)
    with pytest.raises(ValueError):
        lll([[1.5, 2]])
