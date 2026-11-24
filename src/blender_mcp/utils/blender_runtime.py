"""Execute Blender Python with live GUI session preferred over headless subprocess.

Connection priority:
1. TCP socket bridge (new v2 addon — fastest, bidirectional)
2. HTTP poll bridge (legacy v1 addon)
3. Headless subprocess (blender --background, always works)

Headless runs get the shared GPU render setup (``blender_mcp.utils.gpu_render``)
prepended, so a script that does not pick a device itself still renders on the
GPU instead of silently using the CPU.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import socket
import struct
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — env vars
# ---------------------------------------------------------------------------

BRIDGE_HOST = os.environ.get("BLENDER_BRIDGE_HOST", os.environ.get("BLENDER_HOST", "127.0.0.1"))
BRIDGE_PORT = int(os.environ.get("BLENDER_BRIDGE_PORT", "10850"))
BRIDGE_TIMEOUT = int(os.environ.get("BLENDER_BRIDGE_TIMEOUT", "180"))


# ---------------------------------------------------------------------------
# v2 — TCP socket bridge (unidirectional: MCP server → Blender addon)
# ---------------------------------------------------------------------------


def _send_socket_cmd(host: str, port: int, cmd: dict, timeout: int = BRIDGE_TIMEOUT) -> dict[str, Any]:
    """Connect to the Blender bridge addon's TCP socket, send a command, and read the response.

    Wire format: 4-byte big-endian length prefix + UTF-8 JSON payload.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        payload = json.dumps(cmd).encode("utf-8")
        sock.sendall(struct.pack("!I", len(payload)) + payload)

        # Read response: 4-byte length prefix + body
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                raise ConnectionError("Bridge closed connection mid-header")
            header += chunk
        body_len = struct.unpack("!I", header)[0]

        body = b""
        while len(body) < body_len:
            chunk = sock.recv(body_len - len(body))
            if not chunk:
                raise ConnectionError("Bridge closed connection mid-body")
            body += chunk

        return json.loads(body.decode("utf-8"))
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def exec_via_socket_bridge(script: str, timeout: int = BRIDGE_TIMEOUT) -> dict[str, Any] | None:
    """Try to execute via TCP socket bridge. Returns None if bridge is unreachable."""
    try:
        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _send_socket_cmd(
                BRIDGE_HOST,
                BRIDGE_PORT,
                {"type": "execute_code", "params": {"code": script}},
                timeout=timeout,
            ),
        )
        inner = result.get("result", result)
        if result.get("status") == "success":
            return {
                "success": inner.get("success", True),
                "output": inner.get("output", ""),
                "error": inner.get("error"),
                "session_used": True,
                "mode": "socket",
            }
        return {
            "success": False,
            "output": "",
            "error": inner.get("error", inner.get("message", "Unknown bridge error")),
            "session_used": True,
            "mode": "socket",
        }
    except (ConnectionRefusedError, OSError) as e:
        logger.debug("Socket bridge unreachable (%s); trying next transport", e)
        return None
    except Exception as e:
        logger.warning("Socket bridge error (%s); trying next transport", e)
        return None


# ---------------------------------------------------------------------------
# v1 — HTTP poll bridge (legacy)
# ---------------------------------------------------------------------------


async def _exec_via_http_poll(script: str, script_name: str = "exec", timeout: int = 30) -> dict[str, Any] | None:
    """Try the legacy HTTP poll bridge. Returns None if unreachable."""
    try:
        from blender_mcp.app import _exec_in_blender_session

        result = await _exec_in_blender_session(script, script_name=script_name, timeout=timeout)
        if result:
            result["mode"] = "session"
        return result
    except Exception as e:
        logger.debug("HTTP poll bridge error (%s)", e)
        return None


# ---------------------------------------------------------------------------
# GPU setup injection for agent-scripted headless runs
# ---------------------------------------------------------------------------
#
# The render tools embed ``build_gpu_setup_snippet`` themselves, but the generic
# script path used to leave the Cycles preferences untouched: an ad-hoc Cycles
# script therefore rendered on the CPU while the GPU sat idle, because nothing
# set ``scene.cycles.device``/``prefs.compute_device_type``. Every headless run
# now gets that same shared snippet *prepended*, so a script that never picks a
# device renders on the GPU, while a script that does pick one (sets the device
# or re-enables CPU devices itself) still keeps its own choice, since the
# injection only runs first.
#
# The injection is pure string work: the snippet builder is imported lazily and
# only formats Blender-side code — no subprocess, no device probe at import or
# call time. The probe happens once, inside Blender, when the snippet runs.

