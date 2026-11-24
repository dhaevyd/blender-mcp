"""Tests for the shared GPU render-device helper.

The helper ships Blender-side Python, so these tests execute the emitted snippet
against a fake ``bpy`` (and ``gpu``) module. That keeps the device-selection
behaviour — OPTIX preference, CPU device disabling, strict failures — covered
without launching Blender.

No live Blender is required; the render-handler tests only inspect the script
handed to the (mocked) executor.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from blender_mcp.utils.gpu_render import (
    PREFERRED_BACKENDS,
    RENDER_DEVICE_MARKER,
    GpuRenderError,
    build_gpu_setup_snippet,
    ensure_gpu_used,
    gpu_only,
    gpu_requested,
    parse_render_device,
    require_gpu,
)

_SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Fake Blender objects
# ---------------------------------------------------------------------------


class FakeDevice:
    """Blender cycles device with an optional refusal to change ``use``."""

    def __init__(
        self,
        name: str,
        kind: str,
        use: bool = False,
        *,
        refuse_enable: bool = False,
        refuse_disable: bool = False,
    ) -> None:
        self.name = name
        self.type = kind
        self._use = use
        self.refuse_enable = refuse_enable
        self.refuse_disable = refuse_disable

    @property
    def use(self) -> bool:
        return self._use

    @use.setter
    def use(self, value: bool) -> None:
        # Blender device collections accept the write and may leave it unchanged.
        if value and self.refuse_enable:
            return
        if not value and self.refuse_disable:
            return
        self._use = bool(value)


class FakeCyclesPreferences:
    def __init__(self, devices: list[FakeDevice], supported: tuple[str, ...] = ("OPTIX", "CUDA")) -> None:
        self._devices = devices
        self.supported = set(supported)
        self._compute_device_type = "NONE"
        self.devices: list[FakeDevice] = []

    @property
    def compute_device_type(self) -> str:
        return self._compute_device_type

    @compute_device_type.setter
    def compute_device_type(self, value: str) -> None:
        if value != "NONE" and value not in self.supported:
            raise TypeError(f'enum "{value}" not found in {tuple(sorted(self.supported | {"NONE"}))}')
        self._compute_device_type = value

    def get_devices(self) -> None:
        # Blender enumerates devices even when no GPU backend is selected.
        self.devices = list(self._devices)


class RewrappedDevice:
    """Fresh wrapper on each prefs.devices read, sharing state with its device."""

    def __init__(self, source: FakeDevice) -> None:
        self.source = source
        self.name = source.name
        self.type = source.type
        self.id = getattr(source, "id", "")

    @property
    def use(self) -> bool:
        return self.source.use

    @use.setter
    def use(self, value: bool) -> None:
        self.source.use = value


class RewrappingCyclesPreferences(FakeCyclesPreferences):
    @property
    def devices(self) -> list[RewrappedDevice]:
        return [RewrappedDevice(device) for device in self._devices]

    @devices.setter
    def devices(self, _value) -> None:
        # The simulated Blender API does not retain the wrapper collection.
        pass


class FakeAddons(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeCyclesSettings:
    def __init__(self, engine: str = "CYCLES") -> None:
        self.device = "CPU"
        self.samples = 128


class FakeRenderSettings:
    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.resolution_x = 1920
        self.resolution_y = 1080
        self.filepath = ""


class FakeScene:
    def __init__(self, engine: str = "CYCLES") -> None:
        self.render = FakeRenderSettings(engine)
        self.cycles = FakeCyclesSettings()


def make_bpy(scene: FakeScene, prefs: FakeCyclesPreferences | None) -> types.SimpleNamespace:
    addons = FakeAddons()
    if prefs is not None:
        addons["cycles"] = types.SimpleNamespace(preferences=prefs)
    return types.SimpleNamespace(
        context=types.SimpleNamespace(preferences=types.SimpleNamespace(addons=addons), scene=scene),
        app=types.SimpleNamespace(version=(4, 2, 3)),
    )


def make_gpu(renderer: str, backend: str = "OPENGL", vendor: str = "NVIDIA Corporation") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        platform=types.SimpleNamespace(
            backend_type_get=lambda: backend,
            renderer_get=lambda: renderer,
            vendor_get=lambda: vendor,
        )
    )


def make_broken_gpu(message: str = "failed to create GPU context") -> types.SimpleNamespace:
    def explode():
        raise RuntimeError(message)

    return types.SimpleNamespace(
        platform=types.SimpleNamespace(backend_type_get=explode, renderer_get=explode, vendor_get=explode)
    )


def call_with_gpu(function, gpu_module):
    """Call an emitted function with a specific fake gpu module installed."""
    saved = sys.modules.get("gpu")
    sys.modules["gpu"] = gpu_module
    try:
        return function()
    finally:
        if saved is None:
            sys.modules.pop("gpu", None)
        else:
            sys.modules["gpu"] = saved


def run_snippet(snippet: str, bpy_module, gpu_module=None, capsys=None):
    """Execute an emitted snippet with fake bpy/gpu modules and return its namespace."""
    modules = {"bpy": bpy_module}
    if gpu_module is not None:
        modules["gpu"] = gpu_module
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        namespace: dict = {}
        exec(compile(snippet, "<mcp_gpu_setup>", "exec"), namespace)  # noqa: S102 - test of generated code
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return namespace


def gpu_devices(**kwargs) -> list[FakeDevice]:
    return [
        FakeDevice("NVIDIA GeForce RTX 3060", "CUDA", **kwargs),
        FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU"),
        FakeDevice("NVIDIA GeForce RTX 3060", "OPTIX", **kwargs),
    ]


# ---------------------------------------------------------------------------
# Snippet behaviour — Cycles
# ---------------------------------------------------------------------------


def test_snippet_prefers_optix_and_sets_gpu_device(capsys):
    prefs = FakeCyclesPreferences(gpu_devices())
    scene = FakeScene("CYCLES")
    bpy_module = make_bpy(scene, prefs)

    namespace = run_snippet(build_gpu_setup_snippet(require_gpu_override=True), bpy_module, capsys=capsys)

    assert prefs.compute_device_type == "OPTIX"
    assert scene.cycles.device == "GPU"
    report = namespace["MCP_RENDER_DEVICE_INFO"]
    assert report["device"] == "GPU"
    assert report["backend"] == "OPTIX"

    kinds = {device.type: device for device in prefs.devices}
    assert kinds["OPTIX"].use is True
    assert kinds["CPU"].use is False, "the CPU device must never stay enabled next to a GPU"


def test_snippet_keeps_selected_device_across_fresh_blender_wrappers(capsys):
    devices = gpu_devices()
    prefs = RewrappingCyclesPreferences(devices)
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    assert scene.cycles.device == "GPU"
    assert namespace["MCP_RENDER_DEVICE_INFO"]["backend"] == "OPTIX"
    assert [device.use for device in devices] == [False, False, True]


def test_snippet_falls_back_to_cuda_when_optix_missing(capsys):
    prefs = FakeCyclesPreferences(
        [FakeDevice("NVIDIA GeForce RTX 3060", "CUDA"), FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU")],
        supported=("CUDA",),
    )
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    assert prefs.compute_device_type == "CUDA"
    assert scene.cycles.device == "GPU"
    assert namespace["MCP_RENDER_DEVICE_INFO"]["backend"] == "CUDA"
    assert [d.use for d in prefs.devices if d.type == "CPU"] == [False]


@pytest.mark.parametrize(
    ("vendor", "backend", "name"),
    [("NVIDIA", "OPTIX", "GeForce RTX 4090"), ("AMD", "HIP", "Radeon RX 7900"), ("INTEL", "ONEAPI", "Intel Iris Xe")],
)
def test_snippet_selects_vendor_backend(vendor, backend, name, capsys):
    devices = [FakeDevice(name, backend), FakeDevice("CPU", "CPU")]
    prefs = FakeCyclesPreferences(devices, supported=(backend,))
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(gpu_vendor=vendor, gpu_index=0, gpu_name=name, require_gpu_override=True),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    assert prefs.compute_device_type == backend
    assert scene.cycles.device == "GPU"
    assert namespace["MCP_RENDER_DEVICE_INFO"]["backend"] == backend
    assert devices[0].use is True
    assert devices[1].use is False


def test_snippet_selects_only_the_requested_gpu_device(capsys):
    devices = [
        FakeDevice("NVIDIA GeForce RTX 6000", "OPTIX"),
        FakeDevice("NVIDIA GeForce RTX 6000", "OPTIX"),
        FakeDevice("CPU", "CPU"),
    ]
    scene = FakeScene("CYCLES")
    prefs = FakeCyclesPreferences(devices)
    namespace = run_snippet(
        build_gpu_setup_snippet(gpu_index=1, gpu_name="NVIDIA GeForce RTX 6000"),
        make_bpy(scene, prefs),
        capsys=capsys,
    )
    assert devices[0].use is False
    assert devices[1].use is True
    assert devices[2].use is False
    assert namespace["MCP_GPU_DEVICE_INDEX"] == 1


def test_snippet_disables_other_backends_and_keeps_cpu_off(capsys):
    prefs = FakeCyclesPreferences(gpu_devices())
    scene = FakeScene("CYCLES")

    run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    # Only the selected backend's devices may be enabled; CUDA entry stays off
    # because OPTIX was chosen and hybrid CPU+GPU is not wanted.
    assert [(d.type, d.use) for d in prefs.devices] == [("CUDA", False), ("CPU", False), ("OPTIX", True)]


def test_raises_when_gpu_present_but_cannot_be_enabled(capsys):
    devices = gpu_devices(refuse_enable=True)
    prefs = FakeCyclesPreferences(devices)
    scene = FakeScene("CYCLES")

    with pytest.raises(RuntimeError) as excinfo:
        run_snippet(
            build_gpu_setup_snippet(require_gpu_override=True),
            make_bpy(scene, prefs),
            capsys=capsys,
        )

    message = str(excinfo.value)
    assert "GPU" in message
    assert "could not be enabled" in message or "aborting" in message
    assert scene.cycles.device != "GPU"


def test_no_gpu_present_renders_on_cpu_with_warning_even_when_strict(capsys):
    """Requirement: with no GPU at all, CPU is acceptable but must be logged."""
    prefs = FakeCyclesPreferences([FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU")], supported=())
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    stdout = capsys.readouterr().out
    assert "MCP_GPU_WARNING" in stdout
    assert "no GPU device" in stdout
    assert scene.cycles.device == "CPU"
    assert namespace["MCP_RENDER_DEVICE_INFO"]["device"] == "CPU"
    assert [d.use for d in prefs.devices if d.type == "CPU"] == [True]


def test_relaxed_mode_falls_back_to_cpu_with_warning(capsys):
    prefs = FakeCyclesPreferences([FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU")], supported=())
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=False),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    stdout = capsys.readouterr().out
    assert "MCP_GPU_WARNING" in stdout
    assert scene.cycles.device == "CPU"
    assert namespace["MCP_RENDER_DEVICE_INFO"]["device"] == "CPU"
    assert [d.use for d in prefs.devices if d.type == "CPU"] == [True]


def test_gpu_only_refuses_absent_gpu_even_when_require_gpu_is_relaxed(capsys):
    prefs = FakeCyclesPreferences([FakeDevice("CPU", "CPU")], supported=())
    scene = FakeScene("CYCLES")
    with pytest.raises(RuntimeError, match="CPU fallback is disabled"):
        run_snippet(build_gpu_setup_snippet(require_gpu_override=False, environ={"MCP_RENDER_GPU_ONLY": "true"}), make_bpy(scene, prefs), capsys=capsys)
    output = capsys.readouterr().out
    assert "MCP_GPU_ERROR:" in output
    assert "OPTIX" in output and "CUDA" in output
    assert scene.cycles.device == "CPU"


def test_gpu_only_refuses_explicit_cpu_and_eevee_without_context(capsys):
    with pytest.raises(RuntimeError, match="explicit CPU"):
        run_snippet(build_gpu_setup_snippet(device="CPU", environ={"MCP_RENDER_GPU_ONLY": "1"}), make_bpy(FakeScene(), FakeCyclesPreferences(gpu_devices())))
    with pytest.raises(RuntimeError, match="GPU-only policy"):
        run_snippet(build_gpu_setup_snippet(engine="BLENDER_EEVEE_NEXT", environ={"MCP_RENDER_GPU_ONLY": "true"}), make_bpy(FakeScene("BLENDER_EEVEE_NEXT"), None), gpu_module=make_broken_gpu("no context"))


def test_gpu_only_parser_defaults_false():
    assert gpu_only({}) is False
    assert gpu_only({"MCP_RENDER_GPU_ONLY": "false"}) is False
    assert gpu_only({"MCP_RENDER_GPU_ONLY": "true"}) is True


def test_relaxed_mode_warns_when_cpu_cannot_be_disabled(capsys):
    devices = [
        FakeDevice("NVIDIA GeForce RTX 3060", "CUDA"),
        FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU", use=True, refuse_disable=True),
        FakeDevice("NVIDIA GeForce RTX 3060", "OPTIX"),
    ]
    prefs = FakeCyclesPreferences(devices)
    scene = FakeScene("CYCLES")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=False),
        make_bpy(scene, prefs),
        capsys=capsys,
    )

    stdout = capsys.readouterr().out
    assert "MCP_GPU_WARNING" in stdout
    assert "hybrid" in stdout.lower()
    assert namespace["MCP_RENDER_DEVICE_INFO"]["device"] == "CPU"


def test_missing_cycles_addon_is_strict_failure(capsys):
    scene = FakeScene("CYCLES")

    with pytest.raises(RuntimeError) as excinfo:
        run_snippet(build_gpu_setup_snippet(require_gpu_override=True), make_bpy(scene, None), capsys=capsys)

    assert "Cycles preferences are unavailable" in str(excinfo.value)


def test_explicit_cpu_request_is_refused_when_strict(capsys):
    prefs = FakeCyclesPreferences(gpu_devices())
    scene = FakeScene("CYCLES")

    with pytest.raises(RuntimeError) as excinfo:
        run_snippet(
            build_gpu_setup_snippet(device="CPU", require_gpu_override=True),
            make_bpy(scene, prefs),
            capsys=capsys,
        )

    assert "CPU rendering was requested" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Snippet behaviour — EEVEE / GPU context
# ---------------------------------------------------------------------------


def test_eevee_accepts_hardware_gpu_context(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")
    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
        gpu_module=make_gpu("NVIDIA GeForce RTX 3060/PCIe/SSE2"),
        capsys=capsys,
    )

    report = namespace["MCP_RENDER_DEVICE_INFO"]
    assert report["device"] == "GPU"
    assert report["backend"] == "OPENGL"
    assert "RTX 3060" in report["renderer"]


def test_eevee_rejects_software_renderer(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")

    with pytest.raises(RuntimeError) as excinfo:
        run_snippet(
            build_gpu_setup_snippet(require_gpu_override=True),
            make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
            gpu_module=make_gpu("llvmpipe (LLVM 15.0.7, 256 bits)", backend="OPENGL"),
            capsys=capsys,
        )

    assert "hardware GPU context" in str(excinfo.value)
    assert "llvmpipe" in str(excinfo.value)


def test_eevee_without_gpu_context_raises(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")

    with pytest.raises(RuntimeError) as excinfo:
        run_snippet(
            build_gpu_setup_snippet(require_gpu_override=True),
            make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
            gpu_module=make_broken_gpu(),
            capsys=capsys,
        )

    assert "no GPU context" in str(excinfo.value)


def test_eevee_software_context_relaxed_warns(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")

    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=False),
        make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
        gpu_module=make_gpu("llvmpipe (LLVM 15.0.7, 256 bits)"),
        capsys=capsys,
    )

    assert "MCP_GPU_WARNING" in capsys.readouterr().out
    assert namespace["MCP_RENDER_DEVICE_INFO"]["device"] == "CPU"


def test_eevee_background_mode_defers_then_verifies_hardware(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")
    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
        gpu_module=make_broken_gpu("GPU functions for drawing are not available in background mode"),
        capsys=capsys,
    )

    assert namespace["MCP_RENDER_DEVICE_INFO"]["backend"] == "DEFERRED"
    assert "MCP_GPU_DEFERRED" in capsys.readouterr().out

    verified = call_with_gpu(
        namespace["MCP_GPU_VERIFY_AFTER_RENDER"],
        make_gpu("NVIDIA GeForce RTX 3060/PCIe/SSE2"),
    )
    assert verified["device"] == "GPU"
    assert verified["verified"] == "post-render"


def test_eevee_post_render_rejects_software_context(capsys):
    scene = FakeScene("BLENDER_EEVEE_NEXT")
    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
        gpu_module=make_broken_gpu("GPU functions for drawing are not available in background mode"),
        capsys=capsys,
    )

    with pytest.raises(RuntimeError) as excinfo:
        call_with_gpu(
            namespace["MCP_GPU_VERIFY_AFTER_RENDER"],
            make_gpu("llvmpipe (LLVM 15.0.7, 256 bits)"),
        )

    assert "strict GPU rendering rejects" in str(excinfo.value)


def test_post_render_check_is_noop_for_cycles(capsys):
    scene = FakeScene("CYCLES")
    namespace = run_snippet(
        build_gpu_setup_snippet(require_gpu_override=True),
        make_bpy(scene, FakeCyclesPreferences(gpu_devices())),
        capsys=capsys,
    )

    assert namespace["MCP_GPU_VERIFY_AFTER_RENDER"]() is None


# ---------------------------------------------------------------------------
# Host-side helpers
# ---------------------------------------------------------------------------


def test_require_gpu_defaults_to_true_and_accepts_explicit_false():
    assert require_gpu({}) is True
    assert require_gpu({"MCP_RENDER_REQUIRE_GPU": ""}) is True
    assert require_gpu({"MCP_RENDER_REQUIRE_GPU": "true"}) is True
    assert require_gpu({"MCP_RENDER_REQUIRE_GPU": "yes"}) is True
    assert require_gpu({"MCP_RENDER_REQUIRE_GPU": "garbage"}) is True  # typo must not relax strictness
    for value in ("false", "FALSE", "0", "no", "off"):
        assert require_gpu({"MCP_RENDER_REQUIRE_GPU": value}) is False


def test_gpu_requested_defaults_to_gpu():
    assert gpu_requested(None) is True
    assert gpu_requested("GPU") is True
    assert gpu_requested("gpu_compatible") is True
    assert gpu_requested("CPU") is False


def test_preferred_backends_include_supported_vendors():
    assert PREFERRED_BACKENDS == ("OPTIX", "CUDA", "HIP", "ONEAPI")


def test_snippet_indent_applies_to_every_statement():
    import textwrap

    snippet = build_gpu_setup_snippet(require_gpu_override=True, indent=4)
    for line in snippet.splitlines():
        assert not line or line.startswith("    "), line
    compile(textwrap.dedent(snippet), "<indented>", "exec")


def test_parse_render_device_reads_last_marker():
    output = "\n".join(
        [
            "noise",
            RENDER_DEVICE_MARKER + '{"device": "CPU"}',
            RENDER_DEVICE_MARKER + '{"backend": "OPTIX", "device": "GPU"}',
        ]
    )
    assert parse_render_device(output) == {"backend": "OPTIX", "device": "GPU"}
    assert parse_render_device("nothing here") is None
    assert parse_render_device(None) is None
    assert parse_render_device(RENDER_DEVICE_MARKER + "not-json") is None


def test_ensure_gpu_used_rejects_cpu_when_strict():
    report = RENDER_DEVICE_MARKER + '{"device": "CPU", "backend": "CPU"}'
    with pytest.raises(GpuRenderError):
        ensure_gpu_used(report, strict=True)
    assert ensure_gpu_used(report, strict=False)["device"] == "CPU"


def test_ensure_gpu_used_requires_confirmation_when_strict():
    with pytest.raises(GpuRenderError):
        ensure_gpu_used("", strict=True)
    assert ensure_gpu_used("", strict=False)["device"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Render paths use the shared helper
# ---------------------------------------------------------------------------

_HELPER_REFERENCES = {
    "src/blender_mcp/handlers/rendering_handler.py": "set_render_engine",
    "src/blender_mcp/handlers/render_handler.py": "render_preview",
    "src/blender_mcp/handlers/scene_handler.py": "set_render_settings",
    "src/blender_mcp/tools/farm_worker_tools.py": "render_frame",
}

_LEGACY_DEVICE_SNIPPETS = (
    "d['use'] = 1",
    'd["use"] = 1',
    "compute_device_type = 'CUDA'\nbpy.context.preferences",
)


@pytest.mark.parametrize("relative_path", sorted(_HELPER_REFERENCES))
def test_render_paths_import_the_shared_helper(relative_path):
    source = (_SRC.parent / relative_path).read_text(encoding="utf-8")
    assert "gpu_render" in source, f"{relative_path} must use the shared GPU render helper"
    for legacy in _LEGACY_DEVICE_SNIPPETS:
        assert legacy not in source, f"{relative_path} still contains legacy device code: {legacy!r}"


def test_render_tools_pass_device_to_the_shared_helper():
    source = (_SRC / "blender_mcp/tools/render/render_tools.py").read_text(encoding="utf-8")
    assert source.count("device=device") >= 3  # set_engine, preview, current frame, turntable


def test_scene_tools_expose_gpu_device_type():
    from blender_mcp.tools.scene_tools import DeviceType, SetRenderSettingsParams

    assert DeviceType.GPU.value == "GPU"
    assert SetRenderSettingsParams().device is DeviceType.GPU


def test_legacy_device_code_is_gone_from_rendering_handler():
    source = (_SRC / "blender_mcp/handlers/rendering_handler.py").read_text(encoding="utf-8")
    assert "build_gpu_setup_snippet(" in source
    assert "prefs.compute_device_type = 'CUDA'" not in source
    assert "for d in bpy.context.preferences.addons['cycles'].preferences.devices" not in source


@pytest.mark.asyncio
async def test_render_preview_script_disables_cpu_and_checks_backend(monkeypatch, tmp_path):
    from blender_mcp.handlers import render_handler

    captured: dict = {}
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))

    async def fake_execute_script(script, **kwargs):
        captured["script"] = script
        return RENDER_DEVICE_MARKER + '{"backend": "OPTIX", "device": "GPU"}'

    executor = MagicMock()
    executor.execute_script = AsyncMock(side_effect=fake_execute_script)
    monkeypatch.setattr(render_handler, "_executor", executor)

    result = await render_handler.render_preview(output_path=str(tmp_path / "out.png"), samples=4)

    script = captured["script"]
    assert "MCPGpuUnavailableError" in script
    assert "MCP_RENDER_DEVICE:" in script
    assert "OPTIX" in script and "CUDA" in script
    assert "d['use'] = 1" not in script
    assert script.rindex("MCP_GPU_VERIFY_AFTER_RENDER()") > script.rindex("bpy.ops.render.render(write_still=True)")
    assert result["device"] == "GPU" and result["backend"] == "OPTIX"


@pytest.mark.asyncio
async def test_render_turntable_script_contains_gpu_setup(monkeypatch, tmp_path):
    from blender_mcp.handlers import render_handler

    captured: dict = {}
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))

    async def fake_execute_script(script, **kwargs):
        captured["script"] = script
        return RENDER_DEVICE_MARKER + '{"backend": "OPTIX", "device": "GPU"}'

    executor = MagicMock()
    executor.execute_script = AsyncMock(side_effect=fake_execute_script)
    monkeypatch.setattr(render_handler, "_executor", executor)

    await render_handler.render_turntable(output_dir=str(tmp_path / "turntable"), frames=2)

    assert "bpy.ops.render.render(animation=True)" in captured["script"]
    assert captured["script"].index("MCP_RENDER_DEVICE:") < captured["script"].index(
        "bpy.ops.render.render(animation=True)"
    )


@pytest.mark.asyncio
async def test_set_render_settings_script_contains_gpu_setup(monkeypatch, tmp_path):
    from blender_mcp.handlers import scene_handler

    captured: dict = {}
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))

    async def fake_execute_script(script, **kwargs):
        captured["script"] = script
        return RENDER_DEVICE_MARKER + '{"backend": "OPTIX", "device": "GPU"}'

    executor = MagicMock()
    executor.execute_script = AsyncMock(side_effect=fake_execute_script)
    monkeypatch.setattr(scene_handler, "_executor", executor)

    result = await scene_handler.set_render_settings(engine="CYCLES", samples=8)

    assert "MCP_RENDER_DEVICE:" in captured["script"]
    assert "BLENDER_EEVEE_NEXT" in captured["script"]  # 4.2 engine id normalisation
    assert "device: GPU/OPTIX" in result
