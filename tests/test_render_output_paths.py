"""Render output paths resolve inside BLENDER_WORKSPACE, never the process CWD.

A render written relative to the CWD lands in ``/app`` inside the container: the
Gallery cannot see it and it dies with the container. These tests pin the resolver
that every render handler uses, plus the handler results that carry the
workspace-relative path an agent hands to the Gallery.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from blender_mcp.handlers import render_handler, rendering_handler, scene_handler


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A throwaway workspace selected through BLENDER_WORKSPACE."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(root))
    monkeypatch.delenv("MCP_ALLOW_EXTERNAL_RENDER_OUTPUT", raising=False)
    return root


def _mock_executor(monkeypatch, module, output: str = "") -> list[str]:
    """Replace a handler executor and collect the scripts it is asked to run."""
    scripts: list[str] = []

    async def fake_execute_script(script, **_kwargs):
        scripts.append(script)
        return output

    executor = MagicMock()
    executor.execute_script = AsyncMock(side_effect=fake_execute_script)
    monkeypatch.setattr(module, "_executor", executor)
    return scripts


# ---------------------------------------------------------------------------
# Relative paths
# ---------------------------------------------------------------------------


def test_relative_path_resolves_under_the_workspace(workspace):
    output = render_handler.resolve_render_output("renders/shot.png")

    assert output.absolute == workspace / "renders" / "shot.png"
    assert output.relative == "renders/shot.png"
    assert not output.external


def test_relative_path_is_not_anchored_on_the_process_cwd(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    output = render_handler.resolve_render_output("renders/cwd-proof.png")

    assert output.absolute == workspace / "renders" / "cwd-proof.png"
    assert cwd not in output.absolute.parents


def test_relative_path_creates_missing_parent_directories(workspace):
    output = render_handler.resolve_render_output("renders/job/nested/frame.png")

    assert output.absolute.parent.is_dir()
    assert output.absolute.parent == workspace / "renders" / "job" / "nested"
    # Nothing is rendered here, only the folder tree is prepared.
    assert not output.absolute.exists()


def test_relative_path_ignores_leading_dot_slash(workspace):
    output = render_handler.resolve_render_output("./renders/shot.png")

    assert output.absolute == workspace / "renders" / "shot.png"
    assert output.relative == "renders/shot.png"


# ---------------------------------------------------------------------------
# Empty paths
# ---------------------------------------------------------------------------


def test_empty_path_picks_a_workspace_default(workspace):
    output = render_handler.resolve_render_output("", default_name="preview", suffix=".png")

    assert output.absolute.parent == workspace / "renders"
    assert output.absolute.parent.is_dir()
    assert output.absolute.suffix == ".png"
    assert output.absolute.name.endswith("-preview.png")
    assert output.relative == f"renders/{output.absolute.name}"
    assert workspace in output.absolute.parents


def test_empty_path_uses_the_format_suffix(workspace):
    output = render_handler.resolve_render_output("", default_name="preview", suffix=".jpg")

    assert output.absolute.suffix == ".jpg"


def test_empty_directory_picks_a_workspace_default(workspace):
    output = render_handler.resolve_render_output("", directory=True, default_name="turntable")

    assert output.absolute.parent == workspace / "renders"
    assert output.absolute.is_dir()
    assert output.absolute.name.endswith("-turntable")


# ---------------------------------------------------------------------------
# Absolute paths
# ---------------------------------------------------------------------------


def test_absolute_path_inside_workspace_is_accepted(workspace):
    requested = workspace / "renders" / "absolute.png"

    output = render_handler.resolve_render_output(str(requested))

    assert output.absolute == requested
    assert output.relative == "renders/absolute.png"
    assert requested.parent.is_dir()


def test_absolute_path_outside_workspace_is_refused(workspace, tmp_path):
    outside = tmp_path / "outside" / "shot.png"

    with pytest.raises(render_handler.RenderOutputPathError) as excinfo:
        render_handler.resolve_render_output(str(outside))

    message = str(excinfo.value)
    assert str(workspace) in message
    assert "MCP_ALLOW_EXTERNAL_RENDER_OUTPUT" in message
    assert not outside.parent.exists()


def test_absolute_path_outside_workspace_accepted_with_the_env_flag(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_EXTERNAL_RENDER_OUTPUT", "true")
    outside = tmp_path / "outside" / "shot.png"

    output = render_handler.resolve_render_output(str(outside))

    assert output.absolute == outside
    assert output.external is True
    assert output.relative is None
    assert "not shown in the Gallery" in output.summary()
    assert outside.parent.is_dir()


def test_env_flag_does_not_relax_relative_path_containment(workspace, monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_EXTERNAL_RENDER_OUTPUT", "true")

    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("../escape.png")


def test_windows_style_absolute_path_is_refused(workspace):
    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("C:/render/output.png")


# ---------------------------------------------------------------------------
# Traversal and symlinks
# ---------------------------------------------------------------------------


def test_parent_traversal_is_refused(workspace):
    with pytest.raises(render_handler.RenderOutputPathError) as excinfo:
        render_handler.resolve_render_output("../escape.png")

    assert "'..'" in str(excinfo.value)
    assert not (workspace.parent / "escape.png").exists()


def test_nested_parent_traversal_is_refused(workspace):
    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("renders/../../escape.png")


def test_symlinked_parent_is_refused(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(render_handler.RenderOutputPathError) as excinfo:
        render_handler.resolve_render_output("linked/shot.png")

    assert "symlink" in str(excinfo.value)
    assert not (outside / "shot.png").exists()


def test_symlinked_parent_inside_workspace_is_refused(workspace):
    (workspace / "real").mkdir()
    (workspace / "alias").symlink_to(workspace / "real", target_is_directory=True)

    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("alias/shot.png")


def test_absolute_path_through_an_escaping_symlink_is_refused(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output(str(workspace / "linked" / "shot.png"))


def test_existing_symlink_target_file_is_refused(workspace, tmp_path):
    real = tmp_path / "real.png"
    real.write_bytes(b"png")
    (workspace / "renders").mkdir()
    (workspace / "renders" / "shot.png").symlink_to(real)

    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("renders/shot.png")


def test_folder_is_refused_as_a_file_target(workspace):
    (workspace / "renders" / "shot.png").mkdir(parents=True)

    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output("renders/shot.png")


# ---------------------------------------------------------------------------
# Animation / sequence (directory) output
# ---------------------------------------------------------------------------


def test_animation_output_dir_is_treated_like_a_file_parent(workspace):
    output = render_handler.resolve_render_output("renders/animation/frames", directory=True)

    assert output.absolute == workspace / "renders" / "animation" / "frames"
    assert output.absolute.is_dir()
    assert output.relative == "renders/animation/frames"


def test_animation_output_dir_outside_workspace_is_refused(workspace, tmp_path):
    with pytest.raises(render_handler.RenderOutputPathError):
        render_handler.resolve_render_output(str(tmp_path / "elsewhere"), directory=True)


def test_render_output_prefix_points_at_the_workspace_renders_folder(workspace):
    prefix = render_handler.render_output_prefix()

    assert prefix.absolute == workspace / "renders"
    assert prefix.relative == "renders"


def test_workspace_relative_path_maps_inside_and_outside(workspace, tmp_path):
    assert render_handler.workspace_relative_path(workspace / "renders" / "a.png") == "renders/a.png"
    assert render_handler.workspace_relative_path(tmp_path / "outside" / "a.png") is None


# ---------------------------------------------------------------------------
# Handler results carry the workspace-relative path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_preview_result_names_the_workspace_relative_path(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, render_handler)

    result = await render_handler.render_preview(output_path="renders/out.png", samples=4)

    destination = str(workspace / "renders" / "out.png")
    assert result["success"] is True
    assert result["output_path"] == destination
    assert result["workspace_relative_path"] == "renders/out.png"
    assert destination in scripts[-1]


@pytest.mark.asyncio
async def test_render_preview_empty_path_writes_into_the_workspace(workspace, monkeypatch):
    _mock_executor(monkeypatch, render_handler)

    result = await render_handler.render_preview(samples=4)

    assert result["success"] is True
    assert str(workspace / "renders") in result["output_path"]
    assert result["workspace_relative_path"].startswith("renders/")


@pytest.mark.asyncio
async def test_render_turntable_frame_directory_defaults_into_the_workspace(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, render_handler)

    result = await render_handler.render_turntable(frames=2)

    assert f"Rendered 2-frame turntable animation to {workspace / 'renders'}" in result
    assert " (workspace-relative: renders/" in result
    assert str(workspace) in scripts[-1]


@pytest.mark.asyncio
async def test_render_turntable_refuses_a_folder_outside_the_workspace(workspace, tmp_path, monkeypatch):
    _mock_executor(monkeypatch, render_handler)

    with pytest.raises(render_handler.RenderOutputPathError):
        await render_handler.render_turntable(output_dir=str(tmp_path / "elsewhere"), frames=2)


@pytest.mark.asyncio
async def test_scene_render_settings_point_the_scene_at_the_workspace(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, scene_handler)

    result = await scene_handler.set_render_settings(samples=8)

    prefix = f"{workspace / 'renders'}/"
    assert f"bpy.context.scene.render.filepath = {json.dumps(prefix)}" in scripts[-1]
    assert f"output: {prefix}" in result
    assert "workspace-relative: renders/" in result


@pytest.mark.asyncio
async def test_scene_render_settings_accepts_a_relative_output_dir(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, scene_handler)

    result = await scene_handler.set_render_settings(output_dir="renders/anim")

    prefix = f"{workspace / 'renders' / 'anim'}/"
    assert json.dumps(prefix) in scripts[-1]
    assert "workspace-relative: renders/anim/" in result
    assert (workspace / "renders" / "anim").is_dir()


@pytest.mark.asyncio
async def test_scene_render_settings_refuses_a_folder_outside_the_workspace(workspace, tmp_path, monkeypatch):
    _mock_executor(monkeypatch, scene_handler)

    with pytest.raises(render_handler.RenderOutputPathError):
        await scene_handler.set_render_settings(output_dir=str(tmp_path / "elsewhere"))


@pytest.mark.asyncio
async def test_configure_output_settings_reports_the_workspace_folder(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, rendering_handler)

    result = await rendering_handler.configure_output_settings(output_dir="renders/anim", frame_end=48)

    prefix = f"{workspace / 'renders' / 'anim'}/"
    assert result["status"] == "SUCCESS"
    assert result["output_dir"] == prefix
    assert result["workspace_relative_path"] == "renders/anim/"
    assert json.dumps(prefix) in scripts[-1]
    assert "scene.frame_end = 48" in scripts[-1]


@pytest.mark.asyncio
async def test_configure_output_settings_defaults_into_the_workspace(workspace, monkeypatch):
    scripts = _mock_executor(monkeypatch, rendering_handler)

    result = await rendering_handler.configure_output_settings()

    assert Path(result["output_dir"]).parent == workspace / "renders"
    assert result["workspace_relative_path"].startswith("renders/")
    assert str(workspace) in scripts[-1]


@pytest.mark.asyncio
async def test_configure_output_settings_refuses_a_folder_outside_the_workspace(workspace, tmp_path, monkeypatch):
    _mock_executor(monkeypatch, rendering_handler)

    with pytest.raises(render_handler.RenderOutputPathError):
        await rendering_handler.configure_output_settings(output_dir=str(tmp_path / "elsewhere"))


# ---------------------------------------------------------------------------
# No stray writes in the process CWD
# ---------------------------------------------------------------------------


def test_resolver_never_writes_into_the_process_cwd(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    before = set(os.listdir(cwd))

    render_handler.resolve_render_output("")
    render_handler.resolve_render_output("renders/shot.png")

    assert set(os.listdir(cwd)) == before
