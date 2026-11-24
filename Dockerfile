# syntax=docker/dockerfile:1
#
# Blender MCP server image: MCP over HTTP, metrics, and the web dashboard.
#
# Build (from the repo root):
#   docker build -t blender-mcp:local .
#
# Run:
#   docker run --rm -p 10849:10849 -p 9091:9091 blender-mcp:local

ARG BLENDER_VERSION=4.2.3
ARG BLENDER_SERIES=4.2

# ---- Stage 1: build the dashboard -------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app/webapp/frontend
COPY webapp/frontend/package.json webapp/frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY webapp/frontend/ ./
RUN npm run build

# ---- Stage 2: runtime (Python + Blender) ------------------------------------
FROM python:3.12-slim AS runtime

ARG BLENDER_VERSION
ARG BLENDER_SERIES
ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    bzip2 \
    xz-utils \
    gosu \
    libgl1 \
    libegl1 \
    libglib2.0-0 \
    libx11-6 \
    libxi6 \
    libxxf86vm1 \
    libxfixes3 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    libice6 \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q -O /tmp/blender.tar.xz \
        "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
    && tar -xf /tmp/blender.tar.xz -C /opt \
    && mv "/opt/blender-${BLENDER_VERSION}-linux-x64" /opt/blender \
    && rm /tmp/blender.tar.xz

ENV PATH="/opt/blender:${PATH}" \
    BLENDER_EXECUTABLE=/opt/blender/blender \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=10849 \
    PROMETHEUS_PORT=9091 \
    BLENDER_MCP_METRICS_ENABLED=true \
    BLENDER_MCP_LOG_FORMAT=json \
    BLENDER_MCP_VERSION=${VERSION}

RUN useradd --create-home --shell /bin/bash mcp \
    && mkdir -p /app/logs /workspace

WORKDIR /app

# Install dependencies first so source-only changes rebuild quickly.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY addon/ ./addon/
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[monitoring]"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
COPY --from=frontend /app/webapp/frontend/dist/ ./webapp/frontend/dist/

RUN chown -R mcp:mcp /app /workspace

EXPOSE 10849 9091

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10849/api/v1/health', timeout=5)"

# The entrypoint fixes ownership of the mounted log and workspace folders,
# then drops to the unprivileged `mcp` user. Do not set `user:` in compose.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "blender_mcp.server"]

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.title="Blender MCP" \
      org.opencontainers.image.description="AI-Powered 3D Creation MCP Server" \
      org.opencontainers.image.source="https://github.com/dhaevyd/blender-mcp" \
      org.opencontainers.image.licenses="MIT"