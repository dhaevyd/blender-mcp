import { ChevronDown } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  GPU_LABEL_MAX_LENGTH,
  gpuDefaultLabel,
  gpuKey,
  setGpuLabel,
  toggleGpuCollapsed,
  useGpuPrefs,
} from "../pages/gpu-card-prefs";

/**
 * Editable device badge. Renders "GPU 01" until the operator renames the card,
 * then keeps that name for this device in this browser. Enter or blur commits,
 * Escape reverts, and an empty value restores the default.
 */
export function GpuNameBadge({ name, index }: { name?: string | null; index: number }) {
  const deviceKey = gpuKey(name, index);
  const fallback = gpuDefaultLabel(index);
  const { label } = useGpuPrefs(deviceKey, index);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(label);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [editing]);

  function startEditing() {
    setDraft(label);
    setEditing(true);
  }

  function commit() {
    setGpuLabel(deviceKey, draft);
    setEditing(false);
  }

  function cancel() {
    setDraft(label);
    setEditing(false);
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="gpu-name-input"
        value={draft}
        maxLength={GPU_LABEL_MAX_LENGTH}
        aria-label={`Rename ${label}`}
        placeholder={fallback}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          } else if (event.key === "Escape") {
            event.preventDefault();
            cancel();
          }
        }}
      />
    );
  }

  return (
    <button
      type="button"
      className="gpu-name-badge"
      title="Rename this GPU"
      aria-label={`${label} — rename`}
      onClick={startEditing}
    >
      {label}
    </button>
  );
}

/** Chevron that collapses the card body, remembered per device. */
export type GpuCardScope = "overview" | "monitoring";

export function GpuCollapseTitle({
  name,
  index,
  scope = "overview",
  children,
}: {
  name?: string | null;
  index: number;
  scope?: GpuCardScope;
  children: ReactNode;
}) {
  const deviceKey = gpuKey(name, index);
  const { label, collapsed } = useGpuPrefs(deviceKey, index, scope);
  return (
    <button
      type="button"
      className="gpu-title-toggle"
      aria-expanded={!collapsed}
      aria-label={collapsed ? `Expand ${label} details` : `Collapse ${label} details`}
      title={collapsed ? "Expand GPU details" : "Collapse GPU details"}
      onClick={() => toggleGpuCollapsed(deviceKey, undefined, scope)}
    >
      {children}
    </button>
  );
}

export function GpuCollapseButton({
  name,
  index,
  scope = "overview",
}: {
  name?: string | null;
  index: number;
  scope?: GpuCardScope;
}) {
  const deviceKey = gpuKey(name, index);
  const { label, collapsed } = useGpuPrefs(deviceKey, index, scope);
  return (
    <button
      type="button"
      className="gpu-collapse"
      aria-expanded={!collapsed}
      aria-label={collapsed ? `Expand ${label} details` : `Collapse ${label} details`}
      title={collapsed ? "Expand details" : "Collapse details"}
      onClick={() => toggleGpuCollapsed(deviceKey, undefined, scope)}
    >
      <ChevronDown size={14} aria-hidden="true" />
    </button>
  );
}

/** Cards hide their body when collapsed so a long GPU list stays scannable. */
export function useGpuCardState(name: string | null | undefined, index: number, scope: GpuCardScope = "overview") {
  const deviceKey = gpuKey(name, index);
  const { label, collapsed } = useGpuPrefs(deviceKey, index, scope);
  return { deviceKey, label, collapsed };
}
