"""Tests for GPU setup injection into agent-scripted headless runs.

``execute_bpy_script`` prepends the shared ``build_gpu_setup_snippet(...)`` to
every headless script so a bespoke Cycles script that never picks a device still
renders on the GPU (previously it silently rendered on the CPU). These tests
cover the injection contract: where the block goes, when it must not be added,
that it still fails loudly in strict mode, and that traceback line numbers keep
pointing at the caller's own source lines.

The emitted snippet is executed against a fake ``bpy`` module, so no Blender is
required. Device enumeration is the fake ``bpy.context.preferences.addons``
implementation below, which is how "a GPU exists" is simulated.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

from blender_mcp.utils import blender_runtime
from blender_mcp.utils.blender_executor import BlenderExecutor
from blender_mcp.utils.blender_runtime import (
    GPU_SETUP_MARKER,
    _prepare_headless_script,
    execute_bpy_script,
    script_configures_gpu,
)

_SRC = Path(__file__).resolve().parent.parent / "src"

USER_SCRIPT = """import bpy

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 8
print("AGENT_BODY_RAN")
"""

BROKEN_SCRIPT = """import bpy

engine = bpy.context.scene.render.engine
raise RuntimeError("kaboom from the agent script")
"""


# ---------------------------------------------------------------------------
# Fake Blender objects (device enumeration lives here)
# ---------------------------------------------------------------------------


class FakeDevice:
    """Cycles device that can refuse ``use`` writes, like Blender sometimes does."""

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
        self.devices = list(self._devices)


class FakeAddons(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeCyclesSettings:
    def __init__(self) -> None:
        self.device = "CPU"
        self.samples = 128


class FakeRenderSettings:
    def __init__(self, engine: str) -> None:
        self.engine = engine


class FakeScene:
    def __init__(self, engine: str = "BLENDER_EEVEE_NEXT") -> None:
        self.render = FakeRenderSettings(engine)
        self.cycles = FakeCyclesSettings()


def make_bpy(scene: FakeScene, prefs: FakeCyclesPreferences | None) -> types.SimpleNamespace:
    addons = FakeAddons()
    if prefs is not None:
        addons["cycles"] = types.SimpleNamespace(preferences=prefs)
    return types.SimpleNamespace(
        context=types.SimpleNamespace(
            preferences=types.SimpleNamespace(addons=addons),
            scene=scene,
        ),
    )


def gpu_devices(**kwargs) -> list[FakeDevice]:
    return [
        FakeDevice("NVIDIA GeForce RTX 3060", "CUDA", **kwargs),
        FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU"),
        FakeDevice("NVIDIA GeForce RTX 3060", "OPTIX", **kwargs),
    ]


def run_script(script: str, bpy_module, filename: str = "<agent_script>") -> dict:
    """Execute ``script`` with a fake ``bpy`` installed and return its namespace."""
    saved = sys.modules.get("bpy")
    sys.modules["bpy"] = bpy_module
    try:
        namespace: dict = {"__name__": "__main__"}
        exec(compile(script, filename, "exec"), namespace)  # noqa: S102 - test of generated code
    finally:
        if saved is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = saved
    return namespace


class FakeExecutor(BlenderExecutor):
    """Records the script instead of launching Blender.

    It subclasses the real executor so ``_wrap_script_with_error_handling`` — the
    harness that shifts line numbers — is the production one, and running a
    script mirrors what the real executor does: wrap, compile under
    ``<script_name>.py``, execute, report the printed error/traceback markers.
    """

    def __init__(self, *, bpy_module, result: str = "ok") -> None:
        super().__init__("/nonexistent/blender")
        self.scripts: list[str] = []
        self.bpy_module = bpy_module
        self.result = result
        self.stdout = ""

    async def execute_script(self, script, blend_file=None, timeout=None, retry_count=0, script_name=None):
        script_id = script_name or "exec"
        self.scripts.append(script)
        wrapped = self._wrap_script_with_error_handling(script, script_id)
        filename = str(Path(tempfile.gettempdir()) / f"{script_id}.py")
        buffer = io.StringIO()
        saved_out, saved_bpy = sys.stdout, sys.modules.get("bpy")
        sys.stdout = buffer
        sys.modules["bpy"] = self.bpy_module
        try:
            exec(compile(wrapped, filename, "exec"), {"__name__": "__main__"})  # noqa: S102
        except SystemExit:
            pass  # the harness prints the BLENDER_SCRIPT_* markers and exits
        finally:
            sys.stdout = saved_out
            if saved_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = saved_bpy

        stdout = buffer.getvalue()
        self.stdout = stdout
        marker = f"BLENDER_SCRIPT_TRACEBACK: {script_id} - "
        if marker in stdout:
            # BlenderExecutor._process_script_output hands these markers back as
            # the failure text, with the harness traceback printed after them.
            detail = stdout[stdout.index(marker) + len(marker) :].strip()
            raise RuntimeError(f"Script errors: {detail}")
        return self.result


def install_executor(monkeypatch, executor: FakeExecutor) -> FakeExecutor:
    from blender_mcp.utils import blender_executor

    monkeypatch.setattr(blender_executor, "get_blender_executor", lambda *a, **k: executor)
    return executor


# ---------------------------------------------------------------------------
# Injection happens for headless runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headless_run_prepends_gpu_setup(monkeypatch):
    """A plain agent script gets the shared GPU setup before its own body."""
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    scene = FakeScene()
    executor = install_executor(
        monkeypatch, FakeExecutor(bpy_module=make_bpy(scene, FakeCyclesPreferences(gpu_devices())))
    )

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    assert result["mode"] == "headless"
    assert result["success"] is True
    assert result["gpu_setup_injected"] is True

    script = executor.scripts[0]
    assert script.startswith(GPU_SETUP_MARKER), "the GPU setup must be prepended, not appended"
    assert "MCPGpuUnavailableError" in script
    assert "MCP_RENDER_DEVICE:" in script
    # The caller's body is untouched and still runs afterwards.
    assert script.endswith(USER_SCRIPT)
    assert script.index(GPU_SETUP_MARKER) < script.index('scene.render.engine = "CYCLES"')


@pytest.mark.asyncio
async def test_injected_setup_selects_optix_on_the_run(monkeypatch):
    """Requirement: a script with no device of its own renders on the GPU/OPTIX.

    The scene still carries Blender's default EEVEE engine, which is exactly the
    situation that left ``cycles.device`` at CPU before.
    """
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    prefs = FakeCyclesPreferences(gpu_devices())
    scene = FakeScene()
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(scene, prefs)))

    await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    namespace = run_script(executor.scripts[0], make_bpy(scene, prefs))

    assert prefs.compute_device_type == "OPTIX"
    assert scene.cycles.device == "GPU"
    assert [device.use for device in prefs.devices if device.type == "CPU"] == [False]
    assert namespace["MCP_RENDER_DEVICE_INFO"]["device"] == "GPU"


@pytest.mark.asyncio
async def test_script_device_choice_wins_over_the_injected_setup(monkeypatch):
    """Prepending means an explicit choice in the body still applies."""
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    prefs = FakeCyclesPreferences(gpu_devices())
    scene = FakeScene()
    script = USER_SCRIPT + '\nbpy.context.scene.cycles.device = "CPU"\n'
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(scene, prefs)))

    await execute_bpy_script(script, script_name="agent_render", prefer_session=False)

    run_script(executor.scripts[0], make_bpy(scene, prefs))

    assert scene.cycles.device == "CPU", "the script's own device choice must not be overridden"


def test_prepare_leaves_shebang_docstring_and_future_imports_first():
    script = '#!/usr/bin/env python\n"""Module doc."""\nfrom __future__ import annotations\n\nimport bpy\n'
    prepared = _prepare_headless_script(script)

    assert prepared.applied is True
    assert prepared.insert_after == 3
    compile(prepared.script, "<prepared>", "exec")  # must still be valid Python
    assert prepared.script.startswith('#!/usr/bin/env python\n"""Module doc."""\nfrom __future__ import annotations\n')
    assert prepared.script.endswith("\nimport bpy\n")


# ---------------------------------------------------------------------------
# No injection where the user's own Blender/preferences apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_run_is_not_injected(monkeypatch):
    """Session/GUI runs execute in the user's Blender: leave the script alone."""
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    captured: dict = {}

    async def fake_bridge(script, timeout=0):
        captured["script"] = script
        return {"success": True, "output": "ok", "error": None, "session_used": True, "mode": "socket"}

    monkeypatch.setattr(blender_runtime, "exec_via_socket_bridge", fake_bridge)
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), None)))

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=True)

    assert result["mode"] == "socket"
    assert captured["script"] == USER_SCRIPT, "session runs must keep the user's preferences"
    assert executor.scripts == []


