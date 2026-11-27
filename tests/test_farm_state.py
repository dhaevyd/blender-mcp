from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from blender_mcp.utils.farm_state import FarmJobStore


def test_local_rendering_setting_persists_and_defaults_enabled(tmp_path):
    database = tmp_path / "farm.sqlite3"
    store = FarmJobStore(database)
    assert store.get_local_rendering_enabled() is True
    store.set_local_rendering_enabled(False)
    assert FarmJobStore(database).get_local_rendering_enabled() is False
    store.set_local_rendering_enabled(True)
    assert store.get_local_rendering_enabled() is True


def test_store_rejects_symlink_database_and_key(tmp_path):
    from cryptography.fernet import Fernet

    outside = tmp_path / "outside"
    valid_key = Fernet.generate_key()
    outside.write_bytes(valid_key)
    db_link = tmp_path / "db-link"
    db_link.symlink_to(outside)

    with pytest.raises(ValueError):
        FarmJobStore(db_link)

    store = FarmJobStore(tmp_path / "safe.sqlite3")
    key_link = tmp_path / "farm.key"
    key_link.symlink_to(outside)
    before_mode = outside.stat().st_mode & 0o777
    with pytest.raises((ValueError, OSError)):
        store._fernet()
    assert outside.read_bytes() == valid_key
    assert outside.stat().st_mode & 0o777 == before_mode


def test_database_rejects_symlink_created_after_store_initialization(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not a database")
    database = tmp_path / "farm.sqlite3"
    store = FarmJobStore(database)
    database.unlink()
    database.symlink_to(target)

    with pytest.raises((ValueError, OSError)):
        store.create_job("unsafe", "scene.blend", 1, 1)
    assert target.read_bytes() == b"not a database"


def test_concurrent_first_use_creates_one_complete_private_key(tmp_path):
    path = tmp_path / "farm.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: FarmJobStore(path), range(8)))
    secrets = [store._fernet().decrypt(store._fernet().encrypt(b"ok")) for store in stores]
    assert secrets == [b"ok"] * 8
    if os.name == "posix":
        assert (tmp_path / "farm.key").stat().st_mode & 0o777 == 0o600


def test_job_state_survives_store_reopen_at_workspace_database_path(tmp_path):
    database = tmp_path / ".blender-farm" / "farm.sqlite3"
    first = FarmJobStore(database)
    first.create_job("persistent", "scene.blend", 1, 2)
    first.finish_frame("persistent", 1, success=True, output_path="frame.png")

    reopened = FarmJobStore(database)
    progress = reopened.get_progress("persistent")
    assert progress["completed"] == 1
    assert progress["pending"] == 1
    assert reopened.get_frame_records("persistent")[0]["output_path"] == "frame.png"


def test_default_coordinator_database_persists_in_workspace(monkeypatch, tmp_path):
    import blender_mcp.tools.farm_tools as farm_tools

    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("BLENDER_FARM_DB_PATH", raising=False)
    monkeypatch.setattr(farm_tools, "_FARM", None)
    first = farm_tools._coordinator()
    first.store.create_job("default-path", "scene.blend", 4, 4)
    expected = tmp_path / ".blender-farm" / "farm.sqlite3"
    assert first.store.database_path == expected

    monkeypatch.setattr(farm_tools, "_FARM", None)
    reopened = farm_tools._coordinator()
    assert reopened.store.get_progress("default-path")["pending"] == 1


def test_database_is_created_with_private_permissions(tmp_path):
    database = tmp_path / "farm.sqlite3"
    FarmJobStore(database)
    assert database.is_file()
    if os.name == "posix":
        assert database.stat().st_mode & 0o777 == 0o600


def test_new_database_exists_before_sqlite_connect_with_private_mode(tmp_path, monkeypatch):
    database = tmp_path / "farm.sqlite3"
    original_connect = __import__("sqlite3").connect
    observed_modes = []

    def inspect_before_connect(*args, **kwargs):
        observed_modes.append(database.stat().st_mode & 0o777)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr("blender_mcp.utils.farm_state.sqlite3.connect", inspect_before_connect)
    FarmJobStore(database)
    if os.name == "posix":
        assert observed_modes and all(mode == 0o600 for mode in observed_modes)


def test_job_persists_frame_progress_and_reports_missing_frames(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.create_job("job-1", "scene.blend", 1, 4, metadata={"project_id": "job-1"})
    store.mark_frame_started("job-1", 1, "worker-a")
    store.finish_frame("job-1", 1, success=True, output_path="frames/0001.png")
    store.finish_frame("job-1", 2, success=False, error="render failed")

    progress = store.get_progress("job-1")
    assert progress["completed"] == 1
    assert progress["failed"] == 1
    assert progress["pending"] == 2
    assert progress["percent"] == 25
    assert store.get_missing_frames("job-1") == [2, 3, 4]
    assert store.get_pending_frames("job-1") == [3, 4]

    reopened = FarmJobStore(tmp_path / "farm.sqlite3")
    assert reopened.get_progress("job-1")["completed"] == 1
    assert reopened.get_job_metadata("job-1") == {"project_id": "job-1"}
    assert reopened.get_frame_records("job-1")[0]["output_path"] == "frames/0001.png"


def test_frame_records_persist_worker_logs(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.create_job("job-log", "scene.blend", 1, 1)
    store.mark_frame_started("job-log", 1, "worker-a")
    store.finish_frame("job-log", 1, success=True, output_path="frame.png", log="render finished")

    assert store.get_frame_records("job-log")[0]["log"] == "render finished"


def test_pause_resume_cancel_and_failed_frame_retry(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.create_job("job-2", "scene.blend", 10, 12)
    store.finish_frame("job-2", 10, success=False, error="worker lost")

    assert store.pause_job("job-2") is True
    assert store.get_progress("job-2")["status"] == "paused"
    assert store.resume_job("job-2") is True
    assert store.get_progress("job-2")["status"] == "running"
    assert store.retry_failed_frames("job-2") == [10]
    assert store.get_progress("job-2")["pending"] == 3
    assert store.cancel_job("job-2") is True
    assert store.get_progress("job-2")["status"] == "cancelled"


def test_restart_recovers_running_jobs_as_paused_and_requeues_inflight_frames(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.create_job("job-3", "scene.blend", 1, 2)
    store.mark_frame_started("job-3", 1, "worker-a")

    reopened = FarmJobStore(tmp_path / "farm.sqlite3")
    assert reopened.recover_interrupted_jobs() == ["job-3"]
    progress = reopened.get_progress("job-3")
    assert progress["status"] == "paused"
    assert progress["pending"] == 2
