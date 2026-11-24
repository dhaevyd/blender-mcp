"""Workspace-confined project and media operations for the Studio API."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

_ALLOWED = {".png": "image/png", ".mp4": "video/mp4"}

# "Date added" is first-seen time, not a filesystem birth time: Linux overlayfs
# and ext4 do not expose a meaningful creation timestamp. First sight is kept in
# a small JSON index that deliberately lives outside the browsed workspace, so
# browsing stays read-only and never writes next to the user's media.
_GALLERY_INDEX_VERSION = 1
_GALLERY_INDEX_MAX_BYTES = 4 * 1024 * 1024
_GALLERY_INDEX_MAX_ENTRIES = 50_000
_GALLERY_INDEX_PRUNE_OVER = 2_000
_GALLERY_INDEX_PRUNE_CHECKS = 8_000
_GALLERY_INDEX_PRUNE_INTERVAL = 32
_DEFAULT_GALLERY_INDEX_NAME = "blender-mcp-gallery-index.json"

_gallery_index_lock = threading.Lock()
_gallery_index_requests = 0

_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")

# Synthetic gallery id surfacing top-level loose media (.png/.mp4) under the
# workspace root; its assets are the loose files at the root (non-recursive).
_WORKSPACE_ROOT_ID = "workspace-root"


def workspace_root() -> Path:
    root = Path(os.environ.get("BLENDER_WORKSPACE", "/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_project_id(project_id: str) -> str:
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("invalid project id")
    return project_id


def validate_workspace_entry_name(name: str) -> str:
    """Validate a raw workspace-root entry name (top-level folder or loose file).

    Raises the same message as ``validate_project_id`` because the gallery route
    exposes both structured projects and workspace entries under ``project_id``.
    """
    # Hidden entries stay invisible: browse/search already skip dotted names, so
    # addressing them here (e.g. ".trash") produced URLs the gallery could not open.
    if not name or name.startswith(".") or name in ("projects",):
        raise ValueError("invalid project id")
    if "/" in name or "\\" in name:
        raise ValueError("invalid project id")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("invalid project id")
    return name


def humanize_name(name: str) -> str:
    """Turn a machine-ish name into a readable title, preserving letter casing."""
    return re.sub(r"[_\-\s]+", " ", name).strip()


def safe_project(root: Path, project_id: str) -> Path:
    validate_project_id(project_id)
    base = root.resolve()
    projects = base / "projects"
    candidate = projects / project_id
    if projects.is_symlink() or candidate.is_symlink():
        raise ValueError("project path contains a symlink")
    candidate = candidate.resolve()
    if base not in candidate.parents:
        raise ValueError("project path escapes workspace")
    return candidate


def create_project(name: str) -> dict[str, str]:
    clean = " ".join(name.split())
    if not clean or len(clean) > 100:
        raise ValueError("project name must contain 1-100 characters")
    root = workspace_root()
    projects = root / "projects"
    projects.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", clean).strip("-_").lower()[:48] or "project"
    project_id = f"{slug}-{uuid.uuid4().hex[:12]}"
    directory = safe_project(root, project_id)
    directory.mkdir()
    (directory / ".project.json").write_text(json.dumps({"name": clean}), encoding="utf-8")
    return {"id": project_id, "name": clean, "path": f"projects/{project_id}"}


def _resolve_gallery_directory(root: Path, target_id: str) -> tuple[Path, bool]:
    """Resolve a gallery id to a directory under the workspace root.

    Returns ``(directory, is_loose)``. ``is_loose`` is True only for the
    synthetic workspace-root entry whose assets are top-level loose media.
    Existing ``projects/<id>`` ids take precedence so their behaviour is
    unchanged; otherwise the id must name a real direct child of the root.
    """
    base = root.resolve()
    if target_id == _WORKSPACE_ROOT_ID:
        return base, True
    if _PROJECT_ID_RE.fullmatch(target_id):
        projects = base / "projects"
        candidate = projects / target_id
        if not projects.is_symlink() and not candidate.is_symlink():
            resolved = candidate.resolve()
            if base in resolved.parents and resolved.is_dir():
                return resolved, False
    validate_workspace_entry_name(target_id)
    candidate = base / target_id
    if candidate.is_symlink():
        raise ValueError("workspace entry is a symlink")
    resolved = candidate.resolve()
    if base not in resolved.parents:
        raise ValueError("workspace entry escapes workspace")
    return resolved, False


def list_projects() -> list[dict[str, Any]]:
    root = workspace_root()
    base = root.resolve()
    result: list[dict[str, Any]] = []
    projects_dir = base / "projects"
    if projects_dir.is_dir() and not projects_dir.is_symlink():
        for entry in projects_dir.iterdir():
            if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
                continue
            try:
                safe_project(base, entry.name)
            except ValueError:
                continue
            metadata = entry / ".project.json"
            try:
                name = json.loads(metadata.read_text(encoding="utf-8")).get("name", entry.name)
            except (OSError, ValueError, AttributeError):
                name = entry.name
            result.append({"id": entry.name, "name": name, "type": "project", "path": f"projects/{entry.name}"})
    loose_media = False
    for entry in base.iterdir():
        # Dotted entries are workspace state (.trash, .blender-farm, staging dirs),
        # never gallery content, and the browser refuses to open them.
        if entry.is_symlink() or entry.name.startswith("."):
            continue
        if entry == projects_dir:
            continue
        if entry.is_dir():
            result.append({"id": entry.name, "name": humanize_name(entry.name), "type": "folder", "path": entry.name})
        elif entry.is_file() and entry.suffix.lower() == ".blend":
            result.append({"id": entry.name, "name": humanize_name(entry.stem), "type": "project", "path": entry.name})
        elif entry.is_file() and entry.suffix.lower() in _ALLOWED:
            loose_media = True
    if loose_media:
        result.append({"id": _WORKSPACE_ROOT_ID, "name": "Workspace", "type": "folder", "path": "."})
    return sorted(result, key=lambda item: item["name"].lower())


def list_assets(project_id: str) -> list[dict[str, Any]]:
    root = workspace_root()
    directory, loose = _resolve_gallery_directory(root, project_id)
    if not directory.exists():
        raise FileNotFoundError("project not found")
    if not directory.is_dir():
        return []
    files = []
    entries = directory.iterdir() if loose else directory.rglob("*")
    for item in entries:
        suffix = item.suffix.lower()
        if item.is_symlink():
            continue
        # Never descend through a symlinked directory, even if rglob yields it.
        if item.is_dir():
            continue
        if suffix not in _ALLOWED or not item.is_file():
            continue
        resolved = item.resolve()
        if directory not in resolved.parents:
            continue
        relative = item.relative_to(directory).as_posix()
        files.append(
            {
                "name": relative,
                "type": "video" if suffix == ".mp4" else "image",
                "content_type": _ALLOWED[suffix],
                "url": f"/api/v1/gallery/projects/{quote(project_id, safe='')}/assets/{quote(relative, safe='/')}",
            }
        )
    return sorted(files, key=lambda item: item["name"].lower())


def latest_media(media_type: str | None = None) -> dict[str, Any] | None:
    """Return the most recently modified media file across the workspace."""
    root = workspace_root()
    base = root.resolve()

    ids: list[str] = [_WORKSPACE_ROOT_ID]
    for item in list_projects():
        project_id = str(item.get("id") or "")
        # Belt and braces: hidden ids are workspace state, never gallery media.
        if (
            project_id
            and not project_id.startswith(".")
            and project_id != _WORKSPACE_ROOT_ID
            and item.get("type") in {"project", "folder"}
        ):
            ids.append(project_id)

    best_mtime_ns: int | None = None
    best: dict[str, Any] | None = None
    for project_id in ids:
        try:
            assets = list_assets(project_id)
            directory, _loose = _resolve_gallery_directory(base, project_id)
        except (ValueError, FileNotFoundError, OSError):
            continue
        for asset in assets:
            if media_type is not None and asset["type"] != media_type:
                continue
            path = directory / asset["name"]
            if path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if best_mtime_ns is None or info.st_mtime_ns > best_mtime_ns:
                best_mtime_ns = info.st_mtime_ns
                best = {
                    "name": path.name,
                    "type": asset["type"],
                    "url": asset["url"],
                    "project_id": project_id,
                    "modified": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
                    "revision": f"{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}",
                }
    return best


def resolve_asset(project_id: str, filename: str) -> tuple[Path, str]:
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() not in _ALLOWED:
        raise ValueError("unsupported asset")
    root = workspace_root()
    project = safe_project(root, project_id)
    target = project / relative
    if any(part.is_symlink() for part in [project, *list(target.parents)[:-1], target]) or not target.is_file() or project not in target.resolve().parents:
        raise FileNotFoundError("asset not found")
    return target, _ALLOWED[target.suffix.lower()]


def open_asset(project_id: str, filename: str) -> tuple[int, str, int, str]:
    """Open media beneath the workspace using no-follow directory descriptors."""
    relative = Path(filename)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.suffix.lower() not in _ALLOWED:
        raise ValueError("unsupported asset")
    root = workspace_root()
    directory, loose = _resolve_gallery_directory(root, project_id)
    # Loose workspace-root media are top-level only.
    if loose and len(relative.parts) > 1:
        raise ValueError("unsupported asset")
    try:
        components = directory.relative_to(root.resolve()).parts
    except ValueError:
        raise FileNotFoundError("asset not found")
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        fd = os.open(root, flags_dir)
        descriptors.append(fd)
        for component in (*components, *relative.parts[:-1]):
            fd = os.open(component, flags_dir, dir_fd=fd)
            descriptors.append(fd)
        file_fd = os.open(relative.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptors[-1])
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise FileNotFoundError("asset not found")
        return file_fd, _ALLOWED[relative.suffix.lower()], info.st_size, relative.name
    except OSError as exc:
        raise FileNotFoundError("asset not found") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


# This browser is deliberately independent of the legacy project/media gallery.
# Only these exact suffixes may be rendered inline; SVG/HTML and other binaries
# always download as attachments.
_BROWSE_INLINE = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
    ".txt": "text/plain", ".log": "text/plain", ".py": "text/plain",
    ".json": "text/plain", ".md": "text/plain", ".csv": "text/plain",
    ".js": "text/plain", ".ts": "text/plain", ".tsx": "text/plain",
    ".css": "text/plain", ".sh": "text/plain", ".yaml": "text/plain",
    ".yml": "text/plain",
}


def _browser_parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(path, str) or (not path and not allow_root):
        raise ValueError("invalid workspace path")
    if not path:
        return ()
    parts = path.split("/")
    if (path.startswith("/") or "\\" in path or any(
        part in {"", ".", ".."} or part.startswith(".") or
        any(unicodedata.category(char) == "Cc" for char in part)
        for part in parts
    )):
        raise ValueError("invalid workspace path")
    return tuple(parts)


def _open_browser_directory(parts: tuple[str, ...], root: Path | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(workspace_root() if root is None else root, flags)
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            finally:
                os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        raise FileNotFoundError("workspace folder not found") from exc


def gallery_index_path() -> Path:
    """Location of the first-seen index, always outside the browsed workspace."""
    override = (os.environ.get("MCP_GALLERY_INDEX_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / _DEFAULT_GALLERY_INDEX_NAME


def _iso_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _valid_index_stamp(value: Any) -> str | None:
    """Accept only ISO-8601 timestamps with an explicit offset."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if stamp.tzinfo is not None else None


