"""Particle system operations handler for Blender MCP."""

import logging
from enum import StrEnum
from typing import Any

from ..decorators import blender_operation

logger = logging.getLogger(__name__)
from ..utils.blender_executor import get_blender_executor

_executor = get_blender_executor()


class ParticleType(StrEnum):
    EMITTER = "EMITTER"
    # HAIR is the legacy particle-hair system, deprecated in Blender 4.2+/5.x.
    # Use particle_type="HAIR_CURVES" to create a modern Hair Curves object instead.
    HAIR = "HAIR"
    HAIR_CURVES = "HAIR_CURVES"  # Blender 4.2+/5.x: CURVES object via curves_empty_hair_add
    KEYED = "KEYED"
    FLUID_FLOW = "FLUID_FLOW"


class ParticleEmitFrom(StrEnum):
    VERT = "VERT"
    FACE = "FACE"
    VOLUME = "VOLUME"


@blender_operation("create_particle_system", log_args=True)
async def create_particle_system(
    object_name: str,
    system_name: str = "ParticleSystem",
    particle_type: ParticleType | str = ParticleType.EMITTER,
    count: int = 1000,
    frame_start: int = 1,
    frame_end: int = 200,
    lifetime: float = 50.0,
    emit_from: ParticleEmitFrom | str = ParticleEmitFrom.FACE,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a particle system on an object.

    For hair in Blender 4.2+/5.x pass particle_type='HAIR_CURVES' to create
    a modern Hair Curves (CURVES) object attached to the surface mesh.
    The legacy HAIR particle type still works but is deprecated.
    """
    # Route HAIR_CURVES to the new Blender 4.2+/5.x Hair Curves path.
    if str(particle_type) == "HAIR_CURVES":
        script = f"""

def create_hair_curves():
    obj = bpy.data.objects.get({object_name!r})
    if not obj:
        return {{'status': 'ERROR', 'error': 'Object not found'}}

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Hair Curves API — Blender 4.2+/5.x
    if not hasattr(bpy.ops.object, 'curves_empty_hair_add'):
        return {{'status': 'ERROR', 'error': 'Hair Curves require Blender 4.2 or newer'}}

    bpy.ops.object.curves_empty_hair_add()
    hair_obj = bpy.context.active_object
    hair_obj.name = {system_name!r}

    return {{
        'status': 'SUCCESS',
        'system': hair_obj.name,
        'type': 'HAIR_CURVES',
        'object_type': hair_obj.type,
    }}

try:
    result = create_hair_curves()
except Exception as e:
    result = {{'status': 'ERROR', 'error': str(e)}}

print(str(result))
"""
        try:
            output = await _executor.execute_script(script)
            return {"status": "SUCCESS", "output": output}
        except Exception as e:
            logger.error(f"Failed to create hair curves: {e!s}")
            return {"status": "ERROR", "error": str(e)}

    script = f"""

def create_particle_system():
    obj = bpy.data.objects.get({object_name!r})
    if not obj:
        return {{'status': 'ERROR', 'error': 'Object not found'}}

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Legacy HAIR particle type — deprecated in Blender 4.2+/5.x.
    # Prefer particle_type='HAIR_CURVES' for new projects on Blender 4.2+.
    if obj.particle_systems:
        bpy.ops.object.particle_system_add()

    ps = obj.particle_systems[-1]
    ps.name = {system_name!r}
    settings = ps.settings

    settings.type = {str(particle_type)!r}
    settings.count = {count}
    settings.frame_start = {frame_start}
    settings.frame_end = {frame_end}
    settings.lifetime = {lifetime}
    settings.particle_size = {kwargs.get("size", 0.1)!r}
    settings.emit_from = {str(emit_from)!r}

    return {{'status': 'SUCCESS', 'system': ps.name, 'type': {str(particle_type)!r}}}

try:
    result = create_particle_system()
except Exception as e:
    result = {{'status': 'ERROR', 'error': str(e)}}

print(str(result))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to create particle system: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("bake_particles", log_args=True)
async def bake_particles(
    object_name: str,
    system_name: str | None = None,
    frame_start: int = 1,
    frame_end: int = 250,
    **kwargs: Any,
) -> dict[str, Any]:
    """Bake particle simulation."""
    script = f"""

def bake_particles():
    obj = bpy.data.objects.get('{object_name}')
    if not obj:
        return {{'status': 'ERROR', 'error': 'Object not found'}}

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    systems_to_bake = []
    if '{system_name}':
        ps = obj.particle_systems.get('{system_name}')
        if ps:
            systems_to_bake.append(ps)
    else:
        systems_to_bake = list(obj.particle_systems)

    if not systems_to_bake:
        return {{'status': 'ERROR', 'error': 'No particle systems found'}}

    results = []
    for ps in systems_to_bake:
        obj.particle_systems.active = ps
        bpy.ops.ptcache.free_bake()
        bpy.ops.ptcache.bake(
            bake=True,
            frame_start={frame_start},
            frame_end={frame_end},
            use_memory_cache={str(kwargs.get("use_memory_cache", True)).lower()}
        )
        results.append(ps.name)

    return {{
        'status': 'SUCCESS',
        'baked_systems': results,
        'frame_range': ({frame_start}, {frame_end})
    }}

try:
    result = bake_particles()
except Exception as e:
    result = {{'status': 'ERROR', 'error': str(e)}}

print(str(result))
"""
    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to bake particles: {e!s}")
        return {"status": "ERROR", "error": str(e)}
