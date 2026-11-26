"""Authenticated Gallery and ephemeral MCP worker registration routes."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import uuid
from urllib.parse import quote, urlsplit

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from blender_mcp.utils.blend_thumbnail import extract_thumbnail
from blender_mcp.utils.farm_workers import MCPWorkerClient, WorkerConfig, validate_worker_destination
from blender_mcp.utils.monitor_history import gpu_history, start_sampler
from blender_mcp.utils.projects import (
    TrashRefused,
    browse_workspace,
    create_project,
    delete_from_trash,
    latest_media,
    list_assets,
    list_projects,
    list_trash,
    move_to_trash,
    move_workspace_entry,
    open_asset,
    open_workspace_file,
    restore_from_trash,
    search_workspace,
)

_runtime_workers: dict[str, WorkerConfig] = {}


def _safe_worker_data(value, secret: str):
    if isinstance(value, dict):
        return {
            str(k): _safe_worker_data(v, secret)
            for k, v in value.items()
            if not any(part in str(k).lower() for part in ("api_key", "token", "secret", "credential"))
        }
    if isinstance(value, list):
        return [_safe_worker_data(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[redacted]") if secret else value
    return value


def _normalize_worker_url(raw: str) -> str:
    """Normalize supported address shorthand without misreading IPv6 colons."""
    value = raw.strip()
    if "://" in value:
        return value
    host = value
    port = 10849
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("invalid bracketed address")
        host = value[1:end]
        suffix = value[end + 1:]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise ValueError("invalid port")
            port = int(suffix[1:])
    else:
        if value.count(":") > 1:
            candidate, raw_port = value.rsplit(":", 1)
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                pass
            else:
                if len(raw_port) == 4 and raw_port.isdigit():
                    host, port = candidate, int(raw_port)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if value.count(":") == 1:
                candidate, raw_port = value.rsplit(":", 1)
                if raw_port.isdigit():
                    host, port = candidate, int(raw_port)
        else:
            if host == value:
                host = value
    address = ipaddress.ip_address(host)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    authority = f"[{address.compressed}]" if address.version == 6 else str(address)
    return f"http://{authority}:{port}/mcp"


def _valid_mcp_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"http", "https"} and parsed.hostname is not None
                and not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment and parsed.path == "/mcp" and parsed.port is not None)
    except ValueError:
        return False


def _worker_compose_yaml(*, app_version: str, name: str, worker_id: str, workspace: str, port: int, gpu_vendor: str) -> str:
    import yaml

    compose_name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].rstrip("-") or worker_id

    def base_service(require_gpu: str) -> dict:
        # The start script exports MCP_RENDER_REQUIRE_GPU for the profile it picks; the
        # default here only applies when compose is run by hand.
        return {
            "image": f"ghcr.io/dhaevyd/blender-mcp:{app_version}",
            "container_name": "blender_mcp-worker",
            "restart": "unless-stopped",
            "env_file": [".env"],
            "environment": {
                "MCP_ROLE": "worker",
                "BLENDER_FARM_WORKER_ID": worker_id,
                "BLENDER_WORKSPACE": workspace,
                "MCP_RENDER_REQUIRE_GPU": f"${{MCP_RENDER_REQUIRE_GPU:-{require_gpu}}}",
            },
            "volumes": [f"./{worker_id}-workspace:{workspace}"],
            "ports": [f"{port}:10849"],
            "networks": ["proxy"],
        }

    def apply_gpu(service: dict, vendor: str) -> dict:
        if vendor == "nvidia":
            service["gpus"] = "all"
            service["environment"].update(
                {"NVIDIA_VISIBLE_DEVICES": "all", "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,graphics"}
            )
            service["volumes"].append(
                "/usr/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/10_nvidia.json:ro"
            )
        else:
            service["devices"] = ["/dev/dri:/dev/dri"]
            service["group_add"] = ["${VIDEO_GID:-44}", "${RENDER_GID:-109}"]
            if vendor == "amd":
                service["devices"].insert(0, "/dev/kfd:/dev/kfd")
                service["volumes"].append("/opt/rocm:/opt/rocm:ro")
                service["environment"].update(
                    {"HIP_PATH": "/opt/rocm", "LD_LIBRARY_PATH": "/opt/rocm/lib:/opt/rocm/lib64"}
                )
                service["security_opt"] = ["seccomp=unconfined"]
        return service

    if gpu_vendor == "auto":
        # One compose file for every host: the start script turns on exactly one
        # profile after detecting the hardware, so the shared container_name/ports
        # are never requested by two services at once. "cpu" is the fallback for a
        # VM or machine without a GPU; its renders run on CPU instead of failing.
        services = {}
        for vendor in ("nvidia", "amd", "intel"):
            service = base_service("true")
            service["profiles"] = [vendor]
            services[f"blender-worker-{vendor}"] = apply_gpu(service, vendor)
        cpu_service = base_service("false")
        cpu_service["profiles"] = ["cpu"]
        services["blender-worker-cpu"] = cpu_service
    elif gpu_vendor == "cpu":
        services = {"blender-worker": base_service("false")}
    else:
        services = {"blender-worker": apply_gpu(base_service("true"), gpu_vendor)}
    return yaml.safe_dump(
        {
            "name": compose_name,
            "services": services,
            "networks": {"proxy": {"name": "proxy", "external": True}},
        },
        sort_keys=False,
    )


_WORKER_GPU_AUTODETECT_SCRIPT = '''#!/bin/sh
set -eu

has_pci_vendor() {
    grep -hqi "^PCI_ID=$1:" /sys/class/drm/card*/device/uevent 2>/dev/null
}

vendor=""
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia_list=$(nvidia-smi -L 2>/dev/null || true)
    case "$nvidia_list" in
        *GPU*) vendor="nvidia" ;;
    esac
fi
if [ -z "$vendor" ] && has_pci_vendor 10de; then vendor="nvidia"; fi
if [ -z "$vendor" ] && has_pci_vendor 1002; then vendor="amd"; fi
if [ -z "$vendor" ] && has_pci_vendor 8086; then vendor="intel"; fi

if [ -z "$vendor" ]; then
    vendor="cpu"
fi

# GPU hosts must render on the GPU. A host without one renders on CPU, and says so.
if [ "$vendor" = "cpu" ]; then
    export MCP_RENDER_REQUIRE_GPU=false
else
    export MCP_RENDER_REQUIRE_GPU=true
fi
echo "Detected $vendor; starting the matching worker profile (MCP_RENDER_REQUIRE_GPU=$MCP_RENDER_REQUIRE_GPU)."
exec docker compose --env-file .env -f compose.yaml --profile "$vendor" up -d --remove-orphans
'''


async def connect_worker(request: Request):
    try:
        body = await request.json()
        url = str(body.get("url", "")).strip()
        key = str(body.get("api_key", ""))
        if not _valid_mcp_url(url):
            return JSONResponse(
                {"success": False, "error": "Enter a valid HTTP(S) MCP endpoint without embedded credentials."},
                status_code=400,
            )
        try:
            validate_worker_destination(url)
        except ValueError:
            return JSONResponse(
                {"success": False, "error": "Worker URL must resolve only to private/Tailscale addresses."},
                status_code=400,
            )
        if not key or len(key) > 4096:
            return JSONResponse({"success": False, "error": "A valid worker API key is required."}, status_code=400)
        worker_id = "ui-" + uuid.uuid4().hex[:12]
        config = WorkerConfig(worker_id, url, key)
        client = MCPWorkerClient(config)
        tools = await client.list_tools()
        names = {tool["name"] for tool in tools}
        if "blender_farm_worker" not in names:
            return JSONResponse(
                {"success": False, "error": "Authenticated MCP endpoint lacks blender_farm_worker capability."},
                status_code=422,
            )
        identity = await client.call_tool("blender_farm_worker", {"operation": "identity"})
        from blender_mcp.tools.farm_tools import _coordinator

        farm = _coordinator()
        farm.workers[worker_id] = config
        farm._clients[worker_id] = client
        _runtime_workers[worker_id] = config
        return JSONResponse(
            {
                "success": True,
                "worker": {
                    "worker_id": worker_id,
                    "url": url,
                    "online": bool(identity.get("online", identity.get("success", True))),
                    "identity": _safe_worker_data(identity, key),
                    "tools": sorted(names),
                },
            }
        )
    except Exception:
        # Do not return exception text: third-party protocol errors can include request headers.
        return JSONResponse(
            {"success": False, "error": "Worker MCP authentication or capability test failed."}, status_code=502
        )


async def farm_settings_get(request: Request):
    from blender_mcp.utils.farm_workers import mcp_role

    if mcp_role() != "master":
        return JSONResponse({"error": "Master-only farm settings."}, status_code=404)
    try:
        from blender_mcp.server import get_session_ttl_seconds
        from blender_mcp.tools.farm_tools import _coordinator

        enabled = _coordinator().store.get_local_rendering_enabled()
        return JSONResponse(
            {"local_rendering_enabled": enabled, "session_ttl_seconds": get_session_ttl_seconds()},
            headers={"Cache-Control": "private, no-store"},
        )
    except Exception:
        return JSONResponse({"error": "Could not read farm settings."}, status_code=500)


async def farm_settings_patch(request: Request):
    from blender_mcp.utils.farm_workers import mcp_role

    if mcp_role() != "master":
        return JSONResponse({"error": "Master-only farm settings."}, status_code=404)
    body = await _json_body(request)
    if not body:
        return JSONResponse({"error": "Request body required."}, status_code=400)

    updates: dict = {}
    enabled = body.get("local_rendering_enabled")
    session_ttl = body.get("session_ttl_seconds")

    if enabled is not None and not isinstance(enabled, bool):
        return JSONResponse({"error": "local_rendering_enabled must be a boolean."}, status_code=400)
    if session_ttl is not None and (not isinstance(session_ttl, int) or session_ttl < 300):
        return JSONResponse({"error": "session_ttl_seconds must be an integer >= 300."}, status_code=400)

    try:
        from blender_mcp.server import get_session_ttl_seconds, set_session_ttl_seconds
        from blender_mcp.tools.farm_tools import _coordinator

        if enabled is not None:
            _coordinator().store.set_local_rendering_enabled(enabled)
            updates["local_rendering_enabled"] = enabled
        if session_ttl is not None:
            set_session_ttl_seconds(session_ttl)
            updates["session_ttl_seconds"] = get_session_ttl_seconds()

        if not updates:
            return JSONResponse({"error": "No recognised fields to update."}, status_code=400)

        return JSONResponse(updates, headers={"Cache-Control": "private, no-store"})
    except Exception:
        return JSONResponse({"error": "Could not save farm settings."}, status_code=500)


async def workers_get(request: Request):
    try:
        from blender_mcp.tools.farm_tools import _coordinator

        farm = _coordinator()
        managed = farm.store.list_managed_workers()
        runtime = {item.get("worker_id"): item for item in await farm.worker_status()}
        output = []
        ids = set()
        for record in managed:
            wid = record["worker_id"]
            ids.add(wid)
            live = runtime.get(wid, {})
            output.append(
                {
                    **{key: value for key, value in record.items() if key != "slots"},
                    "enabled": bool(record["enabled"]),
                    "verified": bool(record["verified"]),
                    "online": bool(live.get("online", False)),
                    "status": "online" if live.get("online") else ("disabled" if record.get("verified") and not record["enabled"] else ("offline" if record.get("verified") else "pending")),
                    "identity": live.get("identity"),
                }
            )
        output.extend({**item, "source": "environment"} for wid, item in runtime.items() if wid not in ids)
        return JSONResponse({"workers": output, "runtime_only": False}, headers={"Cache-Control": "private, no-store"})
    except Exception:
        return JSONResponse({"error": "Could not read worker status."}, status_code=500)


async def enroll_worker(request: Request):
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"success": False, "error": "Invalid worker settings."}, status_code=400)
    raw = body.get("url", "")
    if not isinstance(raw, str) or any(c.isspace() or ord(c) < 32 for c in raw):
        return JSONResponse({"success": False, "error": "Invalid worker URL."}, status_code=400)
    mode = body.get("mode")
    try:
        url = _normalize_worker_url(raw)
    except ValueError:
        return JSONResponse({"success": False, "error": "Invalid worker URL."}, status_code=400)
    parsed = urlsplit(url)
    if not _valid_mcp_url(url):
        return JSONResponse(
            {"success": False, "error": "Enter a valid private IP or HTTP(S) MCP URL."}, status_code=400
        )
    try:
        validate_worker_destination(url)
    except ValueError:
        return JSONResponse(
            {"success": False, "error": "Worker URL must use a private/Tailscale IP literal."}, status_code=400
        )
    workspace = body.get("workspace", "/workspace")
    gpu_vendor = body.get("gpu_vendor", "nvidia")
    name_value = body.get("name")
    worker_label = name_value.strip() if isinstance(name_value, str) else ""
    name = worker_label or parsed.hostname or ""
    if (
        mode not in {"existing", "compose"}
        or not isinstance(gpu_vendor, str) or gpu_vendor not in {"auto", "nvidia", "amd", "intel", "cpu"}
        or not name or len(name) > 80 or any(ord(c) < 32 for c in name)
        or not isinstance(workspace, str) or not workspace.startswith("/") or len(workspace) > 512
        or any(ord(c) < 32 for c in workspace) or any(x in workspace for x in ("$", "{", "}", "#", ":"))
    ):
        return JSONResponse({"success": False, "error": "Invalid worker settings."}, status_code=400)
    try:
        from blender_mcp.tools.farm_tools import _coordinator

        farm = _coordinator()
        worker_id = re.sub(r"[^a-z0-9]+", "-", worker_label.lower()).strip("-")[:63].rstrip("-") or "ui-" + uuid.uuid4().hex[:12]
        existing_ids = {item["worker_id"] for item in farm.store.list_managed_workers()}
        if worker_id in existing_ids or worker_id in farm.workers:
            return JSONResponse({"success": False, "error": "Worker label is already in use; choose a unique label."}, status_code=409)
        key = secrets.token_urlsafe(32)
        record = {
            "worker_id": worker_id,
            "name": name,
            "url": url,
            "workspace": workspace,
            # Kept as a legacy database column; scheduling no longer uses slots.
            "slots": 1,
            "enabled": False,
        }
        farm.store.save_managed_worker(record, key)
        env = f"MCP_ROLE=worker\nBLENDER_MCP_WORKER_KEY={key}\nBLENDER_FARM_WORKER_ID={worker_id}\nBLENDER_WORKSPACE={workspace}\n"
        result = {
            "success": True,
            "worker": {key: value for key, value in {**record, "online": False, "status": "pending"}.items() if key != "slots"},
            "api_key": key,
            "worker_env": env,
            "gpu_vendor": gpu_vendor,
        }
        if mode == "compose":
            if parsed.scheme != "http" or parsed.path != "/mcp" or parsed.port not in (None, 10849):
                farm.store.delete_managed_worker(worker_id)
                return JSONResponse({"success": False, "error": "Compose mode requires direct HTTP MCP at /mcp on port 10849."}, status_code=400)
            from blender_mcp import app_version

            result["compose_yaml"] = _worker_compose_yaml(
                app_version=app_version(),
                name=name,
                worker_id=worker_id,
                workspace=workspace,
                port=parsed.port or 10849,
                gpu_vendor=gpu_vendor,
            )
            if gpu_vendor == "auto":
                result["setup_script"] = _WORKER_GPU_AUTODETECT_SCRIPT
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
    except Exception:
        return JSONResponse({"success": False, "error": "Could not enroll worker."}, status_code=500)


async def worker_test(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    wid = request.path_params["worker_id"]
    record = farm.store.get_managed_worker(wid)
    if not record:
        return JSONResponse({"success": False, "error": "Worker not found."}, status_code=404)
    key = farm.store.get_managed_worker_secret(wid)
    config = WorkerConfig(wid, record["url"], key, record["workspace"])
    client = MCPWorkerClient(config)
    try:
        names = {tool["name"] for tool in await client.list_tools()}
        if "blender_farm_worker" not in names:
            raise ValueError()
        identity = await client.call_tool("blender_farm_worker", {"operation": "identity"})
        if identity.get("online") is not True:
            raise ValueError("Blender is not ready")
        farm.workers[wid] = config
        farm._clients[wid] = client
        farm.store.update_managed_worker(wid, {"enabled": True, "verified": True})
        return JSONResponse(
            {
                "success": True,
                "worker": {
                    **{key: value for key, value in record.items() if key != "slots"},
                    "enabled": True,
                    "online": True,
                    "status": "online",
                    "identity": _safe_worker_data(identity, key),
                },
            },
            headers={"Cache-Control": "private, no-store"},
        )
    except Exception:
        farm.store.update_managed_worker(wid, {"enabled": False, "verified": False})
        farm.workers.pop(wid, None)
        farm._clients.pop(wid, None)
        return JSONResponse(
            {"success": False, "error": "Worker MCP authentication or capability test failed."}, status_code=502
        )


async def worker_patch(request: Request):
    body = await _json_body(request)
    if body is None or set(body) - {"name", "url", "workspace", "enabled"}:
        return JSONResponse({"success": False, "error": "Invalid worker settings."}, status_code=400)
    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    wid = request.path_params["worker_id"]
    old = farm.store.get_managed_worker(wid)
    if old is None:
        return JSONResponse({"success": False, "error": "Worker not found."}, status_code=404)
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return JSONResponse({"success": False, "error": "enabled must be a boolean."}, status_code=400)
    if "name" in body and (not isinstance(body["name"], str) or not body["name"].strip() or len(body["name"]) > 80 or any(ord(c) < 32 for c in body["name"])):
        return JSONResponse({"success": False, "error": "Invalid name."}, status_code=400)
    if "workspace" in body and (not isinstance(body["workspace"], str) or not body["workspace"].startswith("/") or len(body["workspace"]) > 512 or any(ord(c) < 32 for c in body["workspace"]) or any(x in body["workspace"] for x in ("$", "{", "}", "#", ":"))):
        return JSONResponse({"success": False, "error": "workspace must be a safe absolute path."}, status_code=400)
    if body.get("enabled") is True and not old.get("verified"):
        return JSONResponse({"success": False, "error": "Test the worker before enabling it."}, status_code=409)
    if "url" in body:
        if not isinstance(body["url"], str) or not _valid_mcp_url(body["url"]):
            return JSONResponse({"success": False, "error": "Enter a valid HTTP(S) MCP endpoint."}, status_code=400)
        try:
            validate_worker_destination(body["url"])
        except ValueError:
            return JSONResponse(
                {"success": False, "error": "Worker URL must use a private/Tailscale IP literal."}, status_code=400
            )
    changed = any(k in body and body[k] != old.get(k) for k in ("url", "workspace"))
    updates = dict(body)
    if changed:
        updates.update(verified=False, enabled=False)
    if not farm.store.update_managed_worker(wid, updates):
        return JSONResponse({"success": False, "error": "Worker not found."}, status_code=404)
    if changed or body.get("enabled") is False:
        farm._clients.pop(wid, None)
        farm.workers.pop(wid, None)
    updated = farm.store.get_managed_worker(wid)
    if updated is not None:
        updated.pop("slots", None)
    return JSONResponse(
        {"success": True, "worker": updated}, headers={"Cache-Control": "private, no-store"}
    )


async def worker_delete(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    wid = request.path_params["worker_id"]
    if not farm.store.delete_managed_worker(wid):
        return JSONResponse({"success": False, "error": "Worker not found."}, status_code=404)
    farm.workers.pop(wid, None)
    farm._clients.pop(wid, None)
    return JSONResponse({"success": True})


async def farm_gpus_get(request: Request):
    """Return a live, per-device GPU snapshot from every configured farm worker."""
    try:
        from blender_mcp.tools.farm_tools import _coordinator

        farm = _coordinator()
        worker_ids = list(farm.workers)
        reports = await asyncio.gather(*(farm._read_worker_identity(worker_id) for worker_id in worker_ids))
        workers = []
        for worker_id, report in zip(worker_ids, reports, strict=True):
            stats = report.get("gpu_stats", []) if report else []
            image_tag = report.get("image_tag") if report else None
            if not isinstance(image_tag, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", image_tag):
                image_tag = None
            workers.append(
                {
                    "worker_id": worker_id,
                    "online": report is not None,
                    "image_tag": image_tag,
                    "gpus": stats if isinstance(stats, list) else [],
                }
            )
        return JSONResponse({"workers": workers}, headers={"Cache-Control": "private, no-store"})
    except Exception:
        return JSONResponse({"error": "Could not read farm GPU telemetry."}, status_code=500)


async def farm_status_get(request: Request):
    """Return the coordinator's structured farm snapshot behind API-key middleware."""
    try:
        from blender_mcp.tools.farm_tools import _coordinator

        farm = _coordinator()
        snapshot = _safe_worker_data(await farm.farm_status(), "")
        # A worker error can contain another worker's credential. Replace
        # longer keys first so overlapping credentials are fully redacted.
        secrets = sorted({config.api_key for config in farm.workers.values() if config.api_key}, key=len, reverse=True)
        for secret in secrets:
            snapshot = _safe_worker_data(snapshot, secret)
        return JSONResponse(snapshot, headers={"Cache-Control": "private, no-store"})
    except Exception:
        # Worker errors may contain credentials; never echo exception details.
        return JSONResponse({"success": False, "error": "Could not read farm status."}, status_code=500)


