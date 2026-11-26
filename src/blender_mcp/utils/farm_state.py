"""Persistent coordinator state for distributed Blender render jobs."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any


class FarmJobStore:
    """SQLite-backed job/frame ledger; safe to reopen after coordinator restarts."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        key_path = self.database_path.with_name("farm.key")
        for target in (self.database_path, key_path):
            if target.is_symlink():
                raise ValueError("farm database and key must not be symlinks")
            if target.exists() and not target.is_file():
                raise ValueError("farm database and key must be regular files")

        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS farm_jobs (
                    id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    frame_start INTEGER NOT NULL,
                    frame_end INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
                );
                CREATE TABLE IF NOT EXISTS farm_frames (
                    job_id TEXT NOT NULL REFERENCES farm_jobs(id) ON DELETE CASCADE,
                    frame INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    worker_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    output_path TEXT,
                    error TEXT,
                    log TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                    PRIMARY KEY (job_id, frame)
                );
                CREATE INDEX IF NOT EXISTS idx_farm_frames_status
                    ON farm_frames(job_id, status, frame);
                CREATE TABLE IF NOT EXISTS managed_workers (
                    worker_id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                    workspace TEXT NOT NULL, slots INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    encrypted_key BLOB NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
                );
                CREATE TABLE IF NOT EXISTS farm_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )
            # Serialize schema inspection and migration across concurrent openers.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(farm_jobs)")}
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE farm_jobs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            frame_columns = {row["name"] for row in db.execute("PRAGMA table_info(farm_frames)")}
            if "log" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN log TEXT NOT NULL DEFAULT ''")
            if "gpu_name" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN gpu_name TEXT")
            if "gpu_index" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN gpu_index INTEGER")
            if "started_at" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN started_at REAL")
            if "finished_at" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN finished_at REAL")
            if "reassigned_count" not in frame_columns:
                db.execute("ALTER TABLE farm_frames ADD COLUMN reassigned_count INTEGER NOT NULL DEFAULT 0")
            if "wait_reason" not in columns:
                db.execute("ALTER TABLE farm_jobs ADD COLUMN wait_reason TEXT")
            if "priority" not in columns:
                db.execute("ALTER TABLE farm_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS farm_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL DEFAULT (unixepoch('subsec')), actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL)")
            worker_columns = {row["name"] for row in db.execute("PRAGMA table_info(managed_workers)")}
            if "verified" not in worker_columns:
                db.execute("ALTER TABLE managed_workers ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        if self.database_path.exists() and not self.database_path.is_symlink():
            os.chmod(self.database_path, 0o600)

    def _fernet(self):
        from cryptography.fernet import Fernet

        key_path = self.database_path.with_name("farm.key")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(key_path, flags)
        except FileNotFoundError:
            fd, temporary = tempfile.mkstemp(dir=key_path.parent, prefix=".farm-key-")
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, key_path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
            fd = os.open(key_path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("farm key must be a regular file")
            if os.name == "posix" and info.st_mode & 0o077:
                raise ValueError("farm key permissions must be 0600")
            return Fernet(os.read(fd, 4096))
        finally:
            os.close(fd)

    def save_managed_worker(self, record: dict[str, Any], api_key: str) -> None:
        encrypted = self._fernet().encrypt(api_key.encode())
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO managed_workers(worker_id,name,url,workspace,slots,enabled,encrypted_key,verified,updated_at) VALUES(?,?,?,?,?,?,?,0,unixepoch('subsec'))",
                (
                    record["worker_id"],
                    record["name"],
                    record["url"],
                    record["workspace"],
                    record["slots"],
                    int(record["enabled"]),
                    encrypted,
                ),
            )

    def list_managed_workers(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT worker_id,name,url,workspace,slots,enabled,verified FROM managed_workers ORDER BY name"
                )
            ]

    def get_managed_worker(self, worker_id: str) -> dict[str, Any] | None:
        return next((row for row in self.list_managed_workers() if row["worker_id"] == worker_id), None)

    def get_managed_worker_secret(self, worker_id: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT encrypted_key FROM managed_workers WHERE worker_id=?", (worker_id,)).fetchone()
        if row is None:
            raise KeyError(worker_id)
        try:
            return self._fernet().decrypt(bytes(row["encrypted_key"])).decode()
        except Exception as exc:
            raise ValueError(
                f"Worker {worker_id!r}: could not decrypt stored API key — farm.key may have changed. "
                "Re-enroll the worker and update its BLENDER_MCP_WORKER_KEY in .env."
            ) from exc

    def update_managed_worker(self, worker_id: str, fields: dict[str, Any]) -> bool:
        allowed = {"name", "url", "workspace", "slots", "enabled", "verified"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return self.get_managed_worker(worker_id) is not None
        for flag in ("enabled", "verified"):
            if flag in fields:
                fields[flag] = int(fields[flag])
        with self._connect() as db:
            assignments = ",".join(f"{key}=?" for key in fields if fields[key] is not None)
            values = [value for value in fields.values() if value is not None]
            return (
                db.execute(
                    f"UPDATE managed_workers SET {assignments},updated_at=unixepoch('subsec') WHERE worker_id=?",
                    (*values, worker_id),
                ).rowcount
                == 1
            )

    def get_local_rendering_enabled(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM farm_settings WHERE key='local_rendering_enabled'").fetchone()
        return row is None or row["value"] == "true"

    def set_local_rendering_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("local_rendering_enabled must be a boolean")
        with self._connect() as db:
            db.execute(
                "INSERT INTO farm_settings(key,value) VALUES('local_rendering_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if enabled else "false",),
            )

    def delete_managed_worker(self, worker_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM managed_workers WHERE worker_id=?", (worker_id,)).rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        if self.database_path.is_symlink():
            raise ValueError("farm database must not be a symlink")
        if not self.database_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.database_path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise ValueError("farm database must be a regular file")
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        deadline = time.monotonic() + 30
        while True:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    db.close()
                    raise
                time.sleep(0.05)
        return db

    def create_job(
        self,
        job_id: str,
        project_path: str,
        frame_start: int,
        frame_end: int,
        *,
        metadata: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> None:
        if not job_id.strip() or not project_path.strip():
            raise ValueError("job_id and project_path are required")
        if frame_start < 0 or frame_end < frame_start:
            raise ValueError("invalid frame range")
        with self._connect() as db:
            db.execute(
                "INSERT INTO farm_jobs(id, project_path, frame_start, frame_end, status, metadata_json, priority) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (job_id, project_path, frame_start, frame_end, json.dumps(metadata or {}, sort_keys=True), int(priority)),
            )
            db.executemany(
                "INSERT INTO farm_frames(job_id, frame) VALUES (?, ?)",
                ((job_id, frame) for frame in range(frame_start, frame_end + 1)),
            )

    def record_audit(self, actor: str, action: str, target: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO farm_audit(actor, action, target) VALUES (?, ?, ?)", (actor, action, target))

    def recent_audit(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM farm_audit ORDER BY id DESC LIMIT 100")]

    def set_wait_reason(self, job_id: str, reason: str | None) -> None:
        with self._connect() as db:
            db.execute("UPDATE farm_jobs SET wait_reason=? WHERE id=? AND wait_reason IS NOT ?", (reason, job_id, reason))

    def mark_frame_started(
        self, job_id: str, frame: int, worker_id: str, *, gpu_name: str | None = None, gpu_index: int | None = None
    ) -> None:
        with self._connect() as db:
            job = db.execute("SELECT status FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] != "running":
                raise ValueError(f"job is {job['status']}")
            result = db.execute(
                "UPDATE farm_frames SET status='running', worker_id=?, gpu_name=?, gpu_index=?, "
                "attempts=attempts+1, error=NULL, started_at=unixepoch('subsec'), updated_at=unixepoch('subsec') "
                "WHERE job_id=? AND frame=? AND status IN ('pending','failed')",
                (worker_id, gpu_name, gpu_index, job_id, frame),
            )
            if result.rowcount != 1:
                raise ValueError(f"frame {frame} is not pending or failed")

    def finish_frame(
        self,
        job_id: str,
        frame: int,
        *,
        success: bool,
        output_path: str | None = None,
        error: str | None = None,
        log: str = "",
    ) -> None:
        status = "completed" if success else "failed"
        with self._connect() as db:
            job = db.execute("SELECT status FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] == "cancelled":
                return
            result = db.execute(
                "UPDATE farm_frames SET status=?, output_path=?, error=?, log=?, "
                "finished_at=unixepoch('subsec'), updated_at=unixepoch('subsec') WHERE job_id=? AND frame=?",
                (
                    status,
                    output_path if success else None,
                    None if success else (error or "render failed"),
                    log,
                    job_id,
                    frame,
                ),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown frame {job_id}:{frame}")
            self._refresh_job_status(db, job_id)

    @staticmethod
    def _refresh_job_status(db: sqlite3.Connection, job_id: str) -> None:
        counts = db.execute(
            "SELECT COUNT(*) AS total, SUM(status='completed') AS completed, "
            "SUM(status='failed') AS failed, SUM(status='cancelled') AS cancelled "
            "FROM farm_frames WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if counts is None:
            raise KeyError(job_id)
        if counts["total"] == 0 or counts["completed"] + counts["failed"] + counts["cancelled"] < counts["total"]:
            return
        status = "completed" if counts["completed"] == counts["total"] else "failed"
        db.execute("UPDATE farm_jobs SET status=?, updated_at=unixepoch('subsec') WHERE id=?", (status, job_id))

    def get_job_metadata(self, job_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT metadata_json FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            value = json.loads(row["metadata_json"])
            return value if isinstance(value, dict) else {}

    def set_job_metadata_field(self, job_id: str, key: str, value: Any) -> None:
        with self._connect() as db:
            row = db.execute("SELECT metadata_json FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            meta = json.loads(row["metadata_json"])
            if not isinstance(meta, dict):
                meta = {}
            meta[key] = value
            db.execute("UPDATE farm_jobs SET metadata_json=? WHERE id=?", (json.dumps(meta), job_id))

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, project_path, frame_start, frame_end, status, priority, created_at, updated_at "
                "FROM farm_jobs ORDER BY priority DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_pending_frames(self, job_id: str) -> list[int]:
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if exists is None:
                raise KeyError(job_id)
            rows = db.execute(
                "SELECT frame FROM farm_frames WHERE job_id=? AND status='pending' ORDER BY frame", (job_id,)
            ).fetchall()
            return [int(row["frame"]) for row in rows]

    def get_missing_frames(self, job_id: str) -> list[int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT frame FROM farm_frames WHERE job_id=? AND status!='completed' ORDER BY frame", (job_id,)
            ).fetchall()
            if not db.execute("SELECT 1 FROM farm_jobs WHERE id=?", (job_id,)).fetchone():
                raise KeyError(job_id)
            return [int(row["frame"]) for row in rows]

    def get_frame_records(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if exists is None:
                raise KeyError(job_id)
            rows = db.execute(
                "SELECT frame, status, worker_id, gpu_name, gpu_index, attempts, reassigned_count, "
                "output_path, error, log, started_at, finished_at, updated_at "
                "FROM farm_frames WHERE job_id=? ORDER BY frame",
                (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def requeue_frame(self, job_id: str, frame: int) -> None:
        """Return a frame to pending without incrementing attempts; increments reassigned_count."""
        with self._connect() as db:
            job = db.execute("SELECT status FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] == "cancelled":
                return
            db.execute(
                "UPDATE farm_frames SET status='pending', worker_id=NULL, error=NULL, "
                "reassigned_count=reassigned_count+1, updated_at=unixepoch('subsec') "
                "WHERE job_id=? AND frame=? AND status='running'",
                (job_id, frame),
            )

    def get_progress(self, job_id: str) -> dict[str, Any]:
        with self._connect() as db:
            job = db.execute(
                "SELECT id, project_path, frame_start, frame_end, status, priority, wait_reason, created_at, updated_at FROM farm_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            counts = db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(status='completed') AS completed, SUM(status='failed') AS failed, "
                "SUM(status='running') AS running, SUM(status='pending') AS pending "
                "FROM farm_frames WHERE job_id=?",
                (job_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            completed = int(counts["completed"] or 0)
            # Live estimate: a rendering frame counts for the share of the average
            # completed-frame time it has already used, capped below 100% so the bar
            # cannot finish before the frame does.
            now = float(db.execute("SELECT unixepoch('subsec') AS now").fetchone()["now"])
            average = db.execute(
                "SELECT AVG(updated_at - started_at) AS seconds FROM farm_frames "
                "WHERE job_id=? AND status='completed' AND started_at IS NOT NULL AND updated_at >= started_at",
                (job_id,),
            ).fetchone()["seconds"]
            partial = 0.0
            if average and average > 0:
                for row in db.execute(
                    "SELECT started_at FROM farm_frames WHERE job_id=? AND status='running' AND started_at IS NOT NULL",
                    (job_id,),
                ).fetchall():
                    partial += min(0.95, max(0.0, (now - row["started_at"]) / average))
            if not total or completed == total:
                percent = 100
            else:
                percent = min(99, round((completed + partial) * 100 / total))
            avg_frame_seconds = round(float(average), 2) if average and average > 0 else None
            remaining = int(counts["pending"] or 0) + int(counts["running"] or 0)
            eta_seconds = round(avg_frame_seconds * remaining, 1) if avg_frame_seconds else None
            return {
                **dict(job),
                "total_frames": total,
                "completed": completed,
                "failed": int(counts["failed"] or 0),
                "running": int(counts["running"] or 0),
                "pending": int(counts["pending"] or 0),
                "percent": percent,
                "avg_frame_seconds": avg_frame_seconds,
                "eta_seconds": eta_seconds,
                "missing_frames": self.get_missing_frames(job_id),
            }

    def pause_job(self, job_id: str) -> bool:
        return self._set_job_status(job_id, "running", "paused")

    def resume_job(self, job_id: str) -> bool:
        return self._set_job_status(job_id, "paused", "running")

    def cancel_job(self, job_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE farm_jobs SET status='cancelled', updated_at=unixepoch('subsec') WHERE id=? AND status IN ('running','paused')",
                (job_id,),
            )
            if result.rowcount:
                db.execute(
                    "UPDATE farm_frames SET status='cancelled', updated_at=unixepoch('subsec') "
                    "WHERE job_id=? AND status IN ('pending','running')",
                    (job_id,),
                )
            return result.rowcount == 1

    def _set_job_status(self, job_id: str, current: str, new: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE farm_jobs SET status=?, updated_at=unixepoch('subsec') WHERE id=? AND status=?",
                (new, job_id, current),
            )
            return result.rowcount == 1

    def recover_interrupted_jobs(self) -> list[str]:
        """Mark orphaned coordinator jobs paused and make in-flight frames resumable."""
        with self._connect() as db:
            rows = db.execute("SELECT id FROM farm_jobs WHERE status='running' ORDER BY created_at").fetchall()
            job_ids = [str(row["id"]) for row in rows]
            if job_ids:
                db.execute(
                    "UPDATE farm_jobs SET status='paused', updated_at=unixepoch('subsec') WHERE status='running'"
                )
                db.execute(
                    "UPDATE farm_frames SET status='pending', updated_at=unixepoch('subsec') WHERE status='running'"
                )
            return job_ids

    def retry_failed_frames(self, job_id: str, max_retries: int = 3) -> list[int]:
        with self._connect() as db:
            job = db.execute("SELECT status FROM farm_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in ("failed", "running"):
                raise ValueError(f"cannot retry frames when job is {job['status']}")
            rows = db.execute(
                "SELECT frame, attempts FROM farm_frames WHERE job_id=? AND status='failed' ORDER BY frame", (job_id,)
            ).fetchall()
            # Only retry frames that haven't hit the attempt ceiling.
            frames = [int(row["frame"]) for row in rows if int(row["attempts"]) < max_retries]
            exhausted = [int(row["frame"]) for row in rows if int(row["attempts"]) >= max_retries]
            if exhausted:
                db.execute(
                    f"UPDATE farm_frames SET error='exceeded max retries ({max_retries})', updated_at=unixepoch('subsec') "
                    "WHERE job_id=? AND status='failed' AND attempts>=?",
                    (job_id, max_retries),
                )
            if frames:
                db.executemany(
                    "UPDATE farm_frames SET status='pending', worker_id=NULL, output_path=NULL, error=NULL, updated_at=unixepoch('subsec') "
                    "WHERE job_id=? AND frame=? AND status='failed'",
                    [(job_id, f) for f in frames],
                )
                db.execute(
                    "UPDATE farm_jobs SET status='running', updated_at=unixepoch('subsec') WHERE id=?", (job_id,)
                )
            return frames
