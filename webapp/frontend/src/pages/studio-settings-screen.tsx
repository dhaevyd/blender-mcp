import {
  Activity,
  ArrowUpRight,
  ChevronRight,
  HardDrive,
  LockKeyhole,
  LogOut,
  RefreshCw,
  Server,
} from "lucide-react";
import { useEffect, useState } from "react";
import { dashboardAuthHeaders } from "../api/dashboardAuth";
import type { NotificationPrefs } from "../common/notificationPrefs";

/** Host-side GPU discovery results. Absent on backends older than the probe. */
export type GpuProbe = {
  nvidia?: boolean | null;
  amd?: boolean | null;
  intel?: boolean | null;
  adapters?: Array<{ card?: string | null; driver?: string | null }> | null;
};

export type Diagnostics = {
  server?: {
    name?: string;
    version?: string;
    uptime_seconds?: number;
    tool_count?: number;
    status?: string;
    role?: string;
  };
  system?: {
    cpu_percent?: number;
    cpu_logical_threads?: number | null;
    load_1m?: number | null;
    sampled_at?: string;
    scope?: string;
    memory_percent?: number;
    memory_used_gb?: number;
    memory_total_gb?: number;
    disk_percent?: number | null;
    disk_used_gb?: number | null;
    disk_total_gb?: number | null;
    disk_scope?: string;
    gpu?: Array<{
      name?: string;
      memory_total_mib?: number | null;
      memory_used_mib?: number | null;
      utilization_percent?: number | null;
      temperature_c?: number | null;
      power_draw_w?: number | null;
    }>;
    gpu_probe?: GpuProbe | null;
  };
};

export type SettingsScreenProps = {
  cadence: 12 | 30 | 60 | "manual";
  onCadenceChange: (value: 12 | 30 | 60 | "manual") => void;
  storageError: boolean;
  health: { ok: boolean; version?: string; error?: string } | null;
  diagnostics: Diagnostics | null;
  authReady: boolean;
  authError: string;
  onConnect: () => void;
  onDisconnect: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  onNavigate: (screen: string) => void;
  dashboardSession: "unknown" | "active" | "inactive" | "unavailable";
  onSignOut: () => void;
  signingOut: boolean;
  signOutError: string;
  notificationPrefs: NotificationPrefs;
  onNotificationPrefsChange: (next: NotificationPrefs) => void;
  notificationsSaveError: boolean;
};

const CADENCE_OPTIONS = [12, 30, 60, "manual"] as const;

/** Per-event toggles shown under "Notify me when". The master switch is handled separately. */
const NOTIFICATION_TOGGLES: ReadonlyArray<{
  key: Exclude<keyof NotificationPrefs, "enabled">;
  label: string;
  hint: string;
}> = [
  { key: "jobFinished", label: "A render finishes", hint: "completed jobs" },
  { key: "jobFailed", label: "A render fails", hint: "failed jobs" },
  { key: "workerChanges", label: "A worker goes offline or returns", hint: "farm node status" },
  { key: "serverErrors", label: "The server reports a critical problem", hint: "coordinator and GPU alerts" },
];

