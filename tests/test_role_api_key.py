"""Role-specific inbound API key behavior."""
from blender_mcp import server


def test_operator_can_submit_and_control_jobs_but_viewer_cannot(monkeypatch):
    from blender_mcp.utils.studio_access import scoped_access

    submit = "/api/v1/farm/jobs"
    action = "/api/v1/farm/jobs/job-123/actions"
    assert scoped_access("operator", "POST", submit)
    assert scoped_access("operator", "POST", action)
    assert not scoped_access("viewer", "POST", submit)
    assert not scoped_access("viewer", "POST", action)
    assert not scoped_access("operator", "DELETE", submit)


def test_worker_uses_exact_worker_key_name_and_does_not_fallback(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "worker-secret")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "master-secret")
    assert server._api_key_env_name() == "BLENDER_MCP_WORKER_KEY"
    assert server._api_key() == "worker-secret"
    monkeypatch.delenv("BLENDER_MCP_WORKER_KEY")
    assert server._api_key() == ""


def test_master_and_standalone_use_master_key(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "master-secret")
    for role in ("master", "stand-alone", ""):
        monkeypatch.setenv("MCP_ROLE", role)
        assert server._api_key_env_name() == "BLENDER_MCP_API_KEY"
        assert server._api_key() == "master-secret"


def test_dashboard_signing_uses_selected_role_key(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("MCP_USERNAME", "user")
    monkeypatch.setenv("MCP_PASSWORD", "password")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "worker-secret")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "master-secret")
    worker_signing_key = server._dashboard_signing_key()
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "other-secret")
    assert server._dashboard_signing_key() != worker_signing_key


def test_worker_dashboard_does_not_inject_worker_key(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "worker-secret")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "master-secret")
    monkeypatch.setattr(server, "_dashboard_dist", __import__("pathlib").Path("tests/fixtures"))
    response = server._dashboard_index()
    assert "worker-secret" not in getattr(response, "body", b"").decode(errors="ignore")


def test_worker_dashboard_surface_is_not_served(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "worker-secret")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "master-secret")
    client = TestClient(server.asgi_app)
    for path in (
        "/",
        "/settings",
        "/assets/app-deadbeef.js",
        "/login",
        "/viewer",
        "/api/v1/dashboard/session",
        "/api/v1/viewer/state",
    ):
        assert client.get(path).status_code == 404
    assert client.get("/api/v1/health").status_code == 200


def test_worker_key_authorizes_mcp_api_without_master_key(monkeypatch):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "worker-secret")
    monkeypatch.delenv("BLENDER_MCP_API_KEY", raising=False)
    app = Starlette(
        routes=[Route("/api/private", lambda request: PlainTextResponse("ok"))],
        middleware=[Middleware(server._ApiKeyMiddleware)],
    )

    with TestClient(app) as client:
        worker_response = client.get("/api/private", headers={"authorization": "Bearer worker-secret"})
        master_response = client.get("/api/private", headers={"authorization": "Bearer master-secret"})

    assert worker_response.status_code == 200
    assert master_response.status_code == 401


def test_registered_worker_routes_require_role_selected_key(monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient

    from blender_mcp.tools import farm_tools
    from blender_mcp.utils.studio_api import register_gallery_routes

    monkeypatch.setenv("MCP_ROLE", "worker")
    monkeypatch.setenv("BLENDER_MCP_WORKER_KEY", "dummy-worker-key")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "dummy-master-key")
    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    farm.store.save_managed_worker({"worker_id": "w", "name": "worker", "url": "http://10.0.0.1:10849/mcp", "workspace": "/workspace", "slots": 1, "enabled": False}, "dummy-key")
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    app = Starlette(middleware=[Middleware(server._ApiKeyMiddleware)])
    register_gallery_routes(app)
    with TestClient(app) as client:
        good = client.get("/api/v1/workers", headers={"authorization": "Bearer dummy-worker-key"})
        wrong = client.get("/api/v1/workers", headers={"authorization": "Bearer dummy-master-key"})
        missing = client.get("/api/v1/workers")
    assert good.status_code == 200
    assert wrong.status_code == missing.status_code == 401
