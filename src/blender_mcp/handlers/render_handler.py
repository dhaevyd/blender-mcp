"""Render handler for Blender MCP server.

This module provides rendering functions that can be registered as FastMCP tools.

Every render output path goes through :func:`resolve_render_output`, which anchors
paths on ``BLENDER_WORKSPACE`` instead of the process CWD (``/app`` in the
container) so renders show up in the Gallery and survive a restart.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from ..compat import *
from ..decorators import blender_operation
from ..exceptions import BlenderMCPError, BlenderRenderError
from ..utils.blender_executor import get_blender_executor
from ..utils.gpu_render import build_gpu_setup_snippet, parse_render_device
from ..utils.projects import _ensure_directory, workspace_root

# Initialize the executor with default Blender executable
_executor = get_blender_executor()


# ---------------------------------------------------------------------------
# Render output paths
# ---------------------------------------------------------------------------
# A render path resolved against the process CWD lands in /app inside the
# container: invisible to the Gallery, gone with the container. So relative paths
# are anchored on ``workspace_root()``, absolute paths are accepted only when they
# stay inside it, and the no-follow helpers from ``blender_mcp.utils.projects``
# create the parent folders (one component at a time, never through a symlink).

RENDER_OUTPUT_SUBDIR = "renders"
ALLOW_EXTERNAL_RENDER_OUTPUT_ENV = "MCP_ALLOW_EXTERNAL_RENDER_OUTPUT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FORMAT_SUFFIX = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "WEBP": ".webp",
    "TIFF": ".tif",
    "BMP": ".bmp",
    "OPEN_EXR": ".exr",
}


class RenderOutputPathError(BlenderMCPError, ValueError):
    """A render output path was refused (outside the workspace or via a symlink).

    Subclasses ``BlenderMCPError`` so the operation decorator re-raises it verbatim
    instead of reporting a refusal as an unexpected failure, and ``ValueError`` so
    callers that already treat workspace path problems as validation errors keep
    working (the same convention as ``utils.projects``).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, "RENDER_OUTPUT_PATH_REFUSED")


@dataclass(frozen=True)
class RenderOutput:
    """A resolved render destination.

    ``relative`` is the workspace-relative form handed to the Gallery; it is
    ``None`` for an explicitly allowed external path, which the Gallery cannot see.
    """

    absolute: Path
    relative: str | None
    external: bool = False

    def summary(self) -> str:
        """Suffix for tool result text carrying the Gallery path."""
        if self.relative is None:
            return " (external path, outside the workspace: not shown in the Gallery)"
        return f" (workspace-relative: {self.relative})"


def _allow_external_render_output() -> bool:
    return os.environ.get(ALLOW_EXTERNAL_RENDER_OUTPUT_ENV, "").strip().lower() in _TRUTHY


def _default_render_stem(name: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "render").strip("-") or "render"
    return f"{stamp}-{slug}"


def _is_absolute_request(raw: str) -> bool:
    # Windows drive (C:/...) and UNC prefixes are absolute requests too; they are
    # refused on POSIX because they cannot map inside the workspace.
    return raw.startswith(("/", "\\")) or PureWindowsPath(raw).drive != ""