async def projects_get(request: Request):
    try:
        return JSONResponse({"projects": list_projects()})
    except Exception:
        return JSONResponse({"error": "Could not list workspace projects."}, status_code=500)


async def projects_post(request: Request):
    try:
        body = await request.json()
        project = create_project(str(body.get("name", "")))
        return JSONResponse({"project": project}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not create workspace project."}, status_code=500)


async def project_thumbnail_get(request: Request):
    from blender_mcp.utils.projects import safe_project, workspace_root
    project_id = request.path_params["project_id"]
    try:
        root = workspace_root()
        project_dir = safe_project(root, project_id)
        blend_file = next(
            (f for f in project_dir.iterdir() if f.suffix.lower() == ".blend" and f.is_file()),
            None,
        )
        if blend_file is None:
            # Also check if project_id is itself a .blend file at workspace root
            candidate = root / project_id
            if candidate.is_file() and candidate.suffix.lower() == ".blend":
                blend_file = candidate
        if blend_file is None:
            return JSONResponse({"error": "No .blend file found for project."}, status_code=404)
        png = await asyncio.to_thread(extract_thumbnail, blend_file)
        if png is None:
            return JSONResponse({"error": "No thumbnail embedded in .blend file."}, status_code=404)
        return StreamingResponse(iter([png]), media_type="image/png")
    except (ValueError, FileNotFoundError):
        return JSONResponse({"error": "Project not found."}, status_code=404)
    except Exception:
        return JSONResponse({"error": "Could not extract thumbnail."}, status_code=500)


async def assets_get(request: Request):
    try:
        return JSONResponse({"assets": list_assets(request.path_params["project_id"])})
    except ValueError:
        return JSONResponse({"error": "Invalid project or asset path."}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "Project not found."}, status_code=404)