@pytest.mark.asyncio
async def test_no_injection_when_no_headless_fallback_is_allowed(monkeypatch):
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), None)))

    result = await execute_bpy_script(USER_SCRIPT, prefer_session=False, headless_fallback=False)

    assert result["mode"] == "unavailable"
    assert executor.scripts == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["MCPGpuUnavailableError", "# MCP_GPU_SETUP_INJECTED", "MCP_RENDER_DEVICE:"])
async def test_script_with_gpu_marker_is_not_injected_twice(monkeypatch, body):
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    script = f"# {body}\nimport bpy\nprint('already configured')\n"
    executor = install_executor(
        monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), FakeCyclesPreferences(gpu_devices())))
    )

    result = await execute_bpy_script(script, script_name="agent_render", prefer_session=False)

    assert result["gpu_setup_injected"] is False
    assert executor.scripts[0] == script
    assert executor.scripts[0].count("MCPGpuUnavailableError") <= 1


def test_prepare_is_idempotent_and_marker_detection_is_public():
    prepared = _prepare_headless_script(USER_SCRIPT)
    assert script_configures_gpu(prepared.script)
    assert _prepare_headless_script(prepared.script).script == prepared.script


# ---------------------------------------------------------------------------
# Error line numbers survive the injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_line_numbers_point_at_the_original_source(monkeypatch):
    """The raised line is reported as line 4 of the caller's script, not shifted."""
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    prefs = FakeCyclesPreferences(gpu_devices())
    install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), prefs)))

    result = await execute_bpy_script(BROKEN_SCRIPT, script_name="broken_script", prefer_session=False)

    assert result["success"] is False
    assert "kaboom from the agent script" in result["error"]
    refs = [
        int(line)
        for file, line in re.findall(r'File "([^"]+)", line (\d+)', result["error"])
        if "broken_script" in file
    ]
    assert refs == [4], f"expected the caller's line 4, got references {refs} in:\n{result['error']}"