#: Marker of the injected block. Its presence means "already configured".
GPU_SETUP_MARKER = "# MCP_GPU_SETUP_INJECTED"

#: Fingerprints of a script that already configures the GPU device itself.
#: ``MCPGpuUnavailableError``/``_mcp_configure_gpu_render``/``MCP_RENDER_DEVICE:``
#: come from ``build_gpu_setup_snippet`` output, which callers may paste directly.
_GPU_SETUP_FINGERPRINTS: tuple[str, ...] = (
    GPU_SETUP_MARKER,
    "MCPGpuUnavailableError",
    "_mcp_configure_gpu_render",
    "MCP_RENDER_DEVICE:",
)

_TRACEBACK_FILE_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')

_WRAP_PROBE_LINE = "# MCP_GPU_SETUP_WRAP_PROBE"


def script_configures_gpu(script: str) -> bool:
    """Return whether ``script`` already carries the MCP GPU setup."""
    return any(token in script for token in _GPU_SETUP_FINGERPRINTS)


def _gpu_setup_source() -> str:
    """Return the shared GPU setup snippet, built for the Cycles code path."""
    # Imported lazily so this module's import graph stays free of the render
    # modules and so importing blender_runtime never touches Blender or drivers.
    from blender_mcp.utils.gpu_render import build_gpu_setup_snippet, require_gpu

    # engine="CYCLES" is deliberate. At this point the scene still carries
    # Blender's default engine (BLENDER_EEVEE_NEXT in 4.2), so letting the
    # snippet auto-detect the engine would make it validate the EEVEE GPU
    # context and never configure the Cycles device — exactly the silent CPU
    # fallback this injection exists to prevent. Forcing the Cycles branch
    # configures the Cycles preferences and ``scene.cycles.device`` regardless
    # of the engine the script later selects; an EEVEE script is unaffected by
    # those settings.
    return build_gpu_setup_snippet(engine="CYCLES", require_gpu_override=require_gpu())


def _gpu_setup_block() -> str:
    """Return the text prepended to a headless script (ends with a newline)."""
    header = [
        f"{GPU_SETUP_MARKER}: headless renders use the GPU unless the script overrides it.",
        "# Generated from blender_mcp.utils.gpu_render.build_gpu_setup_snippet().",
        "# The script body below runs afterwards, so it may still choose its own device.",
    ]
    body = _gpu_setup_source().strip("\n").splitlines()
    return "\n".join([*header, *body, "", ""])


def _injection_point(script: str) -> int:
    """Return how many leading lines the GPU setup must stay behind.

    A shebang, coding cookie, module docstring and ``from __future__`` imports
    have to remain the first lines of the file, so the block goes after them.
    Nothing else about the script is inspected, rewritten or reordered.
    """
    lines = script.splitlines(keepends=True)
    lead = 0
    while lead < len(lines) and (not lines[lead].strip() or lines[lead].lstrip().startswith("#")):
        lead += 1

    try:
        tree = ast.parse(script)
    except SyntaxError:
        # A broken script must still reach Blender so the caller gets the real
        # SyntaxError (with the real line number) instead of an injection error.
        return lead

    body = tree.body
    index = 0
    end = lead
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):  # module docstring
            end = body[0].end_lineno or lead
            index = 1
    while index < len(body):
        node = body[index]
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            end = node.end_lineno or end
            index += 1
            continue
        break
    return max(lead, end)


@dataclass(frozen=True)
class GpuSetupInjection:
    """A headless script plus where the GPU setup was inserted."""

    script: str
    insert_after: int = 0
    inserted: int = 0

    @property
    def applied(self) -> bool:
        """Whether the GPU setup was prepended to this run."""
        return self.inserted > 0

    def map_line(self, prepared_line: int) -> int | None:
        """Map a line of :attr:`script` back to the caller's original line.

        Returns ``None`` when the line belongs to the injected GPU setup block.
        """
        if prepared_line <= self.insert_after:
            return prepared_line
        if prepared_line <= self.insert_after + self.inserted:
            return None
        return prepared_line - self.inserted


def _prepare_headless_script(script: str) -> GpuSetupInjection:
    """Return ``script`` with the shared GPU setup prepended, exactly once."""
    if not script or script_configures_gpu(script):
        return GpuSetupInjection(script=script)

    block = _gpu_setup_block()
    insert_after = _injection_point(script)
    lines = script.splitlines(keepends=True)
    head = "".join(lines[:insert_after])
    if head and not head.endswith("\n"):
        head += "\n"
    tail = "".join(lines[insert_after:])
    prepared = head + block + tail
    return GpuSetupInjection(script=prepared, insert_after=insert_after, inserted=block.count("\n"))


