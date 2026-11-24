"""SQLite-backed GPU telemetry history for the monitoring dashboard.

Samples come from :func:`blender_mcp.utils.farm_workers.gpu_stats` (nvidia-smi)
and are kept for a bounded window (three days by default) so the dashboard can
graph utilisation, memory, temperature, and power. Every failure is swallowed:
telemetry must never break the API or crash the sampler thread.
"""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blender_mcp.utils.farm_workers import gpu_stats

DEFAULT_DB_NAME = "blender-mcp-monitoring.db"
DEFAULT_INTERVAL_SECONDS = 1
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 300
DEFAULT_RETENTION_DAYS = 3
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 14
PRUNE_INTERVAL_SECONDS = 3600
MAX_DB_BYTES = 200 * 1024 * 1024
MAX_POINTS = 180
BASE_BUCKET_SECONDS = 1

# Supported windows mapped to their span in seconds. "live" is the last five
# minutes at raw 15s resolution, i.e. the same span as "5m".
WINDOWS: dict[str, int] = {
    "live": 300,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "3h": 10800,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    gpu_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    util REAL,
    mem_used_mib INTEGER,
    mem_total_mib INTEGER,
    temp_c INTEGER,
    power_w REAL,
    PRIMARY KEY (ts, gpu_index)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
"""

_INSERT_SQL = (
    "INSERT OR IGNORE INTO samples"
    "(ts, gpu_index, name, util, mem_used_mib, mem_total_mib, temp_c, power_w)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_HISTORY_SQL = (
    "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket_ts,"
    " gpu_index,"
    " MAX(name) AS name,"
    " AVG(util) AS util,"
    " AVG(mem_used_mib) AS mem_used_mib,"
    " MAX(mem_total_mib) AS mem_total_mib,"
    " MAX(temp_c) AS temp_c,"
    " AVG(power_w) AS power_w"
    " FROM samples WHERE ts >= ?"
    " GROUP BY bucket_ts, gpu_index"
    " ORDER BY bucket_ts ASC, gpu_index ASC"
)

_STATE_LOCK = threading.Lock()
_SAMPLER_THREAD: threading.Thread | None = None
_SAMPLER_STOP = threading.Event()
_LAST_PRUNE = 0.0


def _now() -> float:
    """Wall clock seam; tests replace this to build synthetic spans."""
    return time.time()


def _db_path() -> Path:
    raw = os.environ.get("MCP_MONITOR_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(tempfile.gettempdir()) / DEFAULT_DB_NAME


def _clamped_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _interval_seconds() -> int:
    return _clamped_int("MCP_MONITOR_INTERVAL", DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)


def _retention_days() -> int:
    return _clamped_int(
        "MCP_MONITOR_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, MIN_RETENTION_DAYS, MAX_RETENTION_DAYS
    )


def _bucket_seconds(window_seconds: int) -> int:
    """Smallest 15s multiple that fits ``window_seconds`` into ~180 points."""
    steps = math.ceil(window_seconds / MAX_POINTS / BASE_BUCKET_SECONDS)
    return max(BASE_BUCKET_SECONDS, steps * BASE_BUCKET_SECONDS)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(_SCHEMA)
    return db


@contextmanager
def _session() -> Iterator[sqlite3.Connection]:
    db = _connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _as_float(value: Any, digits: int | None = None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number)


def record_samples(stats: list[dict[str, Any]], ts: float | None = None) -> int:
    """Insert one row per GPU for ``ts``; duplicate (ts, gpu) rows are ignored."""
    if not stats:
        return 0
    moment = _now() if ts is None else float(ts)
    rows: list[tuple[Any, ...]] = []
    for index, gpu in enumerate(stats):
        if not isinstance(gpu, dict):
            continue
        rows.append(
            (
                moment,
                index,
                str(gpu.get("name") or "GPU"),
                _as_float(gpu.get("utilization_percent")),
                _as_int(gpu.get("memory_used_mib")),
                _as_int(gpu.get("memory_total_mib")),
                _as_int(gpu.get("temperature_c")),
                _as_float(gpu.get("power_draw_w")),
            )
        )
    if not rows:
        return 0
    try:
        with _session() as db:
            before = db.total_changes
            db.executemany(_INSERT_SQL, rows)
            return db.total_changes - before
    except Exception:
        return 0


def sample_once(ts: float | None = None) -> int:
    """Take one nvidia-smi reading and persist it; empty readings write nothing."""
    try:
        return record_samples(gpu_stats(), ts=ts)
    except Exception:
        return 0


def _database_size_bytes() -> int:
    path = _db_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += (path.parent / (path.name + suffix)).stat().st_size
        except OSError:
            continue
    return total


def _enforce_size_cap(db: sqlite3.Connection) -> int:
    """Delete the oldest rows when the database file grows past the soft cap."""
    size = _database_size_bytes()
    if size <= MAX_DB_BYTES:
        return 0
    total = int(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
    if total <= 0:
        return 0
    doomed = max(1, int(total * (1 - MAX_DB_BYTES / size)) + 1)
    cursor = db.execute(
        "DELETE FROM samples WHERE rowid IN ("
        " SELECT rowid FROM samples ORDER BY ts ASC, gpu_index ASC LIMIT ?)",
        (doomed,),
    )
    return max(cursor.rowcount, 0)


def prune_history(now: float | None = None) -> int:
    """Drop samples older than the retention window; returns rows removed."""
    moment = _now() if now is None else float(now)
    cutoff = moment - _retention_days() * 86400
    try:
        with _session() as db:
            removed = db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            return max(removed, 0) + _enforce_size_cap(db)
    except Exception:
        return 0


def _maybe_prune() -> None:
    """Rate-limit retention work to once per hour, from the sampler only."""
    global _LAST_PRUNE
    moment = _now()
    with _STATE_LOCK:
        if moment - _LAST_PRUNE < PRUNE_INTERVAL_SECONDS:
            return
        _LAST_PRUNE = moment
    prune_history(moment)


def _sampler_loop(stop_event: threading.Event) -> None:
    # Sampling starts one interval after startup: boot stays cheap (no
    # nvidia-smi call at import time) and the first point lands within
    # MCP_MONITOR_INTERVAL seconds. A 1s default supports live monitoring;
    # the database size cap and aggregation bound storage and query cost.
    while not stop_event.wait(_interval_seconds()):
        try:
            sample_once()
            _maybe_prune()
        except Exception:
            # Monitoring is best-effort: never let one bad tick kill the thread.
            continue


def start_sampler() -> bool:
    """Start the daemon sampler once; returns True when this call started it."""
    global _SAMPLER_THREAD
    with _STATE_LOCK:
        if _SAMPLER_THREAD is not None and _SAMPLER_THREAD.is_alive():
            return False
        _SAMPLER_STOP.clear()
        thread = threading.Thread(target=_sampler_loop, args=(_SAMPLER_STOP,), name="gpu-monitor", daemon=True)
        _SAMPLER_THREAD = thread
        thread.start()
        return True


def stop_sampler(timeout: float = 5.0) -> None:
    """Ask the sampler thread to exit; safe to call when it never started."""
    global _SAMPLER_THREAD
    with _STATE_LOCK:
        thread, _SAMPLER_THREAD = _SAMPLER_THREAD, None
        _SAMPLER_STOP.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


def _history_rows(cutoff: float, bucket: int) -> list[sqlite3.Row]:
    with _session() as db:
        return db.execute(_HISTORY_SQL, (bucket, bucket, cutoff)).fetchall()


def _serialize_gpus(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    gpus: dict[int, dict[str, Any]] = {}
    for row in rows:
        index = int(row["gpu_index"])
        entry = gpus.get(index)
        if entry is None:
            entry = {
                "index": index,
                "name": str(row["name"] or "GPU"),
                "memory_total_mib": None,
                "series": [],
            }
            gpus[index] = entry
        if row["name"]:
            entry["name"] = str(row["name"])
        total = _as_int(row["mem_total_mib"])
        if total is not None and (entry["memory_total_mib"] is None or total > entry["memory_total_mib"]):
            entry["memory_total_mib"] = total
        entry["series"].append(
            {
                "t": int(row["bucket_ts"]),
                "util": _as_float(row["util"], 2),
                "mem_used_mib": _as_int(row["mem_used_mib"]),
                "temp_c": _as_int(row["temp_c"]),
                "power_w": _as_float(row["power_w"], 1),
            }
        )
    for entry in gpus.values():
        # Bucket alignment can straddle one extra partial bucket; cap hard at 180.
        entry["series"] = entry["series"][-MAX_POINTS:]
    return [gpus[index] for index in sorted(gpus)]


def gpu_history(window: str = "live") -> dict[str, Any]:
    """Return bucketed GPU history for a window; raises ValueError when unknown."""
    key = str(window if window is not None else "").strip().lower()
    window_seconds = WINDOWS.get(key)
    if window_seconds is None:
        raise ValueError(f"invalid window: {window!r}")

    bucket = _bucket_seconds(window_seconds)
    retention = _retention_days()
    moment = _now()
    cutoff = max(moment - window_seconds, moment - retention * 86400)
    try:
        rows = _history_rows(cutoff, bucket)
    except Exception:
        rows = []
    return {
        "window": key,
        "bucket_seconds": bucket,
        "retention_days": retention,
        "gpus": _serialize_gpus(rows),
        "sampled_at": datetime.fromtimestamp(moment, UTC).isoformat(),
    }
