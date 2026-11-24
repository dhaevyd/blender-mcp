"""Bounded, allowlisted cleanup of this server's own scratch directories.

The server creates per-request scratch directories under the system temp dir
(downloads, CAD conversion, Poly Haven / Sketchfab fetches, MCP export bundles,
splat exports, Blender session dirs).  Each call site now removes its own
directory, but a crash, a kill or an old build leaves debris behind, and inside
a long-lived container that debris grows without bound.

This module is the safety net, and it is deliberately conservative:

* only ``tempfile.gettempdir()`` is ever touched, never any other root;
* only top-level entries whose name starts with an explicit allowlisted prefix
  are considered, so unrelated temp files are never removed;
* symlinks are unlinked, never followed;
* an entry is stale only when the newest mtime inside it is older than the
  retention window, so an in-flight Blender session or AI generation is safe;
* every filesystem error is contained per entry -- the sweep always completes
  and always returns counters instead of raising.

Environment:
    MCP_TEMP_MAX_AGE_HOURS: delete debris older than this (default 24, clamped 1..168)
    MCP_TEMP_SWEEP_HOURS:   background sweep interval (default 6, 0 disables sweeps)
    MCP_TRASH_RETENTION_DAYS: gallery trash retention (default 7, clamped 1..90);
                            purged as part of the same scheduled sweep.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Directory-name prefixes this server owns inside the system temp dir.
TEMP_PREFIXES: tuple[str, ...] = (
    "blender_mcp_",  # executor/asset scratch + export bundles
    "blender_mcp_download_",
    "blender_mcp_ai_gen",  # persistent AI-generation output
    "ph_hdri_",
    "ph_tex_",
    "ph_model_",
    "sf_dl_",
    "mcp_export_",
    "gs_resonite_",
    "blender_",  # Blender's own temp session dirs
)

DEFAULT_MAX_AGE_HOURS = 24.0
MIN_MAX_AGE_HOURS = 1.0
MAX_MAX_AGE_HOURS = 168.0  # one week

DEFAULT_SWEEP_HOURS = 6.0
MAX_SWEEP_HOURS = 168.0

#: Upper bound on filesystem entries inspected by one pass (sweep or usage scan).
MAX_SCAN_ENTRIES = 4000
#: Recompute the usage snapshot for diagnostics at most this often.
USAGE_REFRESH_SECONDS = 60.0
#: Delay before the background thread's first sweep.
BOOT_DELAY_SECONDS = 30.0
#: Interval used when the sweep is enabled but the interval parse is unusable.
_FALLBACK_SWEEP_SECONDS = 3600.0

_STATE_LOCK = threading.RLock()
_LAST_SWEEP_MONOTONIC = 0.0
_LAST_SWEEP_RESULT: dict[str, Any] = {}
_LAST_SWEEP_AT: float | None = None
_USAGE_CACHE: dict[str, Any] | None = None
_USAGE_CACHE_AT = 0.0
_SWEEPER_THREAD: threading.Thread | None = None
_SWEEPER_STOP = threading.Event()


# ---------------------------------------------------------------------------
# Configuration and small helpers
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.debug("ignoring non-numeric %s=%r", name, raw)
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return value


def _max_age_hours() -> float:
    """Retention window in hours, clamped to a sane 1..168 range."""
    value = _env_float("MCP_TEMP_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS)
    return min(max(value, MIN_MAX_AGE_HOURS), MAX_MAX_AGE_HOURS)


def _sweep_interval_hours() -> float:
    """Background sweep interval in hours; 0 disables the sweeper."""
    value = _env_float("MCP_TEMP_SWEEP_HOURS", DEFAULT_SWEEP_HOURS)
    if value <= 0:
        return 0.0
    return min(value, MAX_SWEEP_HOURS)


def temp_root() -> Path:
    """The one and only directory tree this module may touch."""
    return Path(os.path.realpath(tempfile.gettempdir()))


def _matches(name: str) -> bool:
    return bool(name) and name.startswith(TEMP_PREFIXES)


def _inside(path: Path, root: Path) -> bool:
    """True only when ``path`` is a direct child of ``root`` or a descendant."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    if resolved == root:
        return False
    return root in resolved.parents


def _entries(path: Path | str) -> list[os.DirEntry[str]]:
    """List one directory; isolated so tests can inject failures."""
    with os.scandir(path) as it:
        return list(it)


def _entry_size(entry: os.DirEntry[str]) -> int:
    try:
        return int(entry.stat(follow_symlinks=False).st_size)
    except OSError:
        return 0


