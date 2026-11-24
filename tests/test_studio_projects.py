from __future__ import annotations

import asyncio
import os

import pytest
from starlette.requests import Request

from blender_mcp.utils import projects
from blender_mcp.utils.studio_api import _safe_worker_data, asset_get, connect_worker


def test_worker_response_redacts_secret_and_sensitive_fields():
    payload = {
        "api_key": "secret-value",
        "message": "credential was secret-value",
        "nested": {"token": "secret-value", "online": True},
    }
    assert _safe_worker_data(payload, "secret-value") == {
        "message": "credential was [redacted]",
        "nested": {"online": True},
    }


def test_create_list_and_filter_project_assets(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("My Project")
    folder = tmp_path / project["path"]
    (folder / "still.PNG").write_bytes(b"png")
    (folder / "clip.mp4").write_bytes(b"mp4")
    (folder / "renders" / "job-1").mkdir(parents=True)
    (folder / "renders" / "job-1" / "frame_000001.png").write_bytes(b"render")
    (folder / "notes.txt").write_text("ignore")
    assert project["id"] in [p["id"] for p in projects.list_projects()]
    assert {a["name"] for a in projects.list_assets(project["id"])} == {"still.PNG", "clip.mp4", "renders/job-1/frame_000001.png"}
    media, content_type = projects.resolve_asset(project["id"], "clip.mp4")
    assert media.read_bytes() == b"mp4" and content_type == "video/mp4"


def test_reject_project_traversal_and_symlink_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError):
        projects.safe_project(tmp_path, "../outside")
    project = projects.create_project("Safe")
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"secret")
    (tmp_path / project["path"] / "escape.mp4").symlink_to(outside)
    assert all(a["name"] != "escape.mp4" for a in projects.list_assets(project["id"]))
    with pytest.raises(FileNotFoundError):
        projects.resolve_asset(project["id"], "escape.mp4")


def _request(path_params=None, body=b""):
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http", "http_version": "1.1", "path_params": path_params or {}}, receive=lambda: None)


def test_png_media_response_is_inline_and_route_requires_authenticated_middleware(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("Preview")
    (tmp_path / project["path"] / "frame.png").write_bytes(b"png")
    request = _request({"project_id": project["id"], "asset_path": "frame.png"})
    response = asyncio.run(asset_get(request))
    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"] == "inline"


def test_nested_asset_and_symlink_path_are_confined(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("Nested")
    root = tmp_path / project["path"]
    (root / "renders" / "job").mkdir(parents=True)
    (root / "renders" / "job" / "frame.png").write_bytes(b"png")
    special = root / "renders" / "job" / "space #?% ü.png"
    special.write_bytes(b"special")
    asset = next(a for a in projects.list_assets(project["id"]) if a["name"].endswith("space #?% ü.png"))
    assert "%20" in asset["url"] and "%23" in asset["url"] and "%3F" in asset["url"] and "%25" in asset["url"]
    assert projects.open_asset(project["id"], "renders/job/space #?% ü.png")[0] >= 0
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"outside")
    (root / "linked").symlink_to(outside)
    assert projects.resolve_asset(project["id"], "renders/job/frame.png")[0].read_bytes() == b"png"
    with pytest.raises(ValueError):
        projects.resolve_asset(project["id"], "../outside.png")
    (root / "linked.png").symlink_to(outside)
    with pytest.raises(FileNotFoundError):
        projects.resolve_asset(project["id"], "linked.png")
    with pytest.raises(FileNotFoundError):
        projects.open_asset(project["id"], "linked.png")


@pytest.mark.parametrize("project_id", ["..", "../outside", "nested/project"])
def test_open_asset_rejects_invalid_project_ids_before_traversal(monkeypatch, tmp_path, project_id):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError, match="invalid project id"):
        projects.open_asset(project_id, "frame.png")


def test_asset_response_stream_closes_descriptor(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("Streaming")
    (tmp_path / project["path"] / "frame.png").write_bytes(b"image bytes")
    original_open_asset = projects.open_asset
    opened = []
    def track_open(*args):
        result = original_open_asset(*args)
        opened.append(result[0])
        return result
    monkeypatch.setattr("blender_mcp.utils.studio_api.open_asset", track_open)
    response = asyncio.run(asset_get(_request({"project_id": project["id"], "asset_path": "frame.png"})))
    async def consume():
        body = b"".join([chunk async for chunk in response.body_iterator])
        await response.background()
        return body
    assert asyncio.run(consume()) == b"image bytes"
    with pytest.raises(OSError):
        os.fstat(opened[-1])


def test_worker_destination_rejects_loopback_and_dns_hosts():
    from blender_mcp.utils.farm_workers import validate_worker_destination

    for url in ("http://127.0.0.1:10849/mcp", "http://localhost:10849/mcp"):
        with pytest.raises(ValueError):
            validate_worker_destination(url)

    validate_worker_destination("http://10.0.0.12:10849/mcp")


def test_worker_connect_rejects_invalid_endpoint_without_calling_worker():
    async def receive():
        return {"type": "http.request", "body": b'{"url":"https://user:pass@worker/mcp","api_key":"do-not-log"}', "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/api/v1/workers/connect", "headers": [(b"content-type", b"application/json")], "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http", "http_version": "1.1", "path_params": {}}
    response = asyncio.run(connect_worker(Request(scope, receive)))
    assert response.status_code == 400
    assert b"do-not-log" not in response.body
