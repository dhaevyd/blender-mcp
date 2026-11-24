# Blender MCP Render Farm

`blender_farm` coordinates frame renders through authenticated Blender MCP worker servers. `blender_farm_worker` is exposed by every server that can act as a worker. A coordinator does not start workers or alter deployment configuration; provision each worker separately and approve deployment changes through the normal process.

## Server role (`MCP_ROLE`)

Set `MCP_ROLE` to control which farm tool set a server exposes. Valid values:

- `master` — coordinator. Exposes only `blender_farm`.
- `worker` — render worker. Exposes only `blender_farm_worker`.
- `stand-alone` — default when `MCP_ROLE` is empty or unset. Exposes both `blender_farm` and `blender_farm_worker`. `standalone` and `stand_alone` are accepted aliases, and any unrecognised value logs a warning and falls back to `stand-alone`.

The same value is reported as the `role` field in `/api/v1/health` and in the worker identity payload.

Set the inbound bearer key by role: `master` and `stand-alone` use `BLENDER_MCP_API_KEY`; `worker` uses `BLENDER_MCP_WORKER_KEY` and never falls back to the master key. Keep the worker key out of dashboard/browser configuration; worker role does not serve dashboard, login, static asset, or viewer UI routes. Health and diagnostics remain available.

## Configure workers

Set `BLENDER_FARM_WORKERS` on the coordinator to a JSON array. Each entry provides a unique ID, an MCP URL, the name of an environment variable holding that worker's bearer key, and its workspace path:

```json
[
  {"id":"render-a","url":"https://render-a.example/mcp","api_key_env":"FARM_RENDER_A_KEY","workspace":"/workspace"},
  {"id":"render-b","url":"http://10.0.0.12:10849/mcp","api_key_env":"FARM_RENDER_B_KEY","workspace":"/workspace"}
]
```

HTTP is accepted only for private/loopback hosts; use HTTPS otherwise. Never put API keys in the JSON configuration or URLs. Worker key environment variables must be set separately: worker inbound key is `BLENDER_MCP_WORKER_KEY`, and the coordinator-side variable named by `api_key_env` must contain the same bearer secret. An empty/unset `BLENDER_FARM_WORKERS` defaults to `[]`, and submissions fail clearly until at least one worker is configured. `BLENDER_FARM_DB_PATH` optionally selects the coordinator SQLite ledger; by default it is stored under `.blender-farm/farm.sqlite3` in `BLENDER_WORKSPACE`.

## Project bundles and outputs

Submit an existing `.blend` file under the coordinator workspace, or a ZIP bundle containing exactly one `.blend` file and its relative-path assets. ZIP paths are checked against traversal; compressed uploads are limited to 64 MiB and expanded data to 512 MiB. A lone `.blend` file is wrapped and transferred, but external files are not included—pack external assets into a ZIP bundle. Each worker validates the bundle SHA-256 before extraction. The coordinator verifies output SHA-256 before writing PNG frames under `farm-output/<job-id>` (or a caller-selected in-workspace directory). Per-file worker transfers are limited to 64 MiB.

## MCP operations

- `blender_farm(operation="status")`: coordinator, worker health/identity, and recent jobs.
- `workers`: worker health and capabilities.
- `submit`: provide `project_path`, inclusive `frame_start`/`frame_end`, optional `worker_ids`, and optional `output_directory`.
- `job_status` / `list_jobs`: durable progress, per-frame status, worker, attempts, log and error, and missing frames.
- `pause`, `resume`, `cancel`, `retry_failed`, `missing_frames`, and `outputs`: manage and inspect jobs.

Frames are scheduled across workers' NVIDIA, AMD, and Intel GPUs. Blender uses OPTIX/CUDA for NVIDIA, HIP for AMD, and ONEAPI for Intel. On Linux, workers report NVIDIA memory through `nvidia-smi` and AMD/Intel memory through DRM sysfs. Integrated GPUs use shared GTT/system memory; the coordinator caps free capacity by host-available RAM and serializes shared-memory GPU renders on each worker. Dedicated VRAM is never pooled, and a frame stays on one GPU. Before dispatch, the coordinator calibrates frame memory demand on an eligible GPU, then requires enough free device memory plus a safety margin. A device must have at least `MCP_FARM_GPU_MIN_FREE_MIB` (default 1024 MiB) and 10% of its reported capacity free. This threshold does not predict scene memory exactly.

The enrollment form asks for the worker's GPU vendor and generates a matching Compose device request. Auto-detect supplies NVIDIA, AMD, and Intel Compose variants plus a Linux setup script; the script checks the worker host and selects NVIDIA first, then AMD, then Intel. NVIDIA needs the NVIDIA Container Toolkit. AMD needs ROCm on the Linux host, `/dev/kfd`, `/dev/dri`, and the host ROCm runtime at `/opt/rocm`. Intel needs `/dev/dri` and a compatible Intel Level Zero runtime. The generated Compose configs use workspace bind mounts and add the worker to the host video/render groups; set `VIDEO_GID` and `RENDER_GID` in `.env` if the host uses different IDs. Blender must include the selected Cycles backend, and the host driver must support that GPU. Running Blender calls cannot be forcibly interrupted by coordinator cancellation; cancellation prevents new dispatch and marks unfinished ledger frames cancelled, while an already-running worker call may finish. Coordinator startup pauses interrupted jobs and requeues in-flight frames; resume to continue them. Failed frames can be retried. SQLite state records progress and frame logs across coordinator restarts.

This implementation requires worker MCP URLs and bearer keys, a reachable Blender executable on each worker, and deployment approval before applying changes to a live service. No live workers are enrolled by default.
