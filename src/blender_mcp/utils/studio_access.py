"""Least-privilege bearer credentials for Studio HTTP operations."""

import hmac
import os
import re

_JOB = r"/api/v1/farm/jobs/[a-zA-Z0-9_-]+"


def scoped_role(credential: bytes) -> str | None:
    for role in ("viewer", "operator"):
        key = os.environ.get(f"BLENDER_MCP_{role.upper()}_KEY", "").strip()
        if key and hmac.compare_digest(credential, key.encode()):
            return role
    return None


def scoped_access(role: str, method: str, path: str) -> bool:
    if method in {"GET", "HEAD"}:
        return path in {
            "/api/v1/farm/status", "/api/v1/farm/gpus", "/api/v1/farm/jobs", "/api/v1/farm/operations",
            "/api/v1/workers", "/api/v1/monitoring/gpu", "/api/v1/farm/access",
        } or bool(re.fullmatch(_JOB + r"(?:/frames/\d+/download)?", path))
    return role == "operator" and method == "POST" and (
        path == "/api/v1/farm/jobs" or bool(re.fullmatch(_JOB + "/actions", path))
    )


def worker_audit_action(method: str, path: str) -> str | None:
    if method == "POST" and path in {"/api/v1/workers/enroll", "/api/v1/workers/connect"}:
        return "worker." + path.rsplit("/", 1)[-1]
    if method == "POST" and re.fullmatch(r"/api/v1/workers/[\w-]+/test", path):
        return "worker.test"
    if re.fullmatch(r"/api/v1/workers/[\w-]+", path):
        return {"PATCH": "worker.edit", "DELETE": "worker.remove"}.get(method)
    return None
