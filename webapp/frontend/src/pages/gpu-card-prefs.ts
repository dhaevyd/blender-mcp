/**
 * Per-GPU card preferences: a custom display name and a collapsed state.
 *
 * Both live in localStorage and are shared through one external store. Custom
 * names are shared across pages; collapsed state is scoped by page so Overview
 * and Monitoring can remember independent layouts. Keys derive from hardware
 * name + index, stable across refreshes even if device order changes.
 */

import { useSyncExternalStore } from "react";

const LABELS_KEY = "blender-mcp-gpu-labels";
const COLLAPSED_KEY = "blender-mcp-gpu-collapsed";
export const GPU_LABEL_MAX_LENGTH = 24;

type GpuPrefState = {
  labels: Record<string, string>;
  collapsed: Record<string, boolean>;
};

function readRecord(key: string): Record<string, unknown> {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return parsed as Record<string, unknown>;
  } catch {
    return {};
  }
}

function readLabels(): Record<string, string> {
  const record = readRecord(LABELS_KEY);
  const labels: Record<string, string> = {};
  for (const [key, value] of Object.entries(record)) {
    if (typeof value === "string" && value.trim()) labels[key] = value;
  }
  return labels;
}

function readCollapsed(): Record<string, boolean> {
  const record = readRecord(COLLAPSED_KEY);
  const collapsed: Record<string, boolean> = {};
  for (const [key, value] of Object.entries(record)) {
    if (value === true) collapsed[key] = true;
  }
  return collapsed;
}

const state: GpuPrefState = {
  labels: readLabels(),
  collapsed: readCollapsed(),
};
let version = 0;
const listeners = new Set<() => void>();

function persist() {
  try {
    window.localStorage.setItem(LABELS_KEY, JSON.stringify(state.labels));
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(state.collapsed));
  } catch {
    // Restricted contexts (private mode, full quota) keep the in-memory state.
  }
}

function emit() {
  version += 1;
  persist();
  for (const listener of listeners) listener();
}

export function subscribeGpuPrefs(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function gpuPrefsVersion() {
  return version;
}

/** Stable identity for a device card, independent of render order. */
export function gpuKey(name: string | null | undefined, index: number) {
  return `${name ?? "gpu"}::${index}`;
}

export function gpuDefaultLabel(index: number) {
  return `GPU ${String(index + 1).padStart(2, "0")}`;
}

export function gpuLabelFor(key: string, index: number) {
  const saved = state.labels[key];
  return saved?.trim() ? saved : gpuDefaultLabel(index);
}

function collapseKey(key: string, scope: string) {
  // Keep legacy keys as Overview state so existing saved preferences survive.
  return scope === "overview" ? key : `${scope}::${key}`;
}

export function isGpuCollapsed(key: string, scope = "overview") {
  return state.collapsed[collapseKey(key, scope)] === true;
}

export function setGpuLabel(key: string, value: string) {
  const trimmed = value.trim().slice(0, GPU_LABEL_MAX_LENGTH);
  if (trimmed) state.labels[key] = trimmed;
  else delete state.labels[key];
  emit();
}

export function toggleGpuCollapsed(key: string, next?: boolean, scope = "overview") {
  const storageKey = collapseKey(key, scope);
  const value = typeof next === "boolean" ? next : !isGpuCollapsed(key, scope);
  if (value) state.collapsed[storageKey] = true;
  else delete state.collapsed[storageKey];
  emit();
}

export function setGpuCollapsedForKeys(keys: string[], collapsed: boolean, scope = "overview") {
  for (const key of keys) {
    const storageKey = collapseKey(key, scope);
    if (collapsed) state.collapsed[storageKey] = true;
    else delete state.collapsed[storageKey];
  }
  emit();
}

/** Subscribe a component to this device's saved name and collapsed state. */
export function useGpuPrefs(key: string, index: number, scope = "overview") {
  useSyncExternalStore(subscribeGpuPrefs, gpuPrefsVersion, () => 0);
  return { label: gpuLabelFor(key, index), collapsed: isGpuCollapsed(key, scope) };
}

/** True when every supplied card is collapsed; false for an empty list. */
export function useAllGpusCollapsed(keys: string[], scope = "overview") {
  useSyncExternalStore(subscribeGpuPrefs, gpuPrefsVersion, () => 0);
  return keys.length > 0 && keys.every((key) => isGpuCollapsed(key, scope));
}