def _executor_wrapper_lines(executor: Any) -> int:
    """Return how many lines the executor writes before the script it is given.

    The headless executor wraps scripts in a try/except harness, which already
    shifts every reported line number. Reading the shift back keeps traceback
    line numbers pointing at the caller's original source after injection.
    """
    wrap = getattr(executor, "_wrap_script_with_error_handling", None)
    if not callable(wrap):
        return 0
    try:
        wrapped = wrap(_WRAP_PROBE_LINE, "mcp_gpu_setup_probe")
    except Exception:  # pragma: no cover - defensive, the executor is external
        return 0
    for index, line in enumerate(str(wrapped).splitlines()):
        if line.strip() == _WRAP_PROBE_LINE:
            return index
    return 0


def _remap_traceback(
    error: str,
    injection: GpuSetupInjection,
    *,
    script_id: str,
    wrapper_lines: int = 0,
) -> str:
    """Renumber traceback references to the caller's original script lines."""
    if not error or not injection.applied:
        return error

    def _replace(match: re.Match[str]) -> str:
        if script_id not in match.group("file"):
            return match.group(0)
        prepared = int(match.group("line")) - wrapper_lines
        original = injection.map_line(prepared)
        if original is None:
            return f'File "<mcp-gpu-setup>", line {prepared - injection.insert_after}'
        return f'File "{match.group("file")}", line {original}'

    return _TRACEBACK_FILE_RE.sub(_replace, error)


# ---------------------------------------------------------------------------
# Fallback — headless subprocess
# ---------------------------------------------------------------------------


async def _exec_headless(script: str, script_name: str = "exec", timeout: int = 60) -> dict[str, Any]:
    from blender_mcp.utils.blender_executor import get_blender_executor

    # GPU setup is prepended only here: this is the one path that runs in a
    # fresh ``blender --background`` process, where the MCP server owns the
    # preferences. Session/GUI runs (prefer_session) execute inside the user's
    # own Blender and must keep the user's preferences untouched.
    injection = _prepare_headless_script(script)

    executor = None
    try:
        executor = get_blender_executor()
        output = await executor.execute_script(injection.script, script_name=script_name, timeout=timeout)
        return {
            "success": True,
            "output": output or "",
            "error": None,
            "session_used": False,
            "mode": "headless",
            "gpu_setup_injected": injection.applied,
        }
    except Exception as exc:
        logger.error("Headless Blender execution failed for %s: %s", script_name, exc)
        error = _remap_traceback(
            str(exc),
            injection,
            script_id=script_name,
            wrapper_lines=_executor_wrapper_lines(executor),
        )
        return {
            "success": False,
            "output": "",
            "error": error,
            "session_used": False,
            "mode": "headless",
            "gpu_setup_injected": injection.applied,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def execute_bpy_script(
    script: str,
    *,
    script_name: str = "exec",
    timeout: int = 60,
    prefer_session: bool = True,
    headless_fallback: bool = True,
) -> dict[str, Any]:
    """Run a bpy script preferring a live Blender session over headless subprocess.

    Connection priority:
    1. TCP socket bridge (v2 addon, default bridge port 10850)
    2. HTTP poll bridge (legacy v1 addon, /api/v1/blender/pending)
    3. Headless subprocess (blender --background)

    Returns a dict with keys: success, output, error, session_used, mode
    (``socket`` | ``session`` | ``headless`` | ``unavailable``) and
    ``gpu_setup_injected`` for headless runs.

    Headless runs get the shared GPU render setup prepended (see
    ``_prepare_headless_script``) so a bespoke Cycles script renders on the GPU
    without configuring the device itself. Session runs are never modified: the
    add-on executes them inside the user's Blender, where the user's own
    preferences apply.
    """
    if not prefer_session:
        if not headless_fallback:
            return {
                "success": False,
                "output": "",
                "error": "Session execution disabled and no fallback requested",
                "session_used": False,
                "mode": "unavailable",
            }
        return await _exec_headless(script, script_name=script_name, timeout=timeout)

    # 1. Try TCP socket bridge (fastest)
    result = await exec_via_socket_bridge(script, timeout=timeout)
    if result is not None:
        return result

    # 2. Try legacy HTTP poll bridge
    result = await _exec_via_http_poll(script, script_name=script_name, timeout=timeout)
    if result is not None:
        if result.get("session_used"):
            return result
        if not headless_fallback:
            return result

    # 3. Fall back to headless
    if not headless_fallback:
        return {
            "success": False,
            "output": "",
            "error": "No live Blender session. Start the bridge addon in Blender.",
            "session_used": False,
            "mode": "unavailable",
        }
    return await _exec_headless(script, script_name=script_name, timeout=timeout)