def _newest_mtime(path: Path, budget: int) -> float:
    """Newest mtime inside ``path`` (including itself), bounded by ``budget`` entries."""
    newest = 0.0
    try:
        newest = path.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0
    scanned = 0
    stack = [path]
    while stack and scanned < budget:
        current = stack.pop()
        try:
            children = _entries(current)
        except OSError:
            continue
        for entry in children:
            scanned += 1
            if scanned >= budget:
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
    return newest


def _purge_tree(path: Path, root: Path) -> tuple[int, int, int]:
    """Delete everything under ``path`` without following symlinks.

    Returns ``(files_removed, bytes_freed, errors)``.  Files are removed one by
    one with ``os.remove`` so byte counts are real, then the (now empty) tree is
    removed with ``shutil.rmtree(ignore_errors=True)`` which also covers
    directories that could not be emptied.
    """
    files = 0
    freed = 0
    errors = 0
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        if not _inside(current, root):
            errors += 1
            logger.debug("temp sweep: refusing to descend outside %s", root)
            continue
        try:
            children = _entries(current)
        except OSError as exc:
            errors += 1
            logger.debug("temp sweep: cannot list %s: %s", current, exc)
            continue
        for entry in children:
            try:
                if entry.is_symlink():
                    os.unlink(entry.path)  # unlink the link, never traverse it
                    files += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                size = _entry_size(entry)
                os.remove(entry.path)
                files += 1
                freed += size
            except OSError as exc:
                errors += 1
                logger.debug("temp sweep: cannot remove %s: %s", entry.path, exc)
    shutil.rmtree(path, ignore_errors=True)
    return files, freed, errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sweep_temp_dirs(max_age_hours: float | None = None) -> dict[str, Any]:
    """Remove stale, allowlisted entries directly under the system temp dir.

    Args:
        max_age_hours: Override the retention window (else ``MCP_TEMP_MAX_AGE_HOURS``).

    Returns:
        ``{removed_dirs, removed_files, bytes_freed, skipped, errors, ...}``.
        Never raises: unreadable or vanishing entries are counted in ``errors``
        or ``skipped`` and the sweep continues.
    """
    started = time.monotonic()
    age_hours = _max_age_hours() if max_age_hours is None else float(max_age_hours)
    if age_hours <= 0:
        age_hours = DEFAULT_MAX_AGE_HOURS
    cutoff = time.time() - age_hours * 3600.0
    root = temp_root()

    removed_dirs = 0
    removed_files = 0
    bytes_freed = 0
    skipped = 0
    errors = 0
    scanned = 0

    try:
        top_entries = _entries(root)
    except OSError as exc:
        logger.debug("temp sweep: cannot list %s: %s", root, exc)
        result = {
            "removed_dirs": 0,
            "removed_files": 0,
            "bytes_freed": 0,
            "skipped": 0,
            "errors": 1,
            "root": str(root),
            "max_age_hours": age_hours,
            "error": str(exc),
            "ran_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
            "due": None,
        }
        _record_sweep(result)
        return result

    for entry in top_entries:
        scanned += 1
        if scanned > MAX_SCAN_ENTRIES:
            break
        if not _matches(entry.name):
            continue
        try:
            if entry.is_symlink():
                if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                    skipped += 1
                    continue
                os.unlink(entry.path)
                removed_files += 1
                continue

            if entry.is_dir(follow_symlinks=False):
                target = Path(entry.path)
                if not _inside(target, root):
                    errors += 1
                    continue
                if _newest_mtime(target, MAX_SCAN_ENTRIES) > cutoff:
                    skipped += 1
                    continue
                files, freed, errs = _purge_tree(target, root)
                removed_files += files
                bytes_freed += freed
                errors += errs
                removed_dirs += 1
                continue

            if entry.is_file(follow_symlinks=False):
                if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                    skipped += 1
                    continue
                size = _entry_size(entry)
                os.remove(entry.path)
                removed_files += 1
                bytes_freed += size
                continue

            # Sockets, FIFOs, devices: not ours to touch.
            skipped += 1
        except OSError as exc:
            errors += 1
            logger.debug("temp sweep: entry %s failed: %s", entry.path, exc)

    result = {
        "removed_dirs": removed_dirs,
        "removed_files": removed_files,
        "bytes_freed": bytes_freed,
        "skipped": skipped,
        "errors": errors,
        "root": str(root),
        "max_age_hours": age_hours,
        "scanned": min(scanned, MAX_SCAN_ENTRIES),
        "ran_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        "due": None,
    }
    if removed_dirs or removed_files:
        logger.info(
            "temp sweep removed %d dirs / %d files (%d bytes) from %s",
            removed_dirs,
            removed_files,
            bytes_freed,
            root,
        )
    else:
        logger.debug("temp sweep: nothing to reclaim in %s", root)
    _record_sweep(result)
    return result


