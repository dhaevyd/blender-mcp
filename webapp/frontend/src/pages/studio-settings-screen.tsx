import {
  Activity,
  ArrowUpRight,
  ChevronRight,
  HardDrive,
  LogOut,
  RefreshCw,
  Server,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { dashboardAuthHeaders } from "../api/dashboardAuth";
import type { NotificationLogEntry } from "../common/notificationLog";
import type { NotificationPrefs } from "../common/notificationPrefs";

export type GpuProbe = {
  nvidia?: boolean | null;
  amd?: boolean | null;
  intel?: boolean | null;
  adapters?: Array<{ card?: string | null; driver?: string | null }> | null;
};

export type Diagnostics = {
  host?: {
    gpu_available?: boolean;
    render_device?: string;
    gpu_required?: boolean;
    virtualization?: { kind?: string; virtual?: boolean | null; container?: boolean | null };
  };
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
  eventLog: NotificationLogEntry[];
  onClearEvents: () => void;
};

const CADENCE_OPTIONS = [12, 30, 60, "manual"] as const;

const NOTIFICATION_TOGGLES: ReadonlyArray<{
  key: Exclude<keyof NotificationPrefs, "enabled">;
  label: string;
}> = [
  { key: "jobFinished", label: "A render finishes" },
  { key: "jobFailed", label: "A render fails" },
  { key: "workerChanges", label: "A worker goes offline or returns" },
  { key: "serverErrors", label: "Server reports a critical problem" },
];

function useFarmSettings(isMaster: boolean, authReady: boolean) {
  const [localRenderingEnabled, setLocalRenderingEnabled] = useState(true);
  const [localRenderingReady, setLocalRenderingReady] = useState(false);
  const [localRenderingBusy, setLocalRenderingBusy] = useState(false);
  const [localRenderingError, setLocalRenderingError] = useState("");
  const [sessionTtlSeconds, setSessionTtlSeconds] = useState(900);
  const [sessionTtlInput, setSessionTtlInput] = useState("15");
  const [sessionTtlBusy, setSessionTtlBusy] = useState(false);
  const [sessionTtlError, setSessionTtlError] = useState("");
  const [sessionTtlSaved, setSessionTtlSaved] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!isMaster || !authReady) return;
    const controller = new AbortController();
    fetch("/api/v1/farm/settings", { headers: dashboardAuthHeaders(), signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as { local_rendering_enabled?: boolean; session_ttl_seconds?: number };
        setLocalRenderingEnabled(data.local_rendering_enabled !== false);
        if (typeof data.session_ttl_seconds === "number") {
          setSessionTtlSeconds(data.session_ttl_seconds);
          setSessionTtlInput(String(Math.round(data.session_ttl_seconds / 60)));
        }
        setLocalRenderingReady(true);
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setLocalRenderingError(err instanceof Error ? err.message : "Could not load settings.");
          setLocalRenderingReady(true);
        }
      });
    return () => controller.abort();
  }, [isMaster, authReady]);

  async function saveLocalRendering(enabled: boolean) {
    setLocalRenderingBusy(true);
    setLocalRenderingError("");
    try {
      const res = await fetch("/api/v1/farm/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ local_rendering_enabled: enabled }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setLocalRenderingEnabled(enabled);
    } catch (err) {
      setLocalRenderingError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setLocalRenderingBusy(false);
    }
  }

  async function saveSessionTtl() {
    const minutes = parseInt(sessionTtlInput, 10);
    if (!Number.isFinite(minutes) || minutes < 5) {
      setSessionTtlError("Minimum 5 minutes.");
      return;
    }
    if (minutes > 10080) {
      setSessionTtlError("Maximum 7 days (10080 min).");
      return;
    }
    setSessionTtlBusy(true);
    setSessionTtlError("");
    setSessionTtlSaved(false);
    try {
      const res = await fetch("/api/v1/farm/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ session_ttl_seconds: minutes * 60 }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { session_ttl_seconds?: number };
      if (typeof data.session_ttl_seconds === "number") {
        setSessionTtlSeconds(data.session_ttl_seconds);
        setSessionTtlInput(String(Math.round(data.session_ttl_seconds / 60)));
      }
      setSessionTtlSaved(true);
      if (savedTimer.current) clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSessionTtlSaved(false), 2500);
    } catch (err) {
      setSessionTtlError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setSessionTtlBusy(false);
    }
  }

  return {
    localRenderingEnabled, localRenderingReady, localRenderingBusy, localRenderingError, saveLocalRendering,
    sessionTtlSeconds, sessionTtlInput, setSessionTtlInput, sessionTtlBusy, sessionTtlError, sessionTtlSaved, saveSessionTtl,
  };
}

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
  eventLog,
  onClearEvents,
}: SettingsScreenProps) {
  const system = diagnostics?.system;
  const server = diagnostics?.server;
  const isMaster = server?.role === "master";

  const farm = useFarmSettings(isMaster, authReady);

  const diskAvailable = system?.disk_scope === "workspace filesystem";
  const diskUsage =
    diskAvailable && typeof system?.disk_percent === "number"
      ? `${system.disk_percent}% used${
          typeof system.disk_used_gb === "number" && typeof system.disk_total_gb === "number"
            ? ` · ${system.disk_used_gb} / ${system.disk_total_gb} GB`
            : ""
        }`
      : "Unavailable";

  const connectionStatus = health?.ok ? "good" : "bad";
  const connectionLabel = health?.ok
    ? "Coordinator responding"
    : health
      ? "Coordinator unavailable"
      : "Checking…";

  const sessionStatus =
    dashboardSession === "active" ? "good" : dashboardSession === "unavailable" ? "warn" : "bad";
  const sessionLabel =
    dashboardSession === "active" ? "Signed in"
    : dashboardSession === "inactive" ? "Signed out"
    : dashboardSession === "unavailable" ? "Auth disabled"
    : "Checking…";

  const ttlChanged = farm.sessionTtlInput !== String(Math.round(farm.sessionTtlSeconds / 60));

  return (
    <section className="page settings-page" aria-label="Dashboard settings">
      <header className="page-head">
        <div>
          <div className="page-kicker">Control plane / local preferences</div>
          <h1>Settings<span className="page-period">.</span></h1>
          <p>Connection, refresh cadence, farm controls and runtime details.</p>
        </div>
      </header>

      <div className="settings-grid">

        {/* ── Row 1: This browser ─────────────────────────────────────── */}
        <div className="settings-group-label">This browser</div>

        <section className="nr-card" aria-labelledby="settings-refresh-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-refresh-title">Refresh cadence</h2>
              <p>How often this tab polls the coordinator.</p>
            </div>
            <span className="nr-card-count">{cadence === "manual" ? "Manual" : `${cadence}s`}</span>
          </div>
          <div className="settings-card-body">
            <fieldset className="settings-cadence">
              <legend className="page-kicker">Interval</legend>
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
            <p className="page-muted">Manual still loads once on open.</p>
            {storageError && (
              <p className="page-note is-warn" role="status">
                Browser storage unavailable — choice resets on page close.
              </p>
            )}
            <div className="page-form-actions">
              <button type="button" className="page-btn is-sm" onClick={onRefresh} disabled={refreshing}>
                <RefreshCw size={13} className={refreshing ? "spin" : ""} aria-hidden="true" />
                {refreshing ? "Refreshing…" : "Refresh now"}
              </button>
            </div>
          </div>
        </section>

        <section className="nr-card" aria-labelledby="settings-notifications-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-notifications-title">Notifications</h2>
              <p>In-app alerts when things change. Stored in this browser.</p>
            </div>
            <span className={`settings-status-badge ${notificationPrefs.enabled ? "is-on" : "is-off"}`}>
              {notificationPrefs.enabled ? "On" : "Off"}
            </span>
          </div>
          <div className="settings-card-body">
            <label className="settings-master-toggle">
              <input
                type="checkbox"
                checked={notificationPrefs.enabled}
                onChange={(e) => onNotificationPrefsChange({ ...notificationPrefs, enabled: e.target.checked })}
              />
              <span>Show notifications</span>
            </label>
            <fieldset className="settings-sub-toggles" disabled={!notificationPrefs.enabled}>
              <legend className="page-kicker">Notify when</legend>
              {NOTIFICATION_TOGGLES.map((t) => (
                <label key={t.key} className="settings-sub-toggle">
                  <input
                    type="checkbox"
                    checked={notificationPrefs[t.key]}
                    onChange={(e) => onNotificationPrefsChange({ ...notificationPrefs, [t.key]: e.target.checked })}
                  />
                  <span>{t.label}</span>
                </label>
              ))}
            </fieldset>
            {notificationsSaveError && (
              <p className="page-note is-warn" role="status">
                Storage unavailable — resets on close.
              </p>
            )}
          </div>
        </section>

        <section className="nr-card settings-event-log-card" aria-labelledby="settings-event-log-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-event-log-title">Event log</h2>
              <p>Notifications raised in this browser session, newest first.</p>
            </div>
            <span className="nr-card-count">{eventLog.length}</span>
          </div>
          <div className="settings-card-body">
            {eventLog.length === 0 ? (
              <p className="page-muted">No events yet.</p>
            ) : (
              <ol className="settings-event-list">
                {eventLog.map((entry, i) => (
                  <li key={`${entry.id}-${i}`} className={`settings-event is-${entry.tone}`}>
                    <time dateTime={entry.at}>{new Date(entry.at).toLocaleTimeString()}</time>
                    <strong>{entry.title}</strong>
                    {entry.detail && <span className="page-muted">{entry.detail}</span>}
                  </li>
                ))}
              </ol>
            )}
            <div className="page-form-actions">
              <button type="button" className="page-btn is-sm" onClick={onClearEvents} disabled={eventLog.length === 0}>
                Clear log
              </button>
            </div>
          </div>
        </section>

        {/* ── Row 2: Coordinator ──────────────────────────────────────── */}
        <div className="settings-group-label">Coordinator</div>

        <section className="nr-card" aria-labelledby="settings-connection-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-connection-title">Connection</h2>
              <p>Farm API access for this browser tab.</p>
            </div>
            <span className={`page-chip is-${connectionStatus}`}>
              <i aria-hidden="true" />
              {connectionLabel}
            </span>
          </div>
          <div className="settings-card-body">
            <p className="page-muted">
              {health?.ok
                ? authReady
                  ? "API key loaded — authenticated requests are sent on each refresh."
                  : "Coordinator is reachable. Connect to access farm data."
                : (health?.error ?? "Waiting for health check.")}
            </p>
            {authError && <p className="page-note is-bad" role="alert">{authError}</p>}
            <div className="page-form-actions">
              {authReady ? (
                <button type="button" className="page-btn" onClick={onDisconnect}>
                  Disconnect this tab
                </button>
              ) : (
                <button type="button" className="page-btn is-primary" onClick={onConnect}>
                  Connect <ChevronRight size={14} aria-hidden="true" />
                </button>
              )}
            </div>
            <p className="page-muted settings-fine-print">
              Disconnect forgets the API key in this tab only — no server credentials are changed.
            </p>
          </div>
        </section>

        <section className="nr-card" aria-labelledby="settings-session-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-session-title">Dashboard session</h2>
              <p>Cookie-based login for this UI.</p>
            </div>
            <span className={`page-chip is-${sessionStatus}`}>
              <i aria-hidden="true" />
              {sessionLabel}
            </span>
          </div>
          <div className="settings-card-body">
            {isMaster && authReady && (
              <div className="settings-ttl-field">
                <label className="settings-ttl-label" htmlFor="settings-ttl-input">
                  Session duration
                  <span className="page-muted settings-ttl-unit">(minutes)</span>
                </label>
                <div className="settings-ttl-row">
                  <input
                    id="settings-ttl-input"
                    type="number"
                    className="page-input settings-ttl-input"
                    min={5}
                    max={10080}
                    value={farm.sessionTtlInput}
                    onChange={(e) => { farm.setSessionTtlInput(e.target.value); }}
                    onKeyDown={(e) => { if (e.key === "Enter") void farm.saveSessionTtl(); }}
                    disabled={farm.sessionTtlBusy}
                    aria-describedby="settings-ttl-hint"
                  />
                  <button
                    type="button"
                    className="page-btn is-sm"
                    onClick={() => void farm.saveSessionTtl()}
                    disabled={farm.sessionTtlBusy || !ttlChanged}
                  >
                    {farm.sessionTtlBusy ? "Saving…" : farm.sessionTtlSaved ? "Saved" : "Apply"}
                  </button>
                </div>
                <p id="settings-ttl-hint" className="page-muted settings-ttl-hint">
                  5–10080 min (7 days). Takes effect on next login.
                </p>
                {farm.sessionTtlError && (
                  <p className="page-note is-bad" role="alert">{farm.sessionTtlError}</p>
                )}
              </div>
            )}
            {signOutError && <p className="page-note is-bad" role="alert">{signOutError}</p>}
            {dashboardSession === "active" && (
              <div className="page-form-actions">
                <button type="button" className="page-btn is-danger" onClick={onSignOut} disabled={signingOut}>
                  <LogOut size={13} aria-hidden="true" />
                  {signingOut ? "Signing out…" : "Sign out"}
                </button>
              </div>
            )}
            {dashboardSession !== "active" && dashboardSession !== "unavailable" && (
              <p className="page-muted settings-fine-print">
                Sign-out appears here when the coordinator reports an active session.
              </p>
            )}
          </div>
        </section>

        {/* ── Row 3: Farm (master only) ───────────────────────────────── */}
        {isMaster && (
          <>
            <div className="settings-group-label">Farm</div>

            <section className="nr-card" aria-labelledby="settings-farm-title">
              <div className="nr-card-head">
                <div>
                  <h2 id="settings-farm-title">Local rendering</h2>
                  <p>Allow this master to render as well as dispatch.</p>
                </div>
                <span className={`settings-status-badge ${farm.localRenderingEnabled ? "is-on" : "is-off"}`}>
                  {farm.localRenderingBusy ? "…" : farm.localRenderingEnabled ? "On" : "Off"}
                </span>
              </div>
              <div className="settings-card-body">
                <label className="page-switch settings-farm-toggle">
                  <input
                    type="checkbox"
                    checked={farm.localRenderingEnabled}
                    disabled={!authReady || !farm.localRenderingReady || farm.localRenderingBusy}
                    onChange={(e) => void farm.saveLocalRendering(e.target.checked)}
                  />
                  <span className="page-switch-track" aria-hidden="true" />
                  <span>{farm.localRenderingBusy ? "Saving…" : "Allow local renders"}</span>
                </label>
                <p className="page-muted">
                  Off makes this coordinator dispatch-only — all frames go to enrolled workers.
                </p>
                {farm.localRenderingError && (
                  <p className="page-note is-bad" role="alert">{farm.localRenderingError}</p>
                )}
              </div>
            </section>
          </>
        )}

        {/* ── Row 4: System ───────────────────────────────────────────── */}
        <div className="settings-group-label">System</div>

        <section className="nr-card" aria-labelledby="settings-details-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-details-title">Runtime</h2>
              <p>Read-only values reported by the coordinator.</p>
            </div>
            <HardDrive size={15} aria-hidden="true" className="settings-card-icon" />
          </div>
          <div className="settings-card-body">
            <dl className="page-table settings-facts">
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Version</dt>
                <dd className="settings-fact-value">{server?.version ?? health?.version ?? "—"}</dd>
              </div>
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Role</dt>
                <dd className="settings-fact-value">{server?.role ?? "—"}</dd>
              </div>
              <div className="page-table-row">
                <dt className="page-table-cell-muted">Workspace disk</dt>
                <dd className="settings-fact-value">{diskUsage}</dd>
              </div>
            </dl>
            {diskAvailable && (
              <p className="page-muted settings-fine-print">
                Disk values are for the workspace filesystem, not the container overlay.
              </p>
            )}
          </div>
        </section>

        <section className="nr-card" aria-labelledby="settings-shortcuts-title">
          <div className="nr-card-head">
            <div>
              <h2 id="settings-shortcuts-title">Go to</h2>
              <p>Jump to an operational screen.</p>
            </div>
          </div>
          <div className="settings-shortcut-list">
            <button type="button" className="settings-shortcut" onClick={() => onNavigate("Workers")}>
              <Server size={15} aria-hidden="true" />
              <span>Workers</span>
              <ArrowUpRight size={14} aria-hidden="true" className="settings-shortcut-arrow" />
            </button>
            <button type="button" className="settings-shortcut" onClick={() => onNavigate("Monitoring")}>
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
