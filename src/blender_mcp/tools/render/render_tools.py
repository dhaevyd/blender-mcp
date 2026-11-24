"""
Render tools for Blender MCP.

Provides tools for rendering scenes, animations, and previews.
"""

import json
from pathlib import Path
from typing import Any

from blender_mcp.app import get_app
from blender_mcp.compat import *
from fastmcp.utilities.types import Image
from mcp.types import TextContent

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_MUTATING = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


def _register_render_tools():
    """Register all render-related tools."""
    app = get_app()

    @app.tool(annotations=_MUTATING)
    async def blender_render(
        operation: str = "render_preview",
        output_path: str = "",
        output_dir: str = "",
        resolution_x: int = 1920,
        resolution_y: int = 1080,
        frames: int = 60,
        frame_start: int = 1,
        frame_end: int = 250,
        file_format: str = "PNG",
        render_engine: str = "CYCLES",
        samples: int = 128,
        device: str = "GPU",
        use_denoising: bool = True,
        layer_name: str = "ViewLayer",
        use_bloom: bool = True,
        use_ssao: bool = True,
        use_motion_blur: bool = False,
        use_dof: bool = False,
        shading_mode: str = "SOLID",
        angles: int = 4,
        elevation_deg: float = 25.0,
        camera_radius: float = 5.0,
        prefer_session: bool = True,
    ) -> str:
        """
        PORTMANTEAU PATTERN RATIONALE:
        Consolidates rendering operations into a single interface.

        **Render Operations:**
        - **render_preview**: Generate high-quality single frame preview
        - **render_turntable**: Create 360-degree object rotation animation
        - **render_animation**: Render full timeline animation sequence
        - **render_current_frame**: Render only the current timeline frame
        - **screenshot_viewport**: Capture viewport for agent vision (prefers live GUI session)
        - **render_multi_angle**: Render N stills from orbit angles for review loops
        - **set_engine**: Configure Cycles/EEVEE engine, samples, device
        - **configure_layers**: Enable render passes on a view layer
        - **configure_output**: Point animation/sequence output at a workspace folder
        - **setup_post_processing**: EEVEE bloom, SSAO, motion blur, DOF

        **Output paths:** every ``output_path``/``output_dir`` resolves inside
        BLENDER_WORKSPACE. A relative value such as ``renders/shot.png`` is anchored on
        the workspace root, an absolute value must already be inside it, and an empty
        value picks a timestamped default under ``<workspace>/renders``. Results carry
        both the absolute path and the workspace-relative one to hand to the Gallery.
        Absolute paths outside the workspace are refused unless
        MCP_ALLOW_EXTERNAL_RENDER_OUTPUT=true.

        ## Return Format
        Standard string with operation result details

        ## Examples
        ```python
        await call_tool("blender_render", {"operation": "render_preview", "output_path": "renders/shot.png"})
        await call_tool("blender_render", {"operation": "render_turntable", "output_dir": "renders/turntable"})
        ```
        """
        from blender_mcp.handlers.render_handler import (
            render_multi_angle,
            render_preview,
            render_turntable,
            screenshot_viewport,
        )
        from blender_mcp.handlers.rendering_handler import (
            configure_output_settings,
            configure_render_layers,
            set_render_engine,
            setup_post_processing,
        )

        try:
            if operation in {"render_multi_angle", "render_turntable", "render_preview", "render_current_frame"}:
                from blender_mcp.utils.local_rendering import local_rendering_disabled_reason

                reason = local_rendering_disabled_reason()
                if reason:
                    return reason
            if operation == "screenshot_viewport":
                result = await screenshot_viewport(
                    output_path=output_path,
                    resolution_x=resolution_x,
                    resolution_y=resolution_y,
                    shading_mode=shading_mode,
                    prefer_session=prefer_session,
                )
                return _image_response(result)

            if operation == "set_engine":
                result = await set_render_engine(
                    engine=render_engine,
                    device=device,
                    use_denoising=use_denoising,
                    samples=samples,
                    resolution_x=resolution_x,
                    resolution_y=resolution_y,
                )
                return json.dumps(result, indent=2)

            if operation == "configure_layers":
                result = await configure_render_layers(layer_name=layer_name)
                return json.dumps(result, indent=2)

            if operation == "configure_output":
                result = await configure_output_settings(
                    output_dir=output_dir,
                    file_format=file_format,
                    frame_start=frame_start,
                    frame_end=frame_end,
                )
                return json.dumps(result, indent=2)

            if operation == "setup_post_processing":
                result = await setup_post_processing(
                    use_bloom=use_bloom,
                    use_ssao=use_ssao,
                    use_motion_blur=use_motion_blur,
                    use_dof=use_dof,
                )
                return json.dumps(result, indent=2)

            if operation == "render_multi_angle":
                result = await render_multi_angle(
                    output_dir=output_dir,
                    angles=angles,
                    elevation_deg=elevation_deg,
                    radius=camera_radius,
                    resolution_x=resolution_x,
                    resolution_y=resolution_y,
                    prefer_session=prefer_session,
                )
                return json.dumps(result, indent=2)

            if operation == "render_turntable":
                return await render_turntable(
                    output_dir=output_dir,
                    frames=frames,
                    resolution_x=resolution_x,
                    resolution_y=resolution_y,
                    format=file_format,
                    device=device,
                )

            elif operation == "render_preview":
                result = await render_preview(
                    output_path=output_path, resolution_x=resolution_x, resolution_y=resolution_y, device=device
                )
                return _image_response(result)

            elif operation == "render_animation":
                return (
                    "Animation rendering not yet implemented - set the frame range with "
                    "'configure_output' and use render_turntable for now"
                )

            elif operation == "render_current_frame":
                result = await render_preview(
                    output_path=output_path, resolution_x=resolution_x, resolution_y=resolution_y, device=device
                )
                return _image_response(result)

            else:
                return (
                    f"Unknown render operation: {operation}. Available: render_preview, "
                    "render_turntable, render_animation, render_current_frame, "
                    "screenshot_viewport, render_multi_angle, set_engine, configure_layers, "
                    "configure_output, setup_post_processing"
                )

        except Exception as e:
            return f"Error in render operation '{operation}': {e!s}"


def _image_response(
    handler_result: dict[str, Any] | str,
) -> list[TextContent | Image] | dict[str, Any] | str:
    """Wrap a handler result that has an output_path as MCP ImageContent + TextContent.

    Falls back to the raw value when the result is a string, an error, or the
    file doesn't exist yet (e.g. async farm job).
    """
    if not isinstance(handler_result, dict) or not handler_result.get("success"):
        return handler_result

    image_path = handler_result.get("output_path", "")
    if not image_path or not Path(image_path).is_file():
        return handler_result

    summary = {k: v for k, v in handler_result.items() if k not in ("image_base64",)}
    return [
        TextContent(type="text", text=json.dumps(summary, indent=2)),
        Image(path=image_path),
    ]


_register_render_tools()
