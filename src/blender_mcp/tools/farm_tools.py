"""Blender Farm coordinator MCP tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from blender_mcp.utils.farm_coordinator import BlenderFarmCoordinator
from blender_mcp.utils.farm_workers import load_worker_configs

_FARM: BlenderFarmCoordinator | None = None


def _coordinator() -> BlenderFarmCoordinator:
    global _FARM
    if _FARM is None:
        workspace = Path(os.environ.get("BLENDER_WORKSPACE", "/workspace")).resolve()
        database = Path(os.environ.get("BLENDER_FARM_DB_PATH", str(workspace / ".blender-farm" / "farm.sqlite3")))
        _FARM = BlenderFarmCoordinator(workspace, database, load_worker_configs())
        for record in _FARM.store.list_managed_workers():
            if record["enabled"]:
                from blender_mcp.utils.farm_workers import WorkerConfig

                _FARM.workers[record["worker_id"]] = WorkerConfig(
                    record["worker_id"],
                    record["url"],
                    _FARM.store.get_managed_worker_secret(record["worker_id"]),
                    record["workspace"],
                )
    return _FARM


def _register_farm_tools() -> None:
    from blender_mcp.utils.farm_workers import mcp_role

    if mcp_role() not in {"master", "stand-alone"}:
        return

    from blender_mcp.app import get_app

    app = get_app()

    @app.tool(
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
    )
    async def blender_farm(
        operation: str = "status",
        job_id: str = "",
        project_path: str = "",
        frame_start: int = 1,
        frame_end: int = 1,
        worker_ids: list[str] | None = None,
        output_directory: str = "",
        project_id: str = "",
        limit: int = 20,
        source: str = "",
        destination: str = "",
        mode: str = "copy",
        overwrite: bool = False,
        path: str = "",
        batch_size: int = 1,
        priority: int = 0,
        assemble_video: bool = False,
        video_format: str = "mp4",
        fps: int = 24,
    ) -> dict[str, Any]:
        """Control a distributed Blender render farm across authenticated Blender MCP workers.

        Operations: status, workers, submit, job_status, list_jobs, pause, resume,
        cancel, retry_failed, missing_frames, outputs, transfer, cleanup.
        cleanup deletes path (relative to each worker's workspace) on worker_ids and is permanent.
        transfer copies or cuts a file or folder between master and one worker, using
        source/destination endpoints like "master:renders/shot1" or "<worker_id>:renders/shot1".
        mode is "copy" or "cut"; a cut from master goes to .trash, a cut from a worker is permanent.
        Overwriting an existing destination needs overwrite=true.
        Project submission transfers the .blend bundle to workers and verifies its SHA-256
        before frame dispatch. Outputs are collected into the coordinator workspace.
        """
        farm = _coordinator()
        try:
            if operation == "status":
                return await farm.farm_status()
            if operation == "workers":
                return {"success": True, "workers": await farm.worker_status()}
            if operation == "submit":
                if not project_path:
                    return {"success": False, "error": "project_path is required for submit"}
                result = await farm.submit_render(
                    project_path,
                    frame_start,
                    frame_end,
                    worker_ids=worker_ids,
                    output_directory=output_directory or None,
                    gallery_project_id=project_id or None,
                    batch_size=batch_size,
                    priority=priority,
                    assemble_video=assemble_video,
                    video_format=video_format,
                    fps=fps,
                )
                return result
            if operation == "job_status":
                if not job_id:
                    return {"success": False, "error": "job_id is required"}
                return {"success": True, **farm.job_status(job_id)}
            if operation == "list_jobs":
                return {"success": True, "jobs": farm.list_jobs(limit)}
            if operation == "pause":
                return {"success": farm.pause_job(job_id), "job_id": job_id, "status": "paused"}
            if operation == "resume":
                return {"success": farm.resume_job(job_id), "job_id": job_id, "status": "running"}
            if operation == "cancel":
                return {
                    "success": farm.cancel_job(job_id),
                    "job_id": job_id,
                    "status": "cancelled",
                    "note": "Stops dispatching new frames; already-running Blender frame calls may finish before the worker returns.",
                }
            if operation == "retry_failed":
                frames = farm.retry_failed_frames(job_id)
                return {"success": True, "job_id": job_id, "requeued_frames": frames}
            if operation == "transfer":
                from blender_mcp.utils.farm_transfer import transfer

                return await transfer(farm, source, destination, mode=mode, overwrite=overwrite)
            if operation == "cleanup":
                from blender_mcp.utils.farm_transfer import cleanup

                return await cleanup(farm, list(worker_ids or []), path)
            if operation == "missing_frames":
                return {"success": True, "job_id": job_id, "frames": farm.store.get_missing_frames(job_id)}
            if operation == "outputs":
                progress = farm.job_status(job_id)
                metadata = farm.store.get_job_metadata(job_id)
                root = farm.workspace / metadata["output_directory"]
                paths = [
                    str((root / f"frame_{frame['frame']:06d}.png").resolve())
                    for frame in progress["frames"]
                    if frame["status"] == "completed"
                ]
                return {"success": True, "job_id": job_id, "output_directory": str(root), "files": paths}
            return {
                "success": False,
                "error": f"unknown operation: {operation}",
                "available_operations": [
                    "status",
                    "workers",
                    "submit",
                    "job_status",
                    "list_jobs",
                    "pause",
                    "resume",
                    "cancel",
                    "retry_failed",
                    "missing_frames",
                    "outputs",
                    "transfer",
                    "cleanup",
                ],
            }
        except Exception as exc:
            return {"success": False, "operation": operation, "error": str(exc)}


_register_farm_tools()
