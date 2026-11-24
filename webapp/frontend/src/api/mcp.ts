/**
 * MCP API Client for Blender MCP WebApp
 *
 * Thin read-only helpers for the dashboard (health, diagnostics, farm).
 * Base URL: /mcp in dev (Vite proxy), VITE_API_BASE otherwise.
 */

import { dashboardAuthHeaders } from "./dashboardAuth";

// The Tauri shell (src-tauri) has no dev proxy, so it talks to the backend directly.
const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
const API_BASE = import.meta.env.DEV
  ? "/mcp"
  : (import.meta.env.VITE_API_BASE ?? (isTauri ? "http://127.0.0.1:10849" : ""));

interface HealthPayload {
  status: string;
  server: string;
  version: string;
  uptime_seconds: number;
  tool_count: number;
}

interface DiagnosticsPayload {
  server: { tool_count: number };
  system: { cpu_percent: number; memory_percent: number };
  blender: { connected: boolean };
}

/**
 * Check if backend is reachable (does not use tool bridge).
 * Use to show a clear error when proxy or server is wrong.
 */
export async function getBackendHealth(): Promise<{
  ok: boolean;
  error?: string;
  data?: HealthPayload;
}> {
  try {
    const r = await fetch(`${API_BASE}/api/v1/health`, { headers: dashboardAuthHeaders() });
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    const data = (await r.json()) as HealthPayload;
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "Network error" };
  }
}

/**
 * Get full diagnostics from the backend (system info, tool list, etc.)
 */
export async function getDiagnostics(signal?: AbortSignal): Promise<DiagnosticsPayload | null> {
  try {
    const r = await fetch(`${API_BASE}/api/v1/diagnostics`, { headers: dashboardAuthHeaders(), signal });
    if (!r.ok) return null;
    return (await r.json()) as DiagnosticsPayload;
  } catch {
    return null;
  }
}

export async function getFarmGpuTelemetry<T>(signal?: AbortSignal): Promise<T | null> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/farm/gpus`, {
      headers: dashboardAuthHeaders(),
      cache: "no-store",
      signal,
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export async function getFarmStatus<T>(): Promise<{ success: boolean; data?: T; error?: string }> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/farm/status`, { headers: dashboardAuthHeaders() });
    if (!response.ok) return { success: false, error: `HTTP ${response.status}` };
    const data = (await response.json()) as T & { success?: boolean; error?: string };
    if (data?.success !== true) {
      return { success: false, error: data?.error ?? "Render coordinator did not return status." };
    }
    return { success: true, data };
  } catch {
    return { success: false, error: "Render coordinator status unavailable." };
  }
}
