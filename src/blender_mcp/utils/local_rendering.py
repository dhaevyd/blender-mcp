"""Master-mode policy for rendering inside the coordinator's local Blender session."""

from __future__ import annotations


def local_rendering_disabled_reason() -> str | None:
    """Return an explanatory block reason when local renders are disabled on a master."""
    from blender_mcp.utils.farm_workers import mcp_role

    if mcp_role() != "master":
        return None
    from blender_mcp.tools.farm_tools import _coordinator

    if _coordinator().store.get_local_rendering_enabled():
        return None
    return (
        "Local rendering is disabled on this master. Submit the project and frame range "
        "through blender_farm(operation='submit') to use the worker pool."
    )
