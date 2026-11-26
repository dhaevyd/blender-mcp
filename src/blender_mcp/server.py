"""Blender MCP Server - FastMCP 2.14.3 implementation with modular architecture.

This module provides the main entry point for the Blender MCP server, which exposes
various Blender operations as FastMCP tools using the decorator pattern.
"""

import argparse
import base64
import binascii
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Windows: the shared limiter degrades to per-process counters.
    fcntl = None

logger = logging.getLogger(__name__)

# Global memory buffer for log viewing
_memory_logs = []
_MAX_MEMORY_LOGS = 1000


class _MemoryLogHandler(logging.Handler):
    """Handler that stores recent log records for get_recent_logs / blender_view_logs."""

    def emit(self, record: logging.LogRecord) -> None:
        global _memory_logs
        try:
            _memory_logs.append(
                {
                    "timestamp": datetime.datetime.fromtimestamp(record.created),
                    "level": record.levelname,
                    "name": record.name,
                    "function": record.funcName,
                    "line": record.lineno,
                    "message": record.getMessage(),
                    "extra": getattr(record, "extra", {}),
                }
            )
            if len(_memory_logs) > _MAX_MEMORY_LOGS:
                _memory_logs.pop(0)
        except Exception:
            self.handleError(record)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure stdlib logging with stderr, rotating file, and in-memory buffer."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in root.handlers[:]:
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s")

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / "blender-mcp.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    memory_handler = _MemoryLogHandler()
    memory_handler.setLevel(logging.DEBUG)
    root.addHandler(memory_handler)

    logger.info("Logging initialized: stderr=%s file=%s", log_level, log_dir / "blender-mcp.log")

    if os.getenv("BLENDER_MCP_LOG_FORMAT", "").strip().lower() == "json":
        from blender_mcp.utils.structured_logging import configure_json_logging

        configure_json_logging(root)
        logger.info("JSON log format enabled for Loki ingestion")


# Initialize file logging before any app imports
setup_logging(os.getenv("BLENDER_MCP_LOG_LEVEL", "INFO"))

# Import the app instance (FastMCP)
from blender_mcp.app import app

# ASGI app for uvicorn (webapp/start.ps1): uvicorn blender_mcp.server:asgi_app
asgi_app = app.http_app()

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

_DASH_COOKIE = "blender_dash_session"
_DASH_SESSION_SECONDS = int(os.environ.get("BLENDER_DASH_SESSION_SECONDS", str(15 * 60)))
_DASH_SESSION_SECONDS_MIN = 5 * 60   # 5 minutes
_DASH_SESSION_SECONDS_MAX = 7 * 24 * 60 * 60  # 7 days


def get_session_ttl_seconds() -> int:
    return _DASH_SESSION_SECONDS


def set_session_ttl_seconds(value: int) -> None:
    global _DASH_SESSION_SECONDS
    _DASH_SESSION_SECONDS = max(_DASH_SESSION_SECONDS_MIN, min(_DASH_SESSION_SECONDS_MAX, int(value)))


def _api_key_env_name() -> str:
    """Environment variable providing this role's inbound bearer key."""
    from blender_mcp.utils.farm_workers import mcp_role

    return "BLENDER_MCP_WORKER_KEY" if mcp_role() == "worker" else "BLENDER_MCP_API_KEY"


def _api_key() -> str:
    return os.getenv(_api_key_env_name(), "").strip()


def _dashboard_auth_enabled() -> bool:
    return os.getenv("MCP_DASH_AUTH") == "true"


def _dashboard_credentials() -> tuple[str, str] | None:
    username = os.getenv("MCP_USERNAME", "")
    password = os.getenv("MCP_PASSWORD", "")
    if not username.strip() or not password.strip() or ":" in username:
        return None
    return username, password


def _dashboard_basic_valid(request) -> bool:
    credentials = _dashboard_credentials()
    supplied = request.headers.get("authorization", "")
    parts = supplied.split()
    if credentials is None or len(parts) != 2 or parts[0].lower() != "basic" or len(parts[1]) > 8192:
        return False
    try:
        raw = base64.b64decode(parts[1], validate=True)
        username, password = raw.decode("utf-8").split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    return bool(hmac.compare_digest(username.encode(), credentials[0].encode()) &
                hmac.compare_digest(password.encode(), credentials[1].encode()))


def _dashboard_signing_key() -> bytes:
    # A signed cookie cannot be forged from the injected API key alone.
    username, password = _dashboard_credentials() or ("", "")
    material = (username + "\x00" + password).encode()
    api_key = _api_key().encode()
    return hmac.new(material, b"dashboard-session-v1\x00" + api_key, hashlib.sha256).digest()


def _dashboard_new_session() -> str:
    payload = f"{int(time.time()) + _DASH_SESSION_SECONDS}:{secrets.token_hex(16)}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_dashboard_signing_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _dashboard_session_valid(request) -> bool:
    if not _dashboard_auth_enabled() or not _dashboard_credentials():
        return False
    cookie = request.cookies.get(_DASH_COOKIE, "")
    if not cookie or len(cookie) > 512 or cookie.count(".") != 1:
        return False
    encoded, signature = cookie.split(".", 1)
    if len(signature) != 64 or not hmac.compare_digest(
        hmac.new(_dashboard_signing_key(), encoded.encode(), hashlib.sha256).hexdigest(), signature
    ):
        return False
    try:
        payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
        expiry, nonce = payload.split(":", 1)
        return nonce.isascii() and len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce) and int(time.time()) < int(expiry) <= int(time.time()) + _DASH_SESSION_SECONDS
    except (ValueError, UnicodeError, binascii.Error):
        return False


def _trusted_proxy_enabled() -> bool:
    """Only the exact value 'true' opts into X-Forwarded-* headers."""
    return os.getenv("MCP_TRUSTED_PROXY", "").strip().lower() == "true"


def _forwarded_header(request, name: str, limit: int, allow_list: bool = False) -> str:
    """Defensively validated forwarded header value, or '' when unusable."""
    value = request.headers.get(name, "")
    if not value or len(value) > limit:
        return ""
    if any(ch in value for ch in "\r\n\0"):
        return ""
    if not allow_list and "," in value:
        return ""
    return value.strip()


