"""Shared GPU render-device configuration for every Blender render path.

The MCP server never renders itself: it generates Blender Python that runs inside
either a live Blender session or a headless ``blender --background`` subprocess.
Device selection therefore has to ship as *code*, not as a host-side setting.

``build_gpu_setup_snippet`` returns a self-contained Python snippet for that job.
Every render path (engine configuration, preview/turntable/multi-angle renders,
farm frame renders) embeds it before calling ``bpy.ops.render.render`` so that:

* Cycles selects OPTIX/CUDA for NVIDIA, HIP for AMD, and ONEAPI for Intel.
* GPU devices of the chosen backend are enabled and the CPU device is disabled
  (hybrid CPU+GPU rendering is deliberately not used).
* A GPU device that exists but cannot be enabled is a hard error when
  ``MCP_RENDER_REQUIRE_GPU`` is true, never a silent CPU render. When that
  variable is unset it is true only on hosts that have a GPU.
* No GPU device at all is the accepted CPU case: it renders on CPU and logs
  a warning, because there is no GPU to prefer. GPU-less VMs and containers
  take this path by default.
* EEVEE (4.2 EEVEE Next) is validated against the real GPU context so a software
  or missing context surfaces as an error instead of a broken render.

The snippet only depends on ``bpy`` (plus ``json`` from the stdlib), so the unit
tests can execute it against a fake ``bpy`` module without launching Blender.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence

#: Environment variable that makes GPU rendering strict (default on).
REQUIRE_GPU_ENV = "MCP_RENDER_REQUIRE_GPU"
GPU_ONLY_ENV = "MCP_RENDER_GPU_ONLY"

#: Marker printed by the emitted snippet so callers can read back the choice.
RENDER_DEVICE_MARKER = "MCP_RENDER_DEVICE:"

#: Cycles GPU backends in the order they are attempted.
PREFERRED_BACKENDS: tuple[str, ...] = ("OPTIX", "CUDA", "HIP", "ONEAPI")

#: Device types that count as GPUs in ``prefs.devices``.
GPU_DEVICE_TYPES = frozenset({"CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"})

#: Renderers that mean the GPU context is a software rasterizer, not the card.
SOFTWARE_RENDERER_MARKERS = (
    "llvmpipe",
    "softpipe",
    "swiftshader",
    "software rasterizer",
    "software rendering",
    "swrast",
)

_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


class GpuRenderError(RuntimeError):
    """Host-side failure while configuring or verifying GPU rendering."""


def require_gpu(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether GPU rendering is mandatory (``MCP_RENDER_REQUIRE_GPU``).

    When the variable is unset, strictness follows the host: a machine with a
    GPU renders strictly on it, and a GPU-less VM or container renders on CPU
    instead of failing every render. An explicit value always wins. An
    unrecognised value keeps the strict default so a typo cannot silently
    downgrade renders to CPU.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get(REQUIRE_GPU_ENV, "") or "").strip().lower()
    if not raw:
        from blender_mcp.utils.host_capabilities import host_gpu_present

        return host_gpu_present()
    return raw not in _FALSE_VALUES


def gpu_only(environ: Mapping[str, str] | None = None) -> bool:
    """Whether to refuse renders when no usable GPU exists (default off)."""
    source = os.environ if environ is None else environ
    raw = str(source.get(GPU_ONLY_ENV, "") or "").strip().lower()
    return bool(raw) and raw not in _FALSE_VALUES


def gpu_requested(device: str | None) -> bool:
    """Return whether ``device`` asks for GPU rendering (default is GPU)."""
    return str(device or "GPU").strip().upper() not in {"CPU"}


def build_gpu_setup_snippet(
    *,
    device: str = "GPU",
    engine: str | None = None,
    require_gpu_override: bool | None = None,
    prefer: Sequence[str] = PREFERRED_BACKENDS,
    gpu_index: int | None = None,
    gpu_name: str | None = None,
    gpu_vendor: str | None = None,
    indent: int = 0,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return Blender Python that configures and verifies the render device.

    Args:
        device: ``GPU`` (default) or ``CPU``. ``CPU`` is only accepted silently
            when no GPU is present or ``MCP_RENDER_REQUIRE_GPU`` is false.
        engine: Render engine to configure. ``None`` detects the scene engine at
            runtime, so the same snippet works before an animation render.
        require_gpu_override: Force strict/relaxed behaviour instead of reading
            the environment variable.
        prefer: Cycles backends to try in order when no vendor is specified.
        indent: Leading spaces for every line, for embedding inside a function.
        environ: Environment mapping to read ``MCP_RENDER_REQUIRE_GPU`` from.

    Returns:
        Executable Python source. It defines ``MCPGpuUnavailableError`` and calls
        ``_mcp_configure_gpu_render()``, printing ``MCP_RENDER_DEVICE:{json}``.
        When a GPU is required but unusable it raises, aborting the Blender run.
    """
    strict = require_gpu(environ) if require_gpu_override is None else bool(require_gpu_override)
    only = gpu_only(environ)
    backends = tuple(str(kind).upper() for kind in prefer) or PREFERRED_BACKENDS
    vendor_backends = {
        "NVIDIA": ("OPTIX", "CUDA"),
        "AMD": ("HIP",),
        "INTEL": ("ONEAPI",),
    }
    if gpu_vendor:
        backends = vendor_backends.get(str(gpu_vendor).upper(), backends)
    request = "GPU" if gpu_requested(device) else "CPU"
    snippet = _SNIPPET_TEMPLATE.format(
        require_gpu="True" if strict else "False",
        gpu_only="True" if only else "False",
        request=repr(request),
        engine=repr(None if engine is None else str(engine)),
        backends=repr(backends),
        gpu_index=repr(gpu_index),
        gpu_name=repr(gpu_name),
    )
    if indent > 0:
        pad = " " * indent
        snippet = "\n".join(f"{pad}{line}" if line.strip() else line for line in snippet.splitlines())
    return snippet