async def asset_get(request: Request):
    try:
        fd, media, size, _name = open_asset(request.path_params["project_id"], request.path_params["asset_path"])

        async def stream():
            while chunk := await __import__("asyncio").to_thread(os.read, fd, 65536):
                yield chunk

        return StreamingResponse(
            stream(),
            media_type=media,
            headers={"Content-Length": str(size), "Content-Disposition": "inline"},
            background=BackgroundTask(os.close, fd),
        )
    except ValueError:
        return JSONResponse({"error": "Unsupported asset path."}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "Asset not found."}, status_code=404)


async def latest_get(request: Request):
    try:
        return JSONResponse({"file": latest_media()})
    except Exception:
        return JSONResponse({"error": "Could not read latest workspace media."}, status_code=500)


async def browse_get(request: Request):
    try:
        return JSONResponse(
            {
                "path": request.query_params.get("path", ""),
                "entries": browse_workspace(request.query_params.get("path", "")),
            }
        )
    except ValueError:
        return JSONResponse({"error": "Invalid workspace path."}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "Folder not found."}, status_code=404)


async def search_get(request: Request):
    try:
        results = await asyncio.to_thread(search_workspace, request.query_params.get("q", ""))
        return JSONResponse(results, headers={"Cache-Control": "private, no-store"})
    except ValueError:
        return JSONResponse({"error": "Invalid search query."}, status_code=400)


