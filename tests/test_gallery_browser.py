"""Read-only workspace browser contract and authenticated file serving."""

import os

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils.projects import browse_workspace, open_workspace_file
from blender_mcp.utils.studio_api import register_gallery_routes


@pytest.fixture(autouse=True)
def isolated_gallery_index(tmp_path, monkeypatch):
    """Keep the first-seen "date added" index out of the shared temp dir."""
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(tmp_path.parent / f"{tmp_path.name}-gallery-index.json"))


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "Empty").mkdir()
    (tmp_path / "Nested").mkdir()
    (tmp_path / "Nested" / "inner.py").write_text("print('hello')")
    for name in ("scene.blend", "still.png", "clip.mp4", "photo.jpg", "notes.log"):
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "secret.txt").write_text("secret")
    (tmp_path / ".env").write_text("secret")
    return tmp_path


def test_browser_lists_distinct_root_entries_and_empty_directories(workspace):
    entries = browse_workspace("")
    assert {"Empty", "Nested", "scene.blend", "still.png", "clip.mp4", "photo.jpg", "notes.log"} <= {
        entry["name"] for entry in entries
    }
    assert browse_workspace("Empty") == []
    assert [entry["path"] for entry in browse_workspace("Nested")] == ["Nested/inner.py"]
    assert all("size" in entry and "modified" in entry and "type" in entry for entry in entries)


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../secret",
        "Nested/../notes.log",
        "./notes.log",
        "Nested//inner.py",
        "Nested/.",
        "\\secret",
        "notes\x00.log",
        "notes\n.log",
        "notes\x85.log",
        ".private",
        ".env",
        "Nested/.hidden",
    ],
)
def test_browser_rejects_bad_paths(workspace, bad):
    with pytest.raises(ValueError):
        browse_workspace(bad)
    with pytest.raises(ValueError):
        open_workspace_file(bad)


def test_browser_never_follows_symlinks(workspace):
    (workspace / "linked").symlink_to(workspace / "Nested", target_is_directory=True)
    (workspace / "Nested" / "link.py").symlink_to(workspace / "Nested" / "inner.py")
    assert "linked" not in {item["name"] for item in browse_workspace("")}
    assert "link.py" not in {item["name"] for item in browse_workspace("Nested")}
    with pytest.raises(FileNotFoundError):
        browse_workspace("linked")
    with pytest.raises(FileNotFoundError):
        open_workspace_file("Nested/link.py")


def test_file_descriptor_returns_bytes_without_following_symlinks(workspace):
    fd, info, media = open_workspace_file("Nested/inner.py")
    try:
        assert os.read(fd, 100) == b"print('hello')"
        assert info.st_size == len(b"print('hello')")
        assert media == "text/plain"
    finally:
        os.close(fd)
    with pytest.raises(FileNotFoundError):
        open_workspace_file("Empty")


def test_authenticated_browse_file_range_and_text_cap(workspace, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-token")
    (workspace / "large.txt").write_bytes(b"a" * 65537)
    (workspace / "unsafe.svg").write_text("<svg onload='alert(1)'/>")
    (workspace / "linked").symlink_to(workspace / "Nested", target_is_directory=True)
    app = Starlette(middleware=[Middleware(_ApiKeyMiddleware)])
    register_gallery_routes(app)
    with TestClient(app) as client:
        for endpoint in ("browse", "file"):
            assert client.get(f"/api/v1/gallery/{endpoint}?path=notes.log").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        listing = client.get("/api/v1/gallery/browse", headers=headers)
        assert listing.status_code == 200
        assert "notes.log" in {item["name"] for item in listing.json()["entries"]}
        assert ".private" not in {item["name"] for item in listing.json()["entries"]}
        assert "linked" not in {item["name"] for item in listing.json()["entries"]}
        file = client.get("/api/v1/gallery/file?path=Nested%2Finner.py", headers=headers)
        assert file.status_code == 200 and file.content == b"print('hello')"
        assert file.headers["content-disposition"].startswith("inline")
        assert file.headers["x-content-type-options"] == "nosniff"
        binary = client.get("/api/v1/gallery/file?path=scene.blend", headers=headers)
        assert binary.status_code == 200 and binary.content == b"scene.blend"
        assert binary.headers["content-disposition"].startswith("attachment")
        ranged = client.get("/api/v1/gallery/file?path=clip.mp4", headers={**headers, "Range": "bytes=1-3"})
        assert ranged.status_code == 206 and ranged.content == b"lip"
        assert ranged.headers["content-range"] == "bytes 1-3/8"
        invalid_range = client.get("/api/v1/gallery/file?path=clip.mp4", headers={**headers, "Range": "bytes=99-100"})
        assert invalid_range.status_code == 416
        assert invalid_range.headers["content-range"] == "bytes */8"
        svg = client.get("/api/v1/gallery/file?path=unsafe.svg", headers=headers)
        assert svg.headers["content-disposition"].startswith("attachment")
        assert svg.headers["content-type"].startswith("application/octet-stream")
        assert client.get("/api/v1/gallery/file?path=large.txt&preview=1", headers=headers).status_code == 413
        assert client.get("/api/v1/gallery/file?path=large.txt", headers=headers).status_code == 200
        assert client.get("/api/v1/gallery/file?path=.env", headers=headers).status_code == 400
        assert client.get("/api/v1/gallery/file?path=linked%2Finner.py", headers=headers).status_code == 404
        assert client.get("/api/v1/gallery/browse?path=..%2F", headers=headers).status_code == 400
        assert client.get("/api/v1/gallery/browse?path=Empty", headers=headers).json()["entries"] == []