def _effective_scheme(request) -> str:
    """Browser-facing scheme; forwarded headers only when the proxy is trusted."""
    if not _trusted_proxy_enabled():
        return request.url.scheme
    forwarded = _forwarded_header(request, "x-forwarded-proto", 8)
    scheme = forwarded.lower()
    return scheme if scheme in {"http", "https"} else request.url.scheme


def _direct_client_host(request) -> str:
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def _forwarded_client_host(request) -> str:
    """Client address from the last X-Forwarded-For hop, or '' when implausible.

    A trusted proxy must overwrite (not append to) client-supplied X-Forwarded-*
    headers, so the last hop is the peer address it actually accepted.
    """
    forwarded = _forwarded_header(request, "x-forwarded-for", 1024, allow_list=True)
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return ""
    candidate = hops[-1]
    if candidate.startswith("["):
        end = candidate.find("]")
        candidate = candidate[1:end] if end > 0 else candidate
    elif candidate.count(":") == 1:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    return candidate


def _login_client_key(request) -> str:
    if _trusted_proxy_enabled():
        forwarded = _forwarded_client_host(request)
        if forwarded:
            return forwarded
    return _direct_client_host(request)


_DASH_ORIGIN_MAX_LENGTH = 512
_DASH_ALLOWED_ORIGINS_MAX_LENGTH = 4096
_DASH_ALLOWED_ORIGINS_MAX_ENTRIES = 32
_DASH_DEFAULT_PORTS = {"http": 80, "https": 443}
_DASH_HOST_PATTERN = re.compile(r"[a-z0-9._-]+")


def _normalise_origin(value) -> str:
    """Canonical ``scheme://host[:port]`` for a bare HTTP(S) origin, else ''.

    The browser's Origin header and every MCP_DASH_ALLOWED_ORIGINS entry pass
    through here so the two can be compared exactly. Anything that is not a
    bare origin -- a path, query, fragment, userinfo, wildcard, or whitespace
    inside the value -- is rejected instead of repaired.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if candidate.endswith("/"):
        candidate = candidate[:-1]
    if not candidate or len(candidate) > _DASH_ORIGIN_MAX_LENGTH:
        return ""
    if any(ch.isspace() or ch in "\\\"'<>" for ch in candidate):
        return ""
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in _DASH_DEFAULT_PORTS or not parts.netloc:
        return ""
    if "@" in parts.netloc or parts.netloc.endswith(":") or parts.path or parts.query or parts.fragment:
        return ""
    host = parts.hostname or ""
    if not host or "*" in host:
        return ""
    if ":" in host:
        # Bracketed IPv6 literal; urlsplit has already stripped the brackets.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ""
        host_part = f"[{host}]"
    elif not _DASH_HOST_PATTERN.fullmatch(host):
        return ""
    else:
        host_part = host
    # An explicit non-default port stays part of the origin; a default port is
    # dropped so "https://example.com:443" and "https://example.com" agree.
    if port is not None and port != _DASH_DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host_part}:{port}"
    return f"{scheme}://{host_part}"


def _dashboard_allowed_origins() -> set[str]:
    """Extra browser origins accepted by the dashboard CSRF check.

    Read per request so a restart-free config change (or a test) takes effect;
    malformed entries are skipped rather than raising.
    """
    raw = os.getenv("MCP_DASH_ALLOWED_ORIGINS", "")
    if not raw or len(raw) > _DASH_ALLOWED_ORIGINS_MAX_LENGTH:
        return set()
    allowed = set()
    for entry in raw.split(","):
        normalised = _normalise_origin(entry)
        if normalised:
            allowed.add(normalised)
            if len(allowed) >= _DASH_ALLOWED_ORIGINS_MAX_ENTRIES:
                break
    return allowed


def _dashboard_same_origin(request) -> bool:
    """CSRF gate for the login/logout POSTs: exact own origin or an allowlisted one.

    Behind a TLS-terminating proxy the ASGI request is plain HTTP while the
    browser sends an https Origin, so the computed own origin never matches;
    MCP_DASH_ALLOWED_ORIGINS names the public origin explicitly. Comparison is
    exact, including the scheme, and an absent Origin header is never accepted.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return False
    if origin == f"{_effective_scheme(request)}://{request.url.netloc}":
        return True
    normalised = _normalise_origin(origin)
    return bool(normalised) and normalised in _dashboard_allowed_origins()


def _dashboard_basic_attempted(request) -> bool:
    parts = request.headers.get("authorization", "").split()
    return bool(parts) and parts[0].lower() == "basic"


# Bounded failed-login throttling. Process-local and best-effort: it slows online
# guessing, it is not a distributed limiter and does not replace TLS. Counters are
# kept in memory for the fast path and mirrored to one small JSON file so several
# uvicorn workers or replicas sharing a volume enforce the same cap.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_GLOBAL_MAX_ATTEMPTS = 30
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_TRACKED_CLIENTS = 256
_LOGIN_FILE_MAX_GLOBAL_STAMPS = 4096
_LOGIN_FILE_MAX_KEY_LENGTH = 64

_DEFAULT_LOGIN_LIMIT_FILE = Path(tempfile.gettempdir()) / "blender-mcp-login-limits.json"