async def _json_body(request: Request) -> dict | None:
    """Small helper: a JSON object body, or None for anything else."""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def gallery_delete(request: Request):
    """Soft-delete a workspace entry into .trash; the path leaves the Gallery."""
    body = await _json_body(request)
    path = body.get("path") if body else None
    if not isinstance(path, str) or not path:
        return JSONResponse({"error": "A workspace path is required."}, status_code=400)
    try:
        result = await asyncio.to_thread(move_to_trash, None, path)
    except TrashRefused:
        return JSONResponse({"error": "This item cannot be deleted."}, status_code=409)
    except FileNotFoundError:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "Invalid workspace path."}, status_code=400)
    except OSError:
        return JSONResponse({"error": "Could not delete this item."}, status_code=500)
    return JSONResponse(result)


async def gallery_trash(request: Request):
    """List restorable items in the authenticated workspace trash."""
    try:
        return JSONResponse({"entries": await asyncio.to_thread(list_trash, None)})
    except (OSError, ValueError):
        return JSONResponse({"error": "Could not read the workspace trash."}, status_code=500)


async def gallery_trash_delete(request: Request):
    """Permanently remove one item from workspace trash."""
    body = await _json_body(request)
    trash_id = body.get("trash_id") if body else None
    if not isinstance(trash_id, str) or not trash_id:
        return JSONResponse({"error": "A trash id is required."}, status_code=400)
    try:
        await asyncio.to_thread(delete_from_trash, None, trash_id)
    except FileNotFoundError:
        return JSONResponse({"error": "Trash entry not found."}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "Invalid trash id."}, status_code=400)
    except OSError:
        return JSONResponse({"error": "Could not permanently delete this item."}, status_code=500)
    return JSONResponse({"deleted": True})


