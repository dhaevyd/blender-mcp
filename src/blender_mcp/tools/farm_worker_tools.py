"""Render-worker MCP operations used by the Blender Farm coordinator."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from blender_mcp.utils.farm_workers import gpu_inventory_stats, worker_identity
from blender_mcp.utils.gpu_render import build_gpu_setup_snippet, parse_render_device

MAX_PROJECT_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_PROJECT_BYTES = 2048 * 1024 * 1024


def resolve_workspace_path(workspace: str | Path, requested_path: str) -> Path:
    """Resolve a user-supplied worker path and reject traversal outside workspace."""
    root = Path(workspace).resolve()
    path = Path(requested_path)
    resolved = (path if path.is_absolute() else root / path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path resolves outside worker workspace")
    return resolved


def _gpu_memory_used(devices: list[dict[str, Any]], index: int, vendor: str) -> int | None:
    for device in devices:
        if device.get("index") == index and device.get("backend") == vendor:
            value = device.get("memory_used_mib")
            return value if isinstance(value, int) else None
    return None


async def _sample_gpu_peak(index: int, vendor: str, peak: dict[str, int | None]) -> None:
    while True:
        devices = await asyncio.to_thread(gpu_inventory_stats)
        used = _gpu_memory_used(devices, index, vendor)
        if used is not None and (peak["used"] is None or used > peak["used"]):
            peak["used"] = used
        await asyncio.sleep(0.25)


def completed_frames(directory: str | Path, prefix: str, frame_start: int, frame_end: int) -> list[int]:
    """Return frames with a non-empty expected PNG artifact."""
    root = Path(directory)
    return [
        frame
        for frame in range(frame_start, frame_end + 1)
        if (root / f"{prefix}{frame:06d}.png").is_file() and (root / f"{prefix}{frame:06d}.png").stat().st_size > 0
    ]


def _extract_project_bundle(workspace: Path, project_id: str, bundle_b64: str, expected_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", project_id):
        raise ValueError("invalid project_id")
    try:
        bundle = base64.b64decode(bundle_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("project bundle must be valid base64") from exc
    if len(bundle) > MAX_PROJECT_BUNDLE_BYTES:
        raise ValueError("project bundle exceeds 512 MiB limit")
    actual_sha256 = hashlib.sha256(bundle).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        raise ValueError("project bundle SHA-256 mismatch")

    target = resolve_workspace_path(workspace, f"farm-projects/{project_id}")
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    blend_files: list[Path] = []
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = Path(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("project archive contains an unsafe path")
                # BLENDER-011: track actual written bytes, not header value
                destination = resolve_workspace_path(target, member.filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        extracted += len(chunk)
                        if extracted > MAX_EXTRACTED_PROJECT_BYTES:
                            raise ValueError("expanded project archive exceeds 512 MiB limit")
                        output.write(chunk)
                if destination.suffix.lower() == ".blend":
                    blend_files.append(destination)
    except zipfile.BadZipFile as exc:
        raise ValueError("project bundle must be a ZIP containing a .blend project and its assets") from exc
    if len(blend_files) != 1:
        raise ValueError("project bundle must contain exactly one .blend file")
    return {
        "project_path": str(blend_files[0].relative_to(workspace.resolve())),
        "bundle_sha256": actual_sha256,
        "extracted_bytes": extracted,
        "blend_count": len(blend_files),
    }


def _register_farm_worker_tools() -> None:
    from blender_mcp.utils.farm_workers import mcp_role

    if mcp_role() not in {"worker", "stand-alone"}:
        return

    from blender_mcp.app import get_app
    from blender_mcp.utils.blender_runtime import execute_bpy_script

    app = get_app()

    @app.tool()
    async def blender_farm_worker(
        operation: str = "identity",
        project_id: str = "",
        bundle_base64: str = "",
        bundle_sha256: str = "",
        project_path: str = "",
        frame: int = 1,
        output_path: str = "",
        output_directory: str = "",
        prefix: str = "frame_",
        frame_start: int = 1,
        frame_end: int = 1,
        file_path: str = "",
        gpu_index: int = -1,
        gpu_name: str = "",
        gpu_vendor: str = "",
        log_level: str = "",
        log_limit: int = 100,
        content_base64: str = "",
        overwrite: bool = False,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Worker identity, secure project sync/validation, frame rendering and artifact transfer."""
        workspace = Path(os.environ.get("BLENDER_WORKSPACE", "/workspace")).resolve()
        try:
            if operation == "identity":
                return worker_identity()

            if operation == "logs":
                from blender_mcp.server import get_recent_logs

                limit = max(1, min(int(log_limit or 100), 500))
                entries = get_recent_logs(level_filter=log_level or None, limit=limit)
                return {
                    "success": True,
                    "worker_id": os.environ.get("BLENDER_FARM_WORKER_ID", ""),
                    "count": len(entries),
                    "logs": [
                        {
                            "timestamp": entry["timestamp"].isoformat(),
                            "level": entry["level"],
                            "name": entry["name"],
                            "function": entry["function"],
                            "line": entry["line"],
                            "message": entry["message"],
                        }
                        for entry in entries
                    ],
                }

            if operation == "sync_project":
                free_mib = shutil.disk_usage(workspace).free // (1024 * 1024)
                if free_mib < 500:
                    return {"success": False, "error": f"insufficient disk space: {free_mib} MiB free"}
                result = _extract_project_bundle(workspace, project_id, bundle_base64, bundle_sha256)
                result.update({"success": True, "worker_id": os.environ.get("BLENDER_FARM_WORKER_ID", "")})
                return result

            if operation == "validate_project":
                path = resolve_workspace_path(workspace, project_path)
                if not path.is_file() or path.suffix.lower() != ".blend":
                    return {
                        "success": False,
                        "error": "project must be an existing .blend file inside worker workspace",
                    }
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                return {
                    "success": True,
                    "project_path": str(path.relative_to(workspace)),
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                }

            if operation == "list_frames":
                directory = resolve_workspace_path(workspace, output_directory)
                frames = completed_frames(directory, prefix, frame_start, frame_end)
                expected = list(range(frame_start, frame_end + 1))
                return {
                    "success": True,
                    "completed_frames": frames,
                    "missing_frames": [item for item in expected if item not in frames],
                }

            if operation == "render_frame":
                if frame < 0:
                    return {"success": False, "error": "frame must be non-negative"}
                free_mib = shutil.disk_usage(workspace).free // (1024 * 1024)
                if free_mib < 500:
                    return {"success": False, "error": f"insufficient disk space: {free_mib} MiB free"}
                source = resolve_workspace_path(workspace, project_path)
                target = resolve_workspace_path(workspace, output_path)
                if not source.is_file() or source.suffix.lower() != ".blend":
                    return {
                        "success": False,
                        "error": "project must be an existing .blend file inside worker workspace",
                    }
                target.parent.mkdir(parents=True, exist_ok=True)
                gpu_setup = build_gpu_setup_snippet(
                    device="GPU",
                    engine=None,
                    gpu_index=gpu_index if gpu_index >= 0 else None,
                    gpu_name=gpu_name or None,
                    gpu_vendor=gpu_vendor or None,
                    indent=0,
                )
                script = (
                    "import bpy, json, os\n"
                    f"bpy.ops.wm.open_mainfile(filepath={json.dumps(str(source))})\n"
                    f"scene=bpy.context.scene; scene.frame_set({frame})\n"
                    f"scene.render.filepath={json.dumps(str(target))}\n"
                    f"{gpu_setup}\n"
                    "bpy.ops.render.render(write_still=True)\n"
                    "MCP_GPU_VERIFY_AFTER_RENDER()\n"
                    f"print('FARM_FRAME_RESULT='+json.dumps({{'frame': {frame}, 'output_path': {json.dumps(str(target.relative_to(workspace)))}}}))\n"
                )
                baseline = None
                baseline_used: int | None = None
                peak: dict[str, int | None] = {"used": None}
                monitor = None
                if gpu_index >= 0 and gpu_vendor:
                    baseline = await asyncio.to_thread(gpu_inventory_stats)
                    baseline_used = _gpu_memory_used(baseline, gpu_index, gpu_vendor)
                    peak["used"] = baseline_used
                    monitor = asyncio.create_task(_sample_gpu_peak(gpu_index, gpu_vendor, peak))
                else:
                    baseline_used = None
                try:
                    result = await execute_bpy_script(
                        script,
                        script_name=f"farm_frame_{frame}",
                        timeout=1800,
                        prefer_session=False,
                        headless_fallback=True,
                    )
                finally:
                    if monitor:
                        monitor.cancel()
                        try:
                            await monitor
                        except asyncio.CancelledError:
                            pass
                peak_used = peak["used"]
                return {
                    "success": bool(result.get("success")) and target.is_file() and target.stat().st_size > 0,
                    "frame": frame,
                    "output_path": str(target.relative_to(workspace)) if target.exists() else None,
                    "render_device": parse_render_device(result.get("output", "")),
                    "gpu_memory_delta_mib": (
                        max(0, peak_used - baseline_used)
                        if isinstance(peak_used, int) and isinstance(baseline_used, int)
                        else None
                    ),
                    "error": result.get("error"),
                    "log": result.get("output", ""),
                }

            if operation == "collect_output":
                path = resolve_workspace_path(workspace, file_path)
                if not path.is_file():
                    return {"success": False, "error": "output file not found"}
                size = path.stat().st_size
                if size > MAX_PROJECT_BUNDLE_BYTES:
                    return {"success": False, "error": "output file exceeds 512 MiB transfer limit"}
                data = path.read_bytes()
                return {
                    "success": True,
                    "file_path": str(path.relative_to(workspace)),
                    "size_bytes": size,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }

            if operation == "files_list":
                root = resolve_workspace_path(workspace, file_path or ".")
                if not root.exists():
                    return {"success": False, "error": "path not found"}
                if root.is_file():
                    found = [root]
                else:
                    found = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
                files = [
                    {"path": p.relative_to(workspace).as_posix(), "size_bytes": p.stat().st_size} for p in sorted(found)
                ]
                return {
                    "success": True,
                    "path": root.relative_to(workspace).as_posix(),
                    "is_dir": root.is_dir(),
                    "files": files,
                }

            if operation == "files_write":
                target = resolve_workspace_path(workspace, file_path)
                if target == workspace:
                    return {"success": False, "error": "refusing to write over the workspace root"}
                if target.exists() and not overwrite:
                    return {"success": False, "error": "destination already exists"}
                try:
                    data = base64.b64decode(content_base64, validate=True)
                except (ValueError, binascii.Error):
                    return {"success": False, "error": "content must be valid base64"}
                if len(data) > MAX_PROJECT_BUNDLE_BYTES:
                    return {"success": False, "error": "file exceeds 512 MiB transfer limit"}
                digest = hashlib.sha256(data).hexdigest()
                if not hmac.compare_digest(digest, bundle_sha256.lower()):
                    return {"success": False, "error": "file SHA-256 mismatch on worker"}
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + ".part")
                partial.write_bytes(data)
                os.replace(partial, target)
                return {
                    "success": True,
                    "file_path": target.relative_to(workspace).as_posix(),
                    "size_bytes": len(data),
                    "sha256": digest,
                }

            if operation == "files_delete":
                target = resolve_workspace_path(workspace, file_path)
                if target == workspace:
                    return {"success": False, "error": "refusing to delete the workspace root"}
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    if not recursive:
                        return {"success": False, "error": "folder delete needs recursive=true"}
                    shutil.rmtree(target)
                else:
                    return {"success": False, "error": "path not found"}
                return {"success": True, "file_path": file_path}

            return {
                "success": False,
                "error": f"unknown operation: {operation}",
                "available_operations": [
                    "identity",
                    "logs",
                    "sync_project",
                    "validate_project",
                    "list_frames",
                    "render_frame",
                    "collect_output",
                    "files_list",
                    "files_write",
                    "files_delete",
                ],
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


_register_farm_worker_tools()
