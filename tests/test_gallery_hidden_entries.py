"""Hidden workspace state must never surface in the gallery.

``.trash`` (and staging dirs like ``.blender-farm``) are dotted entries the browser
refuses to open. They used to be listed as gallery folders and scanned by
``latest_media``, so the dashboard could hand the Gallery a path such as
``projects/.trash/<stamp>-<id>/<file>`` that always answered HTTP 400.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blender_mcp.utils.projects import (
    browse_workspace,
    latest_media,
    list_assets,
    list_projects,
)

TRASH_ID = "20260101T000000Z-abcdef12"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(root))
    monkeypatch.setenv("MCP_GALLERY_INDEX_FILE", str(tmp_path / "state" / "gallery-index.json"))
    return root


def _media(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.utime(path, (mtime, mtime))


def _trash_entry(root: Path) -> Path:
    entry = root / ".trash" / TRASH_ID
    entry.mkdir(parents=True)
    (entry / "entry.json").write_text(
        json.dumps(
            {
                "original_path": "renders/verify.png",
                "name": "verify.png",
                "type": "file",
                "size": 8,
                "deleted_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return entry


def test_list_projects_hides_dotted_workspace_entries(workspace):
    alpha = workspace / "projects" / "alpha"
    alpha.mkdir(parents=True)
    (alpha / ".project.json").write_text(json.dumps({"name": "Alpha"}), encoding="utf-8")
    (workspace / ".blender-farm").mkdir()
    (workspace / ".knight-rig-stage").mkdir()
    _trash_entry(workspace)

    ids = {item["id"] for item in list_projects()}

    assert "alpha" in ids
    assert "Alpha" in {item["name"] for item in list_projects()}
    assert not [project_id for project_id in ids if project_id.startswith(".")]


def test_list_projects_hides_dotted_project_directories(workspace):
    hidden = workspace / "projects" / ".hidden-project"
    hidden.mkdir(parents=True)
    _media(hidden / "render.png", 1000.0)

    ids = {item["id"] for item in list_projects()}

    assert ".hidden-project" not in ids


def test_latest_media_ignores_trashed_files(workspace):
    alpha = workspace / "projects" / "alpha"
    alpha.mkdir(parents=True)
    _media(alpha / "render.png", 1_000.0)
    entry = _trash_entry(workspace)
    # The deleted file is newer, which is exactly how it used to win the scan.
    _media(entry / "verify.png", 2_000.0)

    latest = latest_media()

    assert latest is not None
    assert latest["name"] == "render.png"
    assert ".trash" not in latest["url"]
    assert latest["project_id"] == "alpha"


def test_latest_media_returns_none_when_only_trash_has_media(workspace):
    entry = _trash_entry(workspace)
    _media(entry / "verify.png", 2_000.0)

    assert latest_media() is None


def test_hidden_entries_are_not_addressable(workspace):
    entry = _trash_entry(workspace)
    _media(entry / "verify.png", 2_000.0)

    with pytest.raises(ValueError):
        list_assets(".trash")
    with pytest.raises(ValueError):
        browse_workspace(".trash")
    with pytest.raises(ValueError):
        browse_workspace(f".trash/{TRASH_ID}")


def test_browse_root_omits_hidden_entries(workspace):
    (workspace / "renders").mkdir()
    _trash_entry(workspace)
    (workspace / ".blender-farm").mkdir()

    names = {entry["name"] for entry in browse_workspace("")}

    assert "renders" in names
    assert not [name for name in names if name.startswith(".")]