def _load_gallery_index(index_file: Path) -> dict[str, str]:
    """Read the index, treating missing, corrupt or oversized files as empty."""
    try:
        if index_file.stat().st_size > _GALLERY_INDEX_MAX_BYTES:
            return {}
        raw = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _GALLERY_INDEX_VERSION:
        return {}
    stored = raw.get("files")
    if not isinstance(stored, dict):
        return {}
    files: dict[str, str] = {}
    for key, value in stored.items():
        if len(files) >= _GALLERY_INDEX_MAX_ENTRIES:
            break
        stamp = _valid_index_stamp(value)
        if not isinstance(key, str) or not key or stamp is None:
            continue
        if key.startswith("/") or "\\" in key or ".." in key.split("/"):
            continue
        files[key] = stamp
    return files


def _write_gallery_index(index_file: Path, files: dict[str, str]) -> None:
    """Persist atomically (temp file + rename) with owner-only permissions."""
    index_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": _GALLERY_INDEX_VERSION, "files": files}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{index_file.name}.", suffix=".tmp", dir=index_file.parent)
    try:
        try:
            os.fchmod(fd, 0o600)
            while payload:
                payload = payload[os.write(fd, payload) :]
        finally:
            os.close(fd)
        os.replace(temporary, index_file)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _prune_gallery_index(base: Path, files: dict[str, str], offset: int = 0) -> bool:
    """Drop entries whose workspace-relative path no longer names a regular file.

    Only a bounded slice is checked per pass; ``offset`` rotates the window so a
    large index is still covered over successive passes.
    """
    removed = False
    keys = list(files)
    total = len(keys)
    for index in range(min(_GALLERY_INDEX_PRUNE_CHECKS, total)):
        relative = keys[(offset + index) % total]
        parts = relative.split("/")
        if not relative or relative.startswith("/") or "\\" in relative or ".." in parts:
            del files[relative]
            removed = True
            continue
        try:
            info = os.lstat(base.joinpath(*parts))
        except OSError:
            del files[relative]
            removed = True
            continue
        if not stat.S_ISREG(info.st_mode):
            del files[relative]
            removed = True
    return removed


