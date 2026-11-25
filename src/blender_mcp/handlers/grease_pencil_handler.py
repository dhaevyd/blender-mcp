"""Grease Pencil operations handler for Blender MCP.

Targets the GPv3 API introduced in Blender 4.3 and stabilised in 5.x.
Key GPv3 changes vs GPv2:
  - Object type: 'GREASEPENCIL' (was 'GPENCIL')
  - Create op: bpy.ops.object.grease_pencil_add() (was gpencil_add)
  - Layer name attr: layer.name (was layer.info)
  - Modifiers: obj.modifiers (standard, was grease_pencil_modifiers)
  - bpy.ops.gpencil.* operators are removed; use bpy.ops.grease_pencil.*
  - Stroke/point access uses drawing API on each layer frame
"""

import logging
from enum import StrEnum
from typing import Any

from ..compat import *

logger = logging.getLogger(__name__)
from ..decorators import blender_operation
from ..utils.blender_executor import get_blender_executor

_executor = get_blender_executor()


class GPStrokePlacement(StrEnum):
    ORIGIN = "ORIGIN"
    CURSOR = "CURSOR"
    SURFACE = "SURFACE"
    VIEW = "VIEW"


class GPStrokeType(StrEnum):
    LINE = "LINE"
    BOX = "BOX"
    CIRCLE = "CIRCLE"
    ARC = "ARC"
    CURVE = "CURVE"


