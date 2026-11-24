"""Bounded, authenticated workspace-wide Gallery filename search."""

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import blender_mcp.utils.projects as projects
from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils.projects import search_workspace
from blender_mcp.utils.studio_api import register_gallery_routes


@pytest.fixture(autouse=True)
def isolated_gallery_index(tmp_path, monkeypatch):
    """Keep the first-seen "date added" index out of the shared temp dir."""
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(tmp_path.parent / f"{tmp_path.name}-gallery-index.json"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "deep" / "inside").mkdir(parents=True)
    (tmp_path / "deep" / "inside" / "Camera.blend").write_bytes(b"blend")
    (tmp_path / "deep" / "inside" / "camera-notes.md").write_text("camera")
    (tmp_path / "Camera-root.png").write_bytes(b"png")
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "camera-secret.txt").write_text("secret")
    (tmp_path / "deep" / ".camera-hidden").write_text("hidden")
    (tmp_path / "camera-link").symlink_to(tmp_path / "deep", target_is_directory=True)
    (tmp_path / "deep" / "inside" / "camera-alias").symlink_to(tmp_path / "Camera-root.png")
    return tmp_path


def test_nested_filename_matches_have_browser_metadata(workspace):
    result = search_workspace("CAMERA")
    assert [entry["path"] for entry in result["entries"]] == [
        "Camera-root.png",
        "deep/inside/camera-notes.md",
        "deep/inside/Camera.blend",
    ]
    assert result["truncated"] is False
    assert result["entries"][2]["format"] == "BLEND"
    assert result["entries"][2]["preview"] == "none"
    assert result["entries"][2]["size"] == 5


def test_hidden_and_symlink_entries_are_not_searched(workspace):
    assert search_workspace("camera-secret")["entries"] == []
    assert search_workspace("camera-hidden")["entries"] == []
    assert search_workspace("camera-link")["entries"] == []
    assert search_workspace("camera-alias")["entries"] == []


class _ReverseScandir:
    """Directory iterator that yields entries in reverse name order.

    Filesystem readdir order is arbitrary, so deriving the order from the scan
    itself is the only way to prove sorting happened rather than being lucky.
    """

    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        entries = list(self._inner.__enter__())
        entries.sort(key=lambda entry: entry.name.casefold(), reverse=True)
        return iter(entries)

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)


class _ReversedOs:
    """os shim that only reorders directory iteration for the search walk."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)

    def scandir(self, *args, **kwargs):
        return _ReverseScandir(self._module.scandir(*args, **kwargs))


@pytest.fixture
def reversed_scan(monkeypatch):
    """Force scandir to hand back the last-sorted name first."""
    monkeypatch.setattr(projects, "os", _ReversedOs(projects.os))


@pytest.fixture
def scan_order_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(workspace))
    for name in ("camera-alpha.png", "camera-bravo.png", "camera-charlie.png", "Camera-Zulu.png"):
        (workspace / name).write_bytes(b"png")
    return workspace


def test_results_truncated_by_result_limit_stay_sorted(scan_order_workspace, reversed_scan):
    result = search_workspace("camera", max_results=2)
    assert result["truncated"] is True
    paths = [entry["path"] for entry in result["entries"]]
    assert paths == ["camera-charlie.png", "Camera-Zulu.png"]
    assert paths == sorted(paths, key=str.casefold)


def test_results_truncated_by_entry_budget_stay_sorted(scan_order_workspace, reversed_scan):
    result = search_workspace("camera", max_entries=3)
    assert result["truncated"] is True
    paths = [entry["path"] for entry in result["entries"]]
    assert paths == ["camera-bravo.png", "camera-charlie.png", "Camera-Zulu.png"]
    assert paths == sorted(paths, key=str.casefold)


def test_caps_and_invalid_query(workspace):
    assert len(search_workspace("camera", max_results=1)["entries"]) == 1
    assert search_workspace("camera", max_results=1)["truncated"] is True
    assert search_workspace("camera", max_entries=1)["truncated"] is True
    for query in ("", "  ", "x" * 101, "camera\nkey", "../camera"):
        with pytest.raises(ValueError):
            search_workspace(query)


def test_search_route_requires_auth_and_rejects_bad_queries(workspace, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-token")
    app = Starlette(middleware=[Middleware(_ApiKeyMiddleware)])
    register_gallery_routes(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/gallery/search?q=camera").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        response = client.get("/api/v1/gallery/search?q=camera", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "deep/inside/Camera.blend" in {entry["path"] for entry in response.json()["entries"]}
        assert client.get("/api/v1/gallery/search?q=", headers=headers).status_code == 400
        assert client.get("/api/v1/gallery/search?q=..%2Fcamera", headers=headers).status_code == 400
