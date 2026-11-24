"""Vision-review folders resolve inside BLENDER_WORKSPACE, never the process CWD.

``vision_review_bundle`` used to create ``output_dir`` relative to the CWD, which
put a stray empty folder in ``/app`` inside the container before the screenshot
handoff ever ran. These tests pin the resolver it now shares with the render
handlers, and check the bundle hands the resolved absolute path downstream. No
Blender is invoked: the capture helpers are monkeypatched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blender_mcp.handlers import vision_refine_handler
from blender_mcp.handlers.render_handler import RenderOutputPathError


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A throwaway workspace selected through BLENDER_WORKSPACE."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(root))
    monkeypatch.delenv("MCP_ALLOW_EXTERNAL_RENDER_OUTPUT", raising=False)
    return root


def test_relative_review_dir_resolves_under_the_workspace(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    out = vision_refine_handler.resolve_review_output_dir("renders/review")

    assert out == workspace / "renders" / "review"
    assert out.is_dir()
    assert not (cwd / "renders").exists()


def test_empty_review_dir_picks_a_workspace_default(workspace):
    out = vision_refine_handler.resolve_review_output_dir("")

    assert out.parent == workspace / "renders"
    assert out.name.endswith("-vision-review")
    assert out.is_dir()


def test_review_dir_refuses_parent_traversal(workspace):
    with pytest.raises(RenderOutputPathError):
        vision_refine_handler.resolve_review_output_dir("renders/../escaped")

    assert not (workspace / "escaped").exists()
    assert not (workspace.parent / "escaped").exists()


def test_review_dir_refuses_absolute_paths_outside_the_workspace(workspace, tmp_path):
    outside = tmp_path / "outside"

    with pytest.raises(RenderOutputPathError):
        vision_refine_handler.resolve_review_output_dir(str(outside))

    assert not outside.exists()


def test_review_dir_refuses_a_symlinked_component(workspace, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (workspace / "renders").symlink_to(external, target_is_directory=True)

    with pytest.raises(RenderOutputPathError):
        vision_refine_handler.resolve_review_output_dir("renders/review")

    assert not (external / "review").exists()


async def test_review_bundle_creates_the_folder_in_the_workspace(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    calls: dict[str, object] = {}

    async def fake_screenshot(**kwargs):
        calls["screenshot"] = kwargs["output_path"]
        return {"success": True}

    async def fake_multi_angle(**kwargs):
        calls["multi_angle"] = kwargs["output_dir"]
        return {"success": True}

    async def fake_scene_summary(**kwargs):
        return {"scene": "Scene", "object_count": 0, "mesh_objects": []}

    monkeypatch.setattr(vision_refine_handler, "screenshot_viewport", fake_screenshot)
    monkeypatch.setattr(vision_refine_handler, "render_multi_angle", fake_multi_angle)
    monkeypatch.setattr(vision_refine_handler, "_scene_summary", fake_scene_summary)

    result = await vision_refine_handler.vision_review_bundle(output_dir="renders/bundle", goal="test")

    bundle = workspace / "renders" / "bundle"
    assert result["success"] is True
    assert calls["screenshot"] == str(bundle / "review_viewport.png")
    assert calls["multi_angle"] == str(bundle / "angles")
    assert bundle.is_dir()
    assert not (cwd / "renders").exists()
