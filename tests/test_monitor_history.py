"""Tests for SQLite-backed GPU telemetry history and its dashboard route."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from blender_mcp.server import _ApiKeyMiddleware
from blender_mcp.utils import monitor_history
from blender_mcp.utils.studio_api import register_gallery_routes

GPU_A = {
    "name": "NVIDIA GeForce RTX 3060",
    "memory_total_mib": 12288,
    "memory_used_mib": 2019,
    "utilization_percent": 41,
    "temperature_c": 61,
    "power_draw_w": 88.4,
}
GPU_B = {
    "name": "Tesla T4",
    "memory_total_mib": 15360,
    "memory_used_mib": 1234,
    "utilization_percent": 10,
    "temperature_c": 55,
    "power_draw_w": None,
}

BUCKETS = {
    "live": (300, 2),
    "5m": (300, 2),
    "15m": (900, 5),
    "30m": (1800, 10),
    "1h": (3600, 20),
    "3h": (10800, 60),
    "6h": (21600, 120),
    "12h": (43200, 240),
    "1d": (86400, 480),
    "3d": (259200, 1440),
}


@pytest.fixture(autouse=True)
def monitor_db(monkeypatch, tmp_path):
    """Point every test at a throwaway DB and keep the real sampler quiet."""
    path = tmp_path / "monitor.db"
    monkeypatch.setenv("MCP_MONITOR_DB", str(path))
    monkeypatch.delenv("MCP_MONITOR_INTERVAL", raising=False)
    monkeypatch.delenv("MCP_MONITOR_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(monitor_history, "gpu_stats", lambda: [])
    return path


def _insert_rows(path, rows):
    db = sqlite3.connect(path)
    try:
        db.executescript(monitor_history._SCHEMA)
        db.executemany(monitor_history._INSERT_SQL, rows)
        db.commit()
    finally:
        db.close()


def _read_rows(path):
    db = sqlite3.connect(path)
    try:
        return db.execute(
            "SELECT ts, gpu_index, name, util, mem_used_mib, mem_total_mib, temp_c, power_w"
            " FROM samples ORDER BY ts, gpu_index"
        ).fetchall()
    finally:
        db.close()


def test_sample_tick_inserts_one_row_per_gpu_and_skips_empty(monkeypatch, monitor_db):
    monkeypatch.setattr(monitor_history, "gpu_stats", lambda: [GPU_A, GPU_B])

    assert monitor_history.sample_once(ts=1000.0) == 2
    rows = _read_rows(monitor_db)
    assert [row[1] for row in rows] == [0, 1]
    assert rows[0][2] == GPU_A["name"]
    assert rows[0][3] == 41.0
    assert rows[1][7] is None  # missing power stays NULL, never NaN

    monkeypatch.setattr(monitor_history, "gpu_stats", lambda: [])
    assert monitor_history.sample_once(ts=1015.0) == 0
    assert len(_read_rows(monitor_db)) == 2


def test_duplicate_tick_is_ignored(monkeypatch, monitor_db):
    monkeypatch.setattr(monitor_history, "gpu_stats", lambda: [GPU_A])

    assert monitor_history.sample_once(ts=2000.0) == 1
    assert monitor_history.sample_once(ts=2000.0) == 0
    assert len(_read_rows(monitor_db)) == 1


def test_prune_removes_expired_rows_and_keeps_recent(monitor_db):
    now = 1_700_000_000.0
    _insert_rows(
        monitor_db,
        [
            (now - 4 * 86400, 0, "expired", 1.0, 1, 1, 1, 1.0),
            (now - 2 * 86400, 0, "kept", 2.0, 2, 2, 2, 2.0),
            (now - 60, 0, "fresh", 3.0, 3, 3, 3, 3.0),
        ],
    )

    assert monitor_history.prune_history(now=now) == 1
    assert [row[2] for row in _read_rows(monitor_db)] == ["kept", "fresh"]


def test_prune_honours_retention_env(monkeypatch, monitor_db):
    monkeypatch.setenv("MCP_MONITOR_RETENTION_DAYS", "1")
    now = 1_700_000_000.0
    _insert_rows(
        monitor_db,
        [
            (now - 2 * 86400, 0, "expired", 1.0, 1, 1, 1, 1.0),
            (now - 3600, 0, "fresh", 2.0, 2, 2, 2, 2.0),
        ],
    )

    assert monitor_history.prune_history(now=now) == 1
    assert [row[2] for row in _read_rows(monitor_db)] == ["fresh"]


def test_prune_runs_at_most_once_per_hour(monkeypatch, monitor_db):
    calls = []
    monkeypatch.setattr(monitor_history, "prune_history", lambda now: calls.append(now) or 0)
    monkeypatch.setattr(monitor_history, "_LAST_PRUNE", 0.0)
    monkeypatch.setattr(monitor_history, "_now", lambda: 10_000.0)

    monitor_history._maybe_prune()
    monitor_history._maybe_prune()

    assert calls == [10_000.0]


def test_size_cap_deletes_oldest_rows(monkeypatch, monitor_db):
    now = 1_700_000_000.0
    total = 5000
    _insert_rows(
        monitor_db,
        [(now - total + index, 0, "GPU", 1.0, 1, 1, 1, 1.0) for index in range(total)],
    )
    monkeypatch.setattr(monitor_history, "MAX_DB_BYTES", max(1, monitor_history._database_size_bytes() // 2))

    removed = monitor_history.prune_history(now=now)

    remaining = _read_rows(monitor_db)
    assert removed == total - len(remaining) > 0
    assert remaining[-1][0] == now - 1  # newest data survives
    assert remaining[0][0] > now - total  # oldest rows were dropped first


def test_bucket_sizes_return_at_most_180_points(monkeypatch, monitor_db):
    now = 1_700_000_000.0
    monkeypatch.setattr(monitor_history, "_now", lambda: now)
    span = 3 * 86400
    _insert_rows(
        monitor_db,
        [(now - span + step * 15, 0, "GPU", 50.0, 100, 200, 60, 10.0) for step in range(span // 15 + 1)],
    )

    for window, (seconds, expected_bucket) in BUCKETS.items():
        payload = monitor_history.gpu_history(window)
        assert payload["window"] == window
        assert payload["bucket_seconds"] == expected_bucket, window
        assert payload["bucket_seconds"] == monitor_history._bucket_seconds(seconds)
        assert payload["retention_days"] == 3
        assert 1 <= len(payload["gpus"][0]["series"]) <= 180, window


def test_aggregation_averages_util_and_takes_max_temperature(monkeypatch, monitor_db):
    now = 1_700_000_000.0
    monkeypatch.setattr(monitor_history, "_now", lambda: now)
    bucket_start = int(now // 30 * 30) - 300
    _insert_rows(
        monitor_db,
        [
            (bucket_start + 5, 0, GPU_A["name"], 40.0, 1000, 24564, 55, 100.0),
            (bucket_start + 15, 0, GPU_A["name"], 60.0, 2000, 24564, 70, 120.0),
        ],
    )

    payload = monitor_history.gpu_history("1h")
    series = payload["gpus"][0]["series"]

    assert len(series) == 1
    assert series[0]["t"] == bucket_start
    assert series[0]["util"] == 50.0
    assert series[0]["mem_used_mib"] == 1500
    assert series[0]["temp_c"] == 70
    assert series[0]["power_w"] == 110.0
    assert payload["gpus"][0]["memory_total_mib"] == 24564


def test_missing_values_serialize_as_null(monkeypatch, monitor_db):
    now = 1_700_000_000.0
    monkeypatch.setattr(monitor_history, "_now", lambda: now)
    _insert_rows(monitor_db, [(now - 60, 0, "GPU", None, None, None, None, None)])

    payload = monitor_history.gpu_history("live")
    point = payload["gpus"][0]["series"][0]

    assert point == {
        "t": int((now - 60) // 2 * 2),
        "util": None,
        "mem_used_mib": None,
        "temp_c": None,
        "power_w": None,
    }
    assert payload["gpus"][0]["memory_total_mib"] is None


@pytest.mark.parametrize("bad", ["", "  ", "2h", "forever", "15 s", "1dd", None, 5])
def test_invalid_window_raises_value_error(bad):
    with pytest.raises(ValueError):
        monitor_history.gpu_history(bad)


def test_window_lookup_is_case_insensitive(monkeypatch, monitor_db):
    monkeypatch.setattr(monitor_history, "_now", lambda: 1_700_000_000.0)

    assert monitor_history.gpu_history("1H")["window"] == "1h"
    assert monitor_history.gpu_history(" LiVe ")["window"] == "live"


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("MCP_MONITOR_INTERVAL", None, 1),
        ("MCP_MONITOR_INTERVAL", "0", 1),
        ("MCP_MONITOR_INTERVAL", "60", 60),
        ("MCP_MONITOR_INTERVAL", "9999", 300),
        ("MCP_MONITOR_INTERVAL", "nonsense", 1),
        ("MCP_MONITOR_RETENTION_DAYS", "0", 1),
        ("MCP_MONITOR_RETENTION_DAYS", "30", 14),
        ("MCP_MONITOR_RETENTION_DAYS", "7", 7),
    ],
)
def test_env_clamping(monkeypatch, name, raw, expected):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)

    value = monitor_history._interval_seconds() if name == "MCP_MONITOR_INTERVAL" else monitor_history._retention_days()
    assert value == expected


def test_missing_database_directory_degrades_to_empty(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("MCP_MONITOR_DB", str(blocker / "monitor.db"))
    monkeypatch.setattr(monitor_history, "gpu_stats", lambda: [GPU_A])

    payload = monitor_history.gpu_history("live")

    assert payload["gpus"] == []
    assert payload["bucket_seconds"] == 2
    assert monitor_history.sample_once(ts=1.0) == 0
    assert monitor_history.prune_history(now=2.0) == 0


def test_sampler_starts_once_and_is_daemonic(monitor_db):
    monitor_history.stop_sampler()
    try:
        assert monitor_history.start_sampler() is True
        assert monitor_history.start_sampler() is False
        thread = monitor_history._SAMPLER_THREAD
        assert thread is not None
        assert thread.is_alive() is True
        assert thread.daemon is True
    finally:
        monitor_history.stop_sampler()
    assert monitor_history._SAMPLER_THREAD is None


def test_route_requires_auth_and_serves_documented_shape(monkeypatch, monitor_db):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-token")
    now = 1_700_000_000.0
    monkeypatch.setattr(monitor_history, "_now", lambda: now)
    _insert_rows(monitor_db, [(now - 20, 0, GPU_A["name"], 41.2, 8123, 24564, 61, 88.4)])

    app = Starlette(middleware=[Middleware(_ApiKeyMiddleware)])
    register_gallery_routes(app)
    headers = {"Authorization": "Bearer test-token"}
    try:
        with TestClient(app) as client:
            unauthenticated = client.get("/api/v1/monitoring/gpu")
            rejected = client.get("/api/v1/monitoring/gpu", headers={"Authorization": "Bearer wrong-key"})
            live = client.get("/api/v1/monitoring/gpu?window=live", headers=headers)
            default = client.get("/api/v1/monitoring/gpu", headers=headers)
            invalid = client.get("/api/v1/monitoring/gpu?window=2h", headers=headers)
    finally:
        monitor_history.stop_sampler()

    assert unauthenticated.status_code == 401
    assert rejected.status_code == 401
    assert live.status_code == 200
    assert live.headers["cache-control"] == "private, no-store"
    payload = live.json()
    assert set(payload) == {"window", "bucket_seconds", "retention_days", "gpus", "sampled_at"}
    assert payload["window"] == "live"
    assert payload["bucket_seconds"] == 2
    assert payload["retention_days"] == 3
    datetime.fromisoformat(payload["sampled_at"])
    gpu = payload["gpus"][0]
    assert gpu["index"] == 0
    assert gpu["name"] == GPU_A["name"]
    assert gpu["memory_total_mib"] == 24564
    point = gpu["series"][0]
    assert set(point) == {"t", "util", "mem_used_mib", "temp_c", "power_w"}
    assert point["util"] == 41.2 and point["mem_used_mib"] == 8123
    assert point["temp_c"] == 61 and point["power_w"] == 88.4
    assert default.json()["window"] == "live"
    assert invalid.status_code == 400
    assert invalid.json() == {"error": "invalid window"}
