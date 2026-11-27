"""Regression coverage for dashboard static files and API authentication."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from blender_mcp.server import _ApiKeyMiddleware, _root_page


def test_dashboard_route_serves_index_without_opening_api(tmp_path, monkeypatch):
    from blender_mcp import server

    (tmp_path / "index.html").write_text("<html>dashboard app</html>")
    monkeypatch.setattr(server, "_dashboard_dist", tmp_path)
    app = Starlette(
        routes=[
            Route("/settings", _root_page, methods=["GET", "HEAD"]),
            Route("/api/private", lambda request: PlainTextResponse("private")),
        ],
        middleware=[Middleware(_ApiKeyMiddleware)],
    )
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "secret")

    with TestClient(app) as client:
        settings = client.get("/settings", headers={"accept": "text/html"})
        private = client.get("/api/private", headers={"accept": "text/html"})

    assert settings.status_code == 200
    assert "<html>dashboard app</html>" in settings.text
    assert private.status_code == 401


def test_static_asset_is_public_but_unknown_api_stays_protected(tmp_path, monkeypatch):
    (tmp_path / "app.js").write_text("dashboard bundle")
    app = Starlette(
        routes=[
            Mount("/assets", StaticFiles(directory=tmp_path), name="assets"),
            Route("/api/private", lambda request: PlainTextResponse("private")),
        ],
        middleware=[Middleware(_ApiKeyMiddleware)],
    )
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "secret")

    with TestClient(app) as client:
        asset = client.get("/assets/app.js")
        private = client.get("/api/private")

    assert asset.status_code == 200
    assert asset.text == "dashboard bundle"
    assert private.status_code == 401


def test_unconfigured_api_key_still_blocks_private_paths(tmp_path, monkeypatch):
    app = Starlette(
        routes=[Route("/api/private", lambda request: JSONResponse({"ok": True}))],
        middleware=[Middleware(_ApiKeyMiddleware)],
    )
    monkeypatch.delenv("BLENDER_MCP_API_KEY", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/private")
    assert response.status_code == 503