def _record_sweep(result: dict[str, Any]) -> None:
    global _LAST_SWEEP_RESULT, _LAST_SWEEP_AT
    with _STATE_LOCK:
        _LAST_SWEEP_RESULT = dict(result)
        _LAST_SWEEP_AT = time.time()


def _scan_usage(root: Path, budget: int = MAX_SCAN_ENTRIES) -> dict[str, Any]:
    """Bounded size/entry accounting for allowlisted top-level entries."""
    budget = max(1, int(budget))
    files = 0
    total = 0
    dirs = 0
    scanned = 0
    truncated = False

    try:
        top_entries = _entries(root)
    except OSError as exc:
        return {"path": str(root), "entries": 0, "bytes": 0, "truncated": False, "error": str(exc)}

    stack: list[Path] = []
    for entry in top_entries:
        scanned += 1
        if scanned >= budget:
            truncated = True
            break
        if not _matches(entry.name):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                dirs += 1
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                files += 1
                total += _entry_size(entry)
        except OSError:
            continue

    while stack and not truncated:
        current = stack.pop()
        try:
            children = _entries(current)
        except OSError:
            continue
        for entry in children:
            scanned += 1
            if scanned >= budget:
                truncated = True
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs += 1
                    stack.append(Path(entry.path))
                    continue
                files += 1
                total += _entry_size(entry)
            except OSError:
                continue

    return {
        "path": str(root),
        "entries": files,
        "bytes": total,
        "dirs": dirs,
        "truncated": truncated,
    }


def temp_usage(max_entries: int | None = None) -> dict[str, Any]:
    """Current footprint of allowlisted temp entries: ``{path, entries, bytes}``.

    The walk is bounded (``max_entries`` or ``MAX_SCAN_ENTRIES``) and never
    follows symlinks, so it is safe to call from a status endpoint.
    """
    root = temp_root()
    usage = _scan_usage(root) if max_entries is None else _scan_usage(root, max_entries)
    usage["max_age_hours"] = _max_age_hours()
    usage["sweep_interval_hours"] = _sweep_interval_hours()
    return usage


def _purge_trash_safely() -> dict[str, Any]:
    """Purge expired Gallery trash entries as part of the scheduled sweep.

    Imported lazily so importing housekeeping never drags in the workspace
    layer, and wrapped so a broken workspace (or a deleted mount) can never
    abort the temp sweep or raise into the sweeper thread.
    """
    try:
        from blender_mcp.utils.projects import purge_trash

        return purge_trash()
    except Exception:
        logger.debug("gallery trash purge failed", exc_info=True)
        return {"removed_entries": 0, "kept_entries": 0, "errors": 1, "retention_days": None}


def sweep_if_due(now: float | None = None) -> dict[str, Any] | None:
    """Run a sweep only when the configured interval has elapsed.

    Returns the sweep result, or ``None`` when it is not due (or sweeps are
    disabled with ``MCP_TEMP_SWEEP_HOURS=0``).  The due-check is guarded by a
    lock and the timestamp is advanced before the sweep starts, so concurrent
    callers never sweep twice.
    """
    interval = _sweep_interval_hours()
    if interval <= 0:
        return None
    moment = time.monotonic() if now is None else float(now)
    global _LAST_SWEEP_MONOTONIC
    with _STATE_LOCK:
        if _LAST_SWEEP_MONOTONIC and moment - _LAST_SWEEP_MONOTONIC < interval * 3600.0:
            return None
        _LAST_SWEEP_MONOTONIC = moment
    try:
        result = sweep_temp_dirs()
    except Exception:  # never let the sweeper raise into a request path
        logger.debug("temp sweep failed", exc_info=True)
        return None
    result["due"] = True
    # The Gallery trash lives in the workspace, not the temp dir, but this is the
    # one recurring sweep the server already runs: piggyback the retention purge.
    result["trash"] = _purge_trash_safely()
    with _STATE_LOCK:
        _LAST_SWEEP_RESULT = dict(result)
    return result


# ---------------------------------------------------------------------------
# Background sweeper
# ---------------------------------------------------------------------------


def _sweeper_loop(stop_event: threading.Event, boot_delay: float) -> None:
    try:
        if stop_event.wait(boot_delay):
            return
    except Exception:  # pragma: no cover - defensive
        return
    while not stop_event.is_set():
        try:
            sweep_if_due()
        except Exception:
            logger.debug("temp sweeper tick failed", exc_info=True)
        interval = _sweep_interval_hours()
        wait_for = interval * 3600.0 if interval > 0 else _FALLBACK_SWEEP_SECONDS
        try:
            if stop_event.wait(wait_for):
                return
        except Exception:  # pragma: no cover - defensive
            return


