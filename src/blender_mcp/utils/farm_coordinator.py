"""Coordinator for distributing Blender frame renders over MCP workers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import os
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from blender_mcp.utils.farm_state import FarmJobStore
from blender_mcp.utils.farm_workers import MCPWorkerClient, WorkerConfig


def _redact_worker_payload(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_worker_payload(item, secret)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in ("api_key", "token", "secret", "credential"))
        }
    if isinstance(value, list):
        return [_redact_worker_payload(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[redacted]")
    return value


def _workspace_path(workspace: Path, requested_path: str) -> Path:
    root = workspace.resolve()
    requested = Path(requested_path)
    resolved = (requested if requested.is_absolute() else root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path resolves outside coordinator workspace")
    return resolved


def _create_project_render_dir(workspace: Path, project_id: str, job_id: str) -> Path:
    """Create project render dirs by walking each component without following symlinks."""
    from blender_mcp.utils.projects import safe_project

    project = safe_project(workspace, project_id)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(workspace, flags)
    try:
        for component in ("projects", project_id):
            try:
                os.mkdir(component, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        try:
            os.mkdir("renders", dir_fd=fd)
        except FileExistsError:
            pass
        renders_fd = os.open("renders", flags, dir_fd=fd)
        try:
            os.mkdir(job_id, dir_fd=renders_fd)
        finally:
            os.close(renders_fd)
    finally:
        os.close(fd)
    return project / "renders" / job_id


def _write_project_frame(workspace: Path, project_id: str, job_id: str, frame_name: str, data: bytes) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(workspace, flags)
    try:
        for component in ("projects", project_id, "renders", job_id):
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        output_fd = os.open(frame_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=fd)
        with os.fdopen(output_fd, "wb") as output:
            output.write(data)
    finally:
        os.close(fd)
    return workspace / "projects" / project_id / "renders" / job_id / frame_name


class BlenderFarmCoordinator:
    """MCP-worker coordinator with durable frame state and restart recovery."""

    def __init__(
        self,
        workspace: str | Path,
        database_path: str | Path,
        workers: list[WorkerConfig],
        *,
        client_factory: Callable[[WorkerConfig], Any] = MCPWorkerClient,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = FarmJobStore(database_path)
        self.workers = {worker.worker_id: worker for worker in workers}
        self.client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self._worker_gpu_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self.store.recover_interrupted_jobs()

    def _client(self, worker_id: str) -> Any:
        if worker_id not in self._clients:
            self._clients[worker_id] = self.client_factory(self.workers[worker_id])
        return self._clients[worker_id]

    async def worker_status(self) -> list[dict[str, Any]]:
        async def inspect(worker: WorkerConfig) -> dict[str, Any]:
            try:
                client = self._client(worker.worker_id)
                tools = await client.list_tools()
                tool_names = {tool["name"] for tool in tools}
                if "blender_farm_worker" not in tool_names:
                    return {"worker_id": worker.worker_id, "online": False, "error": "worker capability tool is missing"}
                info = await client.call_tool("blender_farm_worker", {"operation": "identity"})
                return {
                    "worker_id": worker.worker_id,
                    "online": bool(info.get("online", info.get("success", True))),
                    "url": worker.url,
                    "tools": sorted(tool_names),
                    "identity": _redact_worker_payload(info, worker.api_key),
                }
            except Exception:
                return {"worker_id": worker.worker_id, "online": False, "url": worker.url, "error": "worker MCP request failed"}

        return await asyncio.gather(*(inspect(worker) for worker in self.workers.values()))

    async def submit_render(
        self,
        project_path: str,
        frame_start: int,
        frame_end: int,
        *,
        worker_ids: list[str] | None = None,
        output_directory: str | None = None,
        gallery_project_id: str | None = None,
    ) -> dict[str, Any]:
        source = _workspace_path(self.workspace, project_path)
        if not source.is_file() or source.suffix.lower() not in {".blend", ".zip"}:
            raise ValueError("project_path must point to an existing .blend or .zip bundle inside the coordinator workspace")
        if gallery_project_id is not None:
            from blender_mcp.utils.projects import safe_project

            project_root = safe_project(self.workspace, gallery_project_id)
            if not project_root.is_dir() or project_root.is_symlink() or not (project_root / ".project.json").is_file():
                raise ValueError("project_id must identify an existing Gallery project")
        if frame_start < 0 or frame_end < frame_start:
            raise ValueError("invalid frame range")
        if source.suffix.lower() == ".blend":
            bundle_buffer = io.BytesIO()
            with zipfile.ZipFile(bundle_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(source, source.name)
            bundle = bundle_buffer.getvalue()

        else:
            bundle = source.read_bytes()

            try:
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    blend_files = []
                    expanded = 0
                    for member in archive.infolist():
                        path = Path(member.filename)
                        if path.is_absolute() or ".." in path.parts:
                            raise ValueError("project ZIP contains an unsafe path")
                        if not member.is_dir():
                            expanded += member.file_size
                            if expanded > 512 * 1024 * 1024:
                                raise ValueError("expanded project ZIP exceeds 512 MiB")
                            if path.suffix.lower() == ".blend":
                                blend_files.append(path)
                    if len(blend_files) != 1:
                        raise ValueError("project ZIP must contain exactly one .blend file")
            except zipfile.BadZipFile as exc:
                raise ValueError("project_path ZIP is invalid") from exc
        if len(bundle) > 64 * 1024 * 1024:
            raise ValueError("compressed project bundle exceeds 64 MiB worker transfer limit")
        if (frame_end - frame_start + 1) > 1_000_000:
            raise ValueError("frame range exceeds the 1,000,000-frame safety limit")
        chosen_ids = worker_ids or list(self.workers)
        if not chosen_ids:
            raise ValueError("no Blender Farm workers are configured")
        if len(set(chosen_ids)) != len(chosen_ids):
            raise ValueError("worker_ids contains duplicates")
        unknown = sorted(set(chosen_ids) - self.workers.keys())
        if unknown:
            raise ValueError(f"unknown worker IDs: {', '.join(unknown)}")

        job_id = uuid.uuid4().hex[:12]
        project_id = job_id
        bundle_b64 = base64.b64encode(bundle).decode("ascii")
        bundle_hash = hashlib.sha256(bundle).hexdigest()

        synced: dict[str, str] = {}
        failed_workers: list[dict[str, str]] = []
        async def sync(worker_id: str) -> None:
            try:
                result = await self._client(worker_id).call_tool(
                    "blender_farm_worker",
                    {
                        "operation": "sync_project",
                        "project_id": project_id,
                        "bundle_base64": bundle_b64,
                        "bundle_sha256": bundle_hash,
                    },
                    timeout=300,
                )
                if not result.get("success") or result.get("bundle_sha256") != bundle_hash:
                    raise RuntimeError(result.get("error", "worker did not verify project bundle"))
                synced[worker_id] = str(result["project_path"])
            except Exception as exc:
                failed_workers.append({"worker_id": worker_id, "error": str(exc)})

        await asyncio.gather(*(sync(worker_id) for worker_id in chosen_ids))
        if not synced:
            raise RuntimeError(f"project sync failed on every selected worker: {failed_workers}")

        if gallery_project_id is not None:
            assert project_root is not None
            output_rel = f"projects/{gallery_project_id}/renders/{job_id}"
            try:
                output_path = _create_project_render_dir(self.workspace, gallery_project_id, job_id)
            except OSError as exc:
                raise ValueError("Gallery render output directory is unsafe or already exists") from exc
            resolved_output = output_path.resolve()
            if project_root not in resolved_output.parents or resolved_output.parent != (project_root / "renders"):
                raise ValueError("Gallery render output escapes project directory")
        else:
            output_rel = output_directory or f"farm-output/{job_id}"
            output_path = _workspace_path(self.workspace, output_rel)
            output_path.mkdir(parents=True, exist_ok=True)
        metadata = {
            "project_id": project_id,
            "bundle_sha256": bundle_hash,
            "worker_ids": sorted(synced),
            "worker_project_paths": synced,
            "output_directory": str(output_path.relative_to(self.workspace)),
            "prefix": "frame_",
            "frame_start": frame_start,
            "frame_end": frame_end,
        }
        self.store.create_job(job_id, str(source.relative_to(self.workspace)), frame_start, frame_end, metadata=metadata)
        event = asyncio.Event()
        event.set()
        self._pause_events[job_id] = event
        self._start_runner(job_id)
        return {
            "success": True,
            "job_id": job_id,
            "status": "running",
            "frame_start": frame_start,
            "frame_end": frame_end,
            "worker_ids": sorted(synced),
            "worker_sync_failures": failed_workers,
            "bundle_sha256": bundle_hash,
            "output_directory": str(output_path),
        }

    def _start_runner(self, job_id: str) -> None:
        current = self._tasks.get(job_id)
        if current and not current.done():
            return
        self._tasks[job_id] = asyncio.create_task(self._run_job(job_id), name=f"blender-farm-{job_id}")

    async def _run_job(self, job_id: str) -> None:
        metadata = self.store.get_job_metadata(job_id)
        await self._recover_worker_outputs(job_id, metadata)
        pending = self.store.get_pending_frames(job_id)
        worker_ids = metadata["worker_ids"]
        if not pending or not worker_ids:
            return
        resources = await self._wait_for_gpu_resources(job_id, worker_ids)
        if not resources:
            return
        # Render one calibration frame on the largest available GPU to measure this
        # scene's actual per-device VRAM demand before fanning frames out.
        pilot = resources[0]
        pilot_peak = await self._render_assigned(job_id, metadata, pilot, [pending[0]])
        pilot_record = next(
            (item for item in self.store.get_frame_records(job_id) if item["frame"] == pending[0]), None
        )
        if pilot_record is None or pilot_record["status"] != "completed":
            reason = pilot_record.get("error", "GPU calibration frame failed") if pilot_record else "GPU calibration frame failed"
            for frame in self.store.get_pending_frames(job_id):
                self.store.finish_frame(job_id, frame, success=False, error=f"Calibration render failed: {reason}")
            return
        remaining = self.store.get_pending_frames(job_id)
        if not remaining:
            return
        if isinstance(pilot_peak, int) and pilot_peak > 0:
            required_free_mib = int(pilot_peak * 1.15) + 512
            minimum_gpu_total_mib = 0
        else:
            # Without a usable measurement, stay on the calibration GPU's capacity tier.
            required_free_mib = 0
            minimum_gpu_total_mib = pilot["memory_total_mib"]
        resources = await self._wait_for_gpu_resources(
            job_id,
            worker_ids,
            required_free_mib=required_free_mib,
            minimum_gpu_total_mib=minimum_gpu_total_mib,
        )
        if not resources:
            return
        for item in resources:
            item["required_free_mib"] = required_free_mib
        # Pull frames dynamically instead of giving every GPU an equal static share.
        # A faster device finishes sooner and claims more frames; slow devices do not
        # hold up the tail of the job with an oversized initial assignment.
        queue: asyncio.Queue[int] = asyncio.Queue()
        for frame in remaining:
            queue.put_nowait(frame)

        async def run_gpu(resource: dict[str, Any]) -> None:
            while True:
                try:
                    frame = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._render_assigned(job_id, metadata, resource, [frame])
                finally:
                    queue.task_done()

        await asyncio.gather(*(run_gpu(resource) for resource in resources))

    @staticmethod
    def _gpu_free_threshold(device: dict[str, Any]) -> int:
        try:
            configured = max(256, int(os.environ.get("MCP_FARM_GPU_MIN_FREE_MIB", "1024")))
        except ValueError:
            configured = 1024
        total = device.get("memory_total_mib")
        fraction = int(total * 0.1) if isinstance(total, (int, float)) and total > 0 else 0
        return max(configured, fraction)

    @classmethod
    def _usable_gpu(cls, device: Any, *, required_free_mib: int = 0, minimum_total_mib: int = 0) -> bool:
        if not isinstance(device, dict):
            return False
        index = device.get("index")
        total = device.get("memory_total_mib")
        free = device.get("memory_free_mib")
        if free is None and isinstance(total, (int, float)) and isinstance(device.get("memory_used_mib"), (int, float)):
            free = max(0, total - device["memory_used_mib"])
        return (
            isinstance(index, int)
            and index >= 0
            and isinstance(device.get("name"), str)
            and bool(device["name"])
            and device.get("backend") in {"NVIDIA", "AMD", "INTEL"}
            and isinstance(total, (int, float))
            and total >= max(1, minimum_total_mib)
            and isinstance(free, (int, float))
            and free >= max(cls._gpu_free_threshold({**device, "memory_free_mib": free}), required_free_mib)
        )

    async def _read_worker_identity(self, worker_id: str) -> dict[str, Any] | None:
        try:
            result = await self._client(worker_id).call_tool("blender_farm_worker", {"operation": "identity"}, timeout=15)
            return result if isinstance(result, dict) and result.get("online") else None
        except Exception:
            return None

    async def _wait_for_gpu_resources(
        self,
        job_id: str,
        worker_ids: list[str],
        *,
        required_free_mib: int = 0,
        minimum_gpu_total_mib: int = 0,
    ) -> list[dict[str, Any]]:
        while self.store.get_progress(job_id)["status"] == "running":
            reports = await asyncio.gather(*(self._read_worker_identity(worker_id) for worker_id in worker_ids))
            resources: list[dict[str, Any]] = []
            reported_memory_stats = False
            largest_gpu_total_mib = 0
            for worker_id, report in zip(worker_ids, reports, strict=True):
                if report is None:
                    continue
                devices = report.get("gpu_stats")
                if not isinstance(devices, list):
                    continue
                for device in devices:
                    if isinstance(device, dict) and isinstance(device.get("memory_total_mib"), (int, float)) and (
                        isinstance(device.get("memory_free_mib"), (int, float))
                        or isinstance(device.get("memory_used_mib"), (int, float))
                    ):
                        reported_memory_stats = True
                    if isinstance(device, dict) and isinstance(device.get("memory_total_mib"), (int, float)):
                        largest_gpu_total_mib = max(largest_gpu_total_mib, int(device["memory_total_mib"]))
                    if self._usable_gpu(
                        device,
                        required_free_mib=required_free_mib,
                        minimum_total_mib=minimum_gpu_total_mib,
                    ):
                        resources.append({
                            "worker_id": worker_id,
                            "index": device["index"],
                            "name": device["name"],
                            "backend": device["backend"],
                            "memory_total_mib": device["memory_total_mib"],
                            "memory_free_mib": device.get("memory_free_mib"),
                            "memory_type": device.get("memory_type", "dedicated"),
                        })
            if resources:
                # Prefer the largest available per-device capacity, then the most free VRAM.
                # VRAM is per-device, not pooled across cards.
                resources.sort(key=lambda item: (item["memory_total_mib"], item["memory_free_mib"]), reverse=True)
                self.store.set_wait_reason(job_id, None)
                return resources
            if reported_memory_stats and required_free_mib > largest_gpu_total_mib:
                error = (
                    f"Measured frame demand ({required_free_mib} MiB with safety margin) exceeds every "
                    "single GPU's VRAM; GPUs do not pool memory."
                )
                for frame in self.store.get_pending_frames(job_id):
                    self.store.finish_frame(job_id, frame, success=False, error=error)
                return []
            if any(report is not None for report in reports) and not reported_memory_stats:
                error = "Workers reported no per-GPU VRAM telemetry; update the worker nodes before dispatching GPU jobs."
                for frame in self.store.get_pending_frames(job_id):
                    self.store.finish_frame(job_id, frame, success=False, error=error)
                return []
            self.store.set_wait_reason(
                job_id, "Waiting for an eligible GPU with enough free VRAM." if any(reports)
                else "Waiting for a worker to come online."
            )
            await asyncio.sleep(2)
        return []

    async def _recover_worker_outputs(self, job_id: str, metadata: dict[str, Any]) -> None:
        pending = set(self.store.get_pending_frames(job_id))
        if not pending:
            return
        for worker_id in metadata["worker_ids"]:
            try:
                manifest = await self._client(worker_id).call_tool(
                    "blender_farm_worker",
                    {
                        "operation": "list_frames",
                        "output_directory": metadata["output_directory"],
                        "prefix": metadata["prefix"],
                        "frame_start": min(pending),
                        "frame_end": max(pending),
                    },
                )
                for frame in sorted(pending.intersection(manifest.get("completed_frames", []))):
                    relative = f"{metadata['output_directory']}/{metadata['prefix']}{frame:06d}.png"
                    await self._collect_frame(job_id, frame, worker_id, relative, metadata)
                    pending.remove(frame)
                if not pending:
                    return
            except Exception:
                continue

    async def _render_assigned(
        self, job_id: str, metadata: dict[str, Any], resource: dict[str, Any], frames: list[int]
    ) -> None:
        worker_id = resource["worker_id"]
        gpu_index = resource["index"]
        gpu_name = resource["name"]
        gpu_vendor = resource["backend"]
        event = self._pause_events.setdefault(job_id, asyncio.Event())
        observed_peak_mib = 0
        if not event.is_set() and self.store.get_progress(job_id)["status"] == "running":
            event.set()
        for frame in frames:
            while True:
                await event.wait()
                if self.store.get_progress(job_id)["status"] != "running":
                    return
                waiting_for_gpu = False
                try:
                    shared_memory = resource.get("memory_type") == "shared"
                    lock_key = (worker_id, "SHARED", 0) if shared_memory else (worker_id, gpu_vendor, gpu_index)
                    lock = self._worker_gpu_locks.setdefault(lock_key, asyncio.Lock())
                    async with lock:
                        if self.store.get_progress(job_id)["status"] != "running":
                            return
                        report = await self._read_worker_identity(worker_id)
                        devices = report.get("gpu_stats", []) if report else []
                        current = next(
                            (
                                item for item in devices
                                if isinstance(item, dict)
                                and item.get("index") == gpu_index
                                and item.get("backend") == gpu_vendor
                            ),
                            None
                        )
                        if not self._usable_gpu(
                            current, required_free_mib=resource.get("required_free_mib", 0)
                        ):
                            waiting_for_gpu = True
                        else:
                            self.store.set_wait_reason(job_id, None)
                            self.store.mark_frame_started(job_id, frame, worker_id, gpu_name=gpu_name, gpu_index=gpu_index)
                            relative = f"{metadata['output_directory']}/{metadata['prefix']}{frame:06d}.png"
                            rendered = await self._client(worker_id).call_tool(
                                "blender_farm_worker",
                                {
                                    "operation": "render_frame",
                                    "project_path": metadata["worker_project_paths"][worker_id],
                                    "frame": frame,
                                    "output_path": relative,
                                    "gpu_index": gpu_index,
                                    "gpu_name": gpu_name,
                                    "gpu_vendor": gpu_vendor,
                                },
                                timeout=1800,
                            )
                            if not rendered.get("success") or not rendered.get("output_path"):
                                raise RuntimeError(rendered.get("error") or f"worker failed frame {frame}")
                            memory_delta = rendered.get("gpu_memory_delta_mib")
                            if isinstance(memory_delta, int):
                                observed_peak_mib = max(observed_peak_mib, memory_delta)
                            await self._collect_frame(job_id, frame, worker_id, str(rendered["output_path"]), metadata)
                            self.store.finish_frame(
                                job_id,
                                frame,
                                success=True,
                                output_path=f"{metadata['output_directory']}/{metadata['prefix']}{frame:06d}.png",
                                log=str(rendered.get("log", "")),
                            )
                    if waiting_for_gpu:
                        self.store.set_wait_reason(job_id, "Waiting for an assigned GPU to become available.")
                        await asyncio.sleep(2)
                        continue
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.finish_frame(job_id, frame, success=False, error=str(exc))
                    break
        return observed_peak_mib or None

    async def _collect_frame(
        self, job_id: str, frame: int, worker_id: str, worker_path: str, metadata: dict[str, Any]
    ) -> None:
        result = await self._client(worker_id).call_tool(
            "blender_farm_worker", {"operation": "collect_output", "file_path": worker_path}, timeout=300
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error", "worker output transfer failed"))
        data = base64.b64decode(result["content_base64"], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, str(result.get("sha256", ""))):
            raise RuntimeError("worker output SHA-256 mismatch")
        if metadata["output_directory"].startswith("projects/"):
            project_id = metadata["output_directory"].split("/")[1]
            job_id = metadata["output_directory"].split("/")[-1]
            target = _write_project_frame(self.workspace, project_id, job_id, f"{metadata['prefix']}{frame:06d}.png", data)
        else:
            target = _workspace_path(self.workspace, f"{metadata['output_directory']}/{metadata['prefix']}{frame:06d}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.store.finish_frame(job_id, frame, success=True, output_path=str(target.relative_to(self.workspace)))

    def job_status(self, job_id: str) -> dict[str, Any]:
        progress = self.store.get_progress(job_id)
        progress["frames"] = self.store.get_frame_records(job_id)
        return progress

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self.store.get_progress(job["id"]) for job in self.store.list_jobs(limit)]

    def pause_job(self, job_id: str) -> bool:
        changed = self.store.pause_job(job_id)
        if changed:
            self._pause_events.setdefault(job_id, asyncio.Event()).clear()
        return changed

    def resume_job(self, job_id: str) -> bool:
        changed = self.store.resume_job(job_id)
        if changed:
            self._pause_events.setdefault(job_id, asyncio.Event()).set()
            self._start_runner(job_id)
        return changed

    def cancel_job(self, job_id: str) -> bool:
        changed = self.store.cancel_job(job_id)
        if changed:
            self._pause_events.setdefault(job_id, asyncio.Event()).set()
        return changed

    def retry_failed_frames(self, job_id: str) -> list[int]:
        frames = self.store.retry_failed_frames(job_id)
        if frames:
            self._pause_events.setdefault(job_id, asyncio.Event()).set()
            self._start_runner(job_id)
        return frames

    async def wait_for_job(self, job_id: str, timeout: float = 60) -> dict[str, Any]:
        async def wait() -> dict[str, Any]:
            while True:
                result = self.job_status(job_id)
                if result["status"] in {"completed", "failed", "cancelled"}:
                    return result
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), timeout=timeout)

    async def farm_status(self) -> dict[str, Any]:
        workers = await self.worker_status()
        names = {record["worker_id"]: record["name"] for record in self.store.list_managed_workers()}
        for worker in workers:
            if worker["worker_id"] in names:
                worker["name"] = names[worker["worker_id"]]
        jobs = self.list_jobs()
        return {
            "success": True,
            "coordinator": {"workspace": str(self.workspace), "worker_count": len(workers)},
            "workers": workers,
            "active_jobs": [job for job in jobs if job["status"] in {"running", "paused"}],
            "jobs": jobs,
        }