@blender_operation("create_grease_pencil", log_args=True)
async def create_grease_pencil(
    name: str = "GPencil",
    placement: GPStrokePlacement | str = GPStrokePlacement.ORIGIN,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a new Grease Pencil object (GPv3, Blender 5.x).

    Args:
        name: Name for the new Grease Pencil object
        placement: Where to place it
        **kwargs: location ([x,y,z]), parent (object name)
    """
    location = kwargs.get("location")
    parent = kwargs.get("parent")

    script = f"""
def create_gp():
    if {name!r} in bpy.data.objects:
        return {{"status": "ERROR", "error": f"Object '{name!r}' already exists"}}

    try:
        bpy.ops.object.grease_pencil_add(type='EMPTY')
        gp_obj = bpy.context.active_object
        gp_obj.name = {name!r}

        if {location!r}:
            gp_obj.location = {location!r}
        elif {str(placement)!r} == 'CURSOR':
            gp_obj.location = bpy.context.scene.cursor.location

        # GPv3: ensure at least one layer exists
        if not gp_obj.data.layers:
            gp_obj.data.layers.new("GP_Layer")

        bpy.context.view_layer.objects.active = gp_obj

        if {parent!r}:
            parent_obj = bpy.data.objects.get({parent!r})
            if parent_obj:
                gp_obj.parent = parent_obj

        return {{
            "status": "SUCCESS",
            "object": gp_obj.name,
            "type": gp_obj.type,
            "location": tuple(gp_obj.location),
        }}
    except Exception as e:
        if 'gp_obj' in locals() and gp_obj.name in bpy.data.objects:
            bpy.data.objects.remove(gp_obj)
        return {{"status": "ERROR", "error": str(e)}}

try:
    result = create_gp()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to create Grease Pencil: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("draw_grease_pencil_stroke", log_args=True)
async def draw_grease_pencil_stroke(
    gp_object: str,
    stroke_type: GPStrokeType | str = GPStrokeType.LINE,
    points: list[tuple[float, float, float]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Draw a stroke on a Grease Pencil object (GPv3).

    Args:
        gp_object: Name of the Grease Pencil object
        stroke_type: LINE, BOX, or CIRCLE
        points: List of (x,y,z) tuples for LINE/CURVE strokes
        **kwargs: layer_name, frame_number, color [r,g,b,a], thickness,
                  cyclic, width (BOX), height (BOX), radius (CIRCLE),
                  segments (CIRCLE)
    """
    if points is None:
        points = []

    layer_name = kwargs.get("layer_name", "GP_Layer")
    frame_number = kwargs.get("frame_number", 1)
    color = kwargs.get("color", [0.0, 0.0, 0.0, 1.0])
    thickness = kwargs.get("thickness", 1.0)
    cyclic = kwargs.get("cyclic", False)

    script = f"""
import math

def draw_stroke():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "Grease Pencil object not found (expected type GREASEPENCIL)"}}

    gp_data = gp_obj.data

    # GPv3: get or create layer by name
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        layer = gp_data.layers.new({layer_name!r})

    # GPv3: get or create a frame at the requested frame number
    frame = None
    for f in layer.frames:
        if f.frame_number == {frame_number}:
            frame = f
            break
    if frame is None:
        frame = layer.frames.new({frame_number})

    # Build point list depending on stroke type
    stroke_type = {str(stroke_type)!r}
    raw_points = []

    if stroke_type == 'LINE':
        raw_points = {points!r}
        if len(raw_points) < 2:
            return {{"status": "ERROR", "error": "At least 2 points required for LINE stroke"}}

    elif stroke_type == 'BOX':
        w = {kwargs.get("width", 2.0)!r}
        h = {kwargs.get("height", 2.0)!r}
        raw_points = [
            (-w/2, -h/2, 0), (w/2, -h/2, 0),
            (w/2,  h/2, 0), (-w/2, h/2, 0),
        ]

    elif stroke_type == 'CIRCLE':
        radius = {kwargs.get("radius", 1.0)!r}
        segs   = {kwargs.get("segments", 32)!r}
        raw_points = [
            (math.cos(2*math.pi*i/segs)*radius,
             math.sin(2*math.pi*i/segs)*radius, 0)
            for i in range(segs)
        ]

    # GPv3 drawing API: use a Drawing on the frame
    # Each frame exposes drawing() which is a GreasePencilDrawing
    drawing = frame.drawing

    # Add a new curve (stroke) to the drawing
    old_count = len(drawing.strokes) if hasattr(drawing, 'strokes') else 0
    drawing.add_strokes([len(raw_points)])
    stroke = drawing.strokes[old_count]

    for i, pt in enumerate(raw_points):
        stroke.points[i].position = pt
        stroke.points[i].radius = {float(thickness)!r}
        stroke.points[i].opacity = 1.0

    stroke.cyclic = {cyclic!r}

    # Assign material — create a simple GP material if needed
    color = {color!r}
    mat_name = f"GP_Mat_{{int(color[0]*255)}}_{{int(color[1]*255)}}_{{int(color[2]*255)}}"
    if mat_name not in bpy.data.materials:
        mat = bpy.data.materials.new(mat_name)
        bpy.data.materials.create_gpencil_data(mat)
        mat.grease_pencil.color = color[:4]
    else:
        mat = bpy.data.materials[mat_name]

    if mat.name not in [m.name for m in gp_data.materials if m]:
        gp_data.materials.append(mat)

    mat_idx = next((i for i, m in enumerate(gp_data.materials) if m and m.name == mat.name), 0)
    stroke.material_index = mat_idx

    return {{
        "status": "SUCCESS",
        "object": gp_obj.name,
        "layer": layer.name,
        "frame": {frame_number},
        "stroke_points": len(stroke.points),
        "material": mat.name,
    }}

try:
    result = draw_stroke()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to draw Grease Pencil stroke: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("set_gp_material", log_args=True)
async def set_gp_material(
    gp_object: str,
    material_name: str = "GP_Material",
    stroke_color: list[float] | None = None,
    fill_color: list[float] | None = None,
) -> dict[str, Any]:
    """Create and assign a Grease Pencil material (GPv3)."""
    if stroke_color is None:
        stroke_color = [0.0, 0.0, 0.0, 1.0]
    if fill_color is None:
        fill_color = [0.0, 0.0, 0.0, 0.0]

    script = f"""
def set_gp_mat():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found (expected type GREASEPENCIL)"}}

    gp_data = gp_obj.data
    mat_name = {material_name!r}

    if mat_name in bpy.data.materials:
        mat = bpy.data.materials[mat_name]
    else:
        mat = bpy.data.materials.new(mat_name)
        # GPv3: initialise GP material data block
        bpy.data.materials.create_gpencil_data(mat)

    gp_mat = mat.grease_pencil
    gp_mat.color = {stroke_color[:4]!r}
    gp_mat.show_stroke = True

    if {fill_color[3]!r} > 0.0:
        gp_mat.show_fill = True
        gp_mat.fill_color = {fill_color[:4]!r}
    else:
        gp_mat.show_fill = False

    if mat.name not in [m.name for m in gp_data.materials if m]:
        gp_data.materials.append(mat)

    return {{"status": "SUCCESS", "material": mat.name,
             "stroke": {stroke_color!r}, "fill": {fill_color!r}}}

try:
    result = set_gp_mat()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to set GP material: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("set_gp_layer", log_args=True)
async def set_gp_layer(
    gp_object: str,
    layer_name: str = "GP_Layer",
    from_layer: str = "",
    to_layer: str = "",
) -> dict[str, Any]:
    """Create or query a Grease Pencil layer (GPv3)."""
    script = f"""
def set_gp_layers():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        layer = gp_data.layers.new({layer_name!r})

    return {{
        "status": "SUCCESS",
        "layer": layer.name,
        "opacity": layer.opacity,
        "visible": not layer.hide,
    }}

try:
    result = set_gp_layers()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to set GP layer: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("animate_gp_stroke", log_args=True)
async def animate_gp_stroke(
    gp_object: str,
    layer_name: str = "GP_Layer",
    frame_number: int = 1,
) -> dict[str, Any]:
    """Keyframe Grease Pencil object transforms on a specific frame (GPv3)."""
    script = f"""
def animate_gp():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        layer = gp_data.layers.new({layer_name!r})

    # Ensure a frame exists
    frame = None
    for f in layer.frames:
        if f.frame_number == {frame_number}:
            frame = f
            break
    if frame is None:
        frame = layer.frames.new({frame_number})

    bpy.context.scene.frame_set({frame_number})
    gp_obj.keyframe_insert(data_path="location")
    gp_obj.keyframe_insert(data_path="rotation_euler")
    gp_obj.keyframe_insert(data_path="scale")

    return {{"status": "SUCCESS", "object": gp_obj.name, "layer": layer.name, "frame": {frame_number}}}

try:
    result = animate_gp()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to animate GP stroke: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("onion_skinning_gp", log_args=True)
async def onion_skinning_gp(
    gp_object: str,
    before_frames: int = 3,
    after_frames: int = 3,
) -> dict[str, Any]:
    """Enable onion skinning on a Grease Pencil object (GPv3)."""
    script = f"""
def onion_skin():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    # GPv3 onion skinning properties
    gp_data.use_onion_skinning = True
    # before/after counts live on the data block in GPv3
    if hasattr(gp_data, 'ghost_before_range'):
        gp_data.ghost_before_range = {before_frames}
        gp_data.ghost_after_range  = {after_frames}

    return {{
        "status": "SUCCESS",
        "before_frames": {before_frames},
        "after_frames": {after_frames},
        "onion_skin_enabled": True,
    }}

try:
    result = onion_skin()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to set onion skinning: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("add_gp_modifier", log_args=True)
async def add_gp_modifier(
    gp_object: str,
    modifier_type: str = "GREASE_PENCIL_BUILD",
    settings: str = "",
) -> dict[str, Any]:
    """Add a modifier to a Grease Pencil object (GPv3).

    GPv3 modifier type names use the GREASE_PENCIL_ prefix, e.g.:
      GREASE_PENCIL_BUILD, GREASE_PENCIL_NOISE, GREASE_PENCIL_SIMPLIFY,
      GREASE_PENCIL_SMOOTH, GREASE_PENCIL_SUBDIV, GREASE_PENCIL_TINT
    """
    script = f"""
def add_gp_mod():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    # GPv3: modifiers live on obj.modifiers, same as mesh objects
    mod = gp_obj.modifiers.new(name={modifier_type + "_mod"!r}, type={modifier_type!r})
    if not mod:
        return {{"status": "ERROR", "error": f"Failed to create modifier of type '{modifier_type!r}'"}}

    return {{"status": "SUCCESS", "modifier": mod.name, "type": {modifier_type!r}}}

try:
    result = add_gp_mod()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to add GP modifier: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("fill_gp_region", log_args=True)
async def fill_gp_region(
    gp_object: str,
    layer_name: str = "GP_Layer",
    frame_number: int = 1,
    fill_color: list[float] | None = None,
) -> dict[str, Any]:
    """Assign a fill material to a Grease Pencil layer (GPv3)."""
    if fill_color is None:
        fill_color = [0.5, 0.5, 0.5, 1.0]

    script = f"""
def fill_region():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        return {{"status": "ERROR", "error": "Layer not found"}}

    fill_rgba = {fill_color[:4]!r}
    mat_name = f"Fill_Mat_{{int(fill_rgba[0]*255)}}_{{int(fill_rgba[1]*255)}}_{{int(fill_rgba[2]*255)}}"

    if mat_name not in bpy.data.materials:
        mat = bpy.data.materials.new(mat_name)
        bpy.data.materials.create_gpencil_data(mat)
        mat.grease_pencil.show_fill = True
        mat.grease_pencil.fill_color = fill_rgba
        mat.grease_pencil.show_stroke = False
    else:
        mat = bpy.data.materials[mat_name]

    if mat.name not in [m.name for m in gp_data.materials if m]:
        gp_data.materials.append(mat)

    return {{"status": "SUCCESS", "material": mat.name, "color": fill_rgba}}

try:
    result = fill_region()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to fill GP region: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("interpolate_gp_frames", log_args=True)
async def interpolate_gp_frames(
    gp_object: str,
    layer_name: str = "GP_Layer",
    frame_start: int = 1,
    frame_end: int = 10,
    num_frames: int = 5,
) -> dict[str, Any]:
    """Report frame range info on a Grease Pencil layer (GPv3).

    Note: GPv3 removed the old gpencil.interpolate_sequence operator.
    This function reports the available frame range; actual interpolation
    is performed by Blender's built-in timeline tools.
    """
    script = f"""
def interpolate_gp():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        return {{"status": "ERROR", "error": "Layer not found"}}

    frame_a = next((f for f in layer.frames if f.frame_number == {frame_start}), None)
    frame_b = next((f for f in layer.frames if f.frame_number == {frame_end}), None)
    if not frame_a or not frame_b:
        return {{"status": "ERROR", "error": "Start or end frame not found"}}

    bpy.context.view_layer.objects.active = gp_obj
    bpy.context.scene.frame_set({frame_start})

    return {{
        "status": "SUCCESS",
        "layer": layer.name,
        "frame_start": {frame_start},
        "frame_end": {frame_end},
        "note": "GPv3 — use Blender timeline tools for in-between frame generation",
    }}

try:
    result = interpolate_gp()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to interpolate GP frames: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("delete_gp_strokes", log_args=True)
async def delete_gp_strokes(
    gp_object: str,
    layer_name: str = "GP_Layer",
    selection_type: str = "ALL",
) -> dict[str, Any]:
    """Delete strokes from a Grease Pencil layer (GPv3)."""
    script = f"""
def delete_gp_strokes_fn():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layer = gp_data.layers.get({layer_name!r})
    if not layer:
        return {{"status": "ERROR", "error": "Layer not found"}}

    count = 0
    for frame in layer.frames:
        drawing = frame.drawing
        if {selection_type!r} == 'ALL':
            count += len(drawing.strokes)
            # GPv3: remove all strokes by removing each one
            while len(drawing.strokes) > 0:
                drawing.remove_stroke(drawing.strokes[0])
        elif {selection_type!r} == 'VISIBLE':
            for stroke in list(drawing.strokes):
                if not getattr(stroke, 'hide', False):
                    drawing.remove_stroke(stroke)
                    count += 1

    return {{"status": "SUCCESS", "deleted": count, "layer": layer.name}}

try:
    result = delete_gp_strokes_fn()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to delete GP strokes: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("list_gp_layers", log_args=True)
async def list_gp_layers(gp_object: str) -> dict[str, Any]:
    """List all layers and frame info on a Grease Pencil object (GPv3)."""
    script = f"""
def list_layers():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "GP object not found"}}

    gp_data = gp_obj.data
    layers_info = []
    for layer in gp_data.layers:
        frames = [f.frame_number for f in layer.frames]
        stroke_total = sum(len(f.drawing.strokes) for f in layer.frames)
        layers_info.append({{
            "name": layer.name,
            "opacity": layer.opacity,
            "visible": not layer.hide,
            "locked": layer.lock,
            "frames": sorted(frames),
            "stroke_count": stroke_total,
        }})

    return {{"status": "SUCCESS", "layers": layers_info, "total_layers": len(layers_info)}}

try:
    result = list_layers()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to list GP layers: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("convert_grease_pencil", log_args=True)
async def convert_grease_pencil(
    gp_object: str,
    target_type: str = "MESH",
    **kwargs: Any,
) -> dict[str, Any]:
    """Convert a Grease Pencil object to another type (GPv3).

    Args:
        gp_object: Name of the Grease Pencil object
        target_type: 'MESH' or 'CURVE'
        **kwargs: keep_original (bool), thickness (float)
    """
    keep_original = kwargs.get("keep_original", False)

    script = f"""
def convert_gp():
    gp_obj = bpy.data.objects.get({gp_object!r})
    if not gp_obj or gp_obj.type != 'GREASEPENCIL':
        return {{"status": "ERROR", "error": "Grease Pencil object not found (expected type GREASEPENCIL)"}}

    try:
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.objects.active = gp_obj
        gp_obj.select_set(True)
        original_name = gp_obj.name

        target = {target_type!r}
        if target == 'MESH':
            # GPv3: convert GP to mesh via standard object convert
            bpy.ops.object.convert(target='MESH')
        elif target == 'CURVE':
            bpy.ops.object.convert(target='CURVE')
        else:
            return {{"status": "ERROR", "error": f"Unsupported target type: {{target}}"}}

        converted_obj = bpy.context.active_object
        if converted_obj and converted_obj != gp_obj:
            converted_obj.name = f"{{original_name}}_{{target.lower()}}"

        if not {keep_original!r} and bpy.data.objects.get(original_name):
            orig = bpy.data.objects.get(original_name)
            if orig and orig != converted_obj:
                bpy.data.objects.remove(orig)

        return {{
            "status": "SUCCESS",
            "original_object": original_name if {keep_original!r} else None,
            "converted_object": converted_obj.name if converted_obj else None,
            "target_type": target,
        }}
    except Exception as e:
        return {{"status": "ERROR", "error": str(e)}}

try:
    result = convert_gp()
    print(str(result))
except Exception as e:
    print(str({{'status': 'ERROR', 'error': str(e)}}))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to convert Grease Pencil: {e!s}")
        return {"status": "ERROR", "error": str(e)}
