import { spawn } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendDir, "../..");
const workspace = path.join(repoRoot, "data", "workspace");
const logs = path.join(repoRoot, "data", "logs");
const envFile = path.join(repoRoot, ".env");
mkdirSync(workspace, { recursive: true });
mkdirSync(logs, { recursive: true });

const backendEnv = {
  ...process.env,
  MCP_TRANSPORT: "http",
  MCP_HOST: "127.0.0.1",
  MCP_PORT: "10850",
  MCP_ROLE: process.env.MCP_ROLE || "stand-alone",
  BLENDER_WORKSPACE: process.env.BLENDER_WORKSPACE || workspace,
  MCP_MONITOR_DB: process.env.MCP_MONITOR_DB || path.join(logs, "monitoring.db"),
};
const backendArgs = ["run", "--project", repoRoot];
if (existsSync(envFile)) backendArgs.push("--env-file", envFile);
backendArgs.push(
  "uvicorn",
  "blender_mcp.server:asgi_app",
  "--host",
  "127.0.0.1",
  "--port",
  "10850",
);

const children = [];
let shuttingDown = false;
function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  }
  process.exitCode = code;
}

const backend = spawn(process.platform === "win32" ? "uv.exe" : "uv", backendArgs, {
  cwd: repoRoot,
  env: backendEnv,
  stdio: "inherit",
});
children.push(backend);
const vite = spawn(
  process.execPath,
  [path.join(frontendDir, "node_modules", "vite", "bin", "vite.js"), "--host", "0.0.0.0", "--port", "8084"],
  { cwd: frontendDir, env: process.env, stdio: "inherit" },
);
children.push(vite);

for (const child of children) {
  child.on("error", (error) => {
    console.error("Development service failed to start:", error.message);
    shutdown(1);
  });
  child.on("exit", (code) => {
    if (!shuttingDown) shutdown(code ?? 1);
  });
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
console.log("Blender MCP dev stack: UI http://0.0.0.0:8084 → source backend http://127.0.0.1:10850");
