"""Gallery soft delete: trash layout, restore, retention purge and routes.

Deleting is the one irreversible gesture in the browser, so these tests pin the
recoverable contract: the entry leaves the workspace but survives byte-identical
in ``.trash`` with provenance, and no path trick can point the move outward.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import blender_mcp.utils.projects as projects
from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils import housekeeping as hk
from blender_mcp.utils.projects import (
    TrashRefused,
    browse_workspace,
    delete_from_trash,
    list_trash,
    move_to_trash,
    purge_trash,
    restore_from_trash,
    search_workspace,
)
from blender_mcp.utils.studio_api import register_gallery_routes

TRASH_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{8}$"


@pytest.fixture(autouse=True)
def isolated_gallery_index(tmp_path, monkeypatch):
    """Keep the first-seen index out of the shared temp dir, one per test."""
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(tmp_path.parent / f"{tmp_path.name}-gallery-index.json"))
    monkeypatch.delenv("MCP_TRASH_RETENTION_DAYS", raising=False)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(root))
    (root / "Folder").mkdir()
    (root / "Folder" / "a.txt").write_text("aaa", encoding="utf-8")
    (root / "Folder" / "sub").mkdir()
    (root / "Folder" / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    (root / "Sibling").mkdir()
    (root / "Sibling" / "c.txt").write_text("ccc", encoding="utf-8")
    (root / "still.png").write_bytes(b"\x89PNG\r\n\x1a\npayload")
    return root


def _index_keys() -> set[str]:
    path = Path(os.environ["MCP_GALLERY_INDEX_FILE"])
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("files", {}))


def _trash_dir(workspace: Path, trash_id: str) -> Path:
    return workspace / ".trash" / trash_id


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-token")
    app = Starlette(middleware=[Middleware(_ApiKeyMiddleware)])
    register_gallery_routes(app)
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_file_moves_into_trash_with_provenance_and_leaves_browse(workspace):
    original = workspace / "still.png"
    before = _digest(original)

    result = move_to_trash(workspace, "still.png")

    assert set(result) == {"trash_id", "original_path", "name", "type"}
    assert result["original_path"] == "still.png"
    assert result["name"] == "still.png"
    assert result["type"] == "file"
    assert re.fullmatch(TRASH_ID_PATTERN, result["trash_id"])

    assert not original.exists()
    names = {entry["name"] for entry in browse_workspace("")}
    assert "still.png" not in names
    assert ".trash" not in names  # the trash is never browsable

    entry_dir = _trash_dir(workspace, result["trash_id"])
    assert (entry_dir / "still.png").is_file()
    assert _digest(entry_dir / "still.png") == before

    metadata = json.loads((entry_dir / "entry.json").read_text(encoding="utf-8"))
    assert metadata["original_path"] == "still.png"
    assert metadata["name"] == "still.png"
    assert metadata["type"] == "file"
    assert metadata["size"] == len(b"\x89PNG\r\n\x1a\npayload")
    deleted_at = datetime.fromisoformat(metadata["deleted_at"])
    assert deleted_at.tzinfo is not None
    assert abs((datetime.now(UTC) - deleted_at).total_seconds()) < 300


def test_folder_moves_with_all_of_its_contents(workspace):
    result = move_to_trash(workspace, "Folder")

    assert result["type"] == "folder"
    assert result["original_path"] == "Folder"
    assert not (workspace / "Folder").exists()
    assert "Folder" not in {entry["name"] for entry in browse_workspace("")}

    payload = _trash_dir(workspace, result["trash_id"]) / "Folder"
    assert (payload / "a.txt").read_text(encoding="utf-8") == "aaa"
    assert (payload / "sub" / "b.txt").read_text(encoding="utf-8") == "bbb"
    metadata = json.loads((payload.parent / "entry.json").read_text(encoding="utf-8"))
    assert metadata["type"] == "folder"
    assert metadata["size"] is None


def test_nested_file_keeps_its_relative_path(workspace):
    result = move_to_trash(workspace, "Folder/sub/b.txt")

    assert result["original_path"] == "Folder/sub/b.txt"
    assert result["name"] == "b.txt"
    entry_dir = _trash_dir(workspace, result["trash_id"])
    assert (entry_dir / "b.txt").read_text(encoding="utf-8") == "bbb"
    assert browse_workspace("Folder/sub") == []


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_restore_returns_the_file_to_the_exact_path_byte_identical(workspace):
    target = workspace / "Folder" / "sub" / "b.txt"
    before = _digest(target)
    trash_id = move_to_trash(workspace, "Folder/sub/b.txt")["trash_id"]

    # The parent directory disappears too, so restore has to recreate it.
    (workspace / "Folder" / "sub").rmdir()

    result = restore_from_trash(workspace, trash_id)

    assert result == {"restored_path": "Folder/sub/b.txt"}
    assert target.is_file()
    assert _digest(target) == before
    assert [entry["path"] for entry in browse_workspace("Folder/sub")] == ["Folder/sub/b.txt"]
    assert list((workspace / ".trash").iterdir()) == []  # the entry dir is gone


def test_restore_returns_a_whole_folder(workspace):
    before = sorted(str(path.relative_to(workspace)) for path in (workspace / "Folder").rglob("*"))
    trash_id = move_to_trash(workspace, "Folder")["trash_id"]

    assert restore_from_trash(workspace, trash_id) == {"restored_path": "Folder"}

    after = sorted(str(path.relative_to(workspace)) for path in (workspace / "Folder").rglob("*"))
    assert after == before
    assert (workspace / "Folder" / "sub" / "b.txt").read_text(encoding="utf-8") == "bbb"
    assert "Folder" in {entry["name"] for entry in browse_workspace("")}


def test_restore_refuses_when_something_occupies_the_path(workspace):
    target = workspace / "still.png"
    trash_id = move_to_trash(workspace, "still.png")["trash_id"]
    target.write_bytes(b"a different file now")

    with pytest.raises(TrashRefused):
        restore_from_trash(workspace, trash_id)

    assert target.read_bytes() == b"a different file now"  # the occupant is untouched
    assert _trash_dir(workspace, trash_id).is_dir()  # the entry survives the refusal

    target.unlink()
    assert restore_from_trash(workspace, trash_id) == {"restored_path": "still.png"}
    assert target.read_bytes() == b"\x89PNG\r\n\x1a\npayload"


def test_restore_rejects_ids_that_are_not_plain_entry_names(workspace):
    for bad in (
        "",
        ".",
        "..",
        "../x",
        "20260101T000000Z-abcdef12/../x",
        "20260101T000000Z-ABCDEF12",
        "20260101T000000Z-abcdef1",
        "20260101T000000Z-abcdef12\x00",
        "/etc",
        "a" * 200,
    ):
        with pytest.raises(ValueError):
            restore_from_trash(workspace, bad)
    with pytest.raises(FileNotFoundError):
        restore_from_trash(workspace, "20260101T000000Z-00000000")


def test_restore_refuses_metadata_that_points_outside_the_workspace(workspace, tmp_path):
    entry_dir = workspace / ".trash" / "20260101T000000Z-aaaaaaaa"
    entry_dir.mkdir(parents=True)
    (entry_dir / "escape.txt").write_text("payload", encoding="utf-8")
    (entry_dir / "entry.json").write_text(
        json.dumps(
            {
                "original_path": "../escaped.txt",
                "name": "escape.txt",
                "type": "file",
                "size": 7,
                "deleted_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TrashRefused):
        restore_from_trash(workspace, "20260101T000000Z-aaaaaaaa")

    assert not (tmp_path / "escaped.txt").exists()
    assert (entry_dir / "escape.txt").is_file()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        ".trash",
        ".trash/20260101T000000Z-abcdef12",
        ".trash/entry.json",
        "..",
        "../escape",
        "Folder/../../escape",
        "/etc/passwd",
        "/",
        "Folder//a.txt",
        "Folder/",
        "\\etc\\passwd",
        "Folder/a.txt\x00",
        ".hidden",
    ],
)
def test_delete_refuses_root_trash_and_escaping_paths(workspace, bad, tmp_path):
    canary = tmp_path / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")

    with pytest.raises(TrashRefused):
        move_to_trash(workspace, bad)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (workspace / "Folder" / "a.txt").is_file()
    assert (workspace / "still.png").is_file()
    assert not (workspace / ".trash").exists()  # a refused delete creates nothing


def test_delete_reports_missing_entries_as_not_found(workspace):
    with pytest.raises(FileNotFoundError):
        move_to_trash(workspace, "nope.txt")
    with pytest.raises(FileNotFoundError):
        move_to_trash(workspace, "Folder/nope.txt")
    assert not (workspace / ".trash").exists()


def test_symlink_pointing_outside_is_removed_as_a_link_and_target_survives(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)

    result = move_to_trash(workspace, "outside-link")

    assert result["type"] == "symlink"
    assert not (workspace / "outside-link").is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert _trash_dir(workspace, result["trash_id"]).joinpath("outside-link").is_symlink()
    assert restore_from_trash(workspace, result["trash_id"]) == {"restored_path": "outside-link"}
    assert (workspace / "outside-link").is_symlink()


def test_delete_never_traverses_a_symlinked_parent(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "alias").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        move_to_trash(workspace, "alias/secret.txt")

    assert (outside / "secret.txt").is_file()
    assert (workspace / "alias").is_symlink()


# ---------------------------------------------------------------------------
# Date-added index pruning
# ---------------------------------------------------------------------------


def test_index_drops_the_deleted_file_only(workspace):
    browse_workspace("Folder/sub")
    browse_workspace("Folder")
    browse_workspace("")
    assert {"Folder/sub/b.txt", "Folder/a.txt", "still.png"} <= _index_keys()

    move_to_trash(workspace, "Folder/sub/b.txt")

    keys = _index_keys()
    assert "Folder/sub/b.txt" not in keys
    assert {"Folder/a.txt", "still.png"} <= keys


def test_index_drops_every_child_of_a_deleted_folder(workspace):
    browse_workspace("Folder")
    browse_workspace("Folder/sub")
    browse_workspace("Sibling")
    browse_workspace("")
    assert {"Folder/a.txt", "Folder/sub/b.txt", "Sibling/c.txt", "still.png"} <= _index_keys()

    move_to_trash(workspace, "Folder")

    keys = _index_keys()
    assert not any(key == "Folder" or key.startswith("Folder/") for key in keys)
    # A sibling whose name merely shares the prefix must survive.
    assert {"Sibling/c.txt", "still.png"} <= keys


def test_index_pruning_survives_an_unusable_index(workspace, monkeypatch):
    index = Path(os.environ["MCP_GALLERY_INDEX_FILE"])
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_bytes(b"{not json")

    result = move_to_trash(workspace, "still.png")

    assert not (workspace / "still.png").exists()
    assert _trash_dir(workspace, result["trash_id"]).is_dir()


# ---------------------------------------------------------------------------
# Retention purge
# ---------------------------------------------------------------------------


def _backdate_entry(workspace: Path, trash_id: str, days: float) -> None:
    """Rewrite entry.json so the entry looks ``days`` old."""
    entry = _trash_dir(workspace, trash_id) / "entry.json"
    payload = json.loads(entry.read_text(encoding="utf-8"))
    payload["deleted_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    entry.write_text(json.dumps(payload), encoding="utf-8")


def test_purge_removes_expired_entries_and_keeps_fresh_ones(workspace):
    (workspace / "old.png").write_bytes(b"old")
    old_id = move_to_trash(workspace, "old.png")["trash_id"]
    fresh_id = move_to_trash(workspace, "still.png")["trash_id"]
    _backdate_entry(workspace, old_id, 30)

    result = purge_trash(workspace, older_than_days=7)

    assert result["removed_entries"] == 1
    assert result["kept_entries"] == 1
    assert result["errors"] == 0
    assert result["retention_days"] == 7.0
    assert not _trash_dir(workspace, old_id).exists()
    assert _trash_dir(workspace, fresh_id).is_dir()
    # The fresh entry is still fully recoverable.
    assert restore_from_trash(workspace, fresh_id) == {"restored_path": "still.png"}


def test_purge_uses_the_directory_mtime_when_metadata_is_gone(workspace):
    trash_id = move_to_trash(workspace, "still.png")["trash_id"]
    entry_dir = _trash_dir(workspace, trash_id)
    (entry_dir / "entry.json").unlink()
    stale = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    os.utime(entry_dir, (stale, stale))

    result = purge_trash(workspace, older_than_days=7)

    assert result["removed_entries"] == 1
    assert not entry_dir.exists()


def test_purge_default_retention_and_clamping(workspace, monkeypatch):
    assert projects._trash_retention_days(None) == 7.0
    assert purge_trash(workspace, older_than_days=0)["retention_days"] == 1.0
    assert purge_trash(workspace, older_than_days=0.2)["retention_days"] == 1.0
    assert purge_trash(workspace, older_than_days=999)["retention_days"] == 90.0
    assert purge_trash(workspace, older_than_days="nonsense")["retention_days"] == 7.0  # type: ignore[arg-type]
    monkeypatch.setenv("MCP_TRASH_RETENTION_DAYS", "3")
    assert purge_trash(workspace)["retention_days"] == 3.0
    monkeypatch.setenv("MCP_TRASH_RETENTION_DAYS", "0")
    assert purge_trash(workspace)["retention_days"] == 1.0
    monkeypatch.setenv("MCP_TRASH_RETENTION_DAYS", "nonsense")
    assert purge_trash(workspace)["retention_days"] == 7.0


def test_purge_never_touches_anything_outside_the_trash(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    junk = workspace / ".trash" / "not-a-trash-id"
    junk.mkdir(parents=True)
    (junk / "payload.txt").write_text("curious", encoding="utf-8")
    (workspace / ".trash" / "20200101T000000Z-deadbeef").symlink_to(outside, target_is_directory=True)

    result = purge_trash(workspace, older_than_days=1)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert junk.is_dir() and (junk / "payload.txt").is_file()
    assert result["errors"] >= 1  # the foreign directory is reported, not deleted
    assert not (workspace / ".trash" / "20200101T000000Z-deadbeef").is_symlink()


def test_purge_is_inert_without_a_trash_directory(workspace, tmp_path):
    assert purge_trash(workspace)["removed_entries"] == 0
    empty = purge_trash(tmp_path / "no-such-workspace")
    assert empty["removed_entries"] == 0 and empty["errors"] == 0
    assert not (tmp_path / "no-such-workspace").exists()  # purge never creates a workspace


def test_housekeeping_sweep_purges_expired_trash(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp-root"))
    (tmp_path / "temp-root").mkdir()
    monkeypatch.setenv("MCP_TEMP_SWEEP_HOURS", "6")
    hk.reset_schedule()
    trash_id = move_to_trash(workspace, "still.png")["trash_id"]
    _backdate_entry(workspace, trash_id, 10)

    result = hk.sweep_if_due(now=1000.0)

    assert result is not None
    assert result["trash"]["removed_entries"] == 1
    assert not _trash_dir(workspace, trash_id).exists()
    assert hk.sweep_if_due(now=1060.0) is None  # still inside the interval
    hk.reset_schedule()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_recycle_bin_lists_and_permanently_deletes_entries(workspace):
    trash_id = move_to_trash(workspace, "still.png")["trash_id"]
    listed = list_trash(workspace)
    assert len(listed) == 1
    assert listed[0]["trash_id"] == trash_id
    assert listed[0]["original_path"] == "still.png"
    assert listed[0]["name"] == "still.png"
    delete_from_trash(workspace, trash_id)
    assert list_trash(workspace) == []
    assert not _trash_dir(workspace, trash_id).exists()


def test_delete_and_restore_routes_round_trip(workspace, monkeypatch):
    before = _digest(workspace / "still.png")
    with _client(monkeypatch) as client:
        response = client.post("/api/v1/gallery/delete", json={"path": "still.png"}, headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"trash_id", "original_path", "name", "type"}
        assert not (workspace / "still.png").exists()
        assert "still.png" not in {
            entry["name"] for entry in client.get("/api/v1/gallery/browse", headers=AUTH).json()["entries"]
        }

        restored = client.post("/api/v1/gallery/restore", json={"trash_id": body["trash_id"]}, headers=AUTH)
        assert restored.status_code == 200
        assert restored.json() == {"restored_path": "still.png"}
    assert _digest(workspace / "still.png") == before


def test_recycle_bin_routes_are_authenticated_and_permanent_delete_works(workspace, monkeypatch):
    with _client(monkeypatch) as client:
        assert client.get("/api/v1/gallery/trash").status_code == 401
        assert client.post("/api/v1/gallery/trash/delete", json={}).status_code == 401
        deleted = client.post("/api/v1/gallery/delete", json={"path": "still.png"}, headers=AUTH).json()
        listed = client.get("/api/v1/gallery/trash", headers=AUTH)
        assert listed.status_code == 200
        assert listed.json()["entries"][0]["trash_id"] == deleted["trash_id"]
        assert client.post("/api/v1/gallery/trash/delete", json={"trash_id": "bad"}, headers=AUTH).status_code == 400
        result = client.post("/api/v1/gallery/trash/delete", json={"trash_id": deleted["trash_id"]}, headers=AUTH)
        assert result.status_code == 200
        assert result.json() == {"deleted": True}
        assert client.get("/api/v1/gallery/trash", headers=AUTH).json()["entries"] == []


def test_delete_route_status_codes(workspace, monkeypatch):
    with _client(monkeypatch) as client:
        assert client.post("/api/v1/gallery/delete", json={"path": "still.png"}).status_code == 401
        assert client.post("/api/v1/gallery/restore", json={"trash_id": "x"}).status_code == 401

        # 400: no usable body / no path.
        assert client.post("/api/v1/gallery/delete", content=b"{not json", headers=AUTH).status_code == 400
        assert client.post("/api/v1/gallery/delete", json={}, headers=AUTH).status_code == 400
        assert client.post("/api/v1/gallery/delete", json={"path": 42}, headers=AUTH).status_code == 400
        assert client.post("/api/v1/gallery/delete", json={"path": ""}, headers=AUTH).status_code == 400

        # 404: nothing there.
        assert client.post("/api/v1/gallery/delete", json={"path": "nope.txt"}, headers=AUTH).status_code == 404

        # 409: root, the trash itself and escaping paths are refused.
        for refused in ("/", ".trash", "../escape", "/etc/passwd"):
            response = client.post("/api/v1/gallery/delete", json={"path": refused}, headers=AUTH)
            assert response.status_code == 409, refused
            assert "error" in response.json()

        # Nothing above leaked a filesystem path or a token.
        for refused in ("/", ".trash", "../escape"):
            text = client.post("/api/v1/gallery/delete", json={"path": refused}, headers=AUTH).text
            assert str(workspace) not in text and "test-token" not in text


def test_restore_route_status_codes(workspace, monkeypatch):
    trash_id = move_to_trash(workspace, "still.png")["trash_id"]
    with _client(monkeypatch) as client:
        assert client.post("/api/v1/gallery/restore", json={}, headers=AUTH).status_code == 400
        assert client.post("/api/v1/gallery/restore", json={"trash_id": "../../etc"}, headers=AUTH).status_code == 400
        unknown = client.post("/api/v1/gallery/restore", json={"trash_id": "20260101T000000Z-00000000"}, headers=AUTH)
        assert unknown.status_code == 404

        (workspace / "still.png").write_bytes(b"occupied")
        occupied = client.post("/api/v1/gallery/restore", json={"trash_id": trash_id}, headers=AUTH)
        assert occupied.status_code == 409
        assert (workspace / "still.png").read_bytes() == b"occupied"

        (workspace / "still.png").unlink()
        assert client.post("/api/v1/gallery/restore", json={"trash_id": trash_id}, headers=AUTH).status_code == 200


def test_search_does_not_see_trashed_entries(workspace):
    move_to_trash(workspace, "Folder/sub/b.txt")

    assert search_workspace("b.txt")["entries"] == []
    assert [entry["path"] for entry in search_workspace("a.txt")["entries"]] == ["Folder/a.txt"]
