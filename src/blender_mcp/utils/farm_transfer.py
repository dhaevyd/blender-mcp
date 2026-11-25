"""Copy or cut files between the coordinator workspace (master) and one render worker.

Endpoints are written as ``master:<path>`` or ``<worker_id>:<path>``. Paths are relative
to the workspace on that side. Worker-to-worker transfers are not supported.

Cut only removes the source after every file has been copied and checksum-verified.
Cutting from master moves the source into the master's ``.trash`` (recoverable). Cutting
from a worker deletes it on the worker, which cannot be undone.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any

from blender_mcp.utils.farm_coordinator import _workspace_path
from blender_mcp.utils.projects import move_to_trash

MAX_FILE_BYTES = 512 * 1024 * 1024
MASTER = "master"


@dataclass(frozen=True)
class Endpoint:
    worker_id: str | None  # None means the master workspace
    rel: str

    @property
    def is_master(self) -> bool:
        return self.worker_id is None


def parse_endpoint(text: str, workers: dict[str, Any]) -> Endpoint:
    name, sep, rel = (text or "").partition(":")
    rel = rel.strip().strip("/")
    if not sep or not rel or rel in {".", ".."}:
        raise ValueError("endpoint must look like master:<path> or <worker_id>:<path>")
    if name == MASTER:
        return Endpoint(None, rel)
    if name not in workers:
        raise ValueError(f"unknown endpoint '{name}'")
    return Endpoint(name, rel)


async def _call(farm: Any, worker_id: str, operation: str, **args: Any) -> dict[str, Any]:
    result = await farm._client(worker_id).call_tool("blender_farm_worker", {"operation": operation, **args})
    if not result.get("success"):
        raise RuntimeError(f"worker {worker_id} {operation} failed: {result.get('error', 'unknown error')}")
    return result


async def _exists(farm: Any, endpoint: Endpoint) -> bool:
    if endpoint.is_master:
        return _workspace_path(farm.workspace, endpoint.rel).exists()
    result = await farm._client(endpoint.worker_id).call_tool(
        "blender_farm_worker", {"operation": "files_list", "file_path": endpoint.rel}
    )
    return bool(result.get("success"))


async def _list(farm: Any, endpoint: Endpoint) -> tuple[list[str], bool]:
    """Return (file paths relative to the workspace, whether the source is a folder)."""
    if endpoint.is_master:
        root = _workspace_path(farm.workspace, endpoint.rel)
        if not root.exists():
            raise FileNotFoundError(f"{MASTER}:{endpoint.rel} not found")
        if root.is_file():
            return [endpoint.rel], False
        found = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        return [p.relative_to(farm.workspace).as_posix() for p in found], True
    result = await _call(farm, endpoint.worker_id, "files_list", file_path=endpoint.rel)
    return [item["path"] for item in result["files"]], bool(result.get("is_dir"))


async def _read(farm: Any, endpoint: Endpoint, path: str) -> bytes:
    if endpoint.is_master:
        target = _workspace_path(farm.workspace, path)
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path} exceeds the 512 MiB transfer limit")
        return target.read_bytes()
    result = await _call(farm, endpoint.worker_id, "collect_output", file_path=path)
    data = base64.b64decode(result["content_base64"], validate=True)
    if hashlib.sha256(data).hexdigest() != result["sha256"]:
        raise RuntimeError(f"checksum mismatch reading {path} from worker {endpoint.worker_id}")
    return data


async def _write(farm: Any, endpoint: Endpoint, path: str, data: bytes, overwrite: bool) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if endpoint.is_master:
        target = _workspace_path(farm.workspace, path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"{MASTER}:{path} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != digest:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"checksum mismatch writing {path} on master")
        os.replace(partial, target)
        return
    await _call(
        farm,
        endpoint.worker_id,
        "files_write",
        file_path=path,
        content_base64=base64.b64encode(data).decode("ascii"),
        bundle_sha256=digest,
        overwrite=overwrite,
    )


async def _remove(farm: Any, endpoint: Endpoint) -> str:
    if endpoint.is_master:
        move_to_trash(farm.workspace, endpoint.rel)
        return "moved to master .trash (recoverable)"
    await _call(farm, endpoint.worker_id, "files_delete", file_path=endpoint.rel, recursive=True)
    return f"deleted on worker {endpoint.worker_id} (permanent)"


async def cleanup(farm: Any, worker_ids: list[str], path: str) -> dict[str, Any]:
    """Delete ``path`` (relative to each worker's workspace) on the given workers.

    Runs on every worker even if one fails, and reports each result separately.
    The workspace root itself is refused by the worker.
    """
    rel = (path or "").strip().strip("/")
    if not rel or rel in {".", ".."}:
        raise ValueError("cleanup needs a path inside the worker workspace")
    if not worker_ids:
        raise ValueError("cleanup needs at least one worker_id")
    results: dict[str, Any] = {}
    for wid in worker_ids:
        if wid not in farm.workers:
            results[wid] = {"success": False, "error": "unknown worker"}
            continue
        try:
            await _call(farm, wid, "files_delete", file_path=rel, recursive=True)
            results[wid] = {"success": True}
        except Exception as exc:
            results[wid] = {"success": False, "error": str(exc)}
    return {
        "success": all(item["success"] for item in results.values()),
        "path": rel,
        "workers": results,
    }


async def transfer(
    farm: Any,
    source: str,
    destination: str,
    mode: str = "copy",
    overwrite: bool = False,
) -> dict[str, Any]:
    if mode not in {"copy", "cut"}:
        raise ValueError("mode must be copy or cut")
    src = parse_endpoint(source, farm.workers)
    dst = parse_endpoint(destination, farm.workers)
    if src.is_master == dst.is_master:
        raise ValueError("transfer must go between master and one worker (worker-to-worker is not supported)")
    if not overwrite and await _exists(farm, dst):
        raise FileExistsError(f"{destination} already exists (set overwrite to replace it)")

    paths, is_folder = await _list(farm, src)
    if not paths:
        raise ValueError(f"nothing to transfer from {source}")

    total = 0
    for path in paths:
        data = await _read(farm, src, path)
        if is_folder:
            relative = path[len(src.rel) :].lstrip("/")
            target = f"{dst.rel}/{relative}"
        else:
            target = dst.rel
        await _write(farm, dst, target, data, overwrite)
        total += len(data)

    removed = ""
    if mode == "cut":
        removed = await _remove(farm, src)

    return {
        "success": True,
        "mode": mode,
        "source": source,
        "destination": destination,
        "files_transferred": len(paths),
        "bytes_transferred": total,
        "source_removed": removed or None,
    }