def _login_limit_file() -> Path:
    """Shared counter file: MCP_LOGIN_LIMIT_FILE, else one under the temp dir."""
    configured = os.getenv("MCP_LOGIN_LIMIT_FILE", "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_LOGIN_LIMIT_FILE


def _prune_login_failures(failures: dict[str, list[float]], global_failures: list[float], now: float) -> bool:
    """Drop stamps older than the window; True when anything was removed."""
    cutoff = now - _LOGIN_WINDOW_SECONDS
    changed = False
    for key, stamps in list(failures.items()):
        fresh = [stamp for stamp in stamps if stamp > cutoff]
        if len(fresh) != len(stamps):
            changed = True
        if fresh:
            failures[key] = fresh
        else:
            del failures[key]
    while global_failures and global_failures[0] <= cutoff:
        global_failures.pop(0)
        changed = True
    return changed


def _login_stamp_cap() -> int:
    """Per-client stamps kept; enough to enforce both per-client and global caps."""
    return max(_LOGIN_MAX_ATTEMPTS, _LOGIN_GLOBAL_MAX_ATTEMPTS)


def _bound_login_failures(failures: dict[str, list[float]], global_failures: list[float]) -> bool:
    """Keep the tracked-client and stamp counts bounded; True when trimmed."""
    changed = False
    for key, stamps in list(failures.items()):
        if len(stamps) > _login_stamp_cap():
            failures[key] = stamps[-_login_stamp_cap() :]
            changed = True
    while len(failures) > _LOGIN_MAX_TRACKED_CLIENTS:
        oldest = min(failures, key=lambda key: failures[key][-1])
        del failures[oldest]
        changed = True
    if len(global_failures) > _LOGIN_FILE_MAX_GLOBAL_STAMPS:
        del global_failures[:-_LOGIN_FILE_MAX_GLOBAL_STAMPS]
        changed = True
    return changed


def _login_retry_from(failures: dict[str, list[float]], global_failures: list[float], client_key: str, now: float) -> int:
    retry = 0
    stamps = failures.get(client_key, [])
    if len(stamps) >= _LOGIN_MAX_ATTEMPTS:
        retry = math.ceil(stamps[-_LOGIN_MAX_ATTEMPTS] + _LOGIN_WINDOW_SECONDS - now)
    if len(global_failures) >= _LOGIN_GLOBAL_MAX_ATTEMPTS:
        retry = max(
            retry,
            math.ceil(global_failures[-_LOGIN_GLOBAL_MAX_ATTEMPTS] + _LOGIN_WINDOW_SECONDS - now),
        )
    return max(retry, 0)


class _LoginLimiter:
    """Failed-login counters shared between processes via an flock'd JSON file.

    The file stores client-IP keys and timestamps only: no usernames, passwords,
    Authorization headers or request bodies. Every read-modify-write happens under
    an exclusive lock so concurrent workers cannot lose each other's increments.
    A missing, corrupt, read-only or unsupported store silently degrades to the
    in-memory counters instead of failing requests.
    """

    def __init__(self, failures=None, global_failures=None, store=None):
        self._memory_failures = failures if failures is not None else {}
        self._memory_global = global_failures if global_failures is not None else []
        self._lock = threading.Lock()
        self._store = str(store) if store is not None else None

    def _store_path(self) -> Path | None:
        if fcntl is None:
            return None
        return Path(self._store) if self._store else _login_limit_file()

    @staticmethod
    def _timestamps(values) -> list[float]:
        usable = [
            float(value)
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
        ]
        return sorted(usable)

    @classmethod
    def _sanitize(cls, payload) -> tuple[dict[str, list[float]], list[float]]:
        failures: dict[str, list[float]] = {}
        raw_failures = payload.get("failures") if isinstance(payload, dict) else None
        if isinstance(raw_failures, dict):
            for key, stamps in raw_failures.items():
                if not isinstance(key, str) or not key or len(key) > _LOGIN_FILE_MAX_KEY_LENGTH:
                    continue
                if isinstance(stamps, list):
                    clean = cls._timestamps(stamps)[-_login_stamp_cap() :]
                    if clean:
                        failures[key] = clean
        raw_global = payload.get("global") if isinstance(payload, dict) else None
        global_failures = cls._timestamps(raw_global)[-_LOGIN_FILE_MAX_GLOBAL_STAMPS:] if isinstance(raw_global, list) else []
        return failures, global_failures

    def _open(self, path: Path):
        module = fcntl
        if module is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+", encoding="utf-8")
        except OSError:
            return None
        try:
            module.flock(handle.fileno(), module.LOCK_EX)
        except OSError:
            handle.close()
            return None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return handle

    @staticmethod
    def _close(handle) -> None:
        module = fcntl
        try:
            if module is not None:
                module.flock(handle.fileno(), module.LOCK_UN)
        except OSError:
            pass
        handle.close()

    def _read(self, handle) -> tuple[dict[str, list[float]], list[float]]:
        try:
            handle.seek(0)
            raw = handle.read()
        except OSError:
            return {}, []
        if not raw.strip():
            return {}, []
        try:
            return self._sanitize(json.loads(raw))
        except (ValueError, UnicodeError):
            return {}, []

    def _write(self, handle, failures, global_failures) -> None:
        payload = {
            "version": 1,
            "failures": failures,
            "global": global_failures[-_LOGIN_FILE_MAX_GLOBAL_STAMPS:],
        }
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, ValueError, TypeError):
            logger.debug("login limiter store write failed", exc_info=True)

    def _transaction(self, now: float, mutate=None) -> tuple[dict[str, list[float]], list[float]]:
        with self._lock:
            path = self._store_path()
            handle = self._open(path) if path is not None else None
            try:
                if handle is not None:
                    failures, global_failures = self._read(handle)
                else:
                    failures = {key: list(stamps) for key, stamps in self._memory_failures.items()}
                    global_failures = list(self._memory_global)
                pruned = _prune_login_failures(failures, global_failures, now)
                if mutate is not None:
                    mutate(failures, global_failures)
                trimmed = _bound_login_failures(failures, global_failures)
                # A read-only check only rewrites the store when pruning trimmed it,
                # so the common allowed/limited path stays a cheap in-memory look.
                changed = pruned or trimmed or mutate is not None
                self._memory_failures.clear()
                self._memory_failures.update(failures)
                self._memory_global[:] = global_failures
                if handle is not None and changed:
                    self._write(handle, failures, global_failures)
                return failures, global_failures
            finally:
                if handle is not None:
                    self._close(handle)

    def retry_after(self, client_key: str, now: float | None = None) -> int:
        """Seconds until the next attempt is allowed, or 0 when allowed now."""
        moment = time.time() if now is None else now
        failures, global_failures = self._transaction(moment)
        return _login_retry_from(failures, global_failures, client_key, moment)

    def record_failure(self, client_key: str, now: float | None = None) -> None:
        moment = time.time() if now is None else now

        def mutate(failures, global_failures):
            failures.setdefault(client_key, []).append(moment)
            global_failures.append(moment)

        self._transaction(moment, mutate)

    def clear(self, client_key: str) -> None:
        def mutate(failures, global_failures):
            failures.pop(client_key, None)

        self._transaction(time.time(), mutate)

    def reset(self) -> None:
        def mutate(failures, global_failures):
            failures.clear()
            global_failures.clear()

        self._transaction(time.time(), mutate)