async def gallery_restore(request: Request):
    """Move a trashed entry back to the exact path it was deleted from."""
    body = await _json_body(request)
    trash_id = body.get("trash_id") if body else None
    if not isinstance(trash_id, str) or not trash_id:
        return JSONResponse({"error": "A trash id is required."}, status_code=400)
    try:
        result = await asyncio.to_thread(restore_from_trash, None, trash_id)
    except TrashRefused:
        return JSONResponse({"error": "This item cannot be restored."}, status_code=409)
    except FileNotFoundError:
        return JSONResponse({"error": "Trash entry not found."}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "Invalid trash id."}, status_code=400)
    except OSError:
        return JSONResponse({"error": "Could not restore this item."}, status_code=500)
    return JSONResponse(result)


async def workspace_file_get(request: Request):
    return _workspace_file_response(request, request.query_params.get("path", ""))


def _workspace_file_response(request: Request, path: str, *, download: bool = False):
    try:
        fd, info, media = open_workspace_file(path)
    except ValueError:
        return JSONResponse({"error": "Invalid workspace path."}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "File not found."}, status_code=404)

    size = info.st_size
    preview = request.query_params.get("preview") == "1"
    if preview and media == "text/plain" and size > 65536:
        os.close(fd)
        return JSONResponse({"error": "Text preview exceeds 64 KiB."}, status_code=413)

    start, end = 0, size - 1
    status_code = 200
    range_header = request.headers.get("range")
    if range_header and media in {"video/mp4", "video/webm"}:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if not match or (not match[1] and not match[2]):
            os.close(fd)
            return JSONResponse(
                {"error": "Invalid range."}, status_code=416, headers={"Content-Range": f"bytes */{size}"}
            )
        if match[1]:
            start = int(match[1])
            end = min(int(match[2]), size - 1) if match[2] else size - 1
        else:
            start = max(0, size - int(match[2]))
        if start >= size or end < start:
            os.close(fd)
            return JSONResponse(
                {"error": "Invalid range."}, status_code=416, headers={"Content-Range": f"bytes */{size}"}
            )
        status_code = 206

    inline = not download and media != "application/octet-stream" and not (media == "text/plain" and size > 65536)
    # RFC 5987 encoding avoids header injection and preserves Unicode filenames.
    filename = path.rsplit("/", 1)[-1]
    headers = {
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename, safe='')}",
        "Content-Length": str(max(0, end - start + 1)),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes" if media in {"video/mp4", "video/webm"} else "none",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    closed = False

    def close_fd():
        nonlocal closed
        if not closed:
            closed = True
            os.close(fd)

    async def stream():
        try:
            remaining = max(0, end - start + 1)
            os.lseek(fd, start, os.SEEK_SET)
            while remaining:
                chunk = await __import__("asyncio").to_thread(os.read, fd, min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            close_fd()

    return StreamingResponse(
        stream(), status_code=status_code, media_type=media, headers=headers, background=BackgroundTask(close_fd)
    )


async def monitoring_gpu_get(request: Request):
    """Bucketed GPU telemetry history; sits under /api so bearer auth applies."""
    try:
        payload = await asyncio.to_thread(gpu_history, request.query_params.get("window", "live"))
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})
    except ValueError:
        return JSONResponse({"error": "invalid window"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not read GPU history."}, status_code=500)


async def farm_jobs_get(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    return JSONResponse({"jobs": _coordinator().list_jobs(100)})


async def farm_job_submit(request: Request):
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "Provide a JSON job request."}, status_code=400)
    project_path = body.get("project_path")
    frame_start = body.get("frame_start", 1)
    frame_end = body.get("frame_end", 1)
    if not isinstance(project_path, str) or not project_path.strip():
        return JSONResponse({"error": "project_path is required."}, status_code=400)
    if (
        isinstance(frame_start, bool) or not isinstance(frame_start, int)
        or isinstance(frame_end, bool) or not isinstance(frame_end, int)
        or frame_start < 0 or frame_end < frame_start or frame_end - frame_start > 999_999
    ):
        return JSONResponse({"error": "Frame range must be valid and contain no more than 1,000,000 frames."}, status_code=400)
    worker_ids = body.get("worker_ids")
    if worker_ids is not None and (
        not isinstance(worker_ids, list) or not worker_ids
        or any(not isinstance(worker_id, str) for worker_id in worker_ids)
    ):
        return JSONResponse({"error": "worker_ids must be a non-empty array of worker IDs."}, status_code=400)
    output_directory = body.get("output_directory")
    project_id = body.get("project_id")
    if output_directory is not None and not isinstance(output_directory, str):
        return JSONResponse({"error": "output_directory must be a string."}, status_code=400)
    if project_id is not None and not isinstance(project_id, str):
        return JSONResponse({"error": "project_id must be a string."}, status_code=400)

    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    try:
        result = await farm.submit_render(
            project_path.strip(), frame_start, frame_end, worker_ids=worker_ids,
            output_directory=output_directory or None, gallery_project_id=project_id or None,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not submit farm job. Check worker connectivity and project files."}, status_code=502)
    farm.store.record_audit(getattr(request.state, "studio_role", "admin"), "job.submit", result["job_id"])
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "private, no-store"})


