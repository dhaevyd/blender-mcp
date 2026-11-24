# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# blender-mcp — AI-Powered Blender Automation

Control Blender with natural language through MCP (Model Context Protocol).

## Commands

**Backend (Python / uv)**
- Run server: `uv run python run_server.py`
- Tests: `uv run pytest`
- Single test: `uv run pytest tests/test_scripting_handler.py::test_name -xvs`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Type check: `uv run mypy src`

**Frontend (webapp/frontend)**
- Dev server: `npm run dev`
- Build: `npm run build`
- E2E tests: `npx playwright test`

**Docker**
- Build + start: `docker compose build && docker compose up -d`
- No `VERSION` build arg required — defaults to `dev` for local builds; CI passes `--build-arg VERSION=x.y.z`.

## Architecture

### Backend

The server is a **FastMCP** application (`src/blender_mcp/server.py`) that exposes Blender operations as MCP tools. The key abstraction layers:

1. **Portmanteau Pattern**: Each domain area (scene, mesh, render, etc.) has a handler file in `handlers/` and a tools file in `tools/`. The handler contains the business logic; the tools file registers MCP tool endpoints that delegate to it. Always pair a new handler function with a tools registration.

2. **`@blender_operation` decorator** (`decorators.py`): Wraps handler functions with logging, error handling, and optional Blender availability validation. Use it on all handler functions.

3. **Three-tier Blender execution** (`utils/blender_runtime.py`): Scripts are sent to Blender via:
   - TCP socket bridge (v2 addon — fastest)
   - HTTP poll bridge (v1 addon — legacy)
   - Headless subprocess (`blender --background` — always works)
   The executor (`utils/blender_executor.py`) selects the method automatically.

4. **Script safety**: User-provided scripts are embedded using `repr()` — never f-string interpolation — to prevent triple-quote injection.

5. **Auth**: API key middleware for MCP endpoints; session tokens (8-char UUID with TTL) for the dashboard. Rate limiting on login.

6. **Session tasks** (`_session_tasks`): In-memory task tracking with full UUIDs and TTL expiry; a prune function cleans stale entries.

### Frontend (webapp/frontend)

React + TypeScript dashboard for fleet visibility:

- **Single-page app**: Entry is `main.tsx` → `pages/studio-dashboard.tsx` (the active app). The old `App.tsx` has been removed.
- **API layer**: `src/api/mcp.ts` (4 active exports) and `src/api/dashboardAuth.ts`. Auth token stored in `sessionStorage`; other state in `localStorage`.
- **Styling**: Tailwind + a large set of custom CSS files co-located with components in `pages/`. Both systems are active.
- **Pages**: All screens live in `src/pages/` as `studio-*.tsx` files with matched CSS.

## Code Style & Standards

- All handler functions must be `async`, use `@blender_operation`, and have type hints on every argument and return value.
- MCP tool names follow the portmanteau pattern: `blender_<domain>_<action>` (e.g. `blender_scene_list_objects`).
- Do not embed user-supplied strings into Blender scripts with f-strings — use `repr()`.
