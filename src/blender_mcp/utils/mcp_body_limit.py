"""Raise the MCP HTTP request-body cap above the SDK's 4 MiB default.

FastMCP builds its streamable-HTTP session manager without a body limit, so the
MCP SDK's ``RequestBodyLimitMiddleware`` answers ``413 Request Entity Too Large``
for any POST over 4 MiB. Farm project bundles (up to 512 MiB, base64-encoded in the
JSON-RPC body) and large scripts need more room. This module subclasses the
session manager and rebuilds that middleware with an explicit limit.
"""

from __future__ import annotations

import logging
import os

try:
    from mcp.server.transport_security import RequestBodyLimitMiddleware as _RequestBodyLimitMiddleware
    RequestBodyLimitMiddleware = _RequestBodyLimitMiddleware
except ImportError:
    RequestBodyLimitMiddleware = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

DEFAULT_LIMIT_BYTES = 1024 * 1024 * 1024
_ENV = "BLENDER_MCP_MAX_REQUEST_BODY_MIB"


def configured_limit_bytes() -> int:
    """Return the body cap in bytes; ``BLENDER_MCP_MAX_REQUEST_BODY_MIB`` overrides it."""
    raw = os.environ.get(_ENV, "").strip()
    if not raw:
        return DEFAULT_LIMIT_BYTES
    try:
        mib = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a whole number; using the default", _ENV, raw)
        return DEFAULT_LIMIT_BYTES
    return max(1, mib) * 1024 * 1024


def apply() -> int:
    """Patch FastMCP's HTTP session manager so MCP requests accept the configured size."""
    import fastmcp.server.http as fastmcp_http

    limit = configured_limit_bytes()
    base = fastmcp_http.FastMCPStreamableHTTPSessionManager
    if getattr(base, "_blender_mcp_body_limit", False):
        return limit

    class _SizedSessionManager(base):
        _blender_mcp_body_limit = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.max_request_body_size = limit
            if RequestBodyLimitMiddleware is not None:
                self.asgi_app = RequestBodyLimitMiddleware(self._handle_request, limit)

    fastmcp_http.FastMCPStreamableHTTPSessionManager = _SizedSessionManager
    logger.info("MCP request body limit set to %d MiB", limit // (1024 * 1024))
    return limit