_login_failures: dict[str, list[float]] = {}
_login_global_failures: list[float] = []
_login_limiter = _LoginLimiter(_login_failures, _login_global_failures)


def _login_retry_after(request) -> int:
    return _login_limiter.retry_after(_login_client_key(request))


def _record_login_failure(request) -> None:
    _login_limiter.record_failure(_login_client_key(request))


def _clear_login_failures(request) -> None:
    _login_limiter.clear(_login_client_key(request))


def _reset_login_limiter() -> None:
    _login_limiter.reset()


_PUBLIC_BUNDLE_NAME = re.compile(r"[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?|svg|png|jpe?g|webp|avif)")
_PUBLIC_LOGIN_ICONS = {"favicon.svg", "favicon-32x32.png", "favicon-16x16.png", "apple-touch-icon.png"}


def _public_login_resource(request) -> bool:
    """Allow only existing Vite bundles and conventional login-page icons."""
    if request.method not in {"GET", "HEAD"}:
        return False
    path = request.url.path
    if path.startswith("/assets/"):
        name = path.removeprefix("/assets/")
        root = (_dashboard_dist / "assets").resolve()
        if not _PUBLIC_BUNDLE_NAME.fullmatch(name):
            return False
    elif path.lstrip("/") in _PUBLIC_LOGIN_ICONS:
        name = path.lstrip("/")
        root = _dashboard_dist.resolve()
    else:
        return False
    candidate = (root / name).resolve()
    return candidate.parent == root and candidate.is_file()