def start_background_sweeper(boot_delay: float | None = None) -> bool:
    """Start the daemon sweeper once; returns True when this call started it.

    Safe to call from any request path and from module startup: repeated calls
    return False while the thread is alive.
    """
    global _SWEEPER_THREAD
    try:
        if _sweep_interval_hours() <= 0:
            return False
        delay = BOOT_DELAY_SECONDS if boot_delay is None else max(0.0, float(boot_delay))
        with _STATE_LOCK:
            if _SWEEPER_THREAD is not None and _SWEEPER_THREAD.is_alive():
                return False
            _SWEEPER_STOP.clear()
            thread = threading.Thread(
                target=_sweeper_loop,
                args=(_SWEEPER_STOP, delay),
                name="temp-sweeper",
                daemon=True,
            )
            _SWEEPER_THREAD = thread
            thread.start()
            return True
    except Exception:
        logger.debug("could not start temp sweeper", exc_info=True)
        return False


def stop_background_sweeper(timeout: float = 5.0) -> None:
    """Ask the sweeper thread to exit; safe when it never started."""
    global _SWEEPER_THREAD
    with _STATE_LOCK:
        thread, _SWEEPER_THREAD = _SWEEPER_THREAD, None
        _SWEEPER_STOP.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


def reset_schedule() -> None:
    """Forget the last sweep time and cached usage (used by tests and re-config)."""
    global _LAST_SWEEP_MONOTONIC, _USAGE_CACHE, _USAGE_CACHE_AT, _LAST_SWEEP_RESULT, _LAST_SWEEP_AT
    with _STATE_LOCK:
        _LAST_SWEEP_MONOTONIC = 0.0
        _LAST_SWEEP_RESULT = {}
        _LAST_SWEEP_AT = None
        _USAGE_CACHE = None
        _USAGE_CACHE_AT = 0.0


def temp_diagnostics(refresh_seconds: float = USAGE_REFRESH_SECONDS) -> dict[str, Any]:
    """Sizes and sweep stats for the diagnostics endpoint (no filenames)."""
    global _USAGE_CACHE, _USAGE_CACHE_AT
    moment = time.monotonic()
    with _STATE_LOCK:
        cached = _USAGE_CACHE
        age = moment - _USAGE_CACHE_AT if cached is not None else None
    if cached is None or age is None or age >= refresh_seconds:
        try:
            cached = temp_usage()
        except Exception:
            logger.debug("temp usage scan failed", exc_info=True)
            cached = {"path": str(temp_root()), "entries": 0, "bytes": 0, "truncated": False, "error": "unavailable"}
        with _STATE_LOCK:
            _USAGE_CACHE = cached
            _USAGE_CACHE_AT = moment
        age = 0.0

    with _STATE_LOCK:
        last_sweep = dict(_LAST_SWEEP_RESULT)
        last_at = _LAST_SWEEP_AT
        thread = _SWEEPER_THREAD
    return {
        "path": cached.get("path", str(temp_root())),
        "entries": int(cached.get("entries", 0)),
        "dirs": int(cached.get("dirs", 0)),
        "bytes": int(cached.get("bytes", 0)),
        "truncated": bool(cached.get("truncated", False)),
        "usage_age_seconds": round(float(age or 0.0), 1),
        "max_age_hours": _max_age_hours(),
        "sweep_interval_hours": _sweep_interval_hours(),
        "sweeper_running": bool(thread is not None and thread.is_alive()),
        "last_sweep_age_seconds": (round(time.time() - last_at, 1) if last_at else None),
        "last_sweep": {
            key: last_sweep.get(key)
            for key in ("removed_dirs", "removed_files", "bytes_freed", "skipped", "errors", "ran_at")
        }
        if last_sweep
        else None,
    }


def cleanup_temp_dir(path: str | Path | None) -> bool:
    """Remove a ``mkdtemp`` directory immediately; never raises.

    Used in ``finally`` blocks at the call sites so normal operation stops
    leaking.  Only paths inside the system temp dir are ever removed.
    """
    if not path:
        return False
    try:
        target = Path(path)
        root = temp_root()
        if not _inside(target, root):
            logger.debug("refusing to remove temp dir outside %s: %s", root, target)
            return False
        if target.is_symlink():
            os.unlink(target)
            return True
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
    except Exception:
        logger.debug("could not clean up temp dir %s", path, exc_info=True)
        return False
