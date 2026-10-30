"""MCP layer: the variator-core FastMCP server, plugin discovery, server-side state."""

from variator.mcp.case import Case, apply_patch
from variator.mcp.plugins import Plugin, discover, load_packs_from_dir
from variator.mcp.server import Services, build_server
from variator.mcp.store import Store

__all__ = [
    "Case",
    "Plugin",
    "Services",
    "Store",
    "apply_patch",
    "build_server",
    "discover",
    "load_packs_from_dir",
]