class _ApiKeyMiddleware(BaseHTTPMiddleware):
    """Protect MCP/API routes with a bearer token when configured."""

    _PUBLIC_PATHS = {
        "/",
        "/health",
        "/api/health",
        "/api/status",
        "/api/v1/status",
        "/api/v1/health",
        "/api/v1/diagnostics",
        "/viewer",
        "/viewer/",
        "/api/v1/viewer/state",
        "/api/v1/viewer/latest",
        "/api/v1/viewer/video",
    }

    _PROTECTED_PREFIXES = ("/api", "/mcp", "/tool", "/metrics")

    @staticmethod
    def _public_dashboard_request(request) -> bool:
        """Existing public routes and dashboard files (without bearer auth)."""
        path = request.url.path
        if path.startswith(_ApiKeyMiddleware._PROTECTED_PREFIXES):
            return path in _ApiKeyMiddleware._PUBLIC_PATHS
        if path in _ApiKeyMiddleware._PUBLIC_PATHS or path.startswith("/assets/"):
            return True
        if request.method not in {"GET", "HEAD"}:
            return False
        rel = path.lstrip("/")
        if rel and not any(part in {"", ".", ".."} for part in rel.split("/")):
            candidate = (_dashboard_dist / rel).resolve()
            if candidate.is_file() and _dashboard_dist.resolve() in candidate.parents:
                return True
        return "text/html" in request.headers.get("accept", "").lower()

    async def dispatch(self, request, call_next):
        path = request.url.path
        from blender_mcp.utils.farm_workers import mcp_role
        if mcp_role() == "worker":
            health_paths = {
                "/health",
                "/api/health",
                "/api/status",
                "/api/v1/status",
                "/api/v1/health",
                "/api/v1/diagnostics",
            }
            dashboard_api = path in {"/api/v1/dashboard", "/api/v1/viewer"} or path.startswith(
                ("/api/v1/dashboard/", "/api/v1/viewer/")
            )
            browser_path = path.startswith("/assets/") or path in {
                "/",
                "/login",
                "/login/",
                "/viewer",
                "/viewer/",
            }
            app_api = path.startswith(("/api/", "/mcp", "/tool", "/metrics"))
            if dashboard_api or browser_path or (not app_api and path not in health_paths):
                return JSONResponse({"error": "not found"}, status_code=404)
        dashboard_auth = _dashboard_auth_enabled()
        public_service = path in self._PUBLIC_PATHS - {"/"}
        auth_endpoint = path in {"/api/v1/dashboard/login", "/api/v1/dashboard/session", "/api/v1/dashboard/logout"}
        login_asset = path in {"/login", "/login/"} and request.method in {"GET", "HEAD"}
        login_icon = path.lstrip("/") in _PUBLIC_LOGIN_ICONS and request.method in {"GET", "HEAD"}
        public_login_resource = dashboard_auth and _public_login_resource(request)
        dashboard = request.method in {"GET", "HEAD"} and not auth_endpoint and (
            path == "/" or path.startswith("/assets/") or (
                not path.startswith(self._PROTECTED_PREFIXES) and not public_service
            )
        )

        # Never serve the dashboard shell (and its injected bearer key) when
        # Basic credentials are enabled but not completely configured.
        if dashboard_auth and (dashboard or auth_endpoint or login_asset or login_icon or public_login_resource) and not _dashboard_credentials():
            return JSONResponse({"error": "dashboard authentication unavailable"}, status_code=503)
        if dashboard_auth and request.method in {"GET", "HEAD"} and path.startswith("/assets/") and _PUBLIC_BUNDLE_NAME.fullmatch(path.removeprefix("/assets/")) and not public_login_resource:
            return PlainTextResponse("Not Found", status_code=404)
        if dashboard_auth and login_icon:
            if not public_login_resource:
                return PlainTextResponse("Not Found", status_code=404)
            return FileResponse(_dashboard_dist / path.lstrip("/"))
        if login_asset or public_login_resource:
            return await call_next(request)
        if dashboard_auth and auth_endpoint:
            return await call_next(request)
        expected = _api_key()
        if not expected:
            return JSONResponse({"error": f"{_api_key_env_name()} is not configured"}, status_code=503)
        if public_service:
            return await call_next(request)
        if dashboard_auth and dashboard:
            if _dashboard_session_valid(request):
                return await call_next(request)
            if _dashboard_basic_attempted(request):
                # The Basic fallback shares the login limiter: a limited client is
                # answered 429 without a fresh challenge so browsers stop prompting.
                retry_after = _login_retry_after(request)
                if retry_after:
                    logger.warning(
                        "dashboard basic auth rate limited: client=%s retry_after=%ss",
                        _login_client_key(request),
                        retry_after,
                    )
                    return JSONResponse(
                        {"error": "too many authentication attempts"},
                        status_code=429,
                        headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                    )
                if _dashboard_basic_valid(request):
                    _clear_login_failures(request)
                    return await call_next(request)
                _record_login_failure(request)
            if "text/html" in request.headers.get("accept", "").lower() and "authorization" not in request.headers:
                return RedirectResponse("/login", status_code=303)
            return JSONResponse(
                {"error": "dashboard authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Blender MCP Dashboard", charset="UTF-8"', "Cache-Control": "no-store"},
            )
        if not dashboard_auth and self._public_dashboard_request(request):
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        scheme, _, credential = supplied.partition(" ")
        # Only the Authorization header is accepted: a ?token= query parameter
        # leaks the API key into logs, history and Referer headers.
        if scheme.lower() != "bearer" or not credential.strip():
            return JSONResponse({"error": "authentication required"}, status_code=401)
        from blender_mcp.utils.studio_access import scoped_access, scoped_role, worker_audit_action

        candidate = credential.strip().encode("utf-8", "replace")
        role = "admin" if hmac.compare_digest(candidate, expected.encode("utf-8", "replace")) else None
        if role is None and mcp_role() != "worker":
            role = scoped_role(candidate)
        if role is None:
            return JSONResponse({"error": "authentication required"}, status_code=401)
        if role != "admin" and not scoped_access(role, request.method, path):
            return JSONResponse({"error": "insufficient permissions"}, status_code=403)
        request.state.studio_role = role
        response = await call_next(request)
        action = worker_audit_action(request.method, path)
        if action and mcp_role() != "worker" and 200 <= response.status_code < 300:
            try:
                from blender_mcp.tools.farm_tools import _coordinator

                _coordinator().store.record_audit(role, action, path)
            except Exception:
                logger.error("Could not write worker audit event")
        return response


class _NoCrossOriginHtmlMiddleware:
    """Never expose HTML responses to other origins.

    The dashboard shell embeds the Blender MCP API key, so it must not be
    CORS-readable: any page on another local port would otherwise be able to
    read the key from `/` (cookies are shared across ports, SameSite=Lax does
    not separate them). Bearer-authenticated JSON API traffic keeps normal CORS
    handling because the token is supplied explicitly per request.
    """

    _CORS_HEADERS = {
        b"access-control-allow-origin",
        b"access-control-allow-credentials",
        b"access-control-expose-headers",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_without_cors(message):
            if message["type"] == "http.response.start" and any(
                name.lower() == b"content-type" and value.lower().startswith(b"text/html")
                for name, value in message.get("headers", ())
            ):
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", ())
                        if name.lower() not in self._CORS_HEADERS
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_without_cors)


async def _health(request):
    return JSONResponse({"status": "ok", "service": "blender-mcp"})


asgi_app.router.routes.append(Route("/health", endpoint=_health))
asgi_app.router.routes.append(Route("/api/health", endpoint=_health))
asgi_app.router.routes.append(Route("/api/status", endpoint=_health))
asgi_app.router.routes.append(Route("/api/v1/status", endpoint=_health))
asgi_app.router.routes.append(Route("/api/v1/health", endpoint=_health))


async def _logs_endpoint(request):
    """REST endpoint for logs: GET /api/v1/logs?level=INFO&limit=50&since_minutes=60"""
    level_filter = request.query_params.get("level")
    module_filter = request.query_params.get("module")
    search = request.query_params.get("search")
    since_minutes_str = request.query_params.get("since_minutes")
    limit_str = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(500, int(limit_str)))
    except (ValueError, TypeError):
        limit = 50
    since_minutes = None
    if since_minutes_str:
        try:
            since_minutes = max(0, int(since_minutes_str))
        except (ValueError, TypeError):
            pass
    logs = get_recent_logs(
        level_filter=level_filter,
        module_filter=module_filter,
        limit=limit,
        since_minutes=since_minutes,
    )
    if search:
        search_lower = search.lower()
        logs = [log for log in logs if search_lower in log["message"].lower() or (search_lower in log["name"].lower())]
    result = []
    for log in logs:
        result.append(
            {
                "timestamp": log["timestamp"].isoformat(),
                "level": log["level"],
                "name": log["name"],
                "function": log["function"],
                "line": log["line"],
                "message": log["message"],
            }
        )
    return JSONResponse({"success": True, "logs": result, "count": len(result)})


asgi_app.router.routes.append(Route("/api/v1/logs", endpoint=_logs_endpoint, methods=["GET"]))

_server_start_time = datetime.datetime.now()


def _system_diagnostics() -> dict:
    """Runtime-visible resources and workspace filesystem, never private paths."""
    import platform

    import psutil

    mem = psutil.virtual_memory()
    result = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sampled_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "scope": "coordinator runtime-visible resources (may include host resources in containers)",
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_logical_threads": None,
        "load_1m": None,
        "memory_percent": mem.percent,
        "memory_used_gb": round(mem.used / (1024**3), 1),
        "memory_total_gb": round(mem.total / (1024**3), 1),
        "disk_percent": None,
        "disk_used_gb": None,
        "disk_total_gb": None,
        "disk_scope": "workspace unavailable",
    }
    try:
        result["cpu_logical_threads"] = psutil.cpu_count(logical=True)
    except (AttributeError, OSError, NotImplementedError):
        pass
    try:
        result["load_1m"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError, NotImplementedError):
        pass
    workspace = Path(os.environ.get("BLENDER_WORKSPACE", "/workspace")).resolve()
    if workspace.is_dir():
        try:
            disk = psutil.disk_usage(str(workspace))
            result.update(
                disk_percent=disk.percent,
                disk_used_gb=round(disk.used / (1024**3), 1),
                disk_total_gb=round(disk.total / (1024**3), 1),
                disk_scope="workspace filesystem",
            )
        except (OSError, ValueError):
            pass
    # Temp-scratch footprint: sizes and sweep counters only, never filenames.
    try:
        from blender_mcp.utils import housekeeping

        housekeeping.start_background_sweeper()
        result["temp"] = housekeeping.temp_diagnostics()
    except Exception:
        result["temp"] = {"available": False}
    return result


