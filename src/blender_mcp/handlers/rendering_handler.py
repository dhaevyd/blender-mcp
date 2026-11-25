"""
Rendering Handler for Blender-MCP

This module provides advanced rendering functionality for Blender scenes,
including EEVEE and Cycles render engine configuration, post-processing effects,
render layer management, and output configuration.
"""

import json
import logging
from typing import Any

from blender_mcp.utils.blender_executor import BlenderExecutor
from blender_mcp.utils.gpu_render import build_gpu_setup_snippet, parse_render_device

from ..compat import *
from ..decorators import blender_operation
from .render_handler import resolve_render_output

# Initialize logger
logger = logging.getLogger(__name__)

# Initialize Blender executor
_executor = BlenderExecutor()

# Constants for render engines
# Blender 4.2+ replaced BLENDER_EEVEE with BLENDER_EEVEE_NEXT (EEVEE Next).
# Blender 5.x only ships EEVEE Next; the old ID no longer works.
RENDER_ENGINE_EEVEE = "BLENDER_EEVEE_NEXT"
RENDER_ENGINE_CYCLES = "CYCLES"
RENDER_ENGINE_WORKBENCH = "BLENDER_WORKBENCH"


class RenderEngineType:
    """Enum for render engine types."""

    EEVEE = RENDER_ENGINE_EEVEE
    CYCLES = RENDER_ENGINE_CYCLES
    WORKBENCH = RENDER_ENGINE_WORKBENCH


