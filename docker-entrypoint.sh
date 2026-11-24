#!/bin/sh
set -e

# Bind mounts are created root-owned by the daemon on first `up`;
# the server runs as the unprivileged `mcp` user, so make the runtime
# dirs writable before dropping privileges.
mkdir -p /app/logs /workspace
chown -R mcp:mcp /app/logs
chown mcp:mcp /workspace

exec gosu mcp "$@"
