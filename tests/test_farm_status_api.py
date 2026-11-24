"""Authenticated structured farm snapshots for the dashboard."""

from starlette.testclient import TestClient

from blender_mcp.server import asgi_app
from blender_mcp.tools import farm_tools
from blender_mcp.utils.farm_coordinator import BlenderFarmCoordinator
from blender_mcp.utils.farm_workers import WorkerConfig


class _Worker:
    async def list_tools(self):
        return [{"name": "blender_farm_worker"}]

    async def call_tool(self, name, arguments):
        assert name == "blender_farm_worker" and arguments == {"operation": "identity"}
        return {"online": True, "hostname": "render-node", "api_key": "worker-secret"}


def test_empty_farm_snapshot_requires_auth_and_returns_json(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")
    farm = BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    with TestClient(asgi_app) as client:
        unauthenticated = client.get("/api/v1/farm/status")
        rejected = client.get("/api/v1/farm/status", headers={"Authorization": "Bearer wrong-key"})
        authenticated = client.get("/api/v1/farm/status", headers={"Authorization": "Bearer dashboard-secret"})
    assert unauthenticated.status_code == 401
    assert rejected.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.headers["cache-control"] == "private, no-store"
    assert authenticated.headers["content-type"].startswith("application/json")
    payload = authenticated.json()
    assert payload["success"] is True
    assert payload["coordinator"]["worker_count"] == 0
    assert payload["workers"] == payload["active_jobs"] == payload["jobs"] == []
    assert "dashboard-secret" not in authenticated.text


def test_farm_snapshot_contains_structured_worker_and_job(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")
    worker = WorkerConfig("gpu-1", "http://127.0.0.1:10849/mcp", "worker-secret")
    farm = BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [worker], client_factory=lambda _: _Worker())
    farm.store.create_job("job-1", "scene.blend", 1, 4)
    farm.store.mark_frame_started("job-1", 1, "gpu-1")
    farm.store.finish_frame("job-1", 1, success=True, output_path="frame_000001.png")
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    with TestClient(asgi_app) as client:
        response = client.get("/api/v1/farm/status", headers={"Authorization": "Bearer dashboard-secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["coordinator"]["worker_count"] == 1
    assert payload["workers"][0]["worker_id"] == "gpu-1"
    assert payload["workers"][0]["online"] is True
    assert payload["workers"][0]["identity"]["hostname"] == "render-node"
    assert payload["jobs"][0]["id"] == "job-1"
    assert payload["jobs"][0]["status"] == "running"
    assert payload["active_jobs"][0]["id"] == "job-1"
    assert "worker-secret" not in response.text


def test_dashboard_can_submit_a_farm_job_and_audit_it(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")
    farm = BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])

    async def submit_render(project_path, frame_start, frame_end, **kwargs):
        return {"success": True, "job_id": "job-submit-1", "status": "running"}

    monkeypatch.setattr(farm, "submit_render", submit_render)
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    with TestClient(asgi_app) as client:
        response = client.post(
            "/api/v1/farm/jobs",
            headers={"Authorization": "Bearer dashboard-secret"},
            json={"project_path": "projects/shot.blend", "frame_start": 3, "frame_end": 7},
        )
    assert response.status_code == 202
    assert response.json()["job_id"] == "job-submit-1"
    assert farm.store.recent_audit()[0]["action"] == "job.submit"


def test_dashboard_job_submission_rejects_invalid_frame_ranges(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")
    with TestClient(asgi_app) as client:
        response = client.post(
            "/api/v1/farm/jobs",
            headers={"Authorization": "Bearer dashboard-secret"},
            json={"project_path": "projects/shot.blend", "frame_start": True, "frame_end": 1},
        )
    assert response.status_code == 400


def test_farm_snapshot_redacts_configured_worker_key_in_error_without_losing_safe_details(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")
    worker = WorkerConfig("gpu-1", "http://127.0.0.1:10849/mcp", "worker-secret")
    second = WorkerConfig("gpu-2", "http://127.0.0.1:10850/mcp", "worker-secret-long")
    farm = BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [worker, second])

    async def worker_status():
        return [
            {
                "worker_id": "gpu-1",
                "online": False,
                "error": "request with worker-secret and worker-secret-long timed out",
            },
            {"worker_id": "safe", "online": False, "error": "worker capability tool is missing"},
        ]

    monkeypatch.setattr(farm, "worker_status", worker_status)
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    with TestClient(asgi_app) as client:
        response = client.get("/api/v1/farm/status", headers={"Authorization": "Bearer dashboard-secret"})
    assert response.status_code == 200
    assert "worker-secret" not in response.text
    assert "worker-secret" not in caplog.text
    assert response.json()["workers"][0]["error"] == "request with [redacted] and [redacted] timed out"
    assert response.json()["workers"][1]["error"] == "worker capability tool is missing"


def test_farm_status_failure_does_not_leak_exception_or_key(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dashboard-secret")

    class BrokenFarm:
        async def farm_status(self):
            raise RuntimeError("dashboard-secret private failure")

    monkeypatch.setattr(farm_tools, "_FARM", BrokenFarm())
    with TestClient(asgi_app) as client:
        response = client.get("/api/v1/farm/status", headers={"Authorization": "Bearer dashboard-secret"})
    assert response.status_code == 500
    assert response.json()["success"] is False
    assert "dashboard-secret" not in response.text
    assert "private failure" not in response.text
