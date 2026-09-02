from __future__ import annotations

from MCPs.fastmcp_compat import FastMCP

from nuclei_mcp.tools import register_tools


mcp = FastMCP(
    "qinyuan-nuclei-command-planner",
    instructions=(
        "Validate authorized Nuclei requests and return sandbox_command "
        "objects. This server never executes Nuclei or reads host templates."
    ),
    json_response=True,
)

register_tools(mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
