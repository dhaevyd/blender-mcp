from __future__ import annotations

import pytest

from blender_mcp.tools.farm_worker_tools import completed_frames, resolve_workspace_path


def test_workspace_path_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="outside worker workspace"):
        resolve_workspace_path(tmp_path, "../secret.blend")


def test_completed_frames_reports_only_existing_expected_outputs(tmp_path):
    (tmp_path / "frame_000001.png").write_bytes(b"one")
    (tmp_path / "frame_000003.png").write_bytes(b"three")

    assert completed_frames(tmp_path, "frame_", 1, 4) == [1, 3]
