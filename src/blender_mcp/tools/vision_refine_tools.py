"""Vision refinement loop MCP tools."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastmcp.utilities.types import Image
from mcp.types import TextContent

logger = logging.getLogger(__name__)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_MUTATING = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


def _register_vision_refine_tools() -> None:
    from blender_mcp.app import get_app
    from blender_mcp.handlers.vision_refine_handler import (
        vision_apply_script,
        vision_capture,
        vision_review_bundle,
    )

    app = get_app()

    @app.tool(annotations=_MUTATING)
    async def blender_vision_refine(
        operation: str = "capture",
        output_path: str = "",
        output_dir: str = "",
        goal: str = "",
        script: str = "",
        resolution_x: int = 1280,
        resolution_y: int = 720,
        include_multi_angle: bool = True,
        angles: int = 4,
    ) -> list[TextContent | Image] | dict[str, Any]:
        """
        Agent vision refinement loop: capture, review bundle, apply fixes.

        Operations:
        - capture: viewport PNG returned as an actual image the AI can see
        - review_bundle: screenshot + multi-angle stills + scene summary + refinement prompt
        - apply_script: run corrective bpy script after vision model feedback

        ## Examples
        ```python
        await call_tool("blender_vision_refine", {"operation": "capture", "output_path": "renders/preview.png"})
        ```
        """
        try:
            if operation == "capture":
                if not output_path:
                    return {"success": False, "error": "output_path is required"}
                result = await vision_capture(
                    output_path=output_path,
                    resolution_x=resolution_x,
                    resolution_y=resolution_y,
                )
                return _image_response(result)

            if operation == "review_bundle":
                if not output_dir:
                    return {"success": False, "error": "output_dir is required"}
                result = await vision_review_bundle(
                    output_dir=output_dir,
                    goal=goal,
                    include_multi_angle=include_multi_angle,
                    angles=angles,
                )
                # Primary screenshot as image; include scene summary + refinement prompt as text
                meta = {k: v for k, v in result.items() if k not in ("screenshot",)}
                screenshot = result.get("screenshot") or {}
                return _image_response(screenshot, extra_text=json.dumps(meta, indent=2))

            if operation == "apply_script":
                if not script.strip():
                    return {"success": False, "error": "script is required"}
                return await vision_apply_script(script=script)

            return {
                "success": False,
                "error": f"Unknown operation '{operation}'",
                "available_operations": ["capture", "review_bundle", "apply_script"],
            }
        except Exception as exc:
            logger.exception("blender_vision_refine failed: %s", exc)
            return {"success": False, "error": str(exc), "operation": operation}


def _image_response(
    handler_result: dict[str, Any],
    extra_text: str = "",
) -> list[TextContent | Image] | dict[str, Any]:
    """Convert a handler result with an image path into MCP content blocks.

    Returns a list so FastMCP emits a proper ImageContent block the AI client can
    display, alongside a TextContent summary. Falls back to the raw dict when no
    valid image path is present (e.g. capture failed).
    """
    if not handler_result.get("success"):
        return handler_result

    image_path = handler_result.get("output_path", "")
    if not image_path or not Path(image_path).is_file():
        return handler_result

    summary = {k: v for k, v in handler_result.items() if k not in ("image_base64",)}
    text_parts = [json.dumps(summary, indent=2)]
    if extra_text:
        text_parts.append(extra_text)

    return [
        TextContent(type="text", text="\n\n".join(text_parts)),
        Image(path=image_path),
    ]


_register_vision_refine_tools()