def _record_added(base: Path, records: list[tuple[str, float]]) -> dict[str, str]:
    """Return first-seen stamps for ``(workspace-relative path, mtime)`` records.

    New files are stamped with their current mtime the first time they are
    listed; later calls reuse the stored value even if the file is touched.
    Every index failure degrades to the file's mtime so browsing still works.
    """
    resolved = {relative: _iso_mtime(mtime) for relative, mtime in records}
    if not records:
        return resolved
    index_file = gallery_index_path()
    try:
        base_resolved = base.resolve()
        index_resolved = index_file.resolve()
    except (OSError, RuntimeError):
        return resolved
    # The workspace is a read-only promise: never create the index inside it.
    if index_resolved == base_resolved or base_resolved in index_resolved.parents:
        return resolved
    global _gallery_index_requests
    with _gallery_index_lock:
        _gallery_index_requests += 1
        files = _load_gallery_index(index_file)
        changed = False
        for relative, mtime in records:
            stamp = files.get(relative)
            if stamp is None:
                stamp = _iso_mtime(mtime)
                files[relative] = stamp
                changed = True
            resolved[relative] = stamp
        if len(files) > _GALLERY_INDEX_PRUNE_OVER or _gallery_index_requests % _GALLERY_INDEX_PRUNE_INTERVAL == 0:
            offset = _gallery_index_requests * _GALLERY_INDEX_PRUNE_CHECKS
            changed = _prune_gallery_index(base_resolved, files, offset) or changed
        if changed:
            try:
                _write_gallery_index(index_file, files)
            except OSError:
                pass  # An unwritable index must never fail a browse or search.
    return resolved


