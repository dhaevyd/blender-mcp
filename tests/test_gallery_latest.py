"""Latest-media endpoint: newest workspace file + auth requirement."""

from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils import projects
from blender_mcp.utils.studio_api import latest_get


def test_latest_media_none_when_no_media(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "scene.blend").write_bytes(b"blend")
    assert projects.latest_media() is None


def test_latest_media_returns_newest_across_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("Scene")
    proj_dir = tmp_path / project["path"]
    (proj_dir / "old.png").write_bytes(b"old")
    os.utime(proj_dir / "old.png", (1000.0, 1000.0))
    folder = tmp_path / "Render Output"
    folder.mkdir()
    (folder / "frame.png").write_bytes(b"frame")
    os.utime(folder / "frame.png", (2000.0, 2000.0))
    (tmp_path / "loose.mp4").write_bytes(b"loose")
    os.utime(tmp_path / "loose.mp4", (3000.0, 3000.0))

    latest = projects.latest_media()
    assert latest is not None
    assert latest["name"] == "loose.mp4"
    assert latest["type"] == "video"
    assert latest["project_id"] == "workspace-root"
    assert latest["url"].startswith("/api/v1/gallery/projects/workspace-root/assets/loose.mp4")
    assert latest["modified"]


def test_latest_media_prefers_newest_project_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "loose.png").write_bytes(b"loose")
    os.utime(tmp_path / "loose.png", (1000.0, 1000.0))
    project = projects.create_project("Scene")
    nested = tmp_path / project["path"] / "renders" / "job"
    nested.mkdir(parents=True)
    (nested / "frame_0001.png").write_bytes(b"frame")
    os.utime(nested / "frame_0001.png", (2000.0, 2000.0))

    latest = projects.latest_media()
    assert latest is not None
    assert latest["name"] == "frame_0001.png"
    assert latest["type"] == "image"
    assert latest["project_id"] == project["id"]


def test_latest_media_revision_changes_when_same_path_is_overwritten(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    media = tmp_path / "preview.png"
    media.write_bytes(b"old")

    first = projects.latest_media()
    media.write_bytes(b"new contents")
    second = projects.latest_media()

    assert first is not None and second is not None
    assert first["url"] == second["url"]
    assert first["revision"] != second["revision"]


def test_latest_media_skips_symlinks(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    outside = tmp_path.parent / f"outside-{tmp_path.name}.png"
    outside.write_bytes(b"secret")
    os.utime(outside, (9999.0, 9999.0))
    (tmp_path / "link.png").symlink_to(outside)
    (tmp_path / "real.png").write_bytes(b"real")
    os.utime(tmp_path / "real.png", (500.0, 500.0))

    latest = projects.latest_media()
    assert latest is not None
    assert latest["name"] == "real.png"


def test_latest_route_requires_auth_and_returns_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "frame.png").write_bytes(b"png")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "sekret")
    app = Starlette(
        routes=[Route("/api/v1/gallery/latest", latest_get, methods=["GET"])],
        middleware=[Middleware(_ApiKeyMiddleware)],
    )
    with TestClient(app) as client:
        unauth = client.get("/api/v1/gallery/latest")
        auth = client.get("/api/v1/gallery/latest", headers={"Authorization": "Bearer sekret"})
    assert unauth.status_code == 401
    assert auth.status_code == 200
    body = auth.json()
    assert body["file"]["name"] == "frame.png"
    assert body["file"]["type"] == "image"
    assert body["file"]["project_id"] == "workspace-root"
    assert body["file"]["url"].startswith("/api/v1/gallery/projects/workspace-root/assets/frame.png")
    assert body["file"]["modified"]



