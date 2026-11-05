from __future__ import annotations

import json
from typing import Any

from MCPs.analysis_common import validate_script, validate_timeout, python_command, json_input_source, validate_sandbox_path
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
def pari(expression: str, timeout_seconds: int = 60, data_path: str | None = None) -> dict[str, Any]:
    """PARI/GP via cypari2: factorization, primality, finite fields, elliptic
    curves and discrete logs. Examples: factor(n), ellinit([a4,a6]),
    znlog(g, Mod(a,p)). Optional data_path is a sandbox JSON object of integer bindings."""

    source = validate_script(expression, label="expression")
    timeout = validate_timeout(timeout_seconds)
    script = (
        "from cypari2 import Pari\n"
        f"_expr = {source!r}\n"
        + ("_qy_pari = Pari()\n" + json_input_source(data_path, "_qy_data")
           + "if not isinstance(_qy_data, dict):\n    raise ValueError('tool_input_invalid: expected integer bindings object')\n"
           + "for _qy_name, _qy_value in _qy_data.items():\n"
           + "    if not _qy_name.isascii() or not _qy_name.isalpha() or type(_qy_value) is not int:\n"
           + "        raise ValueError('tool_input_invalid: PARI bindings require alphabetic names and integer values')\n"
           + "    _qy_pari(f'{_qy_name}={_qy_value}')\n"
           + "print(_qy_pari(_expr))\n" if data_path is not None else "print(Pari()(_expr))\n")
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pari", "bytes": len(source.encode("utf-8")),
            **({"data_path": validate_sandbox_path(data_path)} if data_path is not None else {})},
    }


@mcp.tool(title="Evaluate SymPy Expression")
def sympy(expression: str, timeout_seconds: int = 60, data_path: str | None = None) -> dict[str, Any]:
    """SymPy: factorint, mod_inverse, discrete_log, Matrix and polynomial
    algebra. Example: factorint(n). Optional data_path supplies sandbox JSON variable bindings."""

    source = validate_script(expression, label="expression")
    timeout = validate_timeout(timeout_seconds)
    script = (
        "import sympy\n"
        f"_expr = {source!r}\n"
        + (json_input_source(data_path, "_qy_data")
           + "if not isinstance(_qy_data, dict):\n    raise ValueError('tool_input_invalid: expected bindings object')\n"
           + "print(eval(_expr, {'__builtins__': {}}, {**vars(sympy), **_qy_data}))\n"
           if data_path is not None else "print(eval(_expr, {'__builtins__': {}}, vars(sympy)))\n")
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "sympy", "bytes": len(source.encode("utf-8")),
            **({"data_path": validate_sandbox_path(data_path)} if data_path is not None else {})},
    }


@mcp.tool(title="LLL-Reduce Integer Matrix")
def lll(
    matrix: list[list[int]] | None = None,
    delta: float = 0.99,
    timeout_seconds: int = 120,
    matrix_path: str | None = None,
) -> dict[str, Any]:
    """LLL-reduce a small integer basis with fpylll for Coppersmith/lattice
    attacks; each row is a basis vector, at most 64x64. Provide matrix OR a sandbox JSON matrix_path."""

    if (matrix is None) == (matrix_path is None):
        raise ValueError("tool_input_invalid: provide exactly one of matrix or matrix_path")
    if matrix_path is None and (
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
    load = (f"_rows = {rows}\n" if matrix_path is None else json_input_source(matrix_path, "_rows")
        + "if (not isinstance(_rows, list) or not 1 <= len(_rows) <= 64\n"
        + "    or any(not isinstance(r, list) or not 1 <= len(r) <= 64 for r in _rows)\n"
        + "    or any(len(r) != len(_rows[0]) for r in _rows)\n"
        + "    or any(type(v) is not int for r in _rows for v in r)):\n"
        + "    raise ValueError('tool_input_invalid: expected rectangular integer matrix at most 64x64')\n")
    script = (
        "from fpylll import IntegerMatrix, LLL\n"
        + load +
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
        "request": {"operation": "lll", **({"matrix_path": validate_sandbox_path(matrix_path)}
            if matrix_path is not None else {"rows": len(matrix), "cols": len(matrix[0])})},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
