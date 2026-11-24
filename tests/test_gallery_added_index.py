"""First-seen "date added" index and the Gallery sort contract it feeds."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import blender_mcp.utils.projects as projects
from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils.projects import browse_workspace, search_workspace
from blender_mcp.utils.studio_api import register_gallery_routes


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(root))
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(tmp_path / "state" / "gallery-index.json"))
    (root / "FolderA").mkdir()
    return root


def _index_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "gallery-index.json"


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _write(path: Path, data: bytes, mtime: float) -> None:
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def _sort_by_added(entries: list[dict]) -> list[dict]:
    """Mirror the Gallery comparator: folders first, then newest first, ties by name."""
    return sorted(
        entries,
        key=lambda item: (
            item["type"] != "folder",
            -datetime.fromisoformat(item["added"] or item["modified"]).timestamp(),
            item["name"].casefold(),
            item["path"],
        ),
    )


def test_first_sight_seeds_from_mtime_and_persists(workspace, tmp_path):
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == _iso(1_700_000_000)
    assert shot["modified"] == _iso(1_700_000_000)

    index = _index_file(tmp_path)
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "files": {"shot.png": _iso(1_700_000_000)}}
    assert index.stat().st_mode & 0o777 == 0o600


def test_stored_added_survives_later_modification(workspace, tmp_path):
    target = workspace / "shot.png"
    _write(target, b"png", 1_700_000_000)
    browse_workspace("")

    os.utime(target, (1_800_000_000, 1_800_000_000))
    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == _iso(1_700_000_000)
    assert shot["modified"] == _iso(1_800_000_000)
    assert json.loads(_index_file(tmp_path).read_text(encoding="utf-8"))["files"] == {"shot.png": _iso(1_700_000_000)}


def test_added_timestamps_sort_newest_first_independent_of_modified(workspace, tmp_path):
    for name, mtime in (("old.png", 1_600_000_000), ("middle.png", 1_650_000_000), ("new.png", 1_700_000_000)):
        _write(workspace / name, b"x", mtime)
    browse_workspace("")

    # Reverse the modified order afterwards; first-seen order must be preserved.
    os.utime(workspace / "old.png", (1_800_000_000, 1_800_000_000))
    os.utime(workspace / "new.png", (1_500_000_000, 1_500_000_000))

    ordered = _sort_by_added(browse_workspace(""))
    assert [entry["name"] for entry in ordered] == ["FolderA", "new.png", "middle.png", "old.png"]
    modified_order = sorted(ordered[1:], key=lambda item: -datetime.fromisoformat(item["modified"]).timestamp())
    assert [entry["name"] for entry in modified_order] != ["new.png", "middle.png", "old.png"]


def test_default_index_lives_outside_the_workspace(workspace, tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_GALLERY_INDEX_FILE", raising=False)
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(projects.tempfile, "gettempdir", lambda: str(cache))
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    browse_workspace("")

    assert (cache / "blender-mcp-gallery-index.json").is_file()
    assert list(workspace.rglob("*.json")) == []


def test_configured_index_is_never_written_inside_the_workspace(workspace, tmp_path, monkeypatch):
    inside = workspace / "gallery-index.json"
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(inside))
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == shot["modified"]
    assert not inside.exists()
    assert [entry["name"] for entry in browse_workspace("")] == ["FolderA", "shot.png"]


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b'{"version":1,"files":{"shot.png":"whenever"}}',
        b'{"version":99,"files":{"shot.png":"2023-11-14T22:13:20+00:00"}}',
        b'{"version":1,"files":[]}',
    ],
)
def test_corrupt_index_degrades_to_empty(workspace, tmp_path, payload):
    index = _index_file(tmp_path)
    index.parent.mkdir(parents=True)
    index.write_bytes(payload)
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == _iso(1_700_000_000)
    assert json.loads(index.read_text(encoding="utf-8"))["files"] == {"shot.png": _iso(1_700_000_000)}


def test_oversized_index_degrades_to_empty(workspace, tmp_path):
    index = _index_file(tmp_path)
    index.parent.mkdir(parents=True)
    index.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == _iso(1_700_000_000)
    assert index.stat().st_size < 4 * 1024 * 1024


def test_unwritable_index_does_not_break_browse_or_search(workspace, tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(blocked))
    _write(workspace / "shot.png", b"png", 1_700_000_000)

    shot = next(entry for entry in browse_workspace("") if entry["name"] == "shot.png")
    assert shot["added"] == shot["modified"]
    assert search_workspace("shot")["entries"][0]["added"] == shot["added"]


def test_folders_have_null_added(workspace):
    folder = next(entry for entry in browse_workspace("") if entry["name"] == "FolderA")
    assert folder["type"] == "folder"
    assert folder["added"] is None
    assert search_workspace("FolderA")["entries"][0]["added"] is None


def test_index_only_holds_paths_inside_the_workspace(workspace, tmp_path):
    (workspace / "FolderA" / "deep.png").write_bytes(b"png")

    browse_workspace("")
    search_workspace("deep")

    files = json.loads(_index_file(tmp_path).read_text(encoding="utf-8"))["files"]
    assert set(files) == {"FolderA/deep.png"}
    root = workspace.resolve()
    for relative in files:
        parts = Path(relative).parts
        assert not Path(relative).is_absolute()
        assert ".." not in parts
        assert (root / relative).is_file()
        assert root in (root / relative).resolve().parents


def test_browse_and_search_routes_pass_added_through(workspace, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-token")
    _write(workspace / "shot.png", b"png", 1_700_000_000)
    app = Starlette(middleware=[Middleware(_ApiKeyMiddleware)])
    register_gallery_routes(app)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        listing = client.get("/api/v1/gallery/browse", headers=headers)
        assert listing.status_code == 200
        entries = listing.json()["entries"]
        shot = next(entry for entry in entries if entry["name"] == "shot.png")
        folder = next(entry for entry in entries if entry["name"] == "FolderA")
        assert shot["added"] == _iso(1_700_000_000)
        assert folder["added"] is None

        results = client.get("/api/v1/gallery/search?q=shot", headers=headers).json()["entries"]
        assert results[0]["added"] == shot["added"]
