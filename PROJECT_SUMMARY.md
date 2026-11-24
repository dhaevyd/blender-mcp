# blender-mcp — Project Summary

**AI-powered Blender automation via MCP (Model Context Protocol). Control Blender with natural language through Claude Desktop or any MCP client.**

---

## What It Is

A FastMCP server that exposes 48+ Blender operations as MCP tools. Claude (or any LLM) sends natural-language instructions; the server translates them into `bpy` Python scripts and executes them in Blender — headless or live.

**Repo:** `sandraschi/blender-mcp` | **License:** MIT | **Python:** 3.12+ | **Blender:** 3.0+

---

## Stack

| Layer | Tech |
|-------|------|
| MCP server | Python + FastMCP 3.2 |
| Frontend dashboard | React + TypeScript + Tailwind |
| Packaging | `.mcpb` (drag-drop into Claude Desktop), `uv`, `hatchling` |
| Container | Docker (`ghcr.io/dhaevyd/blender-mcp:1.5.3`) |
| Metrics | Prometheus (port 9091) + Grafana + Loki (optional profile) |
| Auth | API key middleware (MCP); session tokens (dashboard) |

---

## Architecture

### Backend (`src/blender_mcp/`)

- **Portmanteau pattern** — each domain has a `handlers/<domain>.py` (logic) + `tools/<domain>.py` (MCP registration). Always pair them.
- **`@blender_operation` decorator** — wraps all handler functions with logging, error handling, Blender availability check.
- **Three-tier execution** (auto-selected):
  1. TCP socket bridge (v2 addon — fastest, requires live GUI)
  2. HTTP poll bridge (v1 addon — legacy)
  3. Headless subprocess `blender --background` (always works)
- **Script safety** — user scripts embedded with `repr()`, never f-string interpolation.
- **Session tasks** — in-memory task tracking with UUIDs + TTL; pruned automatically.

### Frontend (`webapp/frontend/`)

- SPA: `main.tsx` → `pages/studio-dashboard.tsx`
- API: `src/api/mcp.ts` + `src/api/dashboardAuth.ts`
- Auth token in `sessionStorage`; other state in `localStorage`
- All screens in `src/pages/studio-*.tsx`

---

## Docker Setup (this machine)

```yaml
image: ghcr.io/dhaevyd/blender-mcp:1.5.3
ports: 10849 (MCP/HTTP), 9091 (Prometheus)
volumes: ./data/workspace:/workspace, ./data/logs:/app/logs
GPU: NVIDIA (all devices, compute+graphics)
```

Key env vars: `MCP_TRANSPORT=http`, `BLENDER_EXECUTABLE=/opt/blender/blender`, `MCP_ROLE`, `BLENDER_MCP_API_KEY`

---

## Key Commands

```bash
# Backend
uv run python run_server.py     # run server
uv run pytest                   # tests
uv run ruff check .             # lint
uv run mypy src                 # type check

# Frontend
npm run dev                     # dev server
npm run build                   # production build
npx playwright test             # E2E tests

# Docker
docker compose up blender-mcp
docker compose --profile monitoring up -d   # + Prometheus/Grafana/Loki
```

---

## Capabilities

- Natural-language 3D scene creation (mesh, materials, lighting, animation)
- Live GUI bridge — watch agent build in Blender viewport
- Export: `.glb`, `.gltf`, `.fbx`, `.obj`, `.usd`, `.vrm` (VRChat-ready)
- Render: viewport PNG, MP4 (VSE), Gaussian splats
- Generative AI hooks: Rodin / Tripo / Hunyuan mesh generation
- Webapp dashboard: `http://localhost:10848`

---

## Fleet Integrations

| Downstream | Asset type |
|------------|------------|
| godot-mcp | `.glb/.gltf` game assets |
| vrchat-mcp | `.vrm` avatars |
| unity3d-mcp | `.fbx/.glb` |
| tahoma2d-mcp | Rendered image sequences |
| freecad-mcp | `.step` via intermediate export |

---

## Docs (`docs/`)

`ARCHITECTURE.md` · `CONFIGURATION.md` · `FEATURES.md` · `DEVELOPMENT.md` · `DOCKER.md` · `BLENDER_FARM.md` · `MONITORING.md` · `TROUBLESHOOTING.md` · `ROADMAP.md` · `EXAMPLES.md`

---

*Generated 2026-11-24*