def _blender_bridge_alive(host: str, port: int) -> bool:
    """Check the local Blender bridge without blocking the ASGI event loop."""
    import json as _json
    import socket as _sock
    import struct as _struct

    try:
        with _sock.create_connection((host, port), timeout=2) as connection:
            connection.settimeout(2)
            payload = _json.dumps({"type": "ping"}).encode("utf-8")
            connection.sendall(_struct.pack("!I", len(payload)) + payload)
            header = connection.recv(4)
            if len(header) != 4:
                return False
            body_len = _struct.unpack("!I", header)[0]
            body = connection.recv(body_len)
            return _json.loads(body.decode("utf-8")).get("status") == "success"
    except Exception:
        return False


async def _diagnostics_endpoint(request):
    """GET /api/v1/diagnostics — system status, tool count, uptime, resources."""
    import asyncio

    uptime = (datetime.datetime.now() - _server_start_time).total_seconds()
    tool_list = [t.name for t in await app.list_tools()]
    from blender_mcp.utils.farm_workers import gpu_stats, mcp_role
    from blender_mcp.utils.gpu_render import require_gpu
    from blender_mcp.utils.gpu_sysfs import gpu_probe
    from blender_mcp.utils.host_capabilities import describe_host

    bridge_host = os.environ.get("BLENDER_BRIDGE_HOST", os.environ.get("BLENDER_HOST", "127.0.0.1"))
    try:
        bridge_port = int(os.environ.get("BLENDER_BRIDGE_PORT", "10850"))
    except ValueError:
        bridge_port = 10850
    system, gpus, probe, bridge_alive, host = await asyncio.gather(
        asyncio.to_thread(_system_diagnostics),
        asyncio.to_thread(gpu_stats),
        asyncio.to_thread(gpu_probe),
        asyncio.to_thread(_blender_bridge_alive, bridge_host, bridge_port),
        asyncio.to_thread(describe_host),
    )
    host["render_device"] = "GPU" if host["gpu_available"] else "CPU"
    host["gpu_required"] = require_gpu()

    return JSONResponse(
        {
            "server": {
                "name": "blender-mcp",
                "version": __import__("blender_mcp").app_version(),
                "uptime_seconds": uptime,
                "port": 10849,
                "tool_count": len(tool_list),
                "status": "ok",
                "role": mcp_role(),
            },
            "system": {**system, "gpu": gpus, "gpu_probe": probe},
            "host": host,
            "blender": {
                "connected": bridge_alive,
                "bridge_addon": bridge_alive,
            },
        }
    )


asgi_app.router.routes.append(Route("/api/v1/diagnostics", endpoint=_diagnostics_endpoint, methods=["GET"]))


async def _skills_list_endpoint(request):
    """GET /api/skills — list registered skills from FastMCP resource providers."""
    skills = []
    try:
        providers = getattr(app, "_resource_providers", [])
        for prov in providers:
            if hasattr(prov, "list_resources"):
                resources = await prov.list_resources()
                for r in resources:
                    uri = str(r.uri) if hasattr(r, "uri") else str(r)
                    if "/SKILL.md" in uri or "skill://" in uri:
                        skills.append({"uri": uri, "name": uri.split("/")[-2] if "/" in uri else uri})
    except Exception:
        pass
    return JSONResponse({"success": True, "skills": skills, "count": len(skills)})


async def _skills_detail_endpoint(request):
    """GET /api/skills/{name} — render skill markdown content."""
    name = request.path_params.get("name", "")
    try:
        providers = getattr(app, "_resource_providers", [])
        for prov in providers:
            if hasattr(prov, "list_resources"):
                resources = await prov.list_resources()
                for r in resources:
                    uri = str(r.uri) if hasattr(r, "uri") else str(r)
                    if f"skill://{name}/SKILL.md" in uri or (f"/{name}/" in uri and "/SKILL.md" in uri):
                        content = await prov.read_resource(uri)
                        return JSONResponse({"success": True, "name": name, "content": str(content)})
    except Exception:
        pass
    return JSONResponse({"success": False, "error": f"Skill '{name}' not found"}, status_code=404)


def _viewer_workspace() -> Path:
    return Path(os.getenv("BLENDER_WORKSPACE", "/workspace")).resolve()


def _latest_preview() -> Path | None:
    root = _viewer_workspace()
    if not root.exists():
        return None
    candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def _latest_video() -> Path | None:
    root = _viewer_workspace()
    if not root.exists():
        return None
    candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mov"}]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


async def _viewer_state(request):
    preview = _latest_preview()
    video = _latest_video()
    if preview is None and video is None:
        return JSONResponse({"ready": False, "message": "No preview render yet"})
    root = _viewer_workspace()
    response = {
        "ready": True,
        "updated": max(p.stat().st_mtime for p in (preview, video) if p is not None),
    }
    if preview is not None:
        response.update({"preview": preview.relative_to(root).as_posix(), "size_bytes": preview.stat().st_size})
    if video is not None:
        response.update({"video": video.relative_to(root).as_posix(), "video_size_bytes": video.stat().st_size})
    return JSONResponse(
        response
    )


async def _viewer_latest(request):
    preview = _latest_preview()
    if preview is None:
        return JSONResponse({"error": "No preview render yet"}, status_code=404)
    return FileResponse(preview)


async def _viewer_video(request):
    video = _latest_video()
    if video is None:
        return JSONResponse({"error": "No animation render yet"}, status_code=404)
    media_type = "video/webm" if video.suffix.lower() == ".webm" else "video/mp4"
    return FileResponse(video, media_type=media_type)