@blender_operation("set_render_engine", log_args=True)
async def set_render_engine(
    engine: RenderEngineType | str = RENDER_ENGINE_EEVEE,
    device: str = "GPU",
    use_denoising: bool = True,
    samples: int = 64,
    resolution_x: int = 1920,
    resolution_y: int = 1080,
    **kwargs: Any,
) -> dict[str, Any]:
    """Set the active render engine and its basic configuration.

    Args:
        engine: Render engine to use (EEVEE, CYCLES, or WORKBENCH)
        device: Compute device to use ('CPU', 'GPU', or 'GPU_COMPATIBLE')
        use_denoising: Enable denoising for supported engines
        samples: Number of samples per pixel
        **kwargs: Additional engine-specific settings
            - use_adaptive_sampling: For Cycles (default: True)
            - adaptive_threshold: For Cycles (default: 0.01)
            - tile_size: For Cycles (default: 2048)
            - use_motion_blur: For EEVEE (default: True)
            - motion_blur_steps: For EEVEE (default: 1)
            - use_bloom: For EEVEE (default: True)
            - bloom_threshold: For EEVEE (default: 1.0)

    Returns:
        Dict containing operation status and render settings
    """
    engine = str(engine).upper()
    valid_engines = [RENDER_ENGINE_EEVEE, RENDER_ENGINE_CYCLES, RENDER_ENGINE_WORKBENCH]
    if engine not in valid_engines:
        return {
            "status": "ERROR",
            "error": f"Invalid render engine. Must be one of: {', '.join(valid_engines)}",
        }

    # Get additional settings with defaults
    use_adaptive_sampling = kwargs.get("use_adaptive_sampling", True)
    adaptive_threshold = kwargs.get("adaptive_threshold", 0.01)
    tile_size = kwargs.get("tile_size", 2048)

    # The shared setup selects the vendor's backend and disables the CPU device.
    # It fails when a detected GPU cannot be enabled (MCP_RENDER_REQUIRE_GPU defaults on).
    gpu_setup = build_gpu_setup_snippet(device=device, engine=engine, indent=4)

    script = f"""

def configure_render_engine():
    scene = bpy.context.scene

    # Blender 5.x only ships EEVEE Next (BLENDER_EEVEE_NEXT).
    # Accept the legacy alias gracefully in case a caller passes the old ID.
    _engine = '{engine}'
    if _engine == 'BLENDER_EEVEE':
        _engine = 'BLENDER_EEVEE_NEXT'
    scene.render.engine = _engine

    # Common settings
    scene.render.resolution_x = {resolution_x}
    scene.render.resolution_y = {resolution_y}
    scene.render.resolution_percentage = 100

{gpu_setup}
    if '{engine}' == '{RENDER_ENGINE_CYCLES}':
        # Cycles-specific settings
        scene.cycles.samples = {samples}
        scene.cycles.use_denoising = {use_denoising}
        scene.cycles.use_adaptive_sampling = {use_adaptive_sampling}
        scene.cycles.adaptive_threshold = {adaptive_threshold}
        scene.cycles.tile_size = {tile_size}

    elif _engine.startswith('BLENDER_EEVEE'):
        # EEVEE Next (Blender 4.2+/5.x) settings.
        # Bloom, motion-blur and SSR moved out of scene.eevee in 4.2;
        # configure them via the compositor if needed.
        scene.eevee.taa_render_samples = {samples}

    return {{
        'status': 'SUCCESS',
        'engine': _engine,
        'device': MCP_RENDER_DEVICE_INFO.get('device'),
        'backend': MCP_RENDER_DEVICE_INFO.get('backend'),
        'gpu_devices': MCP_RENDER_DEVICE_INFO.get('devices', []),
        'samples': {samples},
        'use_denoising': {use_denoising},
    }}

try:
    result = configure_render_engine()
except Exception as e:
    result = {{
        'status': 'ERROR',
        'error': str(e)
    }}

print(str(result))
"""

    try:
        output = await _executor.execute_script(script)
        return {
            "status": "SUCCESS",
            "output": output,
            "render_device": parse_render_device(output) or {},
        }
    except Exception as e:
        logger.error(f"Failed to set render engine: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("configure_render_layers", log_args=True)
async def configure_render_layers(
    layer_name: str = "RenderLayer",
    use_pass_combined: bool = True,
    use_pass_z: bool = True,
    use_pass_normal: bool = True,
    use_pass_diffuse: bool = True,
    use_pass_glossy: bool = True,
    use_pass_ambient_occlusion: bool = True,
    use_pass_shadow: bool = True,
    use_pass_emit: bool = True,
    use_pass_environment: bool = True,
    use_pass_indirect: bool = True,
    use_pass_reflection: bool = True,
    use_pass_refraction: bool = True,
    use_pass_uv: bool = False,
    use_pass_mist: bool = False,
    use_pass_object_index: bool = False,
    use_pass_material_index: bool = False,
    use_pass_vector: bool = False,
    use_pass_cryptomatte_object: bool = False,
    use_pass_cryptomatte_material: bool = False,
    use_pass_cryptomatte_asset: bool = False,
    use_pass_shadow_catcher: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Configure render layers and passes for advanced rendering.

    Args:
        layer_name: Name of the render layer to configure
        use_pass_*: Enable/disable specific render passes
        **kwargs: Additional render layer settings
            - use_solid: Enable solid geometry (default: True)
            - use_halo: Enable halo effects (default: True)
            - use_ztransp: Use Z transparency (default: True)
            - use_strand: Enable strand rendering (default: True)
            - use_freestyle: Enable Freestyle rendering (default: False)
            - use_sky: Use world background (default: True)
            - use_edge_enhance: Enable edge enhancement (default: False)
            - use_all_z: Use all Z values (default: False)
            - exclude_raytrace: Exclude from raytracing (default: False)
            - light_override: Override light settings (default: None)

    Returns:
        Dict containing operation status and render layer configuration
    """
    # Get additional settings with defaults
    use_freestyle = kwargs.get("use_freestyle", False)

    script = f"""

def configure_layers():
    scene = bpy.context.scene

    # Get or create the render layer
    rl = scene.view_layers.get({layer_name!r})
    if not rl:
        rl = scene.view_layers.new({layer_name!r})

    scene.view_layers.active = rl

    # Freestyle (still present in 5.x)
    rl.use_freestyle = {str(use_freestyle).lower()}

    # Render passes — in Blender 4.x+/5.x these live directly on the
    # view layer, not on rl.cycles.*. Guard each with hasattr so the script
    # does not error on a build that happens to lack a pass.
    _pass_map = {{
        'use_pass_combined':              {str(use_pass_combined).lower()},
        'use_pass_z':                     {str(use_pass_z).lower()},
        'use_pass_normal':                {str(use_pass_normal).lower()},
        'use_pass_diffuse_direct':        {str(use_pass_diffuse).lower()},
        'use_pass_diffuse_indirect':      {str(use_pass_diffuse).lower()},
        'use_pass_diffuse_color':         {str(use_pass_diffuse).lower()},
        'use_pass_glossy_direct':         {str(use_pass_glossy).lower()},
        'use_pass_glossy_indirect':       {str(use_pass_glossy).lower()},
        'use_pass_glossy_color':          {str(use_pass_glossy).lower()},
        'use_pass_ambient_occlusion':     {str(use_pass_ambient_occlusion).lower()},
        'use_pass_shadow':                {str(use_pass_shadow).lower()},
        'use_pass_emit':                  {str(use_pass_emit).lower()},
        'use_pass_environment':           {str(use_pass_environment).lower()},
        'use_pass_uv':                    {str(use_pass_uv).lower()},
        'use_pass_mist':                  {str(use_pass_mist).lower()},
        'use_pass_object_index':          {str(use_pass_object_index).lower()},
        'use_pass_material_index':        {str(use_pass_material_index).lower()},
        'use_pass_vector':                {str(use_pass_vector).lower()},
        'use_pass_cryptomatte_object':    {str(use_pass_cryptomatte_object).lower()},
        'use_pass_cryptomatte_material':  {str(use_pass_cryptomatte_material).lower()},
        'use_pass_cryptomatte_asset':     {str(use_pass_cryptomatte_asset).lower()},
        'use_pass_shadow_catcher':        {str(use_pass_shadow_catcher).lower()},
    }}
    for attr, val in _pass_map.items():
        # Try view layer first (4.x+), then rl.cycles for older builds
        if hasattr(rl, attr):
            setattr(rl, attr, val)
        elif hasattr(rl, 'cycles') and hasattr(rl.cycles, attr):
            setattr(rl.cycles, attr, val)

    return {{
        'status': 'SUCCESS',
        'render_layer': rl.name,
        'use_freestyle': {str(use_freestyle).lower()},
    }}

try:
    result = configure_layers()
except Exception as e:
    result = {{'status': 'ERROR', 'error': str(e)}}

print(str(result))
"""

    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to configure render layers: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("configure_output_settings", log_args=True)
async def configure_output_settings(
    output_dir: str = "",
    file_format: str = "",
    frame_start: int = 0,
    frame_end: int = 0,
) -> dict[str, Any]:
    """Configure where the scene writes animation/sequence output.

    The folder is resolved by the shared render-output resolver, so it always lands
    inside BLENDER_WORKSPACE (``<workspace>/renders/<timestamp>-animation/`` when
    ``output_dir`` is empty) and invisible-to-the-Gallery CWD paths cannot be set.

    Args:
        output_dir: Folder for the rendered frames. Workspace-relative paths are
            anchored on BLENDER_WORKSPACE; absolute paths must already be inside it.
        file_format: Optional image settings file format (e.g. PNG, JPEG, OPEN_EXR);
            left unchanged when empty.
        frame_start: Optional frame range start; 0 leaves the scene value untouched.
        frame_end: Optional frame range end; 0 leaves the scene value untouched.

    Returns:
        Dict with the absolute and workspace-relative output folder plus script output

    Raises:
        RenderOutputPathError: If the folder escapes the workspace
    """
    destination = resolve_render_output(output_dir, directory=True, default_name="animation")
    prefix = destination.absolute.as_posix().rstrip("/") + "/"
    relative_prefix = f"{(destination.relative or '').rstrip('/')}/"

    script = f"""
import bpy

scene = bpy.context.scene
scene.render.filepath = {json.dumps(prefix)}

_format = {json.dumps((file_format or "").strip().upper())}
if _format:
    scene.render.image_settings.file_format = _format
if {int(frame_start)} > 0:
    scene.frame_start = {int(frame_start)}
if {int(frame_end)} > 0:
    scene.frame_end = {int(frame_end)}

print("OUTPUT_PATH:" + scene.render.filepath)
print("OUTPUT_FRAME_RANGE:" + str(scene.frame_start) + "-" + str(scene.frame_end))
"""

    try:
        output = await _executor.execute_script(script)
        return {
            "status": "SUCCESS",
            "output_dir": prefix,
            "workspace_relative_path": relative_prefix,
            "frame_range": f"{frame_start or 'unchanged'}-{frame_end or 'unchanged'}",
            "output": output,
        }
    except Exception as e:
        logger.error(f"Failed to configure output settings: {e!s}")
        return {"status": "ERROR", "error": str(e)}


@blender_operation("setup_post_processing", log_args=True)
async def setup_post_processing(
    use_bloom: bool = True,
    use_ssao: bool = True,
    use_motion_blur: bool = False,
    use_dof: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Configure post-processing effects for the render.

    Args:
        use_bloom: Enable bloom effect (default: True)
        use_ssao: Enable screen space ambient occlusion (default: True)
        use_motion_blur: Enable motion blur (default: False)
        use_dof: Enable depth of field (default: False)
        **kwargs: Additional post-processing settings
            - bloom_threshold: Bloom intensity threshold (default: 1.0)
            - bloom_radius: Bloom radius (default: 6.5)
            - bloom_color: Bloom color (RGB tuple, default: (1.0, 1.0, 1.0))
            - ssao_factor: SSAO factor (default: 1.0)
            - ssao_distance: SSAO distance (default: 0.2)
            - motion_blur_shutter: Motion blur shutter speed (default: 0.5)
            - dof_focus_object: Object to focus on (name string, default: None)
            - dof_focus_distance: Manual focus distance (default: 10.0)
            - dof_fstop: Aperture f-stop (default: 2.8)
            - dof_blades: Aperture blades (default: 0 for circular)
            - use_volumetric_lights: Enable volumetric lighting (default: True)
            - use_volumetric_shadows: Enable volumetric shadows (default: True)
            - volumetric_tile_size: Volumetric tile size (default: '2')
            - volumetric_samples: Volumetric samples (default: 4)
            - use_subsurface_scattering: Enable SSS (default: True)
            - sss_samples: SSS samples (default: 4)
            - use_screen_space_reflections: Enable SSR (default: True)
            - ssr_quality: SSR quality (0.0-1.0, default: 0.5)
            - ssr_thickness: SSR thickness (default: 0.2)
            - ssr_max_roughness: SSR max roughness (0.0-1.0, default: 0.5)
            - use_taa: Enable temporal anti-aliasing (default: True)
            - taa_samples: TAA samples (default: 16)
            - use_fxaa: Enable FXAA (default: False)
            - use_high_quality_normals: Use high quality normals (default: True)
            - use_soft_shadows: Use soft shadows (default: True)
            - shadow_quality: Shadow quality ('LOW', 'MEDIUM', 'HIGH', 'ULTRA')
            - use_ambient_occlusion: Enable ambient occlusion (default: True)
            - ao_factor: AO factor (default: 1.0)
            - ao_distance: AO distance (default: 0.2)
            - use_glow: Enable glow effect (default: False)
            - glow_threshold: Glow threshold (default: 1.0)
            - glow_color: Glow color (RGB tuple, default: (1.0, 1.0, 1.0))
            - glow_radius: Glow radius (default: 3.0)
            - use_vignette: Enable vignette (default: False)
            - vignette_scale: Vignette scale (default: 1.0)
            - use_color_grading: Enable color grading (default: False)
            - exposure: Exposure compensation (default: 0.0)
            - gamma: Gamma correction (default: 1.0)
            - contrast: Contrast (default: 1.0)
            - saturation: Saturation (default: 1.0)
            - brightness: Brightness (default: 1.0)
            - rgb_curves: RGB curves as list of control points
            - use_lens_distortion: Enable lens distortion (default: False)
            - distortion: Lens distortion amount (-1.0 to 1.0, default: 0.0)
            - dispersion: Chromatic aberration (0.0-1.0, default: 0.0)
            - use_film_grain: Enable film grain (default: False)
            - grain_scale: Grain scale (default: 1.0)
            - grain_strength: Grain strength (default: 0.1)
            - use_color_ramp: Enable color ramp (default: False)
            - color_ramp: Color ramp as list of (position, (r,g,b)) tuples
            - use_z_combine: Enable Z-combine for mist/atmosphere (default: False)
            - mist_start: Mist start distance (default: 5.0)
            - mist_depth: Mist depth (default: 25.0)
            - mist_falloff: Mist falloff type ('QUADRATIC', 'LINEAR', 'INVERSE_QUADRATIC')
            - use_freestyle: Enable Freestyle rendering (default: False)
            - freestyle_line_sets: List of line set configurations
            - use_stereo: Enable stereo rendering (default: False)
            - stereo_mode: Stereo mode ('STEREO', 'ANAGLYPH', 'INTERLACED')
            - stereo_eye_separation: Eye separation (default: 0.065)
            - use_high_bitdepth: Use high bit-depth output (default: False)
            - use_transparent: Enable transparent background (default: False)
            - use_sequencer: Enable sequencer (default: False)
            - use_compositing: Enable node-based compositing (default: True)
            - use_auto_tile: Enable auto-tile rendering (default: False)
            - tile_size: Tile size for rendering (default: 2048)

    Returns:
        Dict containing operation status and post-processing settings
    """
    # Get all settings with defaults
    settings = {
        # Bloom
        "bloom_threshold": kwargs.get("bloom_threshold", 1.0),
        "bloom_radius": kwargs.get("bloom_radius", 6.5),
        "bloom_color": kwargs.get("bloom_color", (1.0, 1.0, 1.0)),
        # SSAO
        "ssao_factor": kwargs.get("ssao_factor", 1.0),
        "ssao_distance": kwargs.get("ssao_distance", 0.2),
        # Motion Blur
        "motion_blur_shutter": kwargs.get("motion_blur_shutter", 0.5),
        # Depth of Field
        "dof_focus_object": kwargs.get("dof_focus_object", ""),
        "dof_focus_distance": kwargs.get("dof_focus_distance", 10.0),
        "dof_fstop": kwargs.get("dof_fstop", 2.8),
        "dof_blades": kwargs.get("dof_blades", 0),
        # Volumetrics
        "use_volumetric_lights": kwargs.get("use_volumetric_lights", True),
        "use_volumetric_shadows": kwargs.get("use_volumetric_shadows", True),
        "volumetric_tile_size": kwargs.get("volumetric_tile_size", "2"),
        "volumetric_samples": kwargs.get("volumetric_samples", 4),
        # SSS
        "use_subsurface_scattering": kwargs.get("use_subsurface_scattering", True),
        "sss_samples": kwargs.get("sss_samples", 4),
        # Screen Space Reflections
        "use_screen_space_reflections": kwargs.get("use_screen_space_reflections", True),
        "ssr_quality": kwargs.get("ssr_quality", 0.5),
        "ssr_thickness": kwargs.get("ssr_thickness", 0.2),
        "ssr_max_roughness": kwargs.get("ssr_max_roughness", 0.5),
        # Anti-aliasing
        "use_taa": kwargs.get("use_taa", True),
        "taa_samples": kwargs.get("taa_samples", 16),
        "use_fxaa": kwargs.get("use_fxaa", False),
        # Quality
        "use_high_quality_normals": kwargs.get("use_high_quality_normals", True),
        "use_soft_shadows": kwargs.get("use_soft_shadows", True),
        "shadow_quality": kwargs.get("shadow_quality", "HIGH"),
        # Ambient Occlusion
        "use_ambient_occlusion": kwargs.get("use_ambient_occlusion", True),
        "ao_factor": kwargs.get("ao_factor", 1.0),
        "ao_distance": kwargs.get("ao_distance", 0.2),
        # Glow
        "glow_threshold": kwargs.get("glow_threshold", 1.0),
        "glow_color": kwargs.get("glow_color", (1.0, 1.0, 1.0)),
        "glow_radius": kwargs.get("glow_radius", 3.0),
        # Vignette
        "vignette_scale": kwargs.get("vignette_scale", 1.0),
        # Color Grading
        "exposure": kwargs.get("exposure", 0.0),
        "gamma": kwargs.get("gamma", 1.0),
        "contrast": kwargs.get("contrast", 1.0),
        "saturation": kwargs.get("saturation", 1.0),
        "brightness": kwargs.get("brightness", 1.0),
        "rgb_curves": kwargs.get("rgb_curves", []),
        # Lens Effects
        "distortion": kwargs.get("distortion", 0.0),
        "dispersion": kwargs.get("dispersion", 0.0),
        # Film Grain
        "grain_scale": kwargs.get("grain_scale", 1.0),
        "grain_strength": kwargs.get("grain_strength", 0.1),
        # Mist/Atmosphere
        "mist_start": kwargs.get("mist_start", 5.0),
        "mist_depth": kwargs.get("mist_depth", 25.0),
        "mist_falloff": kwargs.get("mist_falloff", "QUADRATIC"),
        # Stereo
        "stereo_mode": kwargs.get("stereo_mode", "STEREO"),
        "stereo_eye_separation": kwargs.get("stereo_eye_separation", 0.065),
        # Rendering
        "tile_size": kwargs.get("tile_size", 2048),
    }

    # Convert settings to strings for the script
    settings_str = "\n".join(f"    {k} = {v!r}" for k, v in settings.items())

    script = f"""

def setup_post_processing():
    scene = bpy.context.scene
    eevee = scene.eevee

    # Basic settings
    {settings_str}

    # Bloom — removed from scene.eevee in EEVEE Next (Blender 4.2+/5.x).
    # Configure via the Glare node in the compositor if bloom is needed.

    # SSAO / Ground Truth AO (still present in EEVEE Next)
    eevee.use_gtao = {str(use_ssao).lower()}
    if {str(use_ssao).lower()}:
        if hasattr(eevee, 'gtao_factor'):
            eevee.gtao_factor = ssao_factor
        if hasattr(eevee, 'gtao_distance'):
            eevee.gtao_distance = ssao_distance

    # Motion Blur — removed from scene.eevee in EEVEE Next (Blender 4.2+/5.x).
    # Configure motion blur via scene.render.use_motion_blur if needed.

    # Depth of Field — configure on the camera data directly (works in all versions)
    if {str(use_dof).lower()} and scene.camera:
        cam = scene.camera.data
        cam.dof.use_dof = True
        if dof_focus_object:
            focus_obj = bpy.data.objects.get(dof_focus_object)
            if focus_obj:
                cam.dof.focus_object = focus_obj
        else:
            cam.dof.focus_distance = dof_focus_distance
        cam.dof.aperture_fstop = dof_fstop
        cam.dof.aperture_blades = dof_blades

    # Volumetrics (still present in EEVEE Next)
    if hasattr(eevee, 'use_volumetric_lights'):
        eevee.use_volumetric_lights = {str(settings["use_volumetric_lights"]).lower()}
    if hasattr(eevee, 'use_volumetric_shadows'):
        eevee.use_volumetric_shadows = {str(settings["use_volumetric_shadows"]).lower()}
    if hasattr(eevee, 'volumetric_tile_size'):
        eevee.volumetric_tile_size = volumetric_tile_size
    if hasattr(eevee, 'volumetric_samples'):
        eevee.volumetric_samples = volumetric_samples

    # Screen Space Reflections / SSS / shadow sizes — removed from scene.eevee in EEVEE Next.
    # These are now handled internally by EEVEE Next or via render settings.

    # Anti-aliasing (taa_render_samples is still present in EEVEE Next)
    if hasattr(eevee, 'taa_render_samples'):
        eevee.taa_render_samples = taa_samples

    # Color Management
    scene.view_settings.exposure = exposure
    scene.view_settings.gamma = gamma

    bpy.context.view_layer.update()

    return {{
        'status': 'SUCCESS',
        'settings': {{
            'ssao': {str(use_ssao).lower()},
            'dof': {str(use_dof).lower()},
            'bloom_note': 'Bloom moved to compositor in EEVEE Next (Blender 4.2+/5.x)',
            'motion_blur_note': 'Motion blur moved to scene.render in EEVEE Next',
        }}
    }}

try:
    result = setup_post_processing()
except Exception as e:
    result = {{'status': 'ERROR', 'error': str(e)}}

print(str(result))
"""

    try:
        output = await _executor.execute_script(script)
        return {"status": "SUCCESS", "output": output}
    except Exception as e:
        logger.error(f"Failed to setup post-processing: {e!s}")
        return {"status": "ERROR", "error": str(e)}
