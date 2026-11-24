"""Workspace gallery: listing and serving ALL workspace items (folders + loose files)."""

from __future__ import annotations

import os

import pytest

from blender_mcp.utils import projects


def _ids(items):
    return {item["id"] for item in items}


def test_top_level_directory_listed_with_humanized_name(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "d4rk-v12-4k-gpu-verified-10s-frames").mkdir()
    entries = {p["id"]: p for p in projects.list_projects()}
    assert "d4rk-v12-4k-gpu-verified-10s-frames" in entries
    entry = entries["d4rk-v12-4k-gpu-verified-10s-frames"]
    assert entry["name"] == "d4rk v12 4k gpu verified 10s frames"
    assert entry["type"] == "folder"


def test_loose_blend_listed_with_humanized_stem(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "My_Scene.blend").write_bytes(b"blend")
    entries = {p["id"]: p for p in projects.list_projects()}
    assert "My_Scene.blend" in entries
    entry = entries["My_Scene.blend"]
    assert entry["name"] == "My Scene"
    assert entry["type"] == "project"


def test_loose_media_surfaced_via_workspace_root_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "loose_frame.png").write_bytes(b"png")
    (tmp_path / "clip.mp4").write_bytes(b"mp4")
    entries = {p["id"]: p for p in projects.list_projects()}
    assert "workspace-root" in entries
    entry = entries["workspace-root"]
    assert entry["name"] == "Workspace"
    assert entry["type"] == "folder"
    assets = {a["name"] for a in projects.list_assets("workspace-root")}
    assert assets == {"loose_frame.png", "clip.mp4"}


def test_workspace_root_entry_absent_without_loose_media(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "some_folder").mkdir()
    (tmp_path / "scene.blend").write_bytes(b"blend")
    assert "workspace-root" not in _ids(projects.list_projects())


def test_list_assets_on_root_folder_is_recursive(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    folder = tmp_path / "Render Output"
    folder.mkdir()
    (folder / "frame_0001.png").write_bytes(b"png")
    (folder / "sub" / "nested").mkdir(parents=True)
    (folder / "sub" / "nested" / "clip.mp4").write_bytes(b"mp4")
    (folder / "notes.txt").write_text("ignore")
    assets = {a["name"] for a in projects.list_assets("Render Output")}
    assert assets == {"frame_0001.png", "sub/nested/clip.mp4"}


def test_open_asset_streams_file_from_root_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "frame.png").write_bytes(b"image-bytes")
    fd, content_type, size, name = projects.open_asset("frames", "frame.png")
    try:
        assert content_type == "image/png"
        assert size == len(b"image-bytes")
        assert name == "frame.png"
        assert os.read(fd, 4096) == b"image-bytes"
    finally:
        os.close(fd)


def test_open_asset_streams_loose_media_from_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    (tmp_path / "loose.png").write_bytes(b"loose-bytes")
    fd, content_type, size, _name = projects.open_asset("workspace-root", "loose.png")
    try:
        assert content_type == "image/png"
        assert size == len(b"loose-bytes")
        assert os.read(fd, 4096) == b"loose-bytes"
    finally:
        os.close(fd)


def test_traversal_ids_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    for bad in ["..", "../outside", "a/b", "a\\b", ""]:
        with pytest.raises(ValueError):
            projects.list_assets(bad)
        with pytest.raises(ValueError):
            projects.open_asset(bad, "frame.png")


def test_symlink_top_level_directory_not_listed_or_served(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    outside = tmp_path.parent / f"outside-top-{tmp_path.name}"
    outside.mkdir()
    (outside / "frame.png").write_bytes(b"x")
    (tmp_path / "linked").symlink_to(outside)
    assert "linked" not in _ids(projects.list_projects())
    with pytest.raises(ValueError):
        projects.list_assets("linked")


def test_symlink_inside_root_folder_is_confined(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    folder = tmp_path / "Out"
    folder.mkdir()
    (folder / "frame.png").write_bytes(b"ok")
    outside = tmp_path.parent / f"secret-{tmp_path.name}.png"
    outside.write_bytes(b"secret")
    (folder / "escape.png").symlink_to(outside)
    outside_dir = tmp_path.parent / f"outside-inner-{tmp_path.name}"
    outside_dir.mkdir()
    (folder / "sub").symlink_to(outside_dir)
    assert {a["name"] for a in projects.list_assets("Out")} == {"frame.png"}
    with pytest.raises(FileNotFoundError):
        projects.open_asset("Out", "escape.png")
    with pytest.raises(FileNotFoundError):
        projects.open_asset("Out", "sub/frame.png")


def test_existing_projects_still_listed(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    project = projects.create_project("Existing")
    (tmp_path / "d4rk-frames").mkdir()
    entries = {p["id"]: p for p in projects.list_projects()}
    assert project["id"] in entries
    assert entries[project["id"]]["name"] == "Existing"
    assert entries[project["id"]]["type"] == "project"
    # The structured projects/ directory itself must not appear as a folder entry.
    assert "projects" not in entries
    # Root folder and existing project coexist.
    assert "d4rk-frames" in entries