def parse_render_device(output: str | None) -> dict | None:
    """Return the device report printed by the emitted snippet, if present."""
    if not output:
        return None
    found: dict | None = None
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith(RENDER_DEVICE_MARKER):
            continue
        payload = line[len(RENDER_DEVICE_MARKER) :].strip()
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def ensure_gpu_used(output: str | None, *, strict: bool | None = None) -> dict:
    """Raise when strict GPU rendering was required but the run fell back to CPU.

    Complements the in-Blender check for callers that inspect captured output
    (for example a farm worker reporting a completed frame).
    """
    report = parse_render_device(output)
    want_gpu = require_gpu() if strict is None else bool(strict)
    if report is None:
        if want_gpu:
            raise GpuRenderError(
                "GPU rendering was required but the render reported no device; "
                "the Blender output does not contain a device confirmation"
            )
        return {"device": "UNKNOWN"}
    used = str(report.get("device", "")).upper()
    if want_gpu and used != "GPU":
        raise GpuRenderError(f"GPU rendering was required but Blender rendered on {used or 'an unknown device'}")
    return report


# ---------------------------------------------------------------------------
# Emitted Blender-side code
# ---------------------------------------------------------------------------
#
# Keep this snippet dependency-free apart from bpy/json: it is executed inside
# Blender and also exercised directly by tests with a fake bpy module.
_SNIPPET_TEMPLATE = '''
import bpy


class MCPGpuUnavailableError(RuntimeError):
    """Blender cannot honour the configured GPU render requirement."""


MCP_GPU_REQUIRE = {require_gpu}
MCP_GPU_ONLY = {gpu_only}
MCP_GPU_REQUEST = {request}
MCP_GPU_ENGINE = {engine}
MCP_GPU_PREFERRED_BACKENDS = {backends}
MCP_GPU_DEVICE_INDEX = {gpu_index}
MCP_GPU_DEVICE_NAME = {gpu_name}
MCP_GPU_SOFTWARE_MARKERS = (
    "llvmpipe",
    "softpipe",
    "swiftshader",
    "software rasterizer",
    "software rendering",
    "swrast",
)


def _mcp_gpu_type(device):
    return str(getattr(device, "type", "") or "").upper()


def _mcp_gpu_fail(message):
    """Raise the strict error or warn and allow CPU when the requirement is relaxed."""
    if MCP_GPU_REQUIRE:
        raise MCPGpuUnavailableError(message)
    print("MCP_GPU_WARNING: " + message + " Falling back to CPU rendering.")
    return False


def _mcp_gpu_report(payload):
    import json

    print("MCP_RENDER_DEVICE:" + json.dumps(payload, sort_keys=True))
    return payload


def _mcp_gpu_cycles_preferences():
    addons = getattr(bpy.context.preferences, "addons", None)
    if addons is None:
        return None
    try:
        addon = addons["cycles"]
    except (KeyError, TypeError):
        addon = addons.get("cycles") if hasattr(addons, "get") else None
    return getattr(addon, "preferences", None) if addon is not None else None


def _mcp_gpu_configure_cycles(scene):
    prefs = _mcp_gpu_cycles_preferences()
    if prefs is None:
        _mcp_gpu_fail("Cycles preferences are unavailable, so no GPU device can be enabled.")
        scene.cycles.device = "CPU"
        return _mcp_gpu_report({{"device": "CPU", "engine": "CYCLES", "backend": "CPU", "reason": "cycles-addon-missing"}})

    problems = []
    chosen = None
    devices = []
    for kind in MCP_GPU_PREFERRED_BACKENDS:
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
            # Blender exposes device entries as RNA wrappers. Keep one snapshot:
            # iterating prefs.devices again can create new wrappers, so Python
            # object identity is not stable across property reads.
            enumerated_devices = list(prefs.devices)
        except Exception as exc:  # backend not compiled into this build
            problems.append("%s unavailable (%s)" % (kind, exc))
            continue
        matched = [d for d in enumerated_devices if _mcp_gpu_type(d) == kind]
        if not matched:
            problems.append("%s reported no device" % kind)
            continue
        chosen = kind
        devices = enumerated_devices
        break

    if chosen is None:
        # No GPU device exists at all: CPU rendering is acceptable (requirement),
        # but it is never silent - it is logged and reported in the marker.
        detail = "; ".join(problems) or "no CUDA/OPTIX backend is available"
        if MCP_GPU_ONLY:
            seen = ["%s:%s" % (_mcp_gpu_type(d), getattr(d, "name", "?")) for d in getattr(prefs, "devices", [])]
            message = "GPU-only policy refused rendering; CPU fallback is disabled. engine=%s requested=%s MCP_RENDER_REQUIRE_GPU=%s tried_backends=%s seen_devices=%s" % (MCP_GPU_ENGINE or getattr(scene.render, "engine", "CYCLES"), MCP_GPU_REQUEST, MCP_GPU_REQUIRE, detail, seen)
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        print(
            "MCP_GPU_WARNING: no GPU device was detected by a compatible Blender backend (%s); "
            "rendering on CPU." % detail
        )
        try:
            prefs.get_devices()
        except Exception:
            pass
        try:
            for item in prefs.devices:
                item.use = _mcp_gpu_type(item) == "CPU"
        except Exception:
            pass
        scene.cycles.device = "CPU"
        return _mcp_gpu_report({{"device": "CPU", "engine": "CYCLES", "backend": "CPU", "reason": detail}})

    candidates = [item for item in devices if _mcp_gpu_type(item) == chosen]
    if MCP_GPU_DEVICE_INDEX is not None:
        index_suffix = str(MCP_GPU_DEVICE_INDEX)
        indexed = [
            item for item in candidates
            if str(getattr(item, "id", "")).rstrip().split("_")[-1] == index_suffix
        ]
        named = [item for item in candidates if getattr(item, "name", "") == MCP_GPU_DEVICE_NAME]
        if indexed:
            selected = indexed[:1]
        elif len(named) == 1:
            selected = named
        elif len(named) > 1 and 0 <= MCP_GPU_DEVICE_INDEX < len(named):
            selected = [named[MCP_GPU_DEVICE_INDEX]]
        else:
            message = "The selected farm GPU could not be matched to a Blender device (index=%s name=%r)." % (MCP_GPU_DEVICE_INDEX, MCP_GPU_DEVICE_NAME)
            _mcp_gpu_fail(message)
            raise MCPGpuUnavailableError(message)
    else:
        selected = candidates
    selected_ids = {{id(item) for item in selected}}
    enabled = []
    for item in devices:
        want = id(item) in selected_ids
        try:
            item.use = want
        except Exception as exc:
            problems.append("could not %s %s device %r (%s)" % ("enable" if want else "disable", _mcp_gpu_type(item), getattr(item, "name", "?"), exc))
        if want and bool(getattr(item, "use", False)):
            enabled.append(getattr(item, "name", "?"))
        elif want:
            problems.append("%s device %r refused to enable" % (chosen, getattr(item, "name", "?")))

    cpu_enabled = [getattr(d, "name", "?") for d in devices if _mcp_gpu_type(d) == "CPU" and bool(getattr(d, "use", False))]
    if cpu_enabled and not _mcp_gpu_fail(
        "CPU device(s) %s could not be disabled while %s is enabled; hybrid CPU+GPU rendering is not supported." % (", ".join(cpu_enabled), chosen)
    ):
        try:
            for item in prefs.devices:
                item.use = _mcp_gpu_type(item) == "CPU"
        except Exception:
            pass
        scene.cycles.device = "CPU"
        return _mcp_gpu_report({{"device": "CPU", "engine": "CYCLES", "backend": "CPU", "reason": "cpu-not-disabled"}})

    if not enabled:
        detail = "; ".join(problems) or "no %s device accepted use=True" % chosen
        _mcp_gpu_fail("Blender GPU rendering required but the %s device could not be enabled: " % chosen + detail)
        scene.cycles.device = "CPU"
        return _mcp_gpu_report({{"device": "CPU", "engine": "CYCLES", "backend": "CPU", "reason": detail}})

    scene.cycles.device = "GPU"
    if str(scene.cycles.device).upper() != "GPU":
        raise MCPGpuUnavailableError("scene.cycles.device refused the GPU setting; aborting instead of rendering on CPU.")
    return _mcp_gpu_report({{
        "device": "GPU",
        "engine": "CYCLES",
        "backend": chosen,
        "compute_device_type": str(getattr(prefs, "compute_device_type", chosen)),
        "devices": enabled,
        "cpu_enabled": False,
    }})


def _mcp_gpu_platform():
    import gpu

    return (
        str(gpu.platform.backend_type_get() or ""),
        str(gpu.platform.renderer_get() or ""),
        str(gpu.platform.vendor_get() or ""),
    )


def _mcp_gpu_context_problem(backend, renderer, vendor):
    haystack = (backend + " " + renderer + " " + vendor).lower()
    if not backend or backend.upper() in ("NONE", "UNKNOWN"):
        return "no GPU context (backend=%r)" % backend
    if any(marker in haystack for marker in MCP_GPU_SOFTWARE_MARKERS):
        return "software GPU context (backend=%r renderer=%r)" % (backend, renderer)
    return ""


def _mcp_gpu_configure_gpu_context(scene, engine):
    try:
        backend, renderer, vendor = _mcp_gpu_platform()
    except Exception as exc:
        if "background mode" in str(exc).lower():
            # Blender creates the GPU context lazily; in --background it does not
            # exist until the first render. Verify right after the render instead.
            print(
                "MCP_GPU_DEFERRED: %s GPU context is created lazily in background mode; "
                "it is verified immediately after the render." % (engine or "This engine")
            )
            return _mcp_gpu_report({{
                "device": "GPU",
                "engine": engine,
                "backend": "DEFERRED",
                "reason": str(exc),
            }})
        message = "%s requires a hardware GPU context but Blender reported no GPU context (%s)." % (
            engine or "This engine",
            exc,
        )
        if MCP_GPU_ONLY:
            message += " GPU-only policy enabled; CPU/software fallback disabled."
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        if not _mcp_gpu_fail(message):
            return _mcp_gpu_report({{"device": "CPU", "engine": engine, "backend": "SOFTWARE", "renderer": ""}})
        raise MCPGpuUnavailableError(message)

    problem = _mcp_gpu_context_problem(backend, renderer, vendor)
    if problem:
        message = "%s requires a hardware GPU context but Blender reported %s." % (
            engine or "This engine",
            problem,
        )
        if not _mcp_gpu_fail(message):
            return _mcp_gpu_report({{"device": "CPU", "engine": engine, "backend": "SOFTWARE", "renderer": renderer}})
        raise MCPGpuUnavailableError(message)
    return _mcp_gpu_report({{"device": "GPU", "engine": engine, "backend": backend, "renderer": renderer, "vendor": vendor}})


def MCP_GPU_VERIFY_AFTER_RENDER():
    """Validate the GPU context once a render has created it (EEVEE/Workbench).

    Calling this after ``bpy.ops.render.render`` turns a silent software
    (llvmpipe/SwiftShader) EEVEE render into a hard error, and confirms the
    deferred background-mode check. It is a no-op for Cycles.
    """
    scene = bpy.context.scene
    engine = str(getattr(scene.render, "engine", "") or "").upper()
    if not engine.startswith("BLENDER_EEVEE") and not engine.startswith("BLENDER_WORKBENCH"):
        return None
    try:
        backend, renderer, vendor = _mcp_gpu_platform()
    except Exception as exc:
        message = "%s rendered without a usable GPU context (%s)." % (engine, exc)
        if MCP_GPU_ONLY:
            message += " GPU-only policy enabled; CPU/software fallback disabled."
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        if MCP_GPU_REQUIRE:
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        print("MCP_GPU_WARNING: " + message + " Falling back to CPU rendering.")
        return None
    problem = _mcp_gpu_context_problem(backend, renderer, vendor)
    if problem:
        message = "%s rendered with %s; strict GPU rendering rejects this result." % (engine, problem)
        if MCP_GPU_ONLY:
            message += " GPU-only policy enabled; CPU/software fallback disabled."
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        if MCP_GPU_REQUIRE:
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        print("MCP_GPU_WARNING: " + message)
        return None
    return _mcp_gpu_report({{
        "device": "GPU",
        "engine": engine,
        "backend": backend,
        "renderer": renderer,
        "vendor": vendor,
        "verified": "post-render",
    }})


def _mcp_configure_gpu_render():
    scene = bpy.context.scene
    engine = (MCP_GPU_ENGINE or getattr(scene.render, "engine", "") or "").upper()
    if MCP_GPU_REQUEST == "CPU" and not engine.startswith("BLENDER_EEVEE") and not engine.startswith("BLENDER_WORKBENCH"):
        if MCP_GPU_ONLY:
            message = "GPU-only policy refuses explicit CPU rendering; engine=%s requested=%s" % (engine, MCP_GPU_REQUEST)
            print("MCP_GPU_ERROR: " + message)
            raise MCPGpuUnavailableError(message)
        if MCP_GPU_REQUIRE:
            raise MCPGpuUnavailableError(
                "CPU rendering was requested while " + "MCP_RENDER_REQUIRE_GPU=true; refusing to render on CPU."
            )
        scene.cycles.device = "CPU"
        return _mcp_gpu_report({{"device": "CPU", "engine": engine, "backend": "CPU", "reason": "explicit-cpu"}})
    if engine.startswith("BLENDER_EEVEE") or engine.startswith("BLENDER_WORKBENCH"):
        # EEVEE/Workbench always need a GPU context; a CPU request cannot make them
        # run without one, so validate the context instead of pretending.
        return _mcp_gpu_configure_gpu_context(scene, engine)
    return _mcp_gpu_configure_cycles(scene)


MCP_RENDER_DEVICE_INFO = _mcp_configure_gpu_render()
'''
