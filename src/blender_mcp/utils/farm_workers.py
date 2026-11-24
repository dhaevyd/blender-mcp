"""Configuration and MCP transport helpers for Blender Farm workers."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psutil

from blender_mcp import __version__, app_version
from blender_mcp.utils.gpu_sysfs import amd_gpu_stats, intel_gpu_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    url: str
    api_key: str = field(repr=False)
    workspace: str = "/workspace"


def validate_worker_destination(url: str) -> None:
    """Allow only explicitly private IP literals; DNS is not pinned."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("worker destination must use an IP literal (DNS hostnames are not supported)") from exc
    allowed = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("fc00::/7"),

    )
    if not any(address.version == network.version and address in network for network in allowed):
        raise ValueError("worker endpoint must use an RFC1918, Tailscale, or IPv6 ULA IP")


def mcp_role(environ: dict[str, str] | None = None) -> str:
    """Resolve the server role from MCP_ROLE.

    Values: master (coordinator), worker (render worker), stand-alone (default).
    Empty/unset and the aliases standalone/stand_alone normalize to stand-alone.
    Any other value logs a warning and falls back to stand-alone.
    """
    source = os.environ if environ is None else environ
    raw = (source.get("MCP_ROLE") or "").strip().lower()
    if raw in {"", "stand-alone", "standalone", "stand_alone"}:
        return "stand-alone"
    if raw in {"master", "worker"}:
        return raw
    logger.warning("Unknown MCP_ROLE %r; falling back to 'stand-alone'", source.get("MCP_ROLE"))
    return "stand-alone"


def load_worker_configs(raw: str | None = None, environ: dict[str, str] | None = None) -> list[WorkerConfig]:
    """Read worker endpoints; credentials are referenced by env-var name, never stored in JSON."""
    source = os.environ if environ is None else environ
    raw = source.get("BLENDER_FARM_WORKERS", "[]") if raw is None else raw
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("BLENDER_FARM_WORKERS must be valid JSON") from exc
    if not isinstance(records, list):
        raise ValueError("BLENDER_FARM_WORKERS must be a JSON array")

    configs: list[WorkerConfig] = []
    seen_ids: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("each worker configuration must be a JSON object")
        worker_id = str(item.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", worker_id):
            raise ValueError(f"invalid worker id: {worker_id!r}")
        if worker_id in seen_ids:
            raise ValueError(f"duplicate worker id: {worker_id}")
        seen_ids.add(worker_id)

        url = str(item.get("url", "")).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"worker {worker_id} must have an HTTP(S) MCP URL without embedded credentials")
        validate_worker_destination(url)

        key_env = str(item.get("api_key_env", "")).strip()
        if not key_env:
            raise ValueError(f"worker {worker_id} must name an api_key_env")
        api_key = source.get(key_env, "")
        if not api_key:
            raise ValueError(f"missing worker API key in environment variable {key_env}")
        workspace = str(item.get("workspace", "/workspace")).strip() or "/workspace"
        # Ignore legacy `slots` values so existing environment configuration remains loadable.
        configs.append(WorkerConfig(worker_id=worker_id, url=url, api_key=api_key, workspace=workspace))
    return configs


def worker_identity(config: WorkerConfig | None = None) -> dict[str, Any]:
    """Return non-secret runtime identity and local Blender availability."""
    worker_id = os.environ.get("BLENDER_FARM_WORKER_ID") or (config.worker_id if config else socket.gethostname())
    workspace = os.environ.get("BLENDER_WORKSPACE") or (config.workspace if config else "/workspace")
    blender = os.environ.get("BLENDER_EXECUTABLE", "blender")
    try:
        result = subprocess.run([blender, "--version"], capture_output=True, text=True, timeout=5, check=False)
        version = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None
    except (OSError, subprocess.TimeoutExpired):
        version = None
    gpu_inventory = gpu_inventory_stats()
    gpu = [
        f"{device['name']}, {device['memory_total_mib']} MiB"
        for device in gpu_inventory
        if device.get("name") and device.get("memory_total_mib")
    ]
    capabilities = ["identity", "project_validation", "render_frame", "frame_manifest", "async_jobs"]
    return {
        "worker_id": worker_id,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "role": mcp_role(),
        "workspace": workspace,
        "version": app_version(),
        # Docker sets this from the VERSION build argument used for the tag.
        "image_tag": os.environ.get("BLENDER_MCP_VERSION") or None,
        "blender_version": version,
        "online": version is not None,
        "cpu_threads": psutil.cpu_count(logical=True),
        "memory_total_bytes": psutil.virtual_memory().total,
        "gpu_devices": gpu,
        "gpu_stats": gpu_inventory,
        "workspace_writable": os.path.isdir(workspace) and os.access(workspace, os.W_OK | os.X_OK),
        "capabilities": capabilities,
    }