@pytest.mark.asyncio
async def test_error_inside_the_injected_block_is_labelled_as_gpu_setup(monkeypatch):
    """A failure inside the injected block is not blamed on the caller's script."""
    monkeypatch.setenv("MCP_RENDER_REQUIRE_GPU", "true")
    # Cycles preferences unavailable => the snippet raises from its own code.
    install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), None)))

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    assert result["success"] is False
    assert "Cycles preferences are unavailable" in result["error"]
    assert "<mcp-gpu-setup>" in result["error"]
    assert 'File "/tmp/agent_render.py", line 4' not in result["error"]


# ---------------------------------------------------------------------------
# Strict / relaxed / no-GPU behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_mode_fails_loudly_when_the_gpu_cannot_be_enabled(monkeypatch):
    """Requirement: never a silent CPU render when a GPU exists but fails."""
    monkeypatch.setenv("MCP_RENDER_REQUIRE_GPU", "true")
    prefs = FakeCyclesPreferences(gpu_devices(refuse_enable=True))
    install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(FakeScene(), prefs)))

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    assert result["success"] is False
    assert "MCPGpuUnavailableError" in result["error"]
    assert "could not be enabled" in result["error"]


@pytest.mark.asyncio
async def test_relaxed_mode_injects_but_does_not_raise(monkeypatch):
    """Requirement: MCP_RENDER_REQUIRE_GPU=false injects, warns and continues."""
    monkeypatch.setenv("MCP_RENDER_REQUIRE_GPU", "false")
    prefs = FakeCyclesPreferences(gpu_devices(refuse_enable=True))
    scene = FakeScene()
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(scene, prefs)))

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    assert result["success"] is True
    assert result["gpu_setup_injected"] is True
    assert "MCP_GPU_REQUIRE = False" in executor.scripts[0]
    assert scene.cycles.device == "CPU"


@pytest.mark.asyncio
async def test_no_gpu_environment_keeps_the_script_runnable(monkeypatch):
    """Requirement: with no GPU at all, the script still runs (on the CPU)."""
    monkeypatch.delenv("MCP_RENDER_REQUIRE_GPU", raising=False)
    prefs = FakeCyclesPreferences([FakeDevice("AMD Ryzen 5 3600 6-Core Processor", "CPU")], supported=())
    scene = FakeScene()
    executor = install_executor(monkeypatch, FakeExecutor(bpy_module=make_bpy(scene, prefs)))

    result = await execute_bpy_script(USER_SCRIPT, script_name="agent_render", prefer_session=False)

    assert result["success"] is True
    assert result["gpu_setup_injected"] is True
    assert "MCP_GPU_WARNING" in executor.stdout and "no GPU device" in executor.stdout
    assert "AGENT_BODY_RAN" in executor.stdout
    assert scene.cycles.device == "CPU"
    assert [device.use for device in prefs.devices if device.type == "CPU"] == [True]


# ---------------------------------------------------------------------------
# The injection stays cheap and import-safe
# ---------------------------------------------------------------------------


def test_preparation_never_shells_out(monkeypatch):
    """Requirement: no subprocess / nvidia-smi at injection time."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("GPU setup injection must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    prepared = _prepare_headless_script(USER_SCRIPT)

    assert prepared.applied is True
    assert prepared.inserted == prepared.script.count("\n") - USER_SCRIPT.count("\n")


def test_runtime_module_has_no_device_probe_on_import():
    source = (_SRC / "blender_mcp/utils/blender_runtime.py").read_text(encoding="utf-8")
    assert "nvidia-smi" not in source
    assert "import subprocess" not in source
    assert "subprocess.run" not in source