def _with_added(entries: list[dict[str, Any]], records: list[tuple[str, float]]) -> None:
    """Attach ``added`` to listing entries: folders get null, files get a stamp."""
    base = workspace_root().resolve()
    stamps = _record_added(base, records)
    for entry in entries:
        entry["added"] = None if entry.get("type") == "folder" else stamps.get(entry["path"]) or entry["modified"]


def browse_workspace(path: str = "") -> list[dict[str, Any]]:
    """List one folder by descriptor without exposing hidden state or symlinks."""
    parts = _browser_parts(path, allow_root=True)
    fd = _open_browser_directory(parts)
    try:
        entries = []
        records: list[tuple[str, float]] = []
        for name in os.listdir(fd):
            if name.startswith(".") or any(unicodedata.category(char) == "Cc" for char in name):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            is_folder = stat.S_ISDIR(info.st_mode)
            relative = "/".join((*parts, name))
            entries.append({
                "name": name, "path": relative,
                "type": "folder" if is_folder else "file",
                "size": info.st_size if not is_folder else None,
                "modified": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
                "format": "Folder" if is_folder else (Path(name).suffix[1:].upper() or "File"),
                "preview": "none" if is_folder else (
                    "image" if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                    else "video" if Path(name).suffix.lower() in {".mp4", ".webm"}
                    else "text" if _BROWSE_INLINE.get(Path(name).suffix.lower()) == "text/plain"
                    else "none"
                ),
            })
            if not is_folder:
                records.append((relative, info.st_mtime))
        _with_added(entries, records)
        return sorted(entries, key=lambda item: (item["type"] != "folder", item["name"].casefold()))
    finally:
        os.close(fd)