def _inside_workspace(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def _split_relative(relative: str) -> list[str]:
    return [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]


def _refuse_symlinked_components(root: Path, parts: list[str], *, display: str) -> None:
    """Refuse a path that walks through an existing symlink (no-follow check)."""
    current = root
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RenderOutputPathError(f"render output path {display!r} cannot be inspected: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise RenderOutputPathError(
                f"render output path {display!r} goes through the symlink {str(current)!r}; "
                f"render output must stay in the real workspace tree {root}"
            )


def _refuse_non_file_target(path: Path, *, display: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RenderOutputPathError(f"render output path {display!r} cannot be inspected: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise RenderOutputPathError(
            f"render output path {display!r} is an existing symlink; render output never follows symlinks"
        )
    if stat.S_ISDIR(mode):
        raise RenderOutputPathError(
            f"render output path {display!r} is a folder; name a file such as 'renders/shot.png'"
        )


def _create_in_workspace(root: Path, relative: str, *, directory: bool) -> RenderOutput:
    parts = _split_relative(relative)
    display = "/".join(parts)
    if not parts:
        raise RenderOutputPathError(f"render output path {relative!r} names nothing inside the workspace {root}")
    if ".." in parts:
        raise RenderOutputPathError(
            f"render output path {display!r} contains '..' and would leave its folder; use a plain "
            f"workspace-relative path such as 'renders/shot.png' (workspace: {root})"
        )
    target = root.joinpath(*parts)
    if not _inside_workspace(root, target):
        raise RenderOutputPathError(f"render output path {display!r} resolves outside the workspace {root}")
    parents = parts if directory else parts[:-1]
    _refuse_symlinked_components(root, parents, display=display)
    try:
        _ensure_directory(root, tuple(parents))
    except OSError as exc:
        raise RenderOutputPathError(
            f"render output folder {'/'.join(parents) or '.'!r} could not be created inside {root}: {exc}"
        ) from exc
    if not directory:
        _refuse_non_file_target(target, display=display)
    return RenderOutput(absolute=target, relative=display)


def _create_directory_external(target: Path, *, display: str) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as exc:
        raise RenderOutputPathError(f"render output folder {str(target)!r} could not be created: {exc}") from exc


def resolve_render_output(
    requested: str | None,
    *,
    directory: bool = False,
    default_name: str = "render",
    suffix: str = ".png",
) -> RenderOutput:
    """Resolve a render destination inside ``BLENDER_WORKSPACE``.

    Args:
        requested: Empty for a timestamped default under ``<workspace>/renders``, a
            workspace-relative path (``renders/shot.png``) anchored on the workspace
            root, or an absolute path that must already be inside the workspace.
        directory: True when the path names a folder (animation/sequence output).
        default_name: Name used for the timestamped default.
        suffix: File suffix used for the timestamped default.

    Returns:
        RenderOutput: absolute path plus the workspace-relative form for the Gallery.

    Raises:
        RenderOutputPathError: The path escapes the workspace, contains ``..``, or
            walks through a symlink (unless the external flag is set for absolute
            paths).
    """
    root = workspace_root()
    raw = requested.strip() if isinstance(requested, str) else ""

    if not raw:
        default_relative = f"{RENDER_OUTPUT_SUBDIR}/{_default_render_stem(default_name)}"
        if not directory:
            default_relative += suffix if suffix.startswith(".") else f".{suffix}"
        return _create_in_workspace(root, default_relative, directory=directory)

    if _is_absolute_request(raw):
        target = Path(raw)
        if not target.is_absolute():
            raise RenderOutputPathError(
                f"render output path {raw!r} is not a path this host can use; use a workspace-relative "
                f"path such as 'renders/shot.png' (workspace: {root})"
            )
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise RenderOutputPathError(f"render output path {raw!r} cannot be resolved: {exc}") from exc
        if _inside_workspace(root, resolved):
            return _create_in_workspace(root, resolved.relative_to(root).as_posix(), directory=directory)
        if not _allow_external_render_output():
            raise RenderOutputPathError(
                f"render output path {raw!r} is outside the workspace {root}; use a path inside it "
                f"(e.g. 'renders/shot.png') or set {ALLOW_EXTERNAL_RENDER_OUTPUT_ENV}=true to allow "
                f"external render output"
            )
        _create_directory_external(resolved if directory else resolved.parent, display=raw)
        return RenderOutput(absolute=resolved, relative=None, external=True)

    return _create_in_workspace(root, raw, directory=directory)


def workspace_relative_path(path: str | Path) -> str | None:
    """Return *path* relative to the workspace root, or ``None`` when it is outside."""
    root = workspace_root()
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    if not _inside_workspace(root, resolved):
        return None
    return resolved.relative_to(root).as_posix() or None


def render_output_prefix() -> RenderOutput:
    """The workspace ``renders/`` folder, used as an animation output prefix."""
    return resolve_render_output(RENDER_OUTPUT_SUBDIR, directory=True)


@blender_operation("render_turntable", log_args=True)
async def render_turntable(
    output_dir: str = "",
    frames: int = 60,
    resolution_x: int = 1280,
    resolution_y: int = 720,
    format: str = "PNG",
    device: str = "GPU",
) -> str:
    """Render 360-degree turntable animation of the current scene.

    Args:
        output_dir: Folder for the rendered frames. Workspace-relative paths are
            anchored on BLENDER_WORKSPACE; empty picks ``<workspace>/renders/<timestamp>-turntable``
        frames: Number of frames for the animation (default: 60)
        resolution_x: Horizontal resolution in pixels (default: 1280)
        resolution_y: Vertical resolution in pixels (default: 720)
        format: Output image format (default: "PNG")
        device: Compute device ('GPU' default). CPU is refused when a GPU is present
            unless MCP_RENDER_REQUIRE_GPU=false; with no GPU at all it renders on CPU.

    Returns:
        str: Success message with render details and the workspace-relative folder

    Raises:
        BlenderRenderError: If rendering fails
        RenderOutputPathError: If the output folder escapes the workspace
    """
    output = resolve_render_output(output_dir, directory=True, default_name="turntable")
    output_dir = str(output.absolute)
    try:
        gpu_setup = build_gpu_setup_snippet(device=device, engine=None, indent=4)

        script = f"""
import os
import math
from mathutils import Vector

try:
    scene = bpy.context.scene

    # Setup animation
    scene.frame_start = 1
    scene.frame_end = {frames}
    scene.frame_current = 1

    # Find or create camera
    camera = scene.camera
    if not camera:
        # Create a new camera if none exists
        bpy.ops.object.camera_add(enter_editmode=False, align='VIEW')
        camera = bpy.context.active_object
        scene.camera = camera

        # Position camera to see the entire scene
        camera.location = (5.0, -5.0, 3.0)
        camera.rotation_euler = (1.0, 0, 0.8)

        # Create empty at origin for camera to look at
        bpy.ops.object.empty_add(location=(0, 0, 0))
        empty = bpy.context.active_object
        empty.name = 'CameraTarget'

        # Add track-to constraint
        constraint = camera.constraints.new(type='TRACK_TO')
        constraint.target = empty
        constraint.track_axis = 'TRACK_NEGATIVE_Z'
        constraint.up_axis = 'UP_Y'

    # Set render settings
    scene.render.resolution_x = {resolution_x}
    scene.render.resolution_y = {resolution_y}
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = '{format}'

    # Ensure output directory exists
    os.makedirs(r"{output_dir}", exist_ok=True)
    scene.render.filepath = os.path.join(r"{output_dir}", 'turntable_')

    # Setup animation to rotate camera around the scene
    for frame in range(1, {frames} + 1):
        scene.frame_set(frame)
        angle = (frame / {frames}) * 2 * math.pi
        radius = 5.0  # Fixed distance from center

        # Calculate camera position in a circle around the origin
        camera.location.x = radius * math.cos(angle)
        camera.location.y = radius * math.sin(angle)
        camera.location.z = 3.0  # Fixed height

        # Insert keyframe
        camera.keyframe_insert(data_path="location", frame=frame)

    # Render animation
    print(f"Rendering {frames} frame turntable to {{r'{output_dir}'}}")
{gpu_setup}
    bpy.ops.render.render(animation=True)
    MCP_GPU_VERIFY_AFTER_RENDER()

    print("SUCCESS: Turntable animation rendered!")

except Exception as e:
    print(f"ERROR: Failed to render turntable: {{str(e)}}")
    raise e
"""

        await _executor.execute_script(script)
        return f"Rendered {frames}-frame turntable animation to {output_dir}{output.summary()}"

    except Exception as e:
        raise BlenderRenderError("turntable_animation", f"Failed to render turntable: {e!s}")


@blender_operation("render_preview", log_args=True)
async def render_preview(
    output_path: str = "",
    resolution_x: int = 1920,
    resolution_y: int = 1080,
    samples: int = 256,
    use_denoising: bool = True,
    use_adaptive_sampling: bool = True,
    format: str = "PNG",
    quality: int = 90,
    camera_name: str | None = None,
    use_environment: bool = True,
    use_film_transparent: bool = False,
    device: str = "GPU",
) -> str:
    """Render a high-quality preview of the current scene.

    Args:
        output_path: Image file to write. Relative paths resolve against
            BLENDER_WORKSPACE, absolute paths must already be inside it, and an empty
            value picks ``<workspace>/renders/<timestamp>-preview.<ext>``.
        resolution_x: Horizontal resolution in pixels (default: 1920)
        resolution_y: Vertical resolution in pixels (default: 1080)
        samples: Number of samples per pixel (default: 256)
        use_denoising: Whether to use AI denoising (default: True)
        use_adaptive_sampling: Whether to use adaptive sampling (default: True)
        format: Output image format (default: "PNG")
        quality: Output quality (1-100) for lossy formats (default: 90)
        camera_name: Name of the camera to use (default: active camera)
        use_environment: Whether to use environment lighting (default: True)
        use_film_transparent: Whether to render with transparent background (default: False)
        device: Compute device ('GPU' default, configured through the shared GPU
            helper). CPU is refused when a GPU is present unless MCP_RENDER_REQUIRE_GPU=false.

    Returns:
        str: Success message with the absolute and workspace-relative render path

    Raises:
        BlenderRenderError: If rendering fails or camera is not found
        RenderOutputPathError: If the output path escapes the workspace
    """
    destination = resolve_render_output(
        output_path,
        default_name="preview",
        suffix=_FORMAT_SUFFIX.get((format or "PNG").upper(), ".png"),
    )
    output_path = str(destination.absolute)
    try:
        gpu_setup = build_gpu_setup_snippet(device=device, engine="CYCLES", indent=4)

        script = f"""
import os
from mathutils import Vector

try:
    scene = bpy.context.scene

    # Set active camera if specified
    if "{camera_name}" and "{camera_name}" in bpy.data.objects and bpy.data.objects["{camera_name}"].type == 'CAMERA':
        scene.camera = bpy.data.objects["{camera_name}"]

    # Verify camera exists
    if not scene.camera:
        raise Exception("No camera found in the scene")

    # Set render engine to Cycles for best quality
    scene.render.engine = 'CYCLES'

    # Configure Cycles settings
    cycles = scene.cycles
    cycles.samples = {samples}
    cycles.use_denoising = {use_denoising}
    if {use_denoising}:
        # OptiX denoising needs an OptiX-capable build/device; fall back cleanly.
        try:
            cycles.denoiser = 'OPTIX'
        except TypeError:
            try:
                cycles.denoiser = 'OPENIMAGEDENOISE'
            except TypeError:
                pass
    cycles.use_adaptive_sampling = {use_adaptive_sampling}

    if {use_adaptive_sampling}:
        cycles.adaptive_threshold = 0.01
        cycles.adaptive_min_samples = 32

{gpu_setup}

    # Configure render settings
    scene.render.resolution_x = {resolution_x}
    scene.render.resolution_y = {resolution_y}
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "{format}"
    scene.render.image_settings.quality = {quality}
    scene.render.film_transparent = {use_film_transparent}

    # Set color management for better color accuracy
    scene.view_settings.view_transform = 'Filmic'
    scene.view_settings.look = 'Medium High Contrast'
    scene.view_settings.exposure = 0.2
    scene.view_settings.gamma = 1.0

    # Configure film settings
    scene.render.film_transparent = {use_film_transparent}

    # Set output path
    scene.render.filepath = r"{output_path}"

    # Setup environment lighting if enabled
    if {use_environment} and not scene.world:
        # Create a simple environment world if none exists
        bpy.ops.world.new()
        scene.world = bpy.data.worlds['World']
        scene.world.use_nodes = True

        # Get the world nodes
        world_nodes = scene.world.node_tree.nodes
        world_links = scene.world.node_tree.links

        # Clear default nodes
        world_nodes.clear()

        # Add environment texture node
        bg_node = world_nodes.new('ShaderNodeBackground')
        env_tex = world_nodes.new('ShaderNodeTexEnvironment')
        output = world_nodes.new('ShaderNodeOutputWorld')

        # Link nodes
        world_links.new(env_tex.outputs['Color'], bg_node.inputs['Color'])
        world_links.new(bg_node.outputs['Background'], output.inputs['Surface'])

        # Set default environment strength
        bg_node.inputs['Strength'].default_value = 1.0

        # Position nodes
        env_tex.location = (-300, 0)
        bg_node.location = (-100, 0)
        output.location = (100, 0)

    # Ensure we're in rendered viewport shading
    if bpy.context.screen:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'RENDERED'

    # Render the image
    print(f"Rendering preview to {{r'{output_path}'}}")
    bpy.ops.render.render(write_still=True)
    MCP_GPU_VERIFY_AFTER_RENDER()

    print("SUCCESS: Preview render complete!")

except Exception as e:
    print(f"ERROR: Failed to render preview: {{str(e)}}")
    raise e
"""
        # Execute the render script
        output = await _executor.execute_script(script, script_name="render_preview")
        device_info = parse_render_device(output) or {}
        return {
            "success": True,
            "output_path": output_path,
            "workspace_relative_path": destination.relative or "",
            "device": device_info.get("device", "unknown"),
            "backend": device_info.get("backend", "unknown"),
            "message": f"Rendered preview to {output_path}",
        }

    except Exception as e:
        raise BlenderRenderError("preview_render", f"Failed to render preview: {e!s}")


def _parse_script_lines(output: str) -> dict[str, str]:
    """Parse KEY: value lines printed by generated Blender scripts."""
    parsed: dict[str, str] = {}
    for line in (output or "").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


@blender_operation("screenshot_viewport", log_args=True)
async def screenshot_viewport(
    output_path: str = "",
    resolution_x: int = 1280,
    resolution_y: int = 720,
    shading_mode: str = "SOLID",
    prefer_session: bool = True,
    max_size: int = 1000,
) -> dict[str, Any]:
    """Capture the 3D viewport or still render for agent vision feedback.

    Uses ``bpy.ops.screen.screenshot_area`` in live sessions for instant capture.
    Falls back to ``bpy.ops.render.render`` in headless mode. Resizes output so
    the largest dimension does not exceed ``max_size``. The capture is written into
    BLENDER_WORKSPACE (``<workspace>/renders/<timestamp>-viewport.png`` when
    ``output_path`` is empty) and the payload carries both the absolute path and
    the workspace-relative one.
    """
    import base64
    from pathlib import Path

    from ..utils.blender_runtime import execute_bpy_script

    destination = resolve_render_output(output_path, default_name="viewport")
    output_path = str(destination.absolute)

    gpu_setup = build_gpu_setup_snippet(engine=None, indent=4)

    script = f"""import bpy, os, tempfile
filepath = r"{output_path}"
os.makedirs(os.path.dirname(filepath), exist_ok=True)
scene = bpy.context.scene
scene.render.resolution_x = {resolution_x}
scene.render.resolution_y = {resolution_y}
scene.render.image_settings.file_format = 'PNG'

captured = False
if bpy.context.screen:
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    try:
                        space.shading.type = '{shading_mode}'
                    except Exception:
                        pass
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=filepath)
            captured = os.path.exists(filepath)
            break

if not captured:
    scene.render.filepath = filepath
{gpu_setup}
    bpy.ops.render.render(write_still=True)
    MCP_GPU_VERIFY_AFTER_RENDER()

# Resize if largest dimension exceeds max_size
max_size = {max_size}
if max_size > 0 and os.path.exists(filepath):
    img = bpy.data.images.load(filepath)
    w, h = img.size
    if max(w, h) > max_size:
        scale = max_size / max(w, h)
        img.scale(int(w * scale), int(h * scale))
        img.save()
    bpy.data.images.remove(img)

print("SCREENSHOT_PATH:" + filepath)
print("CAPTURED:" + str(captured))
"""

    result = await execute_bpy_script(
        script,
        script_name="screenshot_viewport",
        timeout=90,
        prefer_session=prefer_session,
        headless_fallback=True,
    )
    if not result.get("success"):
        raise BlenderRenderError("screenshot_viewport", result.get("error") or "Capture failed")

    meta = _parse_script_lines(result.get("output", ""))
    image_path = meta.get("SCREENSHOT_PATH", output_path)
    payload: dict[str, Any] = {
        "success": True,
        "output_path": image_path,
        "workspace_relative_path": workspace_relative_path(image_path),
        "capture_mode": "viewport" if meta.get("CAPTURED") == "True" else "render",
        "session_used": result.get("session_used", False),
        "execution_mode": result.get("mode"),
        "resolution": [resolution_x, resolution_y],
    }
    try:
        image_bytes = Path(image_path).read_bytes()
        payload["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
        payload["mime_type"] = "image/png"
    except OSError as exc:
        payload["warning"] = f"Image saved but could not read for base64: {exc}"
    return payload


@blender_operation("render_multi_angle", log_args=True)
async def render_multi_angle(
    output_dir: str = "",
    angles: int = 4,
    elevation_deg: float = 25.0,
    radius: float = 5.0,
    resolution_x: int = 1024,
    resolution_y: int = 1024,
    prefer_session: bool = True,
) -> dict[str, Any]:
    """Render the scene from multiple camera angles for vision / review loops.

    Frames go into BLENDER_WORKSPACE (``<workspace>/renders/<timestamp>-multi-angle``
    when ``output_dir`` is empty); the payload carries the absolute folder and the
    workspace-relative one.
    """
    import json

    from ..utils.blender_runtime import execute_bpy_script

    destination = resolve_render_output(output_dir, directory=True, default_name="multi-angle")
    output_dir = str(destination.absolute)

    gpu_setup = build_gpu_setup_snippet(engine=None, indent=0)

    script = f"""
import json
import math
import os
import bpy

output_dir = r"{output_dir}"
angles = {angles}
elevation = math.radians({elevation_deg})
radius = {radius}
scene = bpy.context.scene
scene.render.resolution_x = {resolution_x}
scene.render.resolution_y = {resolution_y}
scene.render.image_settings.file_format = 'PNG'

cam = scene.camera
if not cam:
    bpy.ops.object.camera_add(location=(radius, -radius, radius * 0.6))
    cam = bpy.context.active_object
    scene.camera = cam

paths = []
{gpu_setup}
for i in range(angles):
    angle = (i / angles) * 2 * math.pi
    x = radius * math.cos(angle) * math.cos(elevation)
    y = radius * math.sin(angle) * math.cos(elevation)
    z = radius * math.sin(elevation)
    cam.location = (x, y, z)
    import mathutils
    cam.rotation_euler = (mathutils.Vector((0.0, 0.0, 0.0)) - cam.location).to_track_quat('-Z', 'Y').to_euler()
    frame_path = os.path.join(output_dir, f"angle_{{i:02d}}.png")
    scene.render.filepath = frame_path
    bpy.ops.render.render(write_still=True)
    MCP_GPU_VERIFY_AFTER_RENDER()
    paths.append(frame_path)

print("MULTI_ANGLE_PATHS:" + json.dumps(paths))
"""

    result = await execute_bpy_script(
        script,
        script_name="render_multi_angle",
        timeout=max(120, angles * 45),
        prefer_session=prefer_session,
        headless_fallback=True,
    )
    if not result.get("success"):
        raise BlenderRenderError("render_multi_angle", result.get("error") or "Multi-angle render failed")

    meta = _parse_script_lines(result.get("output", ""))
    paths_raw = meta.get("MULTI_ANGLE_PATHS", "[]")
    try:
        paths = json.loads(paths_raw)
    except json.JSONDecodeError:
        paths = []

    return {
        "success": True,
        "output_dir": output_dir,
        "workspace_relative_dir": destination.relative,
        "image_paths": paths,
        "workspace_relative_image_paths": [workspace_relative_path(path) for path in paths],
        "angle_count": len(paths),
        "session_used": result.get("session_used", False),
        "execution_mode": result.get("mode"),
    }