export default function SettingsScreen({
  cadence,
  onCadenceChange,
  storageError,
  health,
  diagnostics,
  authReady,
  authError,
  onConnect,
  onDisconnect,
  onRefresh,
  refreshing,
  onNavigate,
  dashboardSession,
  onSignOut,
  signingOut,
  signOutError,
  notificationPrefs,
  onNotificationPrefsChange,
  notificationsSaveError,
}: SettingsScreenProps) {
  const system = diagnostics?.system;
  const server = diagnostics?.server;
  const isMaster = server?.role === "master";
  const [localRenderingEnabled, setLocalRenderingEnabled] = useState(true);
  const [localRenderingReady, setLocalRenderingReady] = useState(false);
  const [localRenderingBusy, setLocalRenderingBusy] = useState(false);
  const [localRenderingError, setLocalRenderingError] = useState("");

  useEffect(() => {
    if (!isMaster || !authReady) return;
    const controller = new AbortController();
    void fetch("/api/v1/farm/settings", { headers: dashboardAuthHeaders(), signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Settings unavailable (HTTP ${response.status}).`);
        const data = (await response.json()) as { local_rendering_enabled?: boolean };
        setLocalRenderingEnabled(data.local_rendering_enabled !== false);
        setLocalRenderingReady(true);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setLocalRenderingError(cause instanceof Error ? cause.message : "Could not load farm settings.");
          setLocalRenderingReady(true);
        }
      });
    return () => controller.abort();
  }, [isMaster, authReady]);

  async function updateLocalRendering(enabled: boolean) {
    setLocalRenderingBusy(true);
    setLocalRenderingError("");
    try {
      const response = await fetch("/api/v1/farm/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ local_rendering_enabled: enabled }),
      });
      if (!response.ok) throw new Error(`Could not save setting (HTTP ${response.status}).`);
      setLocalRenderingEnabled(enabled);
    } catch (cause) {
      setLocalRenderingError(cause instanceof Error ? cause.message : "Could not save farm settings.");
    } finally {
      setLocalRenderingBusy(false);
    }
  }
  const diskAvailable = system?.disk_scope === "workspace filesystem";
  const diskUsage =
    diskAvailable && typeof system?.disk_percent === "number"
      ? `${system.disk_percent}% used${typeof system.disk_used_gb === "number" && typeof system.disk_total_gb === "number" ? ` · ${system.disk_used_gb} / ${system.disk_total_gb} GB` : ""}`
      : "Unavailable";
  const connectionChip = health?.ok ? "is-good" : "is-bad";
  const connectionLabel = health?.ok
    ? "Coordinator responding"
    : health
      ? "Coordinator unavailable"
      : "Checking coordinator";
  const sessionChip =
    dashboardSession === "active" ? "is-good" : dashboardSession === "unavailable" ? "is-warn" : "is-bad";
  const sessionLabel =
    dashboardSession === "active"
      ? "Signed in"
      : dashboardSession === "inactive"
        ? "Signed out"
        : dashboardSession === "unavailable"
          ? "Disabled"
          : "Checking";
  const sessionCopy =
    dashboardSession === "active"
      ? "Signed in with a dashboard cookie"
      : dashboardSession === "inactive"
        ? "Not signed in"
        : dashboardSession === "unavailable"
          ? "Dashboard login disabled"
          : "Checking session";
  return (
    <section className="page settings-page" aria-label="Dashboard settings">
      <header className="page-head">
        <div>
          <div className="page-kicker">Control plane / local preferences</div>
          <h1>
            Settings<span className="page-period">.</span>
          </h1>
          <p>Connection, refresh cadence, local rendering and runtime details.</p>
        </div>
      </header>
      <div className="page-grid-2 settings-grid">
        <section className="nr-card settings-refresh" aria-labelledby="settings-refresh-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-refresh-title">Refresh cadence</h2>
              <p>How often this tab requests health, diagnostics, farm status and the latest preview.</p>
            </div>
            <span className="nr-card-count">{cadence === "manual" ? "Manual" : `${cadence}s`}</span>
          </div>
          <div className="settings-card-body">
            <fieldset className="settings-cadence">
              <legend className="page-kicker">Automatic refresh interval</legend>
              <div className="page-segment settings-cadence-segment">
                {CADENCE_OPTIONS.map((option) => (
                  <label key={option} className={cadence === option ? "is-active" : undefined}>
                    <input
                      type="radio"
                      name="refresh-cadence"
                      value={option}
                      checked={cadence === option}
                      onChange={() => onCadenceChange(option)}
                    />
                    <span>{option === "manual" ? "Manual" : `${option}s`}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <p className="page-muted settings-cadence-hint">
              Manual mode still loads once when the dashboard opens.
            </p>
            <p className={`page-note settings-note${storageError ? " is-warn" : ""}`} role="status">
              {storageError
                ? "Browser storage unavailable; this choice lasts until the page closes."
                : "Saved in this browser only. No server configuration is changed."}
            </p>
            <div className="page-form-actions">
              <button type="button" className="page-btn is-sm" onClick={onRefresh} disabled={refreshing}>
                <RefreshCw size={13} className={refreshing ? "spin" : ""} aria-hidden="true" />{" "}
                {refreshing ? "Refreshing…" : "Refresh now"}
              </button>
            </div>
          </div>
        </section>

        <section className="nr-card settings-notifications" aria-labelledby="settings-notifications-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-notifications-title">Notifications</h2>
              <p>Pop-up alerts in this dashboard when something changes. Saved in this browser only.</p>
            </div>
            <span className="nr-card-count">{notificationPrefs.enabled ? "On" : "Off"}</span>
          </div>
          <div className="settings-card-body">
            <label className="settings-toggle">
              <input
                type="checkbox"
                checked={notificationPrefs.enabled}
                onChange={(event) => onNotificationPrefsChange({ ...notificationPrefs, enabled: event.target.checked })}
              />
              <span>Show notifications</span>
            </label>
            <fieldset className="settings-toggle-group" disabled={!notificationPrefs.enabled}>
              <legend className="page-kicker">Notify me when</legend>
              {NOTIFICATION_TOGGLES.map((toggle) => (
                <label key={toggle.key} className="settings-toggle">
                  <input
                    type="checkbox"
                    checked={notificationPrefs[toggle.key]}
                    onChange={(event) =>
                      onNotificationPrefsChange({ ...notificationPrefs, [toggle.key]: event.target.checked })
                    }
                  />
                  <span>
                    {toggle.label}
                    <small className="page-muted"> {toggle.hint}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            <p className={`page-note settings-note${notificationsSaveError ? " is-warn" : ""}`} role="status">
              {notificationsSaveError
                ? "Browser storage unavailable; these choices last until the page closes."
                : "Changes apply immediately."}
            </p>
          </div>
        </section>

        <section className="nr-card settings-connection" aria-labelledby="settings-connection-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-connection-title">Coordinator connection</h2>
              <p>Farm access for this browser tab only.</p>
            </div>
            <span className="nr-card-count">{authReady ? "Key loaded" : "No key"}</span>
          </div>
          <div className="settings-card-body">
            <div className="settings-connection-state" role="status">
              <span className={`page-chip ${connectionChip}`}>
                <i aria-hidden="true" />
                {connectionLabel}
              </span>
            </div>
            <p className="page-muted">
              {health?.ok
                ? authReady
                  ? "Key present in this tab; authenticated farm access is attempted on refresh."
                  : "Health is available; connect to access authenticated farm data."
                : (health?.error ?? "Waiting for health check.")}
            </p>
            {authError && (
              <p className="page-note is-bad settings-error" role="alert">
                {authError}
              </p>
            )}
            <div className="page-form-actions settings-actions">
              {authReady ? (
                <button type="button" className="page-btn" onClick={onDisconnect}>
                  Disconnect this tab
                </button>
              ) : (
                <button type="button" className="page-btn is-primary" onClick={onConnect}>
                  Connect coordinator <ChevronRight size={14} aria-hidden="true" />
                </button>
              )}
            </div>
            <p className="page-muted settings-note">
              The key is never displayed here. Disconnect this tab forgets the API key stored in this tab only; it
              does not change server credentials or the browser session.
            </p>
          </div>
        </section>

        {isMaster && (
          <section className="nr-card settings-render-row" aria-labelledby="settings-farm-title">
            <div className="nr-card-head">
              <div>
                <h2 id="settings-farm-title">Local rendering</h2>
                <p>Turn off to keep this master dispatch-only.</p>
              </div>
              <span className="nr-card-count">
                {localRenderingBusy ? "Saving" : localRenderingEnabled ? "On" : "Off"}
              </span>
            </div>
            <div className="settings-card-body">
              <label className="page-switch settings-farm-toggle">
                <input
                  type="checkbox"
                  checked={localRenderingEnabled}
                  disabled={!authReady || !localRenderingReady || localRenderingBusy}
                  onChange={(event) => void updateLocalRendering(event.target.checked)}
                />
                <span className="page-switch-track" aria-hidden="true" />
                <span>{localRenderingBusy ? "Saving…" : "Allow local renders"}</span>
              </label>
              <p className="page-muted">
                When disabled, this coordinator dispatches jobs to workers instead of rendering them itself.
              </p>
              {localRenderingError && (
                <p className="page-note is-bad settings-error" role="alert">
                  {localRenderingError}
                </p>
              )}
            </div>
          </section>
        )}

        <section className="nr-card settings-details" aria-labelledby="settings-details-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-details-title">Runtime &amp; workspace</h2>
              <p>Read-only values reported by the coordinator.</p>
            </div>
            <HardDrive size={15} aria-hidden="true" className="settings-card-icon" />
          </div>
          <div className="settings-card-body">
            <dl className="page-table settings-facts">
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Server version</dt>
                <dd className="settings-fact-value">{server?.version ?? health?.version ?? "Unavailable"}</dd>
              </div>
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Server role</dt>
                <dd className="settings-fact-value">{server?.role ?? "Unavailable"}</dd>
              </div>
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Workspace filesystem</dt>
                <dd className="settings-fact-value">{diskUsage}</dd>
              </div>
            </dl>
            <p className="page-note settings-disk-note">
              {diskAvailable
                ? "Disk values refer to the configured workspace filesystem, not the container overlay."
                : "Workspace filesystem data was not reported. No container overlay estimate is shown."}
            </p>
          </div>
        </section>

        <section className="nr-card settings-session" aria-labelledby="settings-session-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-session-title">Dashboard session</h2>
              <p>Cookie session used by the dashboard UI.</p>
            </div>
            <LockKeyhole size={15} aria-hidden="true" className="settings-card-icon" />
          </div>
          <div className="settings-card-body">
            <div className="settings-session-state" role="status">
              <span className={`page-chip ${sessionChip}`}>
                <i aria-hidden="true" />
                {sessionLabel}
              </span>
              <span className="page-muted">{sessionCopy}</span>
            </div>
            {signOutError && (
              <p className="page-note is-bad settings-error" role="alert">
                {signOutError}
              </p>
            )}
            {dashboardSession === "active" && (
              <div className="page-form-actions">
                <button type="button" className="page-btn is-danger" onClick={onSignOut} disabled={signingOut}>
                  <LogOut size={13} aria-hidden="true" /> {signingOut ? "Signing out…" : "Sign out"}
                </button>
              </div>
            )}
            <p className="page-muted settings-note">
              Sign out is offered only when the coordinator reports an active dashboard session; it ends that cookie
              session server-side and returns to the login page.
            </p>
          </div>
        </section>

        <section className="nr-card settings-shortcuts" aria-labelledby="settings-shortcuts-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-shortcuts-title">Go to</h2>
              <p>Jump to the operational screens.</p>
            </div>
          </div>
          <div className="page-list settings-shortcut-list">
            <button type="button" className="page-list-row settings-shortcut" onClick={() => onNavigate("Workers")}>
              <Server size={15} aria-hidden="true" />
              <span>Workers</span>
              <ArrowUpRight size={14} aria-hidden="true" className="settings-shortcut-arrow" />
            </button>
            <button type="button" className="page-list-row settings-shortcut" onClick={() => onNavigate("Monitoring")}>
              <Activity size={15} aria-hidden="true" />
              <span>Monitoring</span>
              <ArrowUpRight size={14} aria-hidden="true" className="settings-shortcut-arrow" />
            </button>
          </div>
        </section>
      </div>
    </section>
  );
}