async def _viewer_page(request):
    return HTMLResponse(
        """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blender MCP — Live View</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#0b0d10;color:#f1f5f9}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 70% 0,#183047 0,#0b0d10 42%)}
header{padding:28px 5vw 16px;display:flex;justify-content:space-between;align-items:end;border-bottom:1px solid #26313c}
h1{font-size:clamp(24px,4vw,42px);letter-spacing:-.05em;margin:0}.eyebrow{color:#61dafb;text-transform:uppercase;letter-spacing:.18em;font-size:11px;font-weight:700}
main{padding:28px 5vw;max-width:1500px;margin:auto}.status{color:#a8b3bf;font-size:14px;margin:0 0 18px}
.stage{background:#10151b;border:1px solid #293744;border-radius:18px;min-height:60vh;display:grid;place-items:center;overflow:hidden;box-shadow:0 24px 80px #0008}
.stage img,.stage video{display:block;max-width:100%;max-height:78vh;object-fit:contain}.empty{color:#7d8a96;text-align:center;padding:40px}
.meta{display:flex;gap:18px;color:#8e9aa7;font:12px ui-monospace,monospace;margin-top:14px}.live{color:#69e39a}
</style></head><body><header><div><div class="eyebrow">Blender MCP / read-only monitor</div><h1>Live scene view</h1></div><div class="live">● AUTO-REFRESH</div></header>
<main><p id="status" class="status">Connecting to render workspace…</p><section class="stage"><video id="animation" controls loop autoplay muted playsinline hidden></video><img id="preview" alt="Latest Blender preview" hidden><div id="empty" class="empty">No preview render yet.</div></section><div class="meta"><span id="updated">Waiting</span><span id="size"></span></div></main>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const headers=token?{Authorization:`Bearer ${token}`}:{};
const q=token?`?token=${encodeURIComponent(token)}`:'';
async function refresh(){try{const r=await fetch('/api/v1/viewer/state'+q,{headers});const s=await r.json();
 if(!s.ready){document.querySelector('#status').textContent=s.message;return;}
 const img=document.querySelector('#preview'), video=document.querySelector('#animation');
 if(s.video){const videoSrc='/api/v1/viewer/video'+(q?(q+'&'):'?')+'t='+Math.floor(s.updated*1000);if(video.dataset.source!==videoSrc){video.src=videoSrc;video.dataset.source=videoSrc;video.load();}video.hidden=false;img.hidden=true;img.style.display='none';document.querySelector('#empty').hidden=true;video.play().catch(()=>{});document.querySelector('#status').textContent='Latest animation from the Blender workspace';document.querySelector('#size').textContent=Math.round(s.video_size_bytes/1024)+' KB';}
 else if(s.preview){img.src='/api/v1/viewer/latest'+(q?(q+'&'):'?')+'t='+Date.now();img.hidden=false;video.hidden=true;document.querySelector('#empty').hidden=true;document.querySelector('#status').textContent='Latest preview from the Blender workspace';document.querySelector('#size').textContent=Math.round(s.size_bytes/1024)+' KB';}
 document.querySelector('#updated').textContent=new Date(s.updated*1000).toLocaleString();
 }catch(e){document.querySelector('#status').textContent='Viewer unavailable: '+e.message;}}
refresh();setInterval(refresh,2000);
</script></body></html>"""
    )


async def _dashboard_login_page(request):
    if not _dashboard_auth_enabled():
        return RedirectResponse("/", status_code=303)
    index = _dashboard_dist / "index.html"
    if not index.is_file():
        return JSONResponse({"error": "dashboard unavailable"}, status_code=503)
    return HTMLResponse(
        index.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _dashboard_login(request):
    if not _dashboard_auth_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _dashboard_credentials() or not _api_key():
        return JSONResponse({"error": "dashboard authentication unavailable"}, status_code=503)
    if not _dashboard_same_origin(request):
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "JSON required"}, status_code=415)
    retry_after = _login_retry_after(request)
    if retry_after:
        logger.warning(
            "dashboard login rate limited: client=%s retry_after=%ss",
            _login_client_key(request),
            retry_after,
        )
        return JSONResponse(
            {"error": "too many login attempts"},
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    credentials = _dashboard_credentials()
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not credentials or not (
        hmac.compare_digest(username.encode(), credentials[0].encode()) &
        hmac.compare_digest(password.encode(), credentials[1].encode())
    ):
        _record_login_failure(request)
        return JSONResponse({"error": "invalid credentials"}, status_code=401, headers={"Cache-Control": "no-store"})
    _clear_login_failures(request)
    response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        _DASH_COOKIE, _dashboard_new_session(), max_age=_DASH_SESSION_SECONDS,
        httponly=True, secure=_effective_scheme(request) == "https", samesite="lax", path="/",
    )
    return response


async def _dashboard_session(request):
    if not _dashboard_auth_enabled():
        return JSONResponse({"authenticated": False}, status_code=404, headers={"Cache-Control": "no-store"})
    if not _dashboard_credentials() or not _api_key():
        return JSONResponse({"error": "dashboard authentication unavailable"}, status_code=503)
    return JSONResponse(
        {"authenticated": True} if _dashboard_session_valid(request) else {"authenticated": False},
        status_code=200 if _dashboard_session_valid(request) else 401,
        headers={"Cache-Control": "no-store"},
    )


async def _dashboard_logout(request):
    if not _dashboard_auth_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _dashboard_same_origin(request):
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    if not _dashboard_session_valid(request):
        return JSONResponse({"authenticated": False}, status_code=401, headers={"Cache-Control": "no-store"})
    response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(_DASH_COOKIE, path="/")
    return response


def _dashboard_index():
    """Serve the built dashboard shell, auto-injecting the API key for local control-plane roles."""
    index = _dashboard_dist / "index.html"
    if not index.is_file():
        return RedirectResponse(url="/viewer", status_code=307)
    key = os.getenv("BLENDER_MCP_API_KEY", "").strip()
    if key and not any(os.environ.get(f"BLENDER_MCP_{role}_KEY", "").strip() for role in ("VIEWER", "OPERATOR")):
        from blender_mcp.utils.farm_workers import mcp_role

        if mcp_role() in {"stand-alone", "master"}:
            script_key = json.dumps(key).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            script = "<script>window.__BLENDER_MCP_API_KEY__=" + script_key + ";</script>"
            html = index.read_text(encoding="utf-8")
            html = html.replace("</head>", script + "</head>", 1) if "</head>" in html else script + html
            return HTMLResponse(html, headers={"Cache-Control": "private, no-store"})
    return FileResponse(index)


