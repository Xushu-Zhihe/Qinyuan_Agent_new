"""Compatibility initialization for the pinned MCP 1.x runtime."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as _FastMCPSettings


# MCP 1.29 leaves the generic lifespan annotation unresolved. Pydantic
# Settings 2.15 warns on every server construction until the model is rebuilt.
_FastMCPSettings.model_rebuild()


__all__ = ["FastMCP"]