def _sorted_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order search hits by path so truncated and complete results agree."""
    return sorted(matches, key=lambda item: item["path"].casefold())


def search_workspace(query: str, *, max_results: int = 100, max_entries: int = 5000) -> dict[str, Any]:
    """Search visible names across the workspace with bounded, no-follow walks."""
    if (not isinstance(query, str) or not query.strip() or len(query) > 100
            or "/" in query or "\\" in query
            or any(unicodedata.category(char) == "Cc" for char in query)):
        raise ValueError("invalid search query")
    if max_results < 1 or max_entries < 1:
        raise ValueError("invalid search limit")
    needle = query.strip().casefold()
    pending: list[tuple[str, ...]] = [()]
    matches: list[dict[str, Any]] = []
    records: list[tuple[str, float]] = []

    def finish(truncated: bool) -> dict[str, Any]:
        _with_added(matches, records)
        return {"entries": _sorted_matches(matches), "truncated": truncated}

    visited = 0
    while pending:
        parts = pending.pop()
        try:
            fd = _open_browser_directory(parts)
        except FileNotFoundError:
            continue  # A directory was removed between listing and opening.
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = entry.name
                    if visited >= max_entries:
                        return finish(True)
                    visited += 1
                    if name.startswith(".") or "\\" in name or any(unicodedata.category(char) == "Cc" for char in name):
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        continue
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        continue
                    path = "/".join((*parts, name))
                    if needle in name.casefold():
                        if len(matches) >= max_results:
                            return finish(True)
                        is_folder = stat.S_ISDIR(info.st_mode)
                        suffix = Path(name).suffix.lower()
                        matches.append({
                            "name": name, "path": path, "type": "folder" if is_folder else "file",
                            "size": None if is_folder else info.st_size,
                            "modified": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
                            "format": "Folder" if is_folder else (suffix[1:].upper() or "File"),
                            "preview": "none" if is_folder else (
                                "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"}
                                else "video" if suffix in {".mp4", ".webm"}
                                else "text" if _BROWSE_INLINE.get(suffix) == "text/plain"
                                else "none"
                            ),
                        })
                        if not is_folder:
                            records.append((path, info.st_mtime))
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((*parts, name))
        finally:
            os.close(fd)
    return finish(False)


def open_workspace_file(path: str) -> tuple[int, os.stat_result, str]:
    """Open a regular file under workspace with no-follow at every component."""
    parts = _browser_parts(path)
    fd = _open_browser_directory(parts[:-1])
    try:
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as exc:
            raise FileNotFoundError("workspace file not found") from exc
    finally:
        os.close(fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError("workspace file not found")
        return file_fd, info, _BROWSE_INLINE.get(Path(parts[-1]).suffix.lower(), "application/octet-stream")
    except BaseException:
        os.close(file_fd)
        raise


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------
# Deleting is the only irreversible gesture in the Gallery, so it is a move into
# ``<workspace>/.trash/<utc-stamp>-<id>/`` alongside a tiny ``entry.json``. The
# same no-follow discipline as browse/search applies: the path is validated
# lexically by ``_browser_parts``, directories are opened one component at a
# time with ``O_NOFOLLOW``, and a symlink is moved as a link -- never traversed.

_TRASH_DIRNAME = ".trash"
_TRASH_ENTRY_NAME = "entry.json"
_TRASH_ENTRY_MAX_BYTES = 16 * 1024
#: One and only accepted shape for a trash id: no separators, no dots, no dots-dots.
_TRASH_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
DEFAULT_TRASH_RETENTION_DAYS = 7.0
MIN_TRASH_RETENTION_DAYS = 1.0
MAX_TRASH_RETENTION_DAYS = 90.0


class TrashRefused(ValueError):
    """A delete/restore was refused although the request itself was well formed.

    Subclasses ``ValueError`` so callers that already treat workspace path
    problems as validation errors keep working; the API maps it to 409 because
    the name exists and is understood, it just must not be touched.
    """


def _trash_base(workspace: str | Path | None) -> Path:
    """Resolve the workspace root without creating it (purge must be inert)."""
    raw = workspace if workspace is not None else os.environ.get("BLENDER_WORKSPACE", "/workspace")
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("invalid workspace root") from exc


def _trash_workspace_parts(rel_path: str) -> tuple[str, ...]:
    """Validate a workspace-relative path for deletion, reusing ``_browser_parts``."""
    if not isinstance(rel_path, str):
        raise TrashRefused("path is not a deletable workspace path")
    if rel_path == _TRASH_DIRNAME or rel_path.startswith(_TRASH_DIRNAME + "/"):
        raise TrashRefused("the trash folder cannot be deleted")
    try:
        parts = _browser_parts(rel_path)
    except ValueError:
        # Absolute paths, "..", ".", hidden components and control characters.
        raise TrashRefused("path is not a deletable workspace path") from None
    if not parts:
        raise TrashRefused("the workspace root cannot be deleted")
    if parts[0] == _TRASH_DIRNAME:
        raise TrashRefused("the trash folder cannot be deleted")
    return parts


def _entry_stat(base: Path, parts: tuple[str, ...]) -> os.stat_result | None:
    """``lstat`` a workspace-relative path through no-follow descriptors."""
    if not parts:
        return None
    try:
        fd = _open_browser_directory(parts[:-1], base)
    except FileNotFoundError:
        return None
    try:
        return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(fd)


def _trash_container(base: Path, *, create: bool) -> Path | None:
    """Return ``<base>/.trash`` when it is a real directory, never a symlink."""
    container = base / _TRASH_DIRNAME
    try:
        info = os.lstat(container)
    except FileNotFoundError:
        if not create:
            return None
        try:
            container.mkdir(mode=0o700)
        except FileExistsError:  # created by a concurrent delete
            pass
        info = os.lstat(container)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TrashRefused("the trash folder is not a directory")
    return container


def _write_trash_entry(entry_dir: Path, entry: dict[str, Any]) -> None:
    """Write ``entry.json`` owner-only and never through a symlink."""
    payload = json.dumps(entry, separators=(",", ":"), sort_keys=True).encode("utf-8")
    fd = os.open(
        entry_dir / _TRASH_ENTRY_NAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def _read_trash_entry(entry_dir: Path) -> dict[str, Any]:
    """Read a bounded ``entry.json``; malformed content raises ``ValueError``."""
    path = entry_dir / _TRASH_ENTRY_NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise FileNotFoundError("trash entry not found") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _TRASH_ENTRY_MAX_BYTES:
            raise ValueError("invalid trash entry metadata")
        chunks: list[bytes] = []
        remaining = _TRASH_ENTRY_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise FileNotFoundError("trash entry not found") from exc
    finally:
        os.close(fd)
    try:
        entry = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid trash entry metadata") from exc
    if not isinstance(entry, dict):
        raise ValueError("invalid trash entry metadata")
    return entry


def _move_entry(src_fd: int, source: Path, target: Path) -> None:
    """Move the entry ``src_fd`` points at into ``target``.

    ``os.replace`` is atomic and only ever lives inside the workspace; the
    ``shutil.move`` fallback covers the pathological case where ``.trash`` is a
    separate mount (``EXDEV``). The caller is responsible for opening ``src_fd``
    with ``O_NOFOLLOW`` and for closing it.
    """
    dst_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            os.replace(source.name, target.name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    finally:
        os.close(dst_fd)
    shutil.move(str(source), str(target))


def _forget_gallery_paths(base: Path, *relatives: str) -> int:
    """Drop Date-added entries for deleted paths and everything beneath them.

    Never raises: the item is already out of the workspace, so an unusable index
    must not turn a successful delete into an error.
    """
    index_file = gallery_index_path()
    try:
        base_resolved = base.resolve()
        index_resolved = index_file.resolve()
    except (OSError, RuntimeError):
        return 0
    # The index is never created inside the browsed workspace.
    if index_resolved == base_resolved or base_resolved in index_resolved.parents:
        return 0
    targets = {relative for relative in relatives if relative}
    if not targets:
        return 0
    prefixes = tuple(f"{relative}/" for relative in targets)
    removed = 0
    with _gallery_index_lock:
        files = _load_gallery_index(index_file)
        for key in list(files):
            if key in targets or key.startswith(prefixes):
                del files[key]
                removed += 1
        if removed:
            try:
                _write_gallery_index(index_file, files)
            except OSError:
                return 0
    return removed


def move_to_trash(workspace: str | Path | None, rel_path: str) -> dict[str, Any]:
    """Move a workspace entry into ``<workspace>/.trash``, recording what it was.

    ``workspace`` may be ``None`` to use ``BLENDER_WORKSPACE``. Returns
    ``{trash_id, original_path, name, type}``.

    Raises:
        TrashRefused: the root, ``.trash`` itself, an escaping/absolute/hidden
            path or a non-regular entry was named.
        FileNotFoundError: nothing exists at that path.
    """
    base = _trash_base(workspace)
    parts = _trash_workspace_parts(rel_path)
    info = _entry_stat(base, parts)
    if info is None:
        raise FileNotFoundError("workspace entry not found")
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "folder"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        raise TrashRefused("only files, folders and symlinks can be deleted")

    container = _trash_container(base, create=True)
    if container is None:  # unreachable: create=True raises instead of returning None
        raise OSError("could not create the trash folder")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    trash_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    entry_dir = container / trash_id
    entry_dir.mkdir(mode=0o700)

    original = "/".join(parts)
    entry = {
        "original_path": original,
        "name": parts[-1],
        "type": kind,
        "size": int(info.st_size) if kind == "file" else None,
        "deleted_at": datetime.now(UTC).isoformat(),
    }
    try:
        # Metadata first: a trash directory is never payload without provenance.
        _write_trash_entry(entry_dir, entry)
        src_fd = _open_browser_directory(parts[:-1], base)
        try:
            _move_entry(src_fd, base.joinpath(*parts), entry_dir / parts[-1])
        finally:
            os.close(src_fd)
    except OSError:
        shutil.rmtree(entry_dir, ignore_errors=True)
        raise
    _forget_gallery_paths(base, original)
    return {"trash_id": trash_id, "original_path": original, "name": parts[-1], "type": kind}


def list_trash(workspace: str | Path | None = None) -> list[dict[str, Any]]:
    """List restorable trash entries without exposing payload paths."""
    base = _trash_base(workspace)
    container = _trash_container(base, create=False)
    if container is None:
        return []
    result: list[dict[str, Any]] = []
    with os.scandir(container) as entries:
        for candidate in entries:
            if not _TRASH_ID_RE.fullmatch(candidate.name) or not candidate.is_dir(follow_symlinks=False):
                continue
            try:
                entry = _read_trash_entry(Path(candidate.path))
                name = entry.get("name")
                original = entry.get("original_path")
                kind = entry.get("type")
                deleted_at = entry.get("deleted_at")
                if not all(isinstance(value, str) and value for value in (name, original, kind, deleted_at)):
                    continue
                if kind not in {"file", "folder", "symlink"}:
                    continue
                result.append({
                    "trash_id": candidate.name,
                    "name": name,
                    "original_path": original,
                    "type": kind,
                    "deleted_at": deleted_at,
                })
            except (OSError, ValueError):
                continue
    result.sort(key=lambda item: item["deleted_at"], reverse=True)
    return result


def delete_from_trash(workspace: str | Path | None, trash_id: str) -> None:
    """Permanently delete one validated trash entry without following payload symlinks."""
    if not isinstance(trash_id, str) or not _TRASH_ID_RE.fullmatch(trash_id):
        raise ValueError("invalid trash id")
    base = _trash_base(workspace)
    container = _trash_container(base, create=False)
    if container is None:
        raise FileNotFoundError("trash entry not found")
    entry_dir = container / trash_id
    try:
        info = os.lstat(entry_dir)
    except OSError as exc:
        raise FileNotFoundError("trash entry not found") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise FileNotFoundError("trash entry not found")
    shutil.rmtree(entry_dir)


def _ensure_directory(base: Path, parts: tuple[str, ...]) -> None:
    """Create missing directories one component at a time, never via a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(base, flags)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:  # created concurrently
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def restore_from_trash(workspace: str | Path | None, trash_id: str) -> dict[str, Any]:
    """Move a trash entry back to its recorded path.

    Returns ``{restored_path}``. Raises ``ValueError`` for a malformed id,
    ``FileNotFoundError`` when the entry is unknown and ``TrashRefused`` when
    the original path is occupied or the recorded metadata is unusable.
    """
    base = _trash_base(workspace)
    if not isinstance(trash_id, str) or not _TRASH_ID_RE.fullmatch(trash_id):
        raise ValueError("invalid trash id")
    container = _trash_container(base, create=False)
    if container is None:
        raise FileNotFoundError("trash entry not found")
    entry_dir = container / trash_id
    try:
        info = os.lstat(entry_dir)
    except OSError as exc:
        raise FileNotFoundError("trash entry not found") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise FileNotFoundError("trash entry not found")
    try:
        entry = _read_trash_entry(entry_dir)
    except ValueError as exc:
        raise TrashRefused("trash entry metadata is unusable") from exc

    original = entry.get("original_path")
    if not isinstance(original, str) or original == _TRASH_DIRNAME or original.startswith(_TRASH_DIRNAME + "/"):
        raise TrashRefused("trash entry metadata is unusable")
    try:
        parts = _browser_parts(original)
    except ValueError as exc:
        raise TrashRefused("trash entry metadata is unusable") from exc
    if not parts:
        raise TrashRefused("trash entry metadata is unusable")

    name = parts[-1]
    try:
        os.lstat(entry_dir / name)
    except OSError as exc:
        raise FileNotFoundError("trash entry payload is missing") from exc
    if _entry_stat(base, parts) is not None:
        raise TrashRefused("destination already exists")
    try:
        _ensure_directory(base, parts[:-1])
        src_fd = os.open(entry_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _move_entry(src_fd, entry_dir / name, base.joinpath(*parts))
        finally:
            os.close(src_fd)
    except OSError as exc:
        # A symlinked or non-directory component appeared in the destination
        # path between validation and the move: refuse rather than write outside.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EEXIST):
            raise TrashRefused("destination path is not restorable") from exc
        raise
    shutil.rmtree(entry_dir, ignore_errors=True)
    return {"restored_path": original}


def _trash_retention_days(older_than_days: float | None) -> float:
    """Retention window in days, clamped to 1..90 (env: MCP_TRASH_RETENTION_DAYS)."""
    if older_than_days is None:
        raw = (os.environ.get("MCP_TRASH_RETENTION_DAYS") or "").strip()
        try:
            value = float(raw) if raw else DEFAULT_TRASH_RETENTION_DAYS
        except ValueError:
            value = DEFAULT_TRASH_RETENTION_DAYS
    else:
        try:
            value = float(older_than_days)
        except (TypeError, ValueError):
            value = DEFAULT_TRASH_RETENTION_DAYS
    if value != value:  # NaN
        value = DEFAULT_TRASH_RETENTION_DAYS
    return min(max(value, MIN_TRASH_RETENTION_DAYS), MAX_TRASH_RETENTION_DAYS)


def _trash_entry_age(entry_dir: Path) -> datetime | None:
    """Deletion time from ``entry.json`` when usable, else the directory mtime."""
    try:
        entry = _read_trash_entry(entry_dir)
        stamp = entry.get("deleted_at")
        if isinstance(stamp, str):
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is not None:
                return parsed
    except (OSError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(os.lstat(entry_dir).st_mtime, tz=UTC)
    except OSError:
        return None


def purge_trash(workspace: str | Path | None = None, older_than_days: float | None = None) -> dict[str, Any]:
    """Delete trash entries older than the retention window.

    Returns ``{removed_entries, kept_entries, errors, bytes_freed, retention_days}``
    and never escapes ``.trash`` or follows a symlink. Vanished and unreadable
    entries are counted as errors so a racing request cannot abort the sweep.
    """
    days = _trash_retention_days(older_than_days)
    removed = 0
    kept = 0
    errors = 0
    freed = 0
    result: dict[str, Any] = {
        "removed_entries": 0,
        "kept_entries": 0,
        "errors": 0,
        "bytes_freed": 0,
        "retention_days": days,
    }
    try:
        base = _trash_base(workspace)
        container = _trash_container(base, create=False)
    except (ValueError, TrashRefused):
        result["errors"] = 1
        return result
    if container is None:
        return result
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        with os.scandir(container) as entries:
            candidates = list(entries)
    except OSError:
        result["errors"] = 1
        return result

    for candidate in candidates:
        try:
            if candidate.is_symlink():
                os.unlink(candidate.path)  # junk link: unlink it, never traverse
                removed += 1
                continue
            if not candidate.is_dir(follow_symlinks=False):
                errors += 1
                continue
            if not _TRASH_ID_RE.fullmatch(candidate.name):
                errors += 1  # not something this module created; leave it alone
                continue
            age = _trash_entry_age(Path(candidate.path))
            if age is None:
                errors += 1
                continue
            if age > cutoff:
                kept += 1
                continue
            for root, _dirs, files in os.walk(candidate.path, followlinks=False):
                for name in files:
                    try:
                        freed += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        errors += 1
            shutil.rmtree(candidate.path, ignore_errors=True)
            if os.path.lexists(candidate.path):
                errors += 1
            else:
                removed += 1
        except OSError:
            errors += 1  # vanished between listing and removal
    result.update(removed_entries=removed, kept_entries=kept, errors=errors, bytes_freed=freed)
    return result
