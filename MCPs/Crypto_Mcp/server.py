from __future__ import annotations

import json
from typing import Any

from MCPs.analysis_common import validate_script, validate_timeout, python_command
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-crypto-math-planner",
    instructions=(
        "Prepare Python plans for the sandbox's crypto math stack: PARI/GP via "
        "cypari2, SymPy, gmpy2 and fpylll (LLL). Use `pari` for number theory "
        "and elliptic curves, `sympy` for symbolic algebra/factorisation, and "
        "`lll` for lattice reduction. This server never executes Python itself."
    ),
    json_response=True,
)


@mcp.tool(title="Evaluate PARI/GP Expression")
def pari(expression: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """PARI/GP via cypari2: factorization, primality, finite fields, elliptic
    curves and discrete logs. Examples: factor(n), ellinit([a4,a6]),
    znlog(g, Mod(a,p))."""

    source = validate_script(expression, label="expression")
    timeout = validate_timeout(timeout_seconds)
    script = (
        "from cypari2 import Pari\n"
        f"_expr = {source!r}\n"
        "print(Pari()(_expr))\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pari", "bytes": len(source.encode("utf-8"))},
    }


@mcp.tool(title="Evaluate SymPy Expression")
def sympy(expression: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """SymPy: factorint, mod_inverse, discrete_log, Matrix and polynomial
    algebra. Example: factorint(n)."""

    source = validate_script(expression, label="expression")
    timeout = validate_timeout(timeout_seconds)
    script = (
        "import sympy\n"
        f"_expr = {source!r}\n"
        "print(eval(_expr, {'__builtins__': {}}, vars(sympy)))\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "sympy", "bytes": len(source.encode("utf-8"))},
    }


@mcp.tool(title="LLL-Reduce Integer Matrix")
def lll(
    matrix: list[list[int]],
    delta: float = 0.99,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """LLL-reduce a small integer basis with fpylll for Coppersmith/lattice
    attacks; each row is a basis vector, at most 64x64."""

    if (
        not isinstance(matrix, list)
        or not 1 <= len(matrix) <= 64
        or any(not isinstance(row, list) or not 1 <= len(row) <= 64 for row in matrix)
        or any(len(row) != len(matrix[0]) for row in matrix)
        or any(isinstance(value, bool) or not isinstance(value, int) for row in matrix for value in row)
    ):
        raise ValueError("matrix must be a rectangular list of at most 64x64 integers")
    if isinstance(delta, bool) or not isinstance(delta, float) or not 0.25 <= delta <= 0.999:
        raise ValueError("delta must be a float between 0.25 and 0.999")
    timeout = validate_timeout(timeout_seconds)
    rows = json.dumps(matrix, separators=(",", ":"))
    script = (
        "from fpylll import IntegerMatrix, LLL\n"
        f"_rows = {rows}\n"
        "M = IntegerMatrix.from_matrix(_rows)\n"
        f"LLL.reduction(M, delta={delta!r})\n"
        "print([[M[i, j] for j in range(M.ncols)] for i in range(M.nrows)])\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "lll", "rows": len(matrix), "cols": len(matrix[0])},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