def _parse_power_draw(raw: str) -> float | None:
    """Parse a nvidia-smi ``power.draw`` value into watts; ``None`` when unavailable.

    ``nounits`` normally strips the `` W`` suffix, but tolerate it defensively.
    ``[N/A]`` (and variants) mean the device does not report power.
    """
    value = (raw or "").strip().lower()
    if value in {"", "n/a", "[n/a]", "na"}:
        return None
    if value.endswith("w"):
        value = value[:-1].strip()
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _nvidia_gpu_stats() -> list[dict[str, Any]]:
    """Return per-GPU stats from nvidia-smi; an empty list on any failure.

    Per-GPU rows include the nvidia-smi index and total/used/free memory, utilization,
    temperature and power. Rows that cannot be parsed (missing columns, non-integer
    values, ``[N/A]`` placeholders) are skipped.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,index",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    devices: list[dict[str, Any]] = []
    for ordinal, raw_line in enumerate(result.stdout.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            if len(parts) >= 7:
                name = ",".join(parts[:-6]).strip()
                memory_total_mib = int(parts[-6])
                memory_used_mib = int(parts[-5])
                utilization_percent = int(parts[-4])
                temperature_c = int(parts[-3])
                power_raw = parts[-2]
                index = int(parts[-1])
            else:
                name = ",".join(parts[:-5]).strip()
                memory_total_mib = int(parts[-5])
                memory_used_mib = int(parts[-4])
                utilization_percent = int(parts[-3])
                temperature_c = int(parts[-2])
                power_raw = parts[-1]
                index = ordinal
        except (ValueError, TypeError):
            continue
        if not name:
            continue
        devices.append(
            {
                "name": name,
                "index": index,
                "memory_total_mib": memory_total_mib,
                "memory_used_mib": memory_used_mib,
                "memory_free_mib": max(0, memory_total_mib - memory_used_mib),
                "utilization_percent": utilization_percent,
                "temperature_c": temperature_c,
                "power_draw_w": _parse_power_draw(power_raw),
            }
        )
    return devices


def _shared_gpu_memory(device: dict[str, Any]) -> None:
    """Bound shared-GPU scheduling by both GTT capacity and host-available RAM."""
    try:
        memory = psutil.virtual_memory()
    except Exception:
        logger.debug("Shared GPU RAM telemetry unavailable", exc_info=True)
        device["memory_free_mib"] = None
        return
    host_total = max(1, int(memory.total // (1024 * 1024)))
    host_free = max(0, int(memory.available // (1024 * 1024)))
    reported_total = device.get("memory_total_mib")
    if isinstance(reported_total, (int, float)) and reported_total > 0:
        total = min(host_total, int(reported_total))
    else:
        try:
            limit = max(512, int(os.environ.get("MCP_GPU_SHARED_MEMORY_LIMIT_MIB", "8192")))
        except ValueError:
            limit = 8192
        total = min(host_total, limit)
    used = device.get("memory_used_mib")
    gtt_free = max(0, total - int(used)) if isinstance(used, (int, float)) else total
    device["memory_total_mib"] = total
    device["memory_free_mib"] = min(total, host_free, gtt_free)


def gpu_inventory_stats() -> list[dict[str, Any]]:
    """Return per-device memory availability plus stable backend-local GPU indexes."""
    nvidia = _nvidia_gpu_stats()
    vendors = (("AMD", amd_gpu_stats), ("INTEL", intel_gpu_stats))
    devices_by_vendor: list[dict[str, Any]] = []
    for backend, reader in vendors:
        try:
            devices = reader()
        except Exception:
            logger.debug("%s sysfs telemetry unavailable", backend, exc_info=True)
            devices = []
        for index, device in enumerate(devices):
            device["index"] = index
            device["backend"] = backend
            total = device.get("memory_total_mib")
            if backend in {"AMD", "INTEL"} and device.get("memory_type") == "dedicated" and isinstance(total, int) and 0 < total <= 512:
                device["memory_total_mib"] = None
                device["memory_used_mib"] = None
                device["memory_type"] = "shared"
            if device.get("memory_type") == "shared":
                _shared_gpu_memory(device)
            else:
                total, used = device.get("memory_total_mib"), device.get("memory_used_mib")
                device["memory_free_mib"] = (
                    max(0, total - used) if isinstance(total, int) and isinstance(used, int) else None
                )
        devices_by_vendor.extend(devices)
    for device in nvidia:
        device["backend"] = "NVIDIA"
        device["memory_type"] = "dedicated"
    return nvidia + devices_by_vendor


def gpu_stats() -> list[dict[str, Any]]:
    """Return compact GPU telemetry rows for diagnostics."""
    return [
        {key: value for key, value in device.items() if key not in {"index", "memory_free_mib", "backend", "memory_type"}}
        for device in gpu_inventory_stats()
    ]


class MCPWorkerClient:
    """One-call Streamable HTTP MCP client; each call owns and closes its session."""

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config

    @staticmethod
    def _http_client_factory(headers=None, timeout=None, auth=None):
        import httpx

        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False)

    async def list_tools(self) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            self.config.url,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=10,
            sse_read_timeout=30,
            httpx_client_factory=self._http_client_factory,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [{"name": tool.name, "description": tool.description or ""} for tool in result.tools]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], *, timeout: float = 1800
    ) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            self.config.url,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=timeout,
            sse_read_timeout=timeout,
            httpx_client_factory=self._http_client_factory,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                result = await session.call_tool(tool_name, arguments)

                def with_build_metadata(payload: dict[str, Any]) -> dict[str, Any]:
                    if tool_name != "blender_farm_worker" or arguments.get("operation") != "identity":
                        return payload
                    version = initialized.serverInfo.version
                    identity = {**payload}
                    if not identity.get("version") and version:
                        identity["version"] = version
                    # Legacy images announce their build tag as the MCP server version.
                    # An explicit null means the worker itself knows it isn't a tagged build.
                    if "image_tag" not in identity and version and version != __version__:
                        identity["image_tag"] = version
                    return identity

                if result.isError:
                    raise RuntimeError("worker returned an MCP tool error")
                structured = getattr(result, "structuredContent", None)
                if isinstance(structured, dict):
                    return with_build_metadata(structured)
                text_parts = [part.text for part in result.content if getattr(part, "type", None) == "text"]
                if len(text_parts) == 1:
                    try:
                        parsed = json.loads(text_parts[0])
                        if isinstance(parsed, dict):
                            return with_build_metadata(parsed)
                    except json.JSONDecodeError:
                        pass
                return {"content": text_parts}
