"""Temp-housekeeping sweeper: safety rules, counters and call-site cleanup.

Every test runs against an isolated directory installed as ``tempfile.tempdir``,
so the real system temp dir is never touched and the assertions are exact.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from blender_mcp.utils import housekeeping as hk


@pytest.fixture(autouse=True)
def isolated_temp(monkeypatch, tmp_path):
    """Point the temp dir at a private directory and keep the sweeper quiet."""
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.setenv("MCP_TEMP_SWEEP_HOURS", "0")  # no background thread in tests
    monkeypatch.delenv("MCP_TEMP_MAX_AGE_HOURS", raising=False)
    hk.stop_background_sweeper()
    hk.reset_schedule()
    yield root
    hk.stop_background_sweeper()
    hk.reset_schedule()


def _age(path: Path, hours: float) -> None:
    """Backdate a file or a whole tree."""
    stamp = time.time() - hours * 3600.0
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    for target in sorted(targets, key=lambda p: len(p.parts), reverse=True):
        os.utime(target, (stamp, stamp), follow_symlinks=False)


def _dir(root: Path, name: str, payload: bytes = b"x" * 32, age_hours: float | None = None) -> Path:
    path = root / name
    path.mkdir()
    (path / "payload.bin").write_bytes(payload)
    (path / "nested").mkdir()
    (path / "nested" / "deep.bin").write_bytes(b"y" * 16)
    if age_hours is not None:
        _age(path, age_hours)
    return path


# ---------------------------------------------------------------------------
# Sweep rules
# ---------------------------------------------------------------------------


def test_stale_dir_removed_and_fresh_dir_kept(isolated_temp: Path) -> None:
    stale = _dir(isolated_temp, "blender_mcp_stale", age_hours=48)
    fresh = _dir(isolated_temp, "blender_mcp_fresh", age_hours=1)

    result = hk.sweep_temp_dirs()

    assert not stale.exists()
    assert fresh.is_dir()
    assert result["removed_dirs"] == 1
    assert result["removed_files"] == 2  # payload.bin + nested/deep.bin
    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_symlink_is_unlinked_but_never_traversed(isolated_temp: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside-temp")
    keep = outside / "keep.bin"
    keep.write_bytes(b"precious")
    link = isolated_temp / "blender_mcp_link"
    link.symlink_to(outside, target_is_directory=True)
    old = time.time() - 72 * 3600
    os.utime(link, (old, old), follow_symlinks=False)

    result = hk.sweep_temp_dirs()

    assert not link.is_symlink()
    assert not link.exists()
    assert keep.read_bytes() == b"precious"  # the target was never traversed
    assert result["removed_files"] == 1
    assert result["removed_dirs"] == 0


def test_non_allowlisted_entries_are_untouched(isolated_temp: Path) -> None:
    unrelated = _dir(isolated_temp, "some-other-app", age_hours=500)
    near_miss = _dir(isolated_temp, "blender-mcp-gallery-index", age_hours=500)
    index_file = isolated_temp / "blender-mcp-gallery-index.json"
    index_file.write_text("{}", encoding="utf-8")
    _age(index_file, 500)

    result = hk.sweep_temp_dirs()

    assert unrelated.is_dir()
    assert near_miss.is_dir()
    assert index_file.is_file()
    assert result["removed_dirs"] == 0
    assert result["removed_files"] == 0


def test_unreadable_entry_does_not_abort_the_sweep(isolated_temp: Path, monkeypatch) -> None:
    bad = _dir(isolated_temp, "blender_mcp_bad", age_hours=48)
    good = _dir(isolated_temp, "blender_mcp_good", age_hours=48)
    real_entries = hk._entries

    def flaky(path):
        if Path(path) == bad:
            raise PermissionError("entry is unreadable")
        return real_entries(path)

    monkeypatch.setattr(hk, "_entries", flaky)

    result = hk.sweep_temp_dirs()  # must not raise

    assert good.exists() is False
    assert result["errors"] >= 1
    assert result["removed_dirs"] >= 1


def test_usage_and_bytes_are_reported_sensibly(isolated_temp: Path) -> None:
    _dir(isolated_temp, "ph_tex_a", payload=b"a" * 100)
    _dir(isolated_temp, "sf_dl_b", payload=b"b" * 250)
    _dir(isolated_temp, "unrelated", payload=b"c" * 999)

    usage = hk.temp_usage()

    assert usage["path"] == str(isolated_temp)
    assert usage["entries"] == 4  # 2 dirs x (payload + nested/deep) counted as files
    assert usage["bytes"] == 100 + 16 + 250 + 16
    assert usage["dirs"] == 4  # two top-level dirs plus their nested dirs

    _age(isolated_temp / "ph_tex_a", 48)
    _age(isolated_temp / "sf_dl_b", 48)
    result = hk.sweep_temp_dirs(max_age_hours=1)
    assert result["removed_dirs"] == 2
    assert result["removed_files"] == 4
    assert result["bytes_freed"] == 100 + 16 + 250 + 16


def test_sweep_never_deletes_outside_the_temp_root(isolated_temp: Path, tmp_path_factory) -> None:
    canary_root = tmp_path_factory.mktemp("not-the-temp-root")
    canary = _dir(canary_root, "blender_mcp_canary", age_hours=1000)

    result = hk.sweep_temp_dirs()

    assert canary.is_dir()
    assert (canary / "payload.bin").is_file()
    assert result["removed_dirs"] == 0
    # cleanup_temp_dir refuses the same path too
    assert hk.cleanup_temp_dir(canary) is False
    assert canary.is_dir()


def test_cleanup_temp_dir_removes_only_inside_the_root(isolated_temp: Path) -> None:
    inside = _dir(isolated_temp, "gs_resonite_x")
    assert hk.cleanup_temp_dir(inside) is True
    assert not inside.exists()


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_sweep_if_due_respects_interval(isolated_temp: Path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEMP_SWEEP_HOURS", "6")
    _dir(isolated_temp, "mcp_export_x", age_hours=48)

    first = hk.sweep_if_due(now=1000.0)
    assert first is not None
    assert first["removed_dirs"] == 1
    assert hk.sweep_if_due(now=1000.0 + 60) is None  # inside the interval
    assert hk.sweep_if_due(now=1000.0 + 6 * 3600 - 1) is None

    _dir(isolated_temp, "mcp_export_y", age_hours=48)
    later = hk.sweep_if_due(now=1000.0 + 6 * 3600 + 1)
    assert later is not None
    assert later["removed_dirs"] == 1


def test_sweep_if_due_zero_hours_disables_sweeps(isolated_temp: Path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEMP_SWEEP_HOURS", "0")
    stale = _dir(isolated_temp, "blender_mcp_stale", age_hours=200)

    assert hk.sweep_if_due() is None
    assert stale.is_dir()
    assert hk.start_background_sweeper(boot_delay=0) is False


def test_max_age_env_is_clamped(isolated_temp: Path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEMP_MAX_AGE_HOURS", "0.1")
    assert hk._max_age_hours() == 1.0
    monkeypatch.setenv("MCP_TEMP_MAX_AGE_HOURS", "10000")
    assert hk._max_age_hours() == 168.0
    monkeypatch.setenv("MCP_TEMP_MAX_AGE_HOURS", "nonsense")
    assert hk._max_age_hours() == 24.0


# ---------------------------------------------------------------------------
# Diagnostics surface
# ---------------------------------------------------------------------------


def test_diagnostics_reports_sizes_without_filenames(isolated_temp: Path) -> None:
    stale = _dir(isolated_temp, "blender_mcp_secret_project", payload=b"z" * 512)
    _age(stale, 48)
    hk.sweep_temp_dirs(max_age_hours=1)

    payload = hk.temp_diagnostics(refresh_seconds=0)

    assert payload["path"] == str(isolated_temp)
    assert payload["entries"] == 0
    assert payload["bytes"] == 0
    assert payload["last_sweep"]["removed_dirs"] == 1
    assert payload["max_age_hours"] == 24.0
    assert "secret_project" not in json.dumps(payload)


def test_server_diagnostics_includes_temp_block(monkeypatch) -> None:
    from blender_mcp import server

    monkeypatch.setenv("MCP_TEMP_SWEEP_HOURS", "0")
    result = server._system_diagnostics()

    assert "temp" in result
    assert isinstance(result["temp"]["bytes"], int)
    assert isinstance(result["temp"]["entries"], int)
    assert result["temp"]["sweep_interval_hours"] == 0.0
    json.dumps(result)  # must stay JSON-serialisable


# ---------------------------------------------------------------------------
# Call sites stop leaking at the source
# ---------------------------------------------------------------------------


def _leftovers(root: Path, prefix: str) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith(prefix))


def test_download_call_site_leaves_nothing_behind_on_success(isolated_temp: Path, monkeypatch) -> None:
    from blender_mcp.tools import download_tools
    from blender_mcp.utils import blender_executor

    async def fake_download(url, output_path, timeout=30):
        Path(output_path).write_bytes(b"x" * 64)
        return True

    class _Executor:
        async def execute_script(self, script, *args, **kwargs):
            return "SUCCESS: Imported glTF file: model.glb"

    monkeypatch.setattr(download_tools, "_download_file", fake_download)
    monkeypatch.setattr(blender_executor, "get_blender_executor", lambda *a, **k: _Executor())

    message = asyncio.run(download_tools.blender_download(url="https://example.com/model.glb"))

    assert "Imported glTF file" in message
    assert _leftovers(isolated_temp, "blender_mcp_download_") == []


def test_download_call_site_leaves_nothing_behind_on_exception(isolated_temp: Path, monkeypatch) -> None:
    from blender_mcp.tools import download_tools
    from blender_mcp.utils import blender_executor

    async def fake_download(url, output_path, timeout=30):
        Path(output_path).write_bytes(b"x" * 64)
        return True

    class _BrokenExecutor:
        async def execute_script(self, script, *args, **kwargs):
            raise RuntimeError("blender bridge down")

    monkeypatch.setattr(download_tools, "_download_file", fake_download)
    monkeypatch.setattr(blender_executor, "get_blender_executor", lambda *a, **k: _BrokenExecutor())

    message = asyncio.run(download_tools.blender_download(url="https://example.com/model.glb"))

    assert "Error downloading file" in message
    assert _leftovers(isolated_temp, "blender_mcp_download_") == []


def test_cad_import_call_site_leaves_nothing_behind(isolated_temp: Path, monkeypatch, tmp_path) -> None:
    from blender_mcp.app import app
    from blender_mcp.handlers import import_handler
    from blender_mcp.tools.import_tools import import_tools

    cad_file = tmp_path / "part.step"
    cad_file.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")

    tool = asyncio.run(app.get_tool("blender_import"))

    # Conversion failure path.
    failed = asyncio.run(tool.fn(operation="import_cad", filepath=str(cad_file)))
    assert "Failed to convert CAD file" in failed
    assert _leftovers(isolated_temp, "blender_mcp_cad_") == []

    # Success path: conversion writes the mesh, the importer reports success.
    async def fake_conversion(cad_path, output_path, cad_format, mesh_quality, scale_factor):
        Path(output_path).write_text("v 0 0 0\n", encoding="utf-8")
        return True

    async def fake_import(filepath, file_format, **kwargs):
        assert Path(filepath).is_file(), "converted mesh must exist while importing"
        return {"status": "SUCCESS", "objects": 1}

    monkeypatch.setattr(import_tools, "_direct_cad_conversion", fake_conversion)
    monkeypatch.setattr(import_handler, "import_file", fake_import)

    ok = asyncio.run(tool.fn(operation="import_cad", filepath=str(cad_file)))
    assert "CAD import successful" in ok
    assert _leftovers(isolated_temp, "blender_mcp_cad_") == []
