from pathlib import Path
"""Routing regression coverage: root static files + SPA fallback vs API auth."""

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from blender_mcp import server as server_mod
from blender_mcp.server import _ApiKeyMiddleware


def _req(path: str, method: str = "GET", accept: str = "*/*") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"accept", accept.encode())],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
    }
    return Request(scope)


def test_public_dashboard_request_allows_static_and_spa(tmp_path, monkeypatch):
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path / "index.html").write_text("<html>app</html>")
    monkeypatch.setattr(server_mod, "_dashboard_dist", tmp_path)

    assert _ApiKeyMiddleware._public_dashboard_request(_req("/favicon.svg", accept="image/svg+xml")) is True
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/scene", accept="text/html")) is True
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/brand-new-route", accept="text/html")) is True
    # BLENDER-012: diagnostics is no longer a public route.
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/api/v1/diagnostics", accept="text/html")) is False
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/api/v1/logs", accept="text/html")) is False
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/mcp", accept="text/html")) is False
    assert _ApiKeyMiddleware._public_dashboard_request(_req("/metrics", accept="text/html")) is False


def test_static_or_spa_serves_file_then_index(tmp_path, monkeypatch):
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path / "index.html").write_text("<html>app</html>")
    monkeypatch.setattr(server_mod, "_dashboard_dist", tmp_path)

    endpoint = getattr(server_mod, "_static_or_spa", None)
    if endpoint is None:
        pytest.skip("dist not present at import time")
    assert callable(endpoint)

    app = Starlette(
        routes=[
            Route("/api/private", lambda request: PlainTextResponse("private")),
            Route("/{full_path:path}", endpoint, methods=["GET", "HEAD"]),
        ],
        middleware=[Middleware(_ApiKeyMiddleware)],
    )
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "secret")
    with TestClient(app) as client:
        favicon = client.get("/favicon.svg", headers={"accept": "image/svg+xml"})
        spa = client.get("/brand-new-route", headers={"accept": "text/html"})
        private = client.get("/api/private")

    assert favicon.status_code == 200
    assert favicon.text == "<svg/>"
    assert spa.status_code == 200
    assert "<html>app</html>" in spa.text
    assert private.status_code == 401


def test_dashboard_index_never_injects_key_for_control_plane_roles(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><head></head><body>app</body></html>")
    monkeypatch.setattr(server_mod, "_dashboard_dist", tmp_path)
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "sekret")
    monkeypatch.setenv("MCP_ROLE", "stand-alone")
    resp = server_mod._dashboard_index()
    # BLENDER-001: the API key is never embedded in the served page.
    body = bytes(resp.body).decode() if isinstance(resp, HTMLResponse) else Path(resp.path).read_text()
    assert "window.__BLENDER_MCP_API_KEY__" not in body
    assert "sekret" not in body


def test_dashboard_index_skips_injection_for_worker_role(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><head></head><body>app</body></html>")
    monkeypatch.setattr(server_mod, "_dashboard_dist", tmp_path)
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "sekret")
    monkeypatch.setenv("MCP_ROLE", "worker")
    resp = server_mod._dashboard_index()
    assert isinstance(resp, FileResponse)