async def _root_page(request):
    return _dashboard_index()


_dashboard_dist = Path(__file__).resolve().parents[2] / "webapp/frontend/dist"


asgi_app.router.routes.append(Route("/api/skills", endpoint=_skills_list_endpoint, methods=["GET"]))
asgi_app.router.routes.append(Route("/api/skills/{name}", endpoint=_skills_detail_endpoint, methods=["GET"]))
asgi_app.router.routes.append(Route("/viewer", endpoint=_viewer_page, methods=["GET"]))
asgi_app.router.routes.append(Route("/viewer/", endpoint=_viewer_page, methods=["GET"]))
asgi_app.router.routes.append(Route("/login", endpoint=_dashboard_login_page, methods=["GET", "HEAD"]))
asgi_app.router.routes.append(Route("/login/", endpoint=_dashboard_login_page, methods=["GET", "HEAD"]))
asgi_app.router.routes.append(Route("/api/v1/dashboard/login", endpoint=_dashboard_login, methods=["POST"]))
asgi_app.router.routes.append(Route("/api/v1/dashboard/session", endpoint=_dashboard_session, methods=["GET"]))
asgi_app.router.routes.append(Route("/api/v1/dashboard/logout", endpoint=_dashboard_logout, methods=["POST"]))
asgi_app.router.routes.append(Route("/", endpoint=_root_page, methods=["GET", "HEAD"]))
if _dashboard_dist.is_dir():
    from starlette.staticfiles import StaticFiles

    asgi_app.mount("/assets", StaticFiles(directory=_dashboard_dist / "assets"), name="dashboard-assets")
asgi_app.router.routes.append(Route("/api/v1/viewer/state", endpoint=_viewer_state, methods=["GET"]))
asgi_app.router.routes.append(Route("/api/v1/viewer/latest", endpoint=_viewer_latest, methods=["GET"]))
asgi_app.router.routes.append(Route("/api/v1/viewer/video", endpoint=_viewer_video, methods=["GET"]))

# Studio project/media APIs and ephemeral MCP worker enrollment.
from blender_mcp.utils.studio_api import register_gallery_routes

register_gallery_routes(asgi_app)

if _dashboard_dist.is_dir():

    async def _static_or_spa(request):
        full_path = request.path_params.get("full_path", "")
        if full_path and not any(part in {"", ".", ".."} for part in full_path.split("/")):
            candidate = (_dashboard_dist / full_path).resolve()
            if candidate.is_file() and _dashboard_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
        return _dashboard_index()

    asgi_app.router.routes.append(Route("/{full_path:path}", endpoint=_static_or_spa, methods=["GET", "HEAD"]))

asgi_app.add_middleware(_ApiKeyMiddleware)

asgi_app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:10849",
        "http://localhost:10849",
        "http://goliath:10849",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_origin_regex=r"https?://(?:[a-zA-Z0-9-]+\.ts\.net|.*?\.tail-[a-f0-9]+\.ts\.net|tauri\.localhost|localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|100\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?$|^tauri://localhost$",
    # Cookies are never shared with another origin: every dashboard login cookie
    # is same-site across localhost ports, so credentialed CORS would let any
    # local port read key-bearing dashboard HTML. Bearer auth is unaffected.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost: strips CORS headers from HTML (key-bearing dashboard shell).
asgi_app.add_middleware(_NoCrossOriginHtmlMiddleware)


# Ensure tool registration via app.py


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Blender MCP Server")

    # Server configuration
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind the server to")
    parser.add_argument("--port", type=int, default=8000, help="Port to run the server on")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["stdio", "http", "dual"],
        default=None,
        help="Transport mode: stdio, http, or dual (http+stdio). Overrides --http and --stdio.",
    )
    parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode (default)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    return parser.parse_args()


def get_recent_logs(level_filter=None, module_filter=None, limit=50, since_minutes=None):
    """Get recent logs with optional filtering."""
    global _memory_logs
    logs = _memory_logs.copy()

    if since_minutes:
        cutoff_time = datetime.datetime.now() - datetime.timedelta(minutes=since_minutes)
        logs = [log for log in logs if log["timestamp"] > cutoff_time]

    if level_filter:
        level_filter = level_filter.upper()
        logs = [log for log in logs if log["level"] == level_filter]

    if module_filter:
        logs = [log for log in logs if module_filter.lower() in log["name"].lower()]

    return logs[-limit:] if limit else logs


def main():
    """Main entry point for the Blender MCP server with unified transport (FastMCP 2.14.4+)."""
    from .transport import run_server

    # Configure logging before starting
    setup_logging("INFO")

    logger.info("[START] Starting Blender MCP Server")
    logger.info(f"Python version: {sys.version}")

    run_server(app, server_name="blender-mcp")


# =============================================================================
# MCP Entry Points - Industry Standard Installation
# =============================================================================


def create_server():
    """Create and return the MCP server instance.

    This function is used by MCP client libraries to automatically
    discover and instantiate the server.

    Returns:
        FastMCP app instance
    """
    return app


def main_stdio():
    """Entry point for stdio mode - used by most MCP clients.

    This is the standard way MCP servers communicate with clients
    through stdin/stdout streams.
    """
    import logging

    logging.basicConfig(level=logging.INFO)

    logger.info("[MCP] Starting Blender MCP server in stdio mode")
    logger.info("[MCP] Ready to accept MCP protocol messages")

    try:
        app.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("[MCP] Server stopped by user")
    except Exception as e:
        logger.error(f"[MCP] Server error: {e}")
        raise


def main_http(host="127.0.0.1", port=10771):
    """Entry point for HTTP mode - for web-based MCP clients.

    Args:
        host: Host to bind to
        port: Port to bind to
    """
    import logging

    logging.basicConfig(level=logging.INFO)

    logger = logging.getLogger(__name__)
    logger.info(f"[HTTP] Starting Blender MCP server on {host}:{port}")

    try:
        app.run(transport="http", host=host, port=port)
    except KeyboardInterrupt:
        logger.info("[HTTP] Server stopped by user")
    except Exception as e:
        logger.error(f"[HTTP] Server error: {e}")
        raise


if __name__ == "__main__":
    main()
