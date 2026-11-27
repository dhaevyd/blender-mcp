from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import zipfile

import pytest

from blender_mcp.utils.farm_coordinator import BlenderFarmCoordinator
from blender_mcp.utils.farm_workers import WorkerConfig


def test_intel_shared_memory_gpu_is_schedulable():
    assert BlenderFarmCoordinator._usable_gpu(
        {
            "index": 0,
            "name": "Intel Iris Xe",
            "backend": "INTEL",
            "memory_type": "shared",
            "memory_total_mib": 8192,
            "memory_free_mib": 4096,
        }
    )


class FakeWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.files: dict[str, bytes] = {}
        self.rendered: list[int] = []
        self.rendered_gpu: list[tuple[int, int, str]] = []
        self.gpu_inventory = [{
            "index": 0,
            "name": "Fake GPU",
            "backend": "NVIDIA",
            "memory_total_mib": 8192,
            "memory_used_mib": 0,
            "memory_free_mib": 8192,
        }]

    async def list_tools(self):
        return [{"name": "blender_farm_worker"}]

    async def call_tool(self, name, arguments, *, timeout=1800):
        operation = arguments["operation"]
        if operation == "identity":
            return {
                "success": True,
                "worker_id": self.config.worker_id,
                "online": True,
                "gpu_stats": self.gpu_inventory,
            }
        if operation == "sync_project":
            self.bundle_entries = zipfile.ZipFile(io.BytesIO(base64.b64decode(arguments["bundle_base64"]))).namelist()
            return {"success": True, "project_path": f"farm-projects/{arguments['project_id']}/scene.blend", "bundle_sha256": arguments["bundle_sha256"]}
        if operation == "render_frame":
            await asyncio.sleep(0.001)
            frame = arguments["frame"]
            self.rendered.append(frame)
            self.rendered_gpu.append((frame, arguments["gpu_index"], arguments["gpu_name"]))
            path = arguments["output_path"]
            self.files[path] = f"png-frame-{frame}".encode()
            return {
                "success": True,
                "output_path": path,
                "frame": frame,
                "log": f"rendered {frame}",
                "gpu_memory_delta_mib": 2048,
            }
        if operation == "collect_output":
            data = self.files[arguments["file_path"]]
            return {"success": True, "content_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
        if operation in {"files_delete", "files_list", "files_write", "validate_project", "list_frames"}:
            return {"success": True}
        raise AssertionError(operation)


@pytest.mark.asyncio
async def test_worker_status_redacts_credentials_from_identity_and_failures(tmp_path):
    worker = WorkerConfig("gpu-secure", "https://worker.example/mcp", "secret-key")

    class LeakyWorker:
        async def list_tools(self):
            return [{"name": "blender_farm_worker"}]

        async def call_tool(self, *_args, **_kwargs):
            return {"online": True, "api_key": "secret-key", "message": "got secret-key"}

    coordinator = BlenderFarmCoordinator(
        tmp_path, tmp_path / "farm.sqlite3", [worker], client_factory=lambda _config: LeakyWorker()
    )
    status = await coordinator.worker_status()
    assert status[0]["identity"] == {"online": True, "message": "got [redacted]"}
    assert "secret-key" not in str(status)


@pytest.mark.asyncio
async def test_coordinator_syncs_project_distributes_frames_and_collects_outputs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scene = workspace / "scene.blend"
    scene.write_bytes(b"blend-project")
    workers = [
        WorkerConfig("gpu-a", "http://127.0.0.1:10849/mcp", "secret-a"),
        WorkerConfig("gpu-b", "http://127.0.0.1:10850/mcp", "secret-b"),
    ]
    fake_workers = {worker.worker_id: FakeWorker(worker) for worker in workers}
    coordinator = BlenderFarmCoordinator(
        workspace,
        tmp_path / "farm.sqlite3",
        workers,
        client_factory=lambda config: fake_workers[config.worker_id],
    )

    submitted = await coordinator.submit_render("scene.blend", 1, 4)
    result = await coordinator.wait_for_job(submitted["job_id"], timeout=5)

    assert result["status"] == "completed"
    assert result["completed"] == 4
    assert result["frames"][0]["log"] == "rendered 1"
    assert sorted(fake_workers["gpu-a"].rendered + fake_workers["gpu-b"].rendered) == [1, 2, 3, 4]
    assert fake_workers["gpu-a"].rendered
    assert fake_workers["gpu-b"].rendered
    assert len(fake_workers["gpu-a"].rendered) + len(fake_workers["gpu-b"].rendered) == 4
    assert (workspace / "farm-output" / submitted["job_id"] / "frame_000001.png").read_bytes() == b"png-frame-1"


@pytest.mark.asyncio
async def test_coordinator_selects_the_largest_available_gpu_for_a_frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scene.blend").write_bytes(b"blend-project")
    worker = WorkerConfig("gpu-a", "http://127.0.0.1:10849/mcp", "secret-a")
    fake = FakeWorker(worker)
    fake.gpu_inventory = [
        {"index": 0, "name": "Small GPU", "backend": "NVIDIA", "memory_total_mib": 8192, "memory_used_mib": 5692, "memory_free_mib": 2500},
        {"index": 1, "name": "48 GB GPU", "backend": "NVIDIA", "memory_total_mib": 49152, "memory_used_mib": 43152, "memory_free_mib": 6000},
    ]
    coordinator = BlenderFarmCoordinator(workspace, tmp_path / "farm.sqlite3", [worker], client_factory=lambda _: fake)
    submitted = await coordinator.submit_render("scene.blend", 1, 2)
    result = await coordinator.wait_for_job(submitted["job_id"], timeout=5)
    assert result["status"] == "completed"
    assert fake.rendered_gpu == [(1, 1, "48 GB GPU"), (2, 1, "48 GB GPU")]


@pytest.mark.asyncio
async def test_coordinator_syncs_zip_bundle_including_external_assets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle_path = workspace / "scene-project.zip"
    with zipfile.ZipFile(bundle_path, "w") as archive:
        archive.writestr("scene/scene.blend", b"blend-project")
        archive.writestr("scene/textures/wood.png", b"texture-data")
    worker = WorkerConfig("gpu-a", "http://127.0.0.1:10849/mcp", "secret-a")
    fake = FakeWorker(worker)
    coordinator = BlenderFarmCoordinator(
        workspace,
        tmp_path / "farm.sqlite3",
        [worker],
        client_factory=lambda config: fake,
    )

    submitted = await coordinator.submit_render("scene-project.zip", 1, 1)
    result = await coordinator.wait_for_job(submitted["job_id"], timeout=5)

    assert result["status"] == "completed"
    assert fake.bundle_entries == ["scene/scene.blend", "scene/textures/wood.png"]


@pytest.mark.asyncio
async def test_project_render_output_is_confined_and_default_is_unchanged(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scene.blend").write_bytes(b"blend")
    gallery = workspace / "projects" / "sample-id"
    gallery.mkdir(parents=True)
    (gallery / ".project.json").write_text('{"name":"Sample"}')
    worker = WorkerConfig("gpu-a", "http://127.0.0.1:10849/mcp", "secret-a")
    fake = FakeWorker(worker)
    coordinator = BlenderFarmCoordinator(workspace, tmp_path / "db.sqlite", [worker], client_factory=lambda _: fake)
    project_job = await coordinator.submit_render("scene.blend", 1, 1, gallery_project_id="sample-id")
    await coordinator.wait_for_job(project_job["job_id"], timeout=5)
    assert project_job["output_directory"].startswith(str(gallery.resolve() / "renders"))
    assert (gallery / "renders" / project_job["job_id"] / "frame_000001.png").is_file()
    default_job = await coordinator.submit_render("scene.blend", 2, 2)
    await coordinator.wait_for_job(default_job["job_id"], timeout=5)
    assert default_job["output_directory"] == str(workspace / "farm-output" / default_job["job_id"])


@pytest.mark.asyncio
async def test_project_render_rejects_missing_or_unsafe_gallery_project(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scene.blend").write_bytes(b"blend")
    coordinator = BlenderFarmCoordinator(workspace, tmp_path / "db.sqlite", [])
    with pytest.raises(ValueError, match="invalid project id"):
        await coordinator.submit_render("scene.blend", 1, 1, gallery_project_id="../escape")
    with pytest.raises(ValueError, match="existing Gallery project"):
        await coordinator.submit_render("scene.blend", 1, 1, gallery_project_id="missing")


@pytest.mark.asyncio
async def test_project_render_rejects_preexisting_renders_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scene.blend").write_bytes(b"blend")
    gallery = workspace / "projects" / "sample-id"
    gallery.mkdir(parents=True)
    (gallery / ".project.json").write_text('{"name":"Sample"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (gallery / "renders").symlink_to(outside, target_is_directory=True)
    worker = WorkerConfig("gpu-a", "http://127.0.0.1:10849/mcp", "secret-a")
    coordinator = BlenderFarmCoordinator(workspace, tmp_path / "db.sqlite", [worker], client_factory=lambda _: FakeWorker(worker))
    with pytest.raises(ValueError, match="unsafe"):
        await coordinator.submit_render("scene.blend", 1, 1, gallery_project_id="sample-id")
