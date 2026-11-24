"""Blender MCP — AI-powered 3D creation and automation via FastMCP."""

import os

# Hatchling parses this literal for package metadata. Runtime surfaces use
# BLENDER_MCP_VERSION from the image tag when available.
__version__ = "0.11.1"


def app_version() -> str:
    return os.environ.get("BLENDER_MCP_VERSION") or __version__


__author__ = "Blender MCP Contributors"
__license__ = "MIT"
__project_urls__ = {
    "Source": "https://github.com/sandraschi/blender-mcp",
    "Issues": "https://github.com/sandraschi/blender-mcp/issues",
}