async def farm_job_get(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    try:
        job = farm.job_status(request.path_params["job_id"])
    except KeyError:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    for frame in job["frames"]:
        frame.pop("log", None)
    for config in farm.workers.values():
        if config.api_key:
            job = _safe_worker_data(job, config.api_key)
    return JSONResponse(job, headers={"Cache-Control": "private, no-store"})


async def farm_job_stream(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    job_id = request.path_params["job_id"]
    farm = _coordinator()
    try:
        farm.store.get_progress(job_id)
    except KeyError:
        return JSONResponse({"error": "Job not found."}, status_code=404)

    async def _event_generator():
        import json as _json
        terminal = {"done", "failed", "cancelled"}
        while True:
            try:
                progress = farm.store.get_progress(job_id)
            except KeyError:
                break
            total = progress.get("total_frames", 0)
            completed = progress.get("completed_frames", 0)
            failed = progress.get("failed_frames", 0)
            status = progress.get("status", "unknown")
            payload = {
                "job_id": job_id,
                "status": status,
                "total_frames": total,
                "completed_frames": completed,
                "failed_frames": failed,
                "percent": round(completed / total * 100, 1) if total else 0,
            }
            yield f"data: {_json.dumps(payload)}\n\n"
            if status in terminal:
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def farm_job_action(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    body = await _json_body(request)
    action = body.get("action") if body else None
    farm = _coordinator()
    methods = {"pause": farm.pause_job, "resume": farm.resume_job, "cancel": farm.cancel_job,
               "retry_failed": farm.retry_failed_frames}
    if not isinstance(action, str) or action not in methods:
        return JSONResponse({"error": "Unknown job action."}, status_code=400)
    job_id = request.path_params["job_id"]
    try:
        farm.store.get_progress(job_id)
        changed = methods[action](job_id)
    except KeyError:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    except ValueError:
        changed = False
    if not changed:
        return JSONResponse({"error": "Action not available in the current job state."}, status_code=409)
    farm.store.record_audit(getattr(request.state, "studio_role", "admin"), f"job.{action}", job_id)
    return JSONResponse({"success": True})


async def farm_job_download(request: Request):
    from blender_mcp.tools.farm_tools import _coordinator

    try:
        records = _coordinator().store.get_frame_records(request.path_params["job_id"])
    except KeyError:
        records = []
    frame = next((item for item in records if item["frame"] == request.path_params["frame"]), None)
    if not frame or frame["status"] != "completed" or not frame["output_path"]:
        return JSONResponse({"error": "Frame output not found."}, status_code=404)
    return _workspace_file_response(request, frame["output_path"], download=True)


async def farm_operations_get(request: Request):
    import shutil
    import time

    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    alerts = []
    for worker in await farm.worker_status():
        if not worker.get("online"):
            alerts.append(f"Worker {worker['worker_id']} is offline or not ready.")
    for job in farm.list_jobs(100):
        if job["status"] != "running":
            continue
        frames = farm.store.get_frame_records(job["id"])
        activity = max([job["updated_at"], *(frame["updated_at"] for frame in frames)])
        if time.time() - activity > 900:
            alerts.append(f"Job {job['id']} has had no frame activity for 15 minutes.")
    try:
        disk = shutil.disk_usage(farm.workspace)
        if disk.total and disk.free / disk.total <= 0.1:
            alerts.append("Workspace filesystem is at least 90% full.")
    except OSError:
        alerts.append("Workspace disk capacity could not be checked.")
    role = getattr(request.state, "studio_role", "admin")
    return JSONResponse({"alerts": alerts, "audit": farm.store.recent_audit() if role == "admin" else []},
                        headers={"Cache-Control": "private, no-store"})


async def farm_access_get(request: Request):
    return JSONResponse({"role": getattr(request.state, "studio_role", "admin")})


async def worker_logs_get(request: Request):
    """Recent log lines from one enrolled worker's in-memory buffer; behind bearer auth via /api."""
    from blender_mcp.tools.farm_tools import _coordinator

    farm = _coordinator()
    wid = request.path_params["worker_id"]
    if not farm.store.get_managed_worker(wid):
        return JSONResponse({"success": False, "error": "Worker not found."}, status_code=404)
    level = request.query_params.get("level", "").strip().upper()
    if level not in {"", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return JSONResponse({"success": False, "error": "Unknown log level."}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
    except ValueError:
        return JSONResponse({"success": False, "error": "limit must be a whole number."}, status_code=400)
    try:
        result = await farm._client(wid).call_tool(
            "blender_farm_worker",
            {"operation": "logs", "log_level": level, "log_limit": limit},
            timeout=15,
        )
    except Exception:
        return JSONResponse(
            {"success": False, "error": "Worker did not return logs. It may be offline."},
            status_code=502,
        )
    if not isinstance(result, dict) or result.get("success") is not True:
        return JSONResponse({"success": False, "error": "Worker returned no log data."}, status_code=502)
    return JSONResponse(
        {"success": True, "worker_id": wid, "count": result.get("count", 0), "logs": result.get("logs", [])},
        headers={"Cache-Control": "private, no-store"},
    )


MAX_BULK_GALLERY_ITEMS = 500


def _bulk_paths(body: dict | None) -> list[str] | None:
    """Validate a ``paths`` list for the bulk endpoints; None means reject the request."""
    paths = body.get("paths") if body else None
    if not isinstance(paths, list) or not paths or len(paths) > MAX_BULK_GALLERY_ITEMS:
        return None
    if not all(isinstance(item, str) and item for item in paths):
        return None
    return list(dict.fromkeys(paths))


async def gallery_bulk_delete(request: Request):
    """Soft-delete several workspace entries; each one reports its own outcome."""
    body = await _json_body(request)
    paths = _bulk_paths(body)
    if paths is None:
        return JSONResponse({"error": "A list of workspace paths is required."}, status_code=400)
    results = []
    for path in paths:
        try:
            await asyncio.to_thread(move_to_trash, None, path)
            results.append({"path": path, "ok": True})
        except TrashRefused:
            results.append({"path": path, "ok": False, "error": "This item cannot be deleted."})
        except FileNotFoundError:
            results.append({"path": path, "ok": True, "already_gone": True})
        except ValueError:
            results.append({"path": path, "ok": False, "error": "Invalid workspace path."})
        except OSError:
            results.append({"path": path, "ok": False, "error": "Could not delete this item."})
    return JSONResponse({"results": results})


async def gallery_bulk_move(request: Request):
    """Move several workspace entries into one folder; each one reports its own outcome."""
    body = await _json_body(request)
    paths = _bulk_paths(body)
    destination = body.get("destination", "") if body else None
    if paths is None or not isinstance(destination, str):
        return JSONResponse({"error": "A list of paths and a destination folder are required."}, status_code=400)
    results = []
    for path in paths:
        try:
            moved = await asyncio.to_thread(move_workspace_entry, None, path, destination)
            results.append({"path": path, "ok": True, "new_path": moved["new_path"]})
        except FileExistsError:
            results.append({"path": path, "ok": False, "error": "An item with that name already exists there."})
        except TrashRefused as exc:
            results.append({"path": path, "ok": False, "error": str(exc)})
        except FileNotFoundError:
            results.append({"path": path, "ok": False, "error": "Item or destination folder not found."})
        except ValueError:
            results.append({"path": path, "ok": False, "error": "Invalid workspace path."})
        except OSError:
            results.append({"path": path, "ok": False, "error": "Could not move this item."})
    return JSONResponse({"results": results})


def register_gallery_routes(app):
    app.router.routes.extend(
        [
            Route("/api/v1/workers/enroll", enroll_worker, methods=["POST"]),
            Route("/api/v1/workers/{worker_id}/logs", worker_logs_get, methods=["GET"]),
            Route("/api/v1/workers/{worker_id}/test", worker_test, methods=["POST"]),
            Route("/api/v1/workers/{worker_id}", worker_patch, methods=["PATCH"]),
            Route("/api/v1/workers/{worker_id}", worker_delete, methods=["DELETE"]),
            Route("/api/v1/workers/connect", connect_worker, methods=["POST"]),
            Route("/api/v1/workers", workers_get, methods=["GET"]),
            Route("/api/v1/farm/status", farm_status_get, methods=["GET"]),
            Route("/api/v1/farm/access", farm_access_get, methods=["GET"]),
            Route("/api/v1/farm/operations", farm_operations_get, methods=["GET"]),
            Route("/api/v1/farm/jobs", farm_jobs_get, methods=["GET"]),
            Route("/api/v1/farm/jobs", farm_job_submit, methods=["POST"]),
            Route("/api/v1/farm/jobs/{job_id}", farm_job_get, methods=["GET"]),
            Route("/api/v1/farm/jobs/{job_id}/stream", farm_job_stream, methods=["GET"]),
            Route("/api/v1/farm/jobs/{job_id}/actions", farm_job_action, methods=["POST"]),
            Route("/api/v1/farm/jobs/{job_id}/frames/{frame:int}/download", farm_job_download, methods=["GET"]),
            Route("/api/v1/farm/gpus", farm_gpus_get, methods=["GET"]),
            Route("/api/v1/farm/settings", farm_settings_get, methods=["GET"]),
            Route("/api/v1/farm/settings", farm_settings_patch, methods=["PATCH"]),
            Route("/api/v1/monitoring/gpu", monitoring_gpu_get, methods=["GET"]),
            Route("/api/v1/gallery/latest", latest_get, methods=["GET"]),
            Route("/api/v1/gallery/browse", browse_get, methods=["GET"]),
            Route("/api/v1/gallery/search", search_get, methods=["GET"]),
            Route("/api/v1/gallery/delete", gallery_delete, methods=["POST"]),
            Route("/api/v1/gallery/bulk-delete", gallery_bulk_delete, methods=["POST"]),
            Route("/api/v1/gallery/bulk-move", gallery_bulk_move, methods=["POST"]),
            Route("/api/v1/gallery/trash", gallery_trash, methods=["GET"]),
            Route("/api/v1/gallery/trash/delete", gallery_trash_delete, methods=["POST"]),
            Route("/api/v1/gallery/restore", gallery_restore, methods=["POST"]),
            Route("/api/v1/gallery/file", workspace_file_get, methods=["GET"]),
            Route("/api/v1/gallery/projects", projects_get, methods=["GET"]),
            Route("/api/v1/gallery/projects", projects_post, methods=["POST"]),
            Route("/api/v1/gallery/projects/{project_id}/thumbnail", project_thumbnail_get, methods=["GET"]),
            Route("/api/v1/gallery/projects/{project_id}/assets", assets_get, methods=["GET"]),
            Route("/api/v1/gallery/projects/{project_id}/assets/{asset_path:path}", asset_get, methods=["GET"]),
        ]
    )
    # Idempotent: the telemetry sampler lives with the API process, not the route table.
    start_sampler()
