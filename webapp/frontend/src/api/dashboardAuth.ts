const STORAGE_KEY = "blender-mcp-dashboard-key";

export function getDashboardKey(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setDashboardKey(key: string): void {
  window.sessionStorage.setItem(STORAGE_KEY, key.trim());
}

export function clearDashboardKey(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage may be unavailable in restricted browser contexts.
  }
}

export function dashboardAuthHeaders(): HeadersInit {
  const key = getDashboardKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}
