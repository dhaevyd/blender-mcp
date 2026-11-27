import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Bell,
  Box,
  CheckCircle2,
  ChevronRight,
  Cpu,
  Film,
  HardDrive,
  Images,
  Info,
  LayoutDashboard,
  LockKeyhole,
  MemoryStick,
  Plus,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  X,
  Zap,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { clearDashboardKey, dashboardAuthHeaders, getDashboardKey, setDashboardKey } from "../api/dashboardAuth";
import { getBackendHealth, getDiagnostics, getFarmGpuTelemetry, getFarmStatus } from "../api/mcp";
import { GpuCollapseButton, GpuCollapseTitle, GpuNameBadge, useGpuCardState } from "../components/GpuCardChrome";
import {
  GpuHistoryChart,
  type GpuHistoryDevice,
  type GpuHistoryPayload,
  type GpuHistoryPoint,
  type GpuMetricKey,
  type GpuWindow,
  GpuWindowSelector,
  isGpuWindow,
} from "../components/GpuHistoryChart";
import { type MetricSeriesPoint, MetricSparkline } from "../components/MetricSparkline";
import { gpuKey, setGpuCollapsedForKeys, useAllGpusCollapsed } from "./gpu-card-prefs";
import "./farm.css";
import { alertEvents, jobEvents, type ToastMessage, workerEvents } from "../common/notificationEvents";
import { type NotificationPrefs, readNotificationPrefs, writeNotificationPrefs } from "../common/notificationPrefs";
import ToastStack from "../common/ToastStack";
import { type NotificationLogEntry, prependToLog, readNotificationLog, writeNotificationLog } from "../common/notificationLog";
import StudioJobs from "./studio-jobs";
import StudioScreens, { type GalleryFileRequest } from "./studio-screens";
import SettingsScreen, { type Diagnostics, type GpuProbe } from "./studio-settings-screen";
import StudioThemeToggle from "./studio-theme-toggle";

type Worker = {
  worker_id?: string;
  name?: string;
  online?: boolean;
  error?: string;
  identity?: {
    hostname?: string;
    image_tag?: string | null;
    gpu_devices?: string[];
    gpu_stats?: Array<
      GpuDevice & {
        index?: number;
        memory_free_mib?: number | null;
        backend?: string;
      }
    >;
    platform?: string;
  };
};

type Job = {
  id?: string;
  job_id?: string;
  status?: string;
  project_path?: string;
  frame_start?: number;
  frame_end?: number;
  completed_frames?: number;
  total_frames?: number;
  frames_completed?: number;
  frames_total?: number;
  progress?: number;
  percent?: number;
};

type FarmGpuTelemetry = {
  workers?: Array<{
    worker_id: string;
    online: boolean;
    image_tag?: string | null;
    gpus: GpuDevice[];
  }>;
};

type FarmSnapshot = {
  success?: boolean;
  coordinator?: { worker_count?: number };
  workers?: Worker[];
  active_jobs?: Job[];
  jobs?: Job[];
  error?: string;
};

type GpuDevice = {
  name?: string;
  index?: number;
  backend?: string;
  worker_id?: string;
  memory_free_mib?: number | null;
  memory_total_mib?: number | null;
  memory_used_mib?: number | null;
  utilization_percent?: number | null;
  temperature_c?: number | null;
  power_draw_w?: number | null;
};

type LatestMedia = {
  name?: string;
  type?: "image" | "video";
  url?: string;
  project_id?: string;
  modified?: string;
  revision?: string | number;
  updated?: string;
  updated_at?: string;
  mtime?: string;
};

/** Gallery id whose assets are loose media directly under the workspace root. */
const WORKSPACE_ROOT_PROJECT_ID = "workspace-root";

/**
 * Workspace-relative location of the latest render, as the folder browser sees it.
 *
 * `/api/v1/gallery/latest` reports the basename plus a gallery project id, while
 * its URL carries the project-relative asset path (assets may sit in
 * subfolders). Project ids resolve to `projects/<id>`; the synthetic
 * "workspace-root" bucket holds media directly under the root.
 */
export function workspacePathForLatestMedia(media: LatestMedia | null): GalleryFileRequest | null {
  const projectId = media?.project_id?.trim() ?? "";
  const fileName = media?.name?.trim() ?? "";
  if (!projectId || !fileName || /[/\\]/.test(projectId) || projectId === "." || projectId === "..") return null;

  let relative = "";
  const url = media?.url ?? "";
  const marker = url.indexOf("/assets/");
  if (marker >= 0) {
    try {
      relative = decodeURIComponent(url.slice(marker + "/assets/".length).split("?")[0]);
    } catch {
      relative = "";
    }
  }
  // A malformed or missing URL tail falls back to the basename the server reported.
  if (
    !relative ||
    relative.startsWith("/") ||
    relative.split("/").some((part) => !part || part === "." || part === "..")
  )
    relative = fileName;

  const folder = projectId === WORKSPACE_ROOT_PROJECT_ID ? "" : `projects/${projectId}`;
  return { path: folder ? `${folder}/${relative}` : relative, name: fileName };
}

const NAV = [
  { label: "Overview", icon: LayoutDashboard },
  { label: "Jobs", icon: Film },
  { label: "Workers", icon: Server },
  { label: "Gallery", icon: Images },
  { label: "Monitoring", icon: Activity },
  { label: "Settings", icon: Settings },
];

const NAV_SECTIONS = [
  { label: "", items: ["Overview", "Workers", "Jobs"].flatMap((l) => NAV.filter((item) => item.label === l)) },
  { label: "Output", items: NAV.filter((item) => item.label === "Gallery" || item.label === "Monitoring") },
  { label: "System", items: NAV.filter((item) => item.label === "Settings") },
];

/** Series colours for the allocation bars, donut legend and load curves. */
const SERIES_COLORS = [
  "var(--nerve-teal)",
  "var(--nerve-blue)",
  "var(--nerve-yellow)",
  "var(--nerve-coral)",
  "var(--nerve-lavender)",
  "var(--nerve-green)",
];

type AlertSeverity = "critical" | "warning" | "info" | "success";

type StudioAlert = {
  id: string;
  severity: AlertSeverity;
  title: string;
  detail: string;
  meta?: string;
  action?: () => void;
};

type ChartSeries = {
  key: string;
  label: string;
  color: string;
  points: string;
  latest: number;
};

const ALERT_SEVERITY_LABEL: Record<AlertSeverity, string> = {
  critical: "Critical",
  warning: "Warning",
  info: "Info",
  success: "Success",
};

function AlertGlyph({ severity }: { severity: AlertSeverity }) {
  if (severity === "success") return <CheckCircle2 size={13} aria-hidden="true" />;
  if (severity === "info") return <Info size={13} aria-hidden="true" />;
  return <AlertTriangle size={13} aria-hidden="true" />;
}

/** One alert line; actionable alerts become buttons that open their target. */
function StudioAlertRow({
  entry,
  onAction,
  showMeta = true,
}: {
  entry: StudioAlert;
  onAction?: () => void;
  showMeta?: boolean;
}) {
  const className = `nerve-alert-row is-${entry.severity}${showMeta ? "" : " is-compact"}`;
  const body = (
    <>
      <span className="nerve-alert-bar" aria-hidden="true" />
      <span className="nerve-alert-icon">
        <AlertGlyph severity={entry.severity} />
      </span>
      <span className="nerve-alert-body">
        <strong>{entry.title}</strong>
        <span>{entry.detail}</span>
      </span>
      {showMeta && <span className="nerve-alert-meta">{entry.meta ?? ""}</span>}
      <span className="nerve-alert-chip">{ALERT_SEVERITY_LABEL[entry.severity]}</span>
    </>
  );
  if (!entry.action) return <div className={className}>{body}</div>;
  return (
    <button
      type="button"
      className={`${className} is-actionable`}
      onClick={() => {
        entry.action?.();
        onAction?.();
      }}
    >
      {body}
    </button>
  );
}

const LAST_SCREEN_STORAGE_KEY = "blender-mcp-studio-last-screen";

function readLastScreen(): string {
  try {
    const saved = window.localStorage.getItem(LAST_SCREEN_STORAGE_KEY);
    return NAV.some((item) => item.label === saved) ? (saved as string) : "Overview";
  } catch {
    return "Overview";
  }
}

function jobProgress(job: Job): number | null {
  const reported = job.percent ?? job.progress;
  if (typeof reported === "number" && Number.isFinite(reported)) return Math.max(0, Math.min(100, reported));
  const done = job.completed_frames ?? job.frames_completed;
  const total = job.total_frames ?? job.frames_total;
  if (typeof done === "number" && typeof total === "number" && total > 0) return Math.round((done / total) * 100);
  return null;
}

function formatDuration(seconds?: number): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function formatMib(mib?: number): string {
  if (typeof mib !== "number" || !Number.isFinite(mib)) return "—";
  const gb = mib / 1024;
  return gb >= 100 ? gb.toFixed(0) : gb.toFixed(1);
}

function shortName(value?: string): string {
  if (!value) return "Untitled scene";
  return (
    value
      .split(/[\\/]/)
      .pop()
      ?.replace(/\.blend$/i, "") || value
  );
}

/** Device labels sit in tight columns: drop vendor prefixes, keep model numbers. */
function shortGpuName(gpu: GpuDevice): string {
  const name = gpu.name?.trim();
  if (!name) return "GPU";
  return name.replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "").replace(/^RTX\s+(?=\d)/i, "RTX ");
}

type DashboardSession = "unknown" | "active" | "inactive" | "unavailable";

/**
 * Endpoints that reject unauthenticated callers. Unlike /api/v1/diagnostics,
 * a 2xx here proves the candidate key is actually accepted by the coordinator.
 */
const KEY_PROBES = ["/api/v1/farm/status", "/api/v1/gallery/browse"] as const;

/** Same scheme as dashboardAuth.ts; a candidate key is probed before it is persisted. */
const AUTH_SCHEME = "Bearer";

/** Headers carrying a candidate key that has not been persisted yet. */
function candidateAuthHeaders(key: string): HeadersInit {
  return { Authorization: `${AUTH_SCHEME} ${key}`, Accept: "application/json" };
}

async function verifyDashboardKey(key: string): Promise<{ ok: true } | { ok: false; status?: number }> {
  const headers = candidateAuthHeaders(key);
  let lastStatus: number | undefined;
  try {
    for (const probe of KEY_PROBES) {
      const response = await fetch(probe, {
        headers,
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.ok) return { ok: true };
      lastStatus = response.status;
      if (response.status === 401 || response.status === 403) return { ok: false, status: response.status };
    }
  } catch {
    return { ok: false };
  }
  return { ok: false, status: lastStatus };
}

function gpuReadouts(gpu: GpuDevice): { key: GpuMetricKey; label: string; value: string; percent?: number }[] {
  const clamp = (percent: number) => Math.max(0, Math.min(100, percent));
  const used = gpu.memory_used_mib;
  const total = gpu.memory_total_mib;
  const vram =
    typeof used === "number"
      ? typeof total === "number" && total > 0
        ? `${formatMib(used)} / ${formatMib(total)} GB`
        : `${formatMib(used)} GB`
      : "—";
  return [
    {
      key: "vram",
      label: "VRAM",
      value: vram,
      percent:
        typeof used === "number" && typeof total === "number" && total > 0 ? clamp((used / total) * 100) : undefined,
    },
    {
      key: "util",
      label: "Util",
      value: typeof gpu.utilization_percent === "number" ? `${gpu.utilization_percent}%` : "—",
      percent: typeof gpu.utilization_percent === "number" ? clamp(gpu.utilization_percent) : undefined,
    },
    {
      key: "temp",
      label: "Temp",
      value: typeof gpu.temperature_c === "number" ? `${gpu.temperature_c}°C` : "—",
    },
    {
      key: "power",
      label: "Power",
      value: typeof gpu.power_draw_w === "number" ? `${Math.round(gpu.power_draw_w)} W` : "—",
    },
  ];
}
function GpuReadoutBar({ metric, percent }: { metric: GpuMetricKey; percent?: number }) {
  if (percent === undefined || !Number.isFinite(percent)) return null;
  return (
    <span aria-hidden="true" className={`gpu-readout-track is-${metric}`}>
      <span style={{ width: `${percent}%` }} />
    </span>
  );
}


function GpuHistoryCard({
  device,
  index,
  reading,
  gpuWindow,
  bucketSeconds,
  scope = "monitoring",
}: {
  device: GpuHistoryDevice;
  index: number;
  reading: GpuDevice;
  gpuWindow: GpuWindow;
  bucketSeconds?: number | null;
  scope?: "overview" | "monitoring";
}) {
  const deviceName = reading.worker_id ? gpuCardIdentity(reading, index) : (reading.name ?? device.name);
  const deviceIndex = reading.worker_id ? (reading.index ?? index) : index;
  const { label, collapsed } = useGpuCardState(deviceName, deviceIndex, scope);
  const [selectedMetric, setSelectedMetric] = useState<GpuMetricKey | null>(null);
  const metrics = gpuReadouts(reading);
  return (
    <article className={`gpu-history-card${collapsed ? " is-collapsed" : ""}`} aria-label={`${label} history`}>
      <header className="gpu-group-head">
        <div className="gpu-card-identity">
          <GpuCollapseTitle name={deviceName} index={deviceIndex} scope={scope}>
            <strong>
              {reading.name ?? `GPU ${index + 1}`}
              {reading.worker_id ? ` · ${reading.worker_id}` : ""}
            </strong>
          </GpuCollapseTitle>
          <GpuNameBadge name={deviceName} index={deviceIndex} />
        </div>
        <GpuCollapseButton name={deviceName} index={deviceIndex} scope={scope} />
      </header>
      {collapsed ? (
        <div className="gpu-card-compact">
          <span>{typeof reading.memory_used_mib === "number" ? `${formatMib(reading.memory_used_mib)} GB` : "—"}</span>
          <span>{typeof reading.utilization_percent === "number" ? `${reading.utilization_percent}%` : "—"}</span>
          <span>{typeof reading.temperature_c === "number" ? `${reading.temperature_c}°C` : "—"}</span>
        </div>
      ) : (
        <>
          <div className="gpu-history-plot">
            <GpuHistoryChart
              device={device}
              window={gpuWindow}
              bucketSeconds={bucketSeconds}
              selectedMetric={selectedMetric}
            />
          </div>
          <div className="gpu-readouts">
            {metrics.map((metric) => (
              <button
                className="gpu-readout-button"
                key={metric.key}
                type="button"
                aria-label={`Show ${metric.label} on the large GPU graph`}
                aria-pressed={selectedMetric === metric.key}
                onClick={() => setSelectedMetric((current) => (current === metric.key ? null : metric.key))}
              >
                <span>{metric.label}</span>
                <strong>{metric.value}</strong>
                <GpuReadoutBar metric={metric.key} percent={metric.percent} />
              </button>
            ))}
          </div>
        </>
      )}
    </article>
  );
}

type MonitorSample = { at: string; cpu: number; memory: number };
type RefreshCadence = 12 | 30 | 60 | "manual";
const REFRESH_STORAGE_KEY = "blender-mcp-dashboard-refresh-cadence";

function readRefreshCadence(): RefreshCadence {
  try {
    const saved = window.localStorage.getItem(REFRESH_STORAGE_KEY);
    if (saved === "manual") return "manual";
    if (saved === "12" || saved === "30" || saved === "60") return Number(saved) as RefreshCadence;
  } catch {
    // Private/restricted browser contexts can disable storage; keep the default.
  }
  return 12;
}

const GPU_WINDOW_STORAGE_KEY = "blender-mcp-dashboard-gpu-window";

function readGpuWindow(): GpuWindow {
  try {
    const saved = window.localStorage.getItem(GPU_WINDOW_STORAGE_KEY);
    if (isGpuWindow(saved)) return saved;
  } catch {
    // Private/restricted browser contexts can disable storage; keep the default.
  }
  return "live";
}

/**
 * Server-side buckets make fast polling pointless: live follows the dashboard
 * cadence, while coarse windows are re-fetched on a much slower timer.
 */
export function gpuHistoryPollSeconds(gpuWindow: GpuWindow): number | null {
  if (gpuWindow === "live") return 1;
  if (gpuWindow === "1d" || gpuWindow === "3d") return 300;
  if (gpuWindow === "3h" || gpuWindow === "6h" || gpuWindow === "12h") return 120;
  return 60;
}

type GpuHistoryState = {
  window: GpuWindow;
  payload: GpuHistoryPayload | null;
  status: "loading" | "ready" | "unavailable";
};

/**
 * Bucketed GPU history. A missing endpoint, a rejected key, a network failure or
 * a malformed body all degrade to a quiet "unavailable" state: the diagnostics
 * panel keeps working and nothing is written to the console.
 */
function useGpuHistory(gpuWindow: GpuWindow): GpuHistoryState {
  const [state, setState] = useState<GpuHistoryState>({
    window: gpuWindow,
    payload: null,
    status: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      try {
        const response = await fetch(`/api/v1/monitoring/gpu?window=${encodeURIComponent(gpuWindow)}`, {
          headers: dashboardAuthHeaders(),
          cache: "no-store",
          signal: controller.signal,
        });
        if (cancelled) return;
        if (!response.ok) {
          setState({ window: gpuWindow, payload: null, status: "unavailable" });
          return;
        }
        const payload = (await response.json()) as GpuHistoryPayload | null;
        if (cancelled) return;
        const valid = Boolean(payload && Array.isArray(payload.gpus));
        setState({
          window: gpuWindow,
          payload: valid ? payload : null,
          status: valid ? "ready" : "unavailable",
        });
      } catch {
        if (!cancelled && !controller.signal.aborted) {
          setState({ window: gpuWindow, payload: null, status: "unavailable" });
        }
      }
    };
    void load();
    const seconds = gpuHistoryPollSeconds(gpuWindow);
    const timer = seconds === null ? null : window.setInterval(() => void load(), seconds * 1000);
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) window.clearInterval(timer);
    };
  }, [gpuWindow]);

  // A payload from a previous window must never be drawn against the new axis labels.
  return state.window === gpuWindow ? state : { window: gpuWindow, payload: null, status: "loading" };
}

/** Newest bucket of a history device; used as the current reading when diagnostics lags. */
export function latestGpuReading(device: GpuHistoryDevice): GpuHistoryPoint | null {
  const series = Array.isArray(device.series) ? device.series : [];
  let latest: GpuHistoryPoint | null = null;
  for (const point of series) {
    if (typeof point?.t !== "number" || !Number.isFinite(point.t)) continue;
    if (!latest || (typeof latest.t === "number" && point.t > latest.t)) latest = point;
  }
  return latest;
}

/** Diagnostics is the live source; the newest history bucket fills any sensor it omits. */
export function currentReading(device: GpuHistoryDevice, live?: GpuDevice): GpuDevice {
  const latest = latestGpuReading(device);
  return {
    ...live,
    name: live?.name ?? device.name ?? undefined,
    memory_total_mib: live?.memory_total_mib ?? device.memory_total_mib ?? undefined,
    memory_used_mib: live?.memory_used_mib ?? latest?.mem_used_mib ?? undefined,
    utilization_percent: live?.utilization_percent ?? latest?.util ?? undefined,
    temperature_c: live?.temperature_c ?? latest?.temp_c ?? undefined,
    power_draw_w: live?.power_draw_w ?? latest?.power_w ?? undefined,
  };
}

/** The history series for one diagnostics device, matched by index then by name. */
export function historyDeviceFor(devices: GpuHistoryDevice[], index: number, name?: string): GpuHistoryDevice | null {
  const device =
    devices.find((entry) => typeof entry.index === "number" && entry.index === index) ??
    devices.find((entry) => Boolean(name) && entry.name === name) ??
    (devices.length === 1 ? devices[0] : undefined);
  const series = Array.isArray(device?.series) ? device.series : [];
  if (!device || series.length === 0) return null;
  return { ...device, series };
}

/** One short sentence naming the host probes that came back empty. */
export function gpuEmptyMessage(probe?: GpuProbe | null): string {
  if (!probe) return "No GPU telemetry reported by the coordinator.";
  const missing: string[] = [];
  if (probe.nvidia === false) missing.push("nvidia-smi unavailable");
  if (probe.amd === false) missing.push("no AMD device in /sys/class/drm");
  if (probe.intel === false) missing.push("no Intel device in /sys/class/drm");
  // Intel cards are detected but expose no utilisation/VRAM counters in sysfs, so
  // say that outright instead of implying there is no GPU at all.
  if (probe.intel === true) missing.push("Intel GPU detected but it exposes no utilisation/VRAM telemetry");
  if (missing.length === 0) return "No GPU telemetry reported by the coordinator.";
  return `No GPU telemetry — ${missing.join(", ")}.`;
}

/** A history device earns a chart card only once it has a timestamped sample. */
function hasHistorySeries(device: GpuHistoryDevice): boolean {
  const series = Array.isArray(device.series) ? device.series : [];
  return series.some((point) => Boolean(point) && typeof point.t === "number" && Number.isFinite(point.t));
}

export type GpuCardEntry = {
  index: number;
  gpu: GpuDevice;
  history: GpuHistoryDevice;
};
export type FarmGpuHistory = Record<string, GpuHistoryDevice>;

function farmGpuHistoryKey(gpu: GpuDevice): string {
  return JSON.stringify([gpu.worker_id, gpu.backend, gpu.index ?? gpu.name]);
}

function gpuCardIdentity(gpu: GpuDevice, index: number): string {
  return gpu.worker_id ? farmGpuHistoryKey(gpu) : gpuKey(gpu.name, index);
}

function snapshotGpuHistory(gpu: GpuDevice, timestamp: number): GpuHistoryDevice {
  return {
    index: gpu.index,
    name: gpu.name,
    memory_total_mib: gpu.memory_total_mib,
    series: [
      {
        t: timestamp,
        util: gpu.utilization_percent,
        mem_used_mib: gpu.memory_used_mib,
        temp_c: gpu.temperature_c,
        power_w: gpu.power_draw_w,
      },
    ],
  };
}

/** Match coordinator history by index/name and worker history by host and device. */
export function gpuCardEntries(
  gpus: ReadonlyArray<GpuDevice>,
  historyDevices: ReadonlyArray<GpuHistoryDevice>,
  farmHistory: FarmGpuHistory = {},
  timestamp = Date.now() / 1000,
): GpuCardEntry[] {
  const available = historyDevices.filter(hasHistorySeries);
  const entries: GpuCardEntry[] = [];
  for (let index = 0; index < gpus.length; index += 1) {
    const gpu = gpus[index];
    const match = gpu.worker_id
      ? (farmHistory[farmGpuHistoryKey(gpu)] ?? snapshotGpuHistory(gpu, timestamp))
      : (available.find((device) => typeof device.index === "number" && device.index === index) ??
        (gpu.name ? available.find((device) => device.name === gpu.name) : undefined) ??
        (gpus.length === 1 && available.length === 1 ? available[0] : undefined));
    if (match && !gpu.worker_id) available.splice(available.indexOf(match), 1);
    entries.push({
      index,
      gpu,
      history: match ?? snapshotGpuHistory(gpu, timestamp),
    });
  }
  if (gpus.length === 0) {
    for (const device of available) {
      entries.push({
        index: typeof device.index === "number" ? device.index : entries.length,
        gpu: currentReading(device),
        history: device,
      });
    }
  }
  return entries;
}

/** CPU/RAM samples carry an ISO timestamp; fall back to even spacing when the
 *  backend sends an unparseable one so the trend still renders. */
function resourcePoints(samples: ReadonlyArray<MonitorSample>, key: "cpu" | "memory"): MetricSeriesPoint[] {
  const step = 15_000;
  const anchor = Date.now() - samples.length * step;
  return samples.map((sample, index) => {
    const parsed = Date.parse(sample.at);
    return {
      tMs: Number.isNaN(parsed) ? anchor + index * step : parsed,
      value: sample[key],
    };
  });
}

function MonitoringScreen({
  diagnostics,
  farm,
  health,
  samples,
  authReady,
  onConnect,
  onOpenWorkers,
  farmGpuTelemetry,
  farmGpuHistory,
  gpuWindow,
  onGpuWindowChange,
  gpuHistory,
  gpuHistoryStatus,
}: {
  diagnostics: Diagnostics | null;
  farm: FarmSnapshot | null;
  health: { ok: boolean; error?: string } | null;
  samples: MonitorSample[];
  authReady: boolean;
  onConnect: () => void;
  onOpenWorkers: () => void;
  farmGpuTelemetry: FarmGpuTelemetry | null;
  farmGpuHistory: FarmGpuHistory;
  gpuWindow: GpuWindow;
  onGpuWindowChange: (value: GpuWindow) => void;
  gpuHistory: GpuHistoryPayload | null;
  gpuHistoryStatus: "loading" | "ready" | "unavailable";
}) {
  const system = diagnostics?.system;
  const server = diagnostics?.server;
  const coordinatorGpus = system?.gpu ?? [];
  const workerGpus = (farmGpuTelemetry?.workers ?? []).flatMap((worker) =>
    worker.online ? worker.gpus.map((gpu) => ({ ...gpu, worker_id: worker.worker_id })) : [],
  );
  const gpus = [...coordinatorGpus, ...workerGpus];
  const historyDevices = gpuHistory?.gpus ?? [];
  const bucketSeconds = gpuHistory?.bucket_seconds ?? null;
  const farmReady = authReady && Boolean(farm && !farm.error && farm.success !== false);
  const workers = farmReady ? (farm?.workers ?? []) : [];
  const jobs = farmReady ? (farm?.jobs ?? []) : [];
  const online = workers.filter((worker) => worker.online).length;
  const count = (status: string) => jobs.filter((job) => job.status === status).length;
  const active = farmReady
    ? farm?.active_jobs?.find((job) => job.status === "running" || job.status === "paused")
    : undefined;
  const activeCount = count("running") + count("paused");
  const progress = active ? jobProgress(active) : null;
  // Keys must match the ones each card derives, or the bulk toggle misses a card.
  const gpuCards = gpuCardEntries(
    gpus,
    historyDevices,
    farmGpuHistory,
    system?.sampled_at ? Date.parse(system.sampled_at) / 1000 : Date.now() / 1000,
  );
  const gpuCardKeys = gpuCards.map(({ gpu, history, index }) =>
    gpu.worker_id ? gpuKey(gpuCardIdentity(gpu, index), gpu.index ?? index) : gpuKey(gpu.name ?? history.name, index),
  );
  const allGpusCollapsed = useAllGpusCollapsed(gpuCardKeys, "monitoring");
  const cpuPoints = useMemo(() => resourcePoints(samples, "cpu"), [samples]);
  const memoryPoints = useMemo(() => resourcePoints(samples, "memory"), [samples]);
  const lastSample = system?.sampled_at ? new Date(system.sampled_at) : null;
  const sampleValid = lastSample && !Number.isNaN(lastSample.getTime());
  const diskAttention = typeof system?.disk_percent === "number" && system.disk_percent >= 90;
  const farmAttention = farmReady && online < workers.length;
  const state = !health?.ok
    ? "Coordinator unreachable"
    : !diagnostics
      ? "Telemetry unavailable"
      : diskAttention || farmAttention
        ? "Attention needed"
        : "Coordinator responding";
  const renderNumber = (value?: number | null, suffix = "") =>
    typeof value === "number" ? `${value}${suffix}` : "Unavailable";
  return (
    <section className="page monitor-page" aria-label="Operations monitoring">
      <header className="page-head monitor-heading">
        <div>
          <h1>
            Monitoring<span className="page-period">.</span>
          </h1>
          <p>Coordinator, farm and GPU telemetry in one place.</p>
        </div>
        <div className="page-actions">
          <span className="monitor-freshness" role="status">
            {sampleValid ? `Live · updated ${lastSample.toLocaleTimeString()}` : "Waiting for telemetry"}
          </span>
          <div className="page-segment monitor-window-segment" role="group" aria-label="GPU history range">
            <GpuWindowSelector value={gpuWindow} onChange={onGpuWindowChange} />
          </div>
        </div>
      </header>
      <div
        className={`page-note monitor-health${
          !health?.ok ? " is-bad" : diskAttention || farmAttention ? " is-warn" : ""
        }`}
      >
        <span
          className={`monitor-beacon ${!health?.ok ? "offline" : diskAttention || farmAttention ? "attention" : ""}`}
          aria-hidden="true"
        />
        <div className="monitor-health-main">
          <h2>{state}</h2>
          <p>
            {!health?.ok
              ? (health?.error ?? "Waiting for coordinator")
              : state === "Attention needed"
                ? [
                    diskAttention && `Disk usage is ${system?.disk_percent}% (90% threshold)`,
                    farmAttention &&
                      `Offline workers (${workers.length - online}): ${workers
                        .filter((worker) => !worker.online)
                        .map((worker) => worker.name ?? worker.worker_id ?? "Unknown worker")
                        .join(", ")}`,
                  ]
                    .filter(Boolean)
                    .join(" · ")
                : `MCP ${server?.version ?? "version unavailable"} · ${server?.role ?? "role unavailable"} · ${renderNumber(server?.tool_count)} tools`}
          </p>
        </div>
      </div>
      <div className="page-stats monitor-stats">
        <div className="page-stat">
          <span className="page-stat-label">
            <Cpu size={12} aria-hidden="true" /> CPU
          </span>
          <span className="page-stat-value">{renderNumber(system?.cpu_percent, "%")}</span>
          <span className="page-stat-foot">
            {renderNumber(system?.cpu_logical_threads)} threads · 1m load {renderNumber(system?.load_1m)}
          </span>
        </div>
        <div className="page-stat">
          <span className="page-stat-label">
            <MemoryStick size={12} aria-hidden="true" /> Memory
          </span>
          <span className="page-stat-value">{renderNumber(system?.memory_percent, "%")}</span>
          <span className="page-stat-foot">
            {typeof system?.memory_used_gb === "number" && typeof system.memory_total_gb === "number"
              ? `${system.memory_used_gb} / ${system.memory_total_gb} GB`
              : "Unavailable"}
          </span>
        </div>
        <div className="page-stat">
          <span className="page-stat-label">
            <Zap size={12} aria-hidden="true" /> GPU devices
          </span>
          <span className="page-stat-value">{gpus.length}</span>
          <span className="page-stat-foot">
            {coordinatorGpus.length} coordinator · {workerGpus.length} farm
          </span>
        </div>
        <div className="page-stat">
          <span className="page-stat-label">
            <Activity size={12} aria-hidden="true" /> Active jobs
          </span>
          <span className="page-stat-value">{activeCount}</span>
          <span className="page-stat-foot">
            {farmReady ? `${count("running")} rendering · ${count("paused")} paused` : "Farm snapshot unavailable"}
          </span>
        </div>
        <div className="page-stat">
          <span className="page-stat-label">
            <Server size={12} aria-hidden="true" /> Queue
          </span>
          <span className="page-stat-value">{count("queued")}</span>
          <span className="page-stat-foot">
            {count("failed")} failed · {count("completed")} done
          </span>
        </div>
      </div>
      <div className="page-grid-2 monitor-columns">
        <section className="nr-card monitor-resource-panel" aria-label="Coordinator resources">
          <div className="nr-card-head">
            <div>
              <h2>Coordinator resources</h2>
              <p>Runtime compute and the workspace mount</p>
            </div>
            <span className="nr-card-count">{sampleValid ? "live" : "waiting"}</span>
          </div>
          <div className="monitor-resource-list">
            <div className="monitor-resource-row">
              <span className="monitor-resource-label">
                <Cpu size={14} aria-hidden="true" />
                CPU
              </span>
              <div className="monitor-resource-trend">
                <MetricSparkline
                  points={cpuPoints}
                  color="var(--primary)"
                  domain={[0, 100]}
                  height={26}
                  formatValue={(value) => `${Math.round(value)}%`}
                  emptyLabel="Collecting samples"
                  emptyClassName="monitor-trend-empty"
                  ariaLabel="CPU load over the last few samples"
                />
              </div>
              <strong>{renderNumber(system?.cpu_percent, "%")}</strong>
              <small>
                {renderNumber(system?.cpu_logical_threads)} threads · 1m load {renderNumber(system?.load_1m)}
              </small>
            </div>
            <div className="monitor-resource-row">
              <span className="monitor-resource-label">
                <MemoryStick size={14} aria-hidden="true" />
                RAM
              </span>
              <div className="monitor-resource-trend">
                <MetricSparkline
                  points={memoryPoints}
                  color="var(--primary)"
                  domain={[0, 100]}
                  height={26}
                  formatValue={(value) => `${Math.round(value)}%`}
                  emptyLabel="Collecting samples"
                  emptyClassName="monitor-trend-empty"
                  ariaLabel="Memory usage over the last few samples"
                />
              </div>
              <strong>
                {typeof system?.memory_used_gb === "number" && typeof system.memory_total_gb === "number"
                  ? `${system.memory_used_gb} / ${system.memory_total_gb} GB`
                  : "Unavailable"}
              </strong>
              <small>{renderNumber(system?.memory_percent, "% used")}</small>
            </div>
            <div className={`monitor-resource-row ${diskAttention ? "monitor-attention" : ""}`}>
              <span className="monitor-resource-label">
                <HardDrive size={14} aria-hidden="true" />
                Workspace
              </span>
              <div className="monitor-resource-trend">
                <div
                  className="monitor-meter monitor-meter-lg page-meter is-thin"
                  role="meter"
                  aria-label="Workspace disk usage"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={system?.disk_percent ?? undefined}
                >
                  <span style={{ width: `${system?.disk_percent ?? 0}%` }} />
                </div>
              </div>
              <strong>{renderNumber(system?.disk_percent, "%")}</strong>
              <small>
                {typeof system?.disk_used_gb === "number" && typeof system.disk_total_gb === "number"
                  ? `${system.disk_used_gb} of ${system.disk_total_gb} GB · visible mount`
                  : "Mount unavailable · no overlay fallback"}
              </small>
            </div>
          </div>
          <p className="monitor-scope">
            CPU and RAM are visible to the coordinator runtime; in a container they may reflect host resources, not
            container limits. Disk is the configured workspace filesystem, never the container overlay.
          </p>
        </section>
        <section className="nr-card monitor-farm-panel" aria-label="Render farm">
          <div className="nr-card-head">
            <div>
              <h2>Render farm</h2>
              <p>Authenticated worker snapshot and recent queue</p>
            </div>
            <span className={`monitor-farm-state ${farmReady ? "is-online" : ""}`}>
              <i aria-hidden="true" />
              {farmReady ? "Farm online" : "Farm offline"}
            </span>
          </div>
          {!farmReady ? (
            <div className="monitor-farm-empty">
              <span className="monitor-farm-empty-title">
                {authReady ? "Farm snapshot unavailable" : "Connect to read the farm"}
              </span>
              <p>
                {!authReady
                  ? "The worker roster, queue counts and the active render come from the coordinator's authenticated snapshot."
                  : (farm?.error ?? "The coordinator did not return a worker snapshot.")}
              </p>
              {!authReady && (
                <button type="button" className="page-btn is-sm" onClick={onConnect}>
                  Connect to view farm
                </button>
              )}
            </div>
          ) : workers.length === 0 && jobs.length === 0 ? (
            <div className="monitor-farm-empty">
              <span className="monitor-farm-empty-title">No workers enrolled</span>
              <p>This coordinator is running on its own. Enrol a machine to queue renders across the farm.</p>
              <button type="button" className="page-btn is-sm" onClick={onOpenWorkers}>
                Open workers
              </button>
            </div>
          ) : (
            <>
              <div className="monitor-farm-summary">
                <div className="monitor-worker-tally">
                  <span className="monitor-summary-label">Workers online</span>
                  <strong>
                    {online}
                    <small> / {workers.length}</small>
                  </strong>
                  <span className="monitor-summary-detail">
                    {workers.length === 0 ? "No workers enrolled" : `${workers.length - online} offline`}
                  </span>
                </div>
                <div className="monitor-queue-grid" role="group" aria-label="Recent job counts">
                  {[
                    { key: "active", label: "Active", value: activeCount },
                    { key: "queued", label: "Queued", value: count("queued") },
                    { key: "failed", label: "Failed", value: count("failed") },
                    {
                      key: "completed",
                      label: "Done",
                      value: count("completed"),
                    },
                  ].map(({ key, label, value }) => (
                    <div
                      key={key}
                      className={`monitor-queue-item monitor-queue-${key}${value > 0 ? " has-count" : ""}`}
                      data-zero={value === 0 ? "true" : undefined}
                    >
                      <strong>{value}</strong>
                      <span>{label}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="monitor-queue-caption">
                <span>Recent jobs</span>
                <span>{jobs.length} of up to 20 · snapshot counts</span>
              </div>
              {active ? (
                <div className={`monitor-job${active.status === "paused" ? " is-paused" : ""}`}>
                  <div className="monitor-job-heading">
                    <span className="monitor-job-state">{active.status === "paused" ? "Paused" : "Rendering"}</span>
                    <strong>{progress === null ? "—" : `${progress}%`}</strong>
                  </div>
                  <span className="monitor-job-project" title={active.project_path}>
                    {shortName(active.project_path)}
                  </span>
                  <div
                    className="monitor-meter page-meter is-thin"
                    role="progressbar"
                    aria-label="Current render progress"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={progress ?? undefined}
                  >
                    <span style={{ width: `${progress ?? 0}%` }} />
                  </div>
                </div>
              ) : jobs.length > 0 ? (
                <p className="monitor-queue-empty">No active render. Recent jobs remain in the snapshot above.</p>
              ) : (
                <p className="monitor-queue-empty">No recent jobs.</p>
              )}
            </>
          )}
        </section>
      </div>
      <div className="page-stack">
        <section className="nr-card monitor-gpu-panel" aria-label="Farm GPU Telemetry">
          <div className="nr-card-head">
            <div>
              <h2>Farm GPU telemetry</h2>
              <p>Utilisation, VRAM, temperature and power per device</p>
            </div>
            <div className="monitor-gpu-controls">
              {gpuCardKeys.length > 1 ? (
                <button
                  type="button"
                  className="gpu-collapse-all page-btn is-sm is-ghost"
                  onClick={() => setGpuCollapsedForKeys(gpuCardKeys, !allGpusCollapsed, "monitoring")}
                >
                  {allGpusCollapsed ? "Expand all" : "Collapse all"}
                </button>
              ) : null}
              <span className="nr-card-count">{gpus.length} reported</span>
            </div>
          </div>
          {gpuHistoryStatus === "unavailable" && <p className="monitor-gpu-empty">Saved history unavailable.</p>}
          {gpuCards.length ? (
            <div className="gpu-history-grid">
              {gpuCards.map((card) => (
                <GpuHistoryCard
                  key={gpuCardIdentity(card.gpu, card.index)}
                  device={card.history}
                  index={card.index}
                  reading={card.gpu}
                  gpuWindow={gpuWindow}
                  bucketSeconds={card.gpu.worker_id ? undefined : bucketSeconds}
                />
              ))}
            </div>
          ) : diagnostics ? (
            <div className="monitor-cpu-fallback" role="status">
              <p className="monitor-gpu-empty">
                No GPU on this host, so renders use CPU.{" "}
                {diagnostics.host?.virtualization?.kind && diagnostics.host.virtualization.kind !== "none"
                  ? `Platform: ${diagnostics.host.virtualization.kind}. `
                  : ""}
                Coordinator probe: {gpuEmptyMessage(system?.gpu_probe)}
              </p>
              <div className="monitor-cpu-fallback-grid">
                <div>
                  <span>CPU</span>
                  <strong>{renderNumber(system?.cpu_percent, "%")}</strong>
                  <small>
                    {renderNumber(system?.cpu_logical_threads)} threads · 1m load {renderNumber(system?.load_1m)}
                  </small>
                </div>
                <div>
                  <span>RAM</span>
                  <strong>
                    {typeof system?.memory_used_gb === "number" && typeof system.memory_total_gb === "number"
                      ? `${system.memory_used_gb} / ${system.memory_total_gb} GB`
                      : "Unavailable"}
                  </strong>
                  <small>{renderNumber(system?.memory_percent, "% used")}</small>
                </div>
              </div>
            </div>
          ) : (
            <p className="monitor-gpu-empty">GPU telemetry unavailable until diagnostics respond.</p>
          )}
        </section>
      </div>
    </section>
  );
}

export default function StudioDashboard() {
  const [apiKey, setApiKey] = useState(getDashboardKey);
  const disconnectedRef = useRef(false);
  const authReady = Boolean(apiKey);
  const [screen, setScreen] = useState(readLastScreen);
  const [workerView, setWorkerView] = useState<"roster" | "onboarding">("roster");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const notificationsRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!mobileMenuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileMenuOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mobileMenuOpen]);

  // The notification popover keeps its own Escape handler so closing it never
  // depends on the drawer state.
  useEffect(() => {
    if (!notificationsOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNotificationsOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [notificationsOpen]);

  useEffect(() => {
    if (!notificationsOpen) return;
    const closeOnPointer = (event: PointerEvent) => {
      if (!notificationsRef.current?.contains(event.target as Node)) setNotificationsOpen(false);
    };
    document.addEventListener("pointerdown", closeOnPointer);
    return () => document.removeEventListener("pointerdown", closeOnPointer);
  }, [notificationsOpen]);

  const selectScreen = (nextScreen: string) => {
    setScreen(nextScreen);
    setMobileMenuOpen(false);
    setNotificationsOpen(false);
  };

  useEffect(() => {
    try {
      window.localStorage.setItem(LAST_SCREEN_STORAGE_KEY, screen);
    } catch {
      // The current tab remains navigable when browser storage is unavailable.
    }
  }, [screen]);
  useLayoutEffect(() => {
    mainRef.current?.scrollTo(0, 0);
    // On narrow screens the document, rather than .studio-main, scrolls.
    window.scrollTo(0, 0);
  }, [screen]);
  const [showConnect, setShowConnect] = useState(() => !getDashboardKey());
  useEffect(() => {
    if (!showConnect) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setShowConnect(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showConnect]);
  const [draftKey, setDraftKey] = useState("");
  const [authError, setAuthError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [cadence, setCadence] = useState<RefreshCadence>(readRefreshCadence);
  const [storageError, setStorageError] = useState(false);
  function changeCadence(value: RefreshCadence) {
    setCadence(value);
    try {
      window.localStorage.setItem(REFRESH_STORAGE_KEY, String(value));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  }
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [health, setHealth] = useState<{
    ok: boolean;
    version?: string;
    uptime?: number;
    tools?: number;
    error?: string;
  } | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [monitorSamples, setMonitorSamples] = useState<MonitorSample[]>([]);
  const [farm, setFarm] = useState<FarmSnapshot | null>(null);
  const [notificationPrefs, setNotificationPrefs] = useState<NotificationPrefs>(readNotificationPrefs);
  const [notificationsSaveError, setNotificationsSaveError] = useState(false);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [eventLog, setEventLog] = useState<NotificationLogEntry[]>(readNotificationLog);
  // Alert ids the user cleared. An alert leaves this set once its condition clears, so it can fire again later.
  const [dismissedAlerts, setDismissedAlerts] = useState<Set<string>>(() => new Set());
  // Last state seen per entity. Null until the first snapshot arrives, so history is not replayed.
  const seenJobs = useRef<Map<string, string> | null>(null);
  const seenWorkers = useRef<Map<string, boolean> | null>(null);
  const seenAlerts = useRef<Set<string> | null>(null);
  const [farmGpuTelemetry, setFarmGpuTelemetry] = useState<FarmGpuTelemetry | null>(null);
  const [farmGpuHistory, setFarmGpuHistory] = useState<FarmGpuHistory>({});
  const [authRequired, setAuthRequired] = useState(false);
  const [latestMedia, setLatestMedia] = useState<LatestMedia | null>(null);
  const [latestBlobUrl, setLatestBlobUrl] = useState<string | null>(null);
  const latestBlobUrlRef = useRef<string | null>(null);
  const [dashboardSession, setDashboardSession] = useState<DashboardSession>("unknown");
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState("");
  const [verifying, setVerifying] = useState(false);
  const refreshSequence = useRef(0);
  const refreshController = useRef<AbortController | null>(null);
  const [openFile, setOpenFile] = useState<GalleryFileRequest | null>(null);
  const openFileSequence = useRef(0);
  const [gpuWindow, setGpuWindow] = useState<GpuWindow>(readGpuWindow);
  const gpuHistory = useGpuHistory(screen === "Overview" ? "live" : gpuWindow);

  function changeGpuWindow(value: GpuWindow) {
    setGpuWindow(value);
    try {
      window.localStorage.setItem(GPU_WINDOW_STORAGE_KEY, value);
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  }

  /** Ask the gallery to open exactly one workspace file, replacing any pending request. */
  const openFileInGallery = useCallback((target: GalleryFileRequest) => {
    openFileSequence.current += 1;
    setOpenFile({ ...target, id: openFileSequence.current });
    setScreen("Gallery");
  }, []);

  /** Only the request the gallery actually consumed is cleared; a newer click keeps its own. */
  const handleOpenFileHandled = useCallback((id: number | undefined) => {
    setOpenFile((current) => (current && current.id === id ? null : current));
  }, []);

  const refresh = useCallback(async () => {
    const injected = (window as { __BLENDER_MCP_API_KEY__?: string }).__BLENDER_MCP_API_KEY__;
    if (injected && !disconnectedRef.current && !getDashboardKey()) {
      setDashboardKey(injected);
      setApiKey(injected);
    }

    // Every refresh owns a generation. Starting a new one aborts the previous
    // request; any response that lands after its generation was superseded
    // (disconnect, key change, newer poll) is discarded instead of written back.
    refreshSequence.current += 1;
    const sequence = refreshSequence.current;
    refreshController.current?.abort();
    const controller = new AbortController();
    refreshController.current = controller;
    const isCurrent = () => sequence === refreshSequence.current && !controller.signal.aborted;

    setRefreshing(true);
    const hasApiKey = Boolean(getDashboardKey());
    try {
      const farmPromise: Promise<{
        success: boolean;
        error?: string;
        data?: FarmSnapshot;
      }> = hasApiKey
        ? getFarmStatus<FarmSnapshot>()
        : Promise.resolve({ success: false, error: "Authentication required" });
      const latestPromise = hasApiKey
        ? fetch("/api/v1/gallery/latest", {
            headers: dashboardAuthHeaders(),
            cache: "no-store",
            signal: controller.signal,
          })
            .then(async (r) => {
              if (!r.ok) return null;
              const data = (await r.json()) as { file?: LatestMedia | null };
              return data.file ?? null;
            })
            .catch(() => null)
        : Promise.resolve(null as LatestMedia | null);
      const [backend, diag, farmResult, latestFile] = await Promise.all([
        getBackendHealth(),
        getDiagnostics(),
        farmPromise,
        latestPromise,
      ]);

      if (!isCurrent()) return;

      const rejectedKey = hasApiKey && Boolean(farmResult.error?.includes("401"));
      const requiresAuth = Boolean(backend.ok && (!hasApiKey || rejectedKey));
      setAuthRequired(requiresAuth);
      if (rejectedKey) {
        clearDashboardKey();
        setApiKey("");
        setAuthError("The saved key was rejected. Enter the current server key to reconnect.");
      }
      if (backend.ok) {
        setHealth({
          ok: true,
          version: backend.data?.version,
          uptime: backend.data?.uptime_seconds,
          tools: backend.data?.tool_count,
        });
        const monitorDiag = diag as Diagnostics | null;
        setDiagnostics(monitorDiag);
        if (
          monitorDiag?.system?.sampled_at &&
          typeof monitorDiag.system.cpu_percent === "number" &&
          typeof monitorDiag.system.memory_percent === "number"
        ) {
          const sample: MonitorSample = {
            at: monitorDiag.system.sampled_at,
            cpu: monitorDiag.system.cpu_percent,
            memory: monitorDiag.system.memory_percent,
          };
          setMonitorSamples((previous) =>
            previous.at(-1)?.at === sample.at ? previous : [...previous, sample].slice(-30),
          );
        }
        if (hasApiKey && !rejectedKey) {
          if (farmResult.success && farmResult.data) setFarm(farmResult.data);
          else
            setFarm({
              error: farmResult.error ?? "Render coordinator did not return status.",
            });
        } else {
          setFarm(null);
        }
      } else {
        setHealth({ ok: false, error: backend.error ?? "Backend unavailable" });
        setFarm(null);
        setDiagnostics(null);
      }

      setLatestMedia(latestFile);
      setLastUpdated(new Date());
    } finally {
      if (refreshSequence.current === sequence) {
        refreshController.current = null;
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, authReady]);

  useEffect(() => {
    if (cadence === "manual") return;
    const timer = window.setInterval(() => void refresh(), cadence * 1000);
    return () => window.clearInterval(timer);
  }, [refresh, cadence]);

  // While a render is running, poll the farm every 3 s so the progress bar moves
  // between frame completions, regardless of the chosen dashboard cadence.
  const renderingNow = (farm?.active_jobs ?? []).some((job) => job.status === "running");
  useEffect(() => {
    if (!renderingNow) return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [renderingNow, refresh]);

  // Monitoring gets its own fast, serialized diagnostics loop. Other screens
  // keep the lighter dashboard cadence; scheduling after each response avoids
  // piling requests up if a hardware probe is briefly slow.
  useEffect(() => {
    if (screen !== "Monitoring") return;
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const poll = async () => {
      controller = new AbortController();
      const next = await getDiagnostics(controller.signal);
      if (cancelled) return;
      if (next) {
        const monitorDiag = next as Diagnostics;
        setDiagnostics(monitorDiag);
        if (
          monitorDiag.system?.sampled_at &&
          typeof monitorDiag.system.cpu_percent === "number" &&
          typeof monitorDiag.system.memory_percent === "number"
        ) {
          const sample: MonitorSample = {
            at: monitorDiag.system.sampled_at,
            cpu: monitorDiag.system.cpu_percent,
            memory: monitorDiag.system.memory_percent,
          };
          setMonitorSamples((previous) =>
            previous.at(-1)?.at === sample.at ? previous : [...previous, sample].slice(-180),
          );
        }
      }
      if (!cancelled) timer = window.setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [screen]);

  // Poll worker GPU inventory independently of coordinator-local diagnostics.
  useEffect(() => {
    if (!apiKey || (screen !== "Monitoring" && screen !== "Overview")) return;
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const poll = async () => {
      controller = new AbortController();
      const next = await getFarmGpuTelemetry<FarmGpuTelemetry>(controller.signal);
      if (cancelled) return;
      setFarmGpuTelemetry(next);
      timer = window.setTimeout(poll, 2000);
    };
    void poll();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [screen, apiKey]);

  useEffect(() => {
    if (!farmGpuTelemetry) return;
    const timestamp = Date.now() / 1000;
    setFarmGpuHistory((previous) => {
      const next = { ...previous };
      for (const worker of farmGpuTelemetry.workers ?? []) {
        if (!worker.online) continue;
        for (const gpu of worker.gpus) {
          const device = { ...gpu, worker_id: worker.worker_id };
          const key = farmGpuHistoryKey(device);
          const oldSeries = previous[key]?.series ?? [];
          next[key] = {
            index: gpu.index,
            name: gpu.name,
            memory_total_mib: gpu.memory_total_mib,
            series: [
              ...oldSeries,
              {
                t: timestamp,
                util: gpu.utilization_percent,
                mem_used_mib: gpu.memory_used_mib,
                temp_c: gpu.temperature_c,
                power_w: gpu.power_draw_w,
              },
            ].slice(-1800),
          };
        }
      }
      return next;
    });
  }, [farmGpuTelemetry]);

  // A render that overwrites the same path keeps its URL; the revision part of
  // the key (file mtime/updated metadata, or the applied refresh when the server
  // reports none) is what makes the preview reload. Old object URLs are revoked
  // when they are replaced so repeated renders cannot leak blobs.
  const previewRevision = latestMedia
    ? (latestMedia.revision ??
      latestMedia.modified ??
      latestMedia.updated ??
      latestMedia.updated_at ??
      latestMedia.mtime ??
      (lastUpdated ? String(lastUpdated.getTime()) : ""))
    : "";
  const previewKey = latestMedia?.url ? `${latestMedia.url}|${latestMedia.name ?? ""}|${previewRevision}` : "";

  useEffect(() => {
    const url = latestMedia?.url;
    if (!url) {
      const previous = latestBlobUrlRef.current;
      latestBlobUrlRef.current = null;
      setLatestBlobUrl(null);
      if (previous) URL.revokeObjectURL(previous);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(url, {
          headers: dashboardAuthHeaders(),
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) return;
        const blob = await response.blob();
        if (cancelled) return;
        const objectUrl = URL.createObjectURL(blob);
        const previous = latestBlobUrlRef.current;
        latestBlobUrlRef.current = objectUrl;
        setLatestBlobUrl(objectUrl);
        if (previous && previous !== objectUrl) URL.revokeObjectURL(previous);
      } catch {
        // Aborted or failed preview loads keep the previous preview in place.
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [previewKey]);

  useEffect(
    () => () => {
      if (latestBlobUrlRef.current) URL.revokeObjectURL(latestBlobUrlRef.current);
      latestBlobUrlRef.current = null;
    },
    [],
  );

  const workers = farm?.workers ?? [];
  const liveWorkerGpu = new Map((farmGpuTelemetry?.workers ?? []).map((worker) => [worker.worker_id, worker]));
  const onlineWorkers = workers.filter(
    (worker) => liveWorkerGpu.get(worker.worker_id ?? "")?.online ?? worker.online,
  ).length;
  const activeJobs = farm?.active_jobs ?? [];
  const currentJob = activeJobs[0];
  const currentProgress = currentJob ? jobProgress(currentJob) : null;
  const queuedJobs = farm?.jobs ?? [];
  const coordinatorGpus = diagnostics?.system?.gpu ?? [];
  const workerGpus = (farmGpuTelemetry?.workers ?? []).flatMap((worker) =>
    worker.online ? worker.gpus.map((gpu) => ({ ...gpu, worker_id: worker.worker_id })) : [],
  );
  const gpus: GpuDevice[] = [...coordinatorGpus, ...workerGpus];

  const backendReachable = Boolean(health?.ok);
  const needsAuth = authRequired || Boolean(health?.error?.includes("401")) || (!apiKey && backendReachable);
  const backendReady = backendReachable;
  const hasPreview = Boolean(latestMedia && latestBlobUrl);
  const latestOpenFile = hasPreview ? workspacePathForLatestMedia(latestMedia) : null;

  const failedJobs = queuedJobs.filter((job) => job.status === "failed");
  const completedJobCount = queuedJobs.filter((job) => job.status === "completed").length;
  const offlineWorkers = workers.filter(
    (worker) => !(liveWorkerGpu.get(worker.worker_id ?? "")?.online ?? worker.online),
  );
  const hotGpus = gpus.filter((gpu) => typeof gpu.temperature_c === "number" && gpu.temperature_c >= 80);
  const busyGpus = gpus.filter(
    (gpu) => typeof gpu.utilization_percent === "number" && gpu.utilization_percent >= 95,
  );
  const avgUtil = gpus.length
    ? Math.round(gpus.reduce((sum, gpu) => sum + (gpu.utilization_percent ?? 0), 0) / gpus.length)
    : 0;
  const vramUsed = gpus.reduce((sum, gpu) => sum + (gpu.memory_used_mib ?? 0), 0);
  const vramTotal = gpus.reduce((sum, gpu) => sum + (gpu.memory_total_mib ?? 0), 0);
  const vramPct = vramTotal > 0 ? Math.round((vramUsed / vramTotal) * 100) : 0;
  const donutColor =
    avgUtil > 90 ? "var(--nerve-coral)" : avgUtil >= 70 ? "var(--nerve-yellow)" : "var(--nerve-teal)";
  const donutLength = 2 * Math.PI * 56;

  const alerts = useMemo<StudioAlert[]>(() => {
    const list: StudioAlert[] = [];
    if (!backendReady && !needsAuth) {
      list.push({
        id: "coordinator",
        severity: "critical",
        title: "Coordinator unreachable",
        detail: health?.error ?? "Check Blender MCP on port 10849",
        meta: "LIVE",
        action: () => void refresh(),
      });
    }
    if (needsAuth) {
      list.push({
        id: "auth",
        severity: "warning",
        title: "Authentication required",
        detail: "Connect to manage workers, gallery and render jobs",
        meta: "LIVE",
        action: () => setShowConnect(true),
      });
    }
    if (failedJobs.length > 0) {
      list.push({
        id: "failed-jobs",
        severity: "critical",
        title: `${failedJobs.length} render job${failedJobs.length === 1 ? "" : "s"} failed`,
        detail: failedJobs
          .slice(0, 2)
          .map((job) => shortName(job.project_path))
          .join(", "),
        meta: "LIVE",
      });
    }
    if (offlineWorkers.length > 0) {
      list.push({
        id: "offline-workers",
        severity: "warning",
        title: `${offlineWorkers.length} worker${offlineWorkers.length === 1 ? "" : "s"} offline`,
        detail: offlineWorkers
          .slice(0, 2)
          .map((worker) => worker.name ?? worker.worker_id ?? "Worker")
          .join(", "),
        meta: "LIVE",
      });
    }
    for (const gpu of hotGpus) {
      list.push({
        id: `gpu-hot-${gpuCardIdentity(gpu, gpu.index ?? 0)}`,
        severity: "critical",
        title: `GPU ${gpu.name ?? "device"} at ${gpu.temperature_c}°C`,
        detail: gpu.worker_id ?? "Coordinator",
        meta: "LIVE",
      });
    }
    for (const gpu of busyGpus) {
      list.push({
        id: `gpu-busy-${gpuCardIdentity(gpu, gpu.index ?? 0)}`,
        severity: "warning",
        title: `GPU ${gpu.name ?? "device"} exceeding 95% utilization`,
        detail: `${gpu.utilization_percent}% load`,
        meta: "LIVE",
      });
    }
    if (queuedJobs.length > 5) {
      list.push({
        id: "queue-backlog",
        severity: "info",
        title: `Queue backlog: ${queuedJobs.length} waiting`,
        detail: "Frames queued ahead of the active render",
        meta: "LIVE",
      });
    }
    if (activeJobs.length > 0 && currentJob) {
      list.push({
        id: "rendering",
        severity: "info",
        title: `Rendering: ${shortName(currentJob.project_path)}`,
        detail: `${currentProgress ?? 0}% complete`,
        meta: "LIVE",
      });
    }
    if (completedJobCount > 0) {
      list.push({
        id: "completed-jobs",
        severity: "success",
        title: `${completedJobCount} job${completedJobCount === 1 ? "" : "s"} completed`,
        detail: "Recent farm throughput",
        meta: `${completedJobCount} completed`,
      });
    }
    if (backendReady && !list.some((entry) => entry.severity === "critical" || entry.severity === "warning")) {
      list.push({
        id: "farm-healthy",
        severity: "success",
        title: "Farm healthy",
        detail: `${onlineWorkers}/${workers.length} workers online`,
        meta: "LIVE",
      });
    }
    if (list.length === 0) {
      list.push({
        id: "no-events",
        severity: "info",
        title: "No live events",
        detail: "Connect the coordinator to stream farm events",
      });
    }
    return list;
  }, [
    backendReady,
    needsAuth,
    health?.error,
    failedJobs,
    offlineWorkers,
    hotGpus,
    busyGpus,
    queuedJobs.length,
    activeJobs.length,
    currentJob,
    currentProgress,
    completedJobCount,
    onlineWorkers,
    workers.length,
    refresh,
  ]);

  const visibleAlerts = alerts.filter((entry) => !dismissedAlerts.has(entry.id));
  const unreadAlertCount = visibleAlerts.filter(
    (entry) => entry.severity === "critical" || entry.severity === "warning",
  ).length;
  const hasCriticalAlert = visibleAlerts.some((entry) => entry.severity === "critical");

  // Forget dismissals for alerts whose condition has cleared, so a repeat failure shows again.
  useEffect(() => {
    const live = new Set(alerts.map((entry) => entry.id));
    setDismissedAlerts((previous) => {
      const next = new Set([...previous].filter((id) => live.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [alerts]);

  const openEventLog = () => {
    setNotificationsOpen(false);
    selectScreen("Settings");
    // Wait for the Settings screen to mount before scrolling to its event log.
    window.setTimeout(() => {
      document.getElementById("settings-event-log")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  const pushEvents = useCallback((events: ToastMessage[]) => {
    if (events.length === 0) return;
    setToasts((current) =>
      [...current.filter((toast) => !events.some((event) => event.id === toast.id)), ...events].slice(-4),
    );
    setEventLog((current) => {
      const next = prependToLog(current, events);
      writeNotificationLog(next);
      return next;
    });
  }, []);

  // Clear wipes the log and toasts, and dismisses every live alert so the bell and feed empty too.
  const clearEvents = () => {
    setToasts([]);
    setEventLog([]);
    writeNotificationLog([]);
    setDismissedAlerts(new Set(alerts.map((entry) => entry.id)));
  };

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const changeNotificationPrefs = useCallback((next: NotificationPrefs) => {
    setNotificationPrefs(next);
    setNotificationsSaveError(!writeNotificationPrefs(next));
  }, []);

  // Turn snapshot changes into toasts. Each ref keeps only the last snapshot, so a change fires once.
  useEffect(() => {
    if (farm && !farm.error) {
      const jobById = new Map<string, { id: string; status: string; label: string }>();
      for (const job of [...(farm.jobs ?? []), ...(farm.active_jobs ?? [])]) {
        const id = job.id ?? job.job_id;
        if (id) jobById.set(id, { id, status: job.status ?? "", label: shortName(job.project_path) || "Render job" });
      }
      const jobList = [...jobById.values()];

      const workerList = (farm.workers ?? [])
        .filter((worker): worker is Worker & { worker_id: string } => Boolean(worker.worker_id))
        .map((worker) => ({
          id: worker.worker_id,
          label: worker.name ?? worker.worker_id,
          online: Boolean(worker.online),
        }));

      const events = [
        ...jobEvents(seenJobs.current, jobList, notificationPrefs),
        ...workerEvents(seenWorkers.current, workerList, notificationPrefs),
      ];
      seenJobs.current = new Map(jobList.map((job) => [job.id, job.status]));
      seenWorkers.current = new Map(workerList.map((worker) => [worker.id, worker.online]));
      pushEvents(events);
    }

    const criticalIds = alerts.filter((alert) => alert.severity === "critical").map((alert) => alert.id);
    const alertToasts = alertEvents(seenAlerts.current, alerts, notificationPrefs);
    seenAlerts.current = new Set(criticalIds);
    pushEvents(alertToasts);
  }, [farm, alerts, notificationPrefs, pushEvents]);

  const workerLoad = useMemo(() => {
    const byWorker = new Map<string, { id: string; total: number; count: number }>();
    for (const gpu of gpus) {
      const id = gpu.worker_id ?? "Coordinator";
      const entry = byWorker.get(id) ?? { id, total: 0, count: 0 };
      entry.total += gpu.utilization_percent ?? 0;
      entry.count += 1;
      byWorker.set(id, entry);
    }
    return [...byWorker.values()]
      .map((entry) => ({
        name: workers.find((worker) => worker.worker_id === entry.id)?.name ?? entry.id,
        avg: entry.total / Math.max(1, entry.count),
      }))
      .sort((a, b) => a.avg - b.avg);
  }, [gpus, workers]);
  const idleWorkerName = workerLoad[0]?.name;
  const busiestWorkerName = workerLoad.length > 1 ? workerLoad[workerLoad.length - 1]?.name : undefined;

  const chartSeries = useMemo<ChartSeries[]>(() => {
    const series: ChartSeries[] = [];
    Object.entries(farmGpuHistory).forEach(([key, device], index) => {
      const samples = (device.series ?? [])
        .map((point) => (typeof point.util === "number" ? point.util : null))
        .filter((value): value is number => value !== null)
        .slice(-48);
      if (samples.length < 2) return;
      const points = samples
        .map((value, pointIndex) => {
          const x = (pointIndex / (samples.length - 1)) * 560;
          const y = 200 - Math.max(0, Math.min(100, value)) * 2;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(" ");
      series.push({
        key,
        label: device.name ?? `GPU ${index + 1}`,
        color: SERIES_COLORS[index % SERIES_COLORS.length],
        points,
        latest: Math.round(samples[samples.length - 1]),
      });
    });
    return series;
  }, [farmGpuHistory]);

  const sortedWorkers = [...workers].sort(
    (a, b) =>
      Number(Boolean(liveWorkerGpu.get(b.worker_id ?? "")?.online ?? b.online)) -
      Number(Boolean(liveWorkerGpu.get(a.worker_id ?? "")?.online ?? a.online)),
  );

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) return;
    const key = draftKey.trim();
    if (!key) return;

    setVerifying(true);
    setAuthError("");
    try {
      // Verify against a protected route while the key is still only in memory:
      // /api/v1/diagnostics is public, so a 2xx there proves nothing.
      const verdict = await verifyDashboardKey(key);
      if (!verdict.ok) {
        setAuthError(
          verdict.status === 401 || verdict.status === 403
            ? "That key was not accepted. Nothing was saved to this tab."
            : verdict.status
              ? `Could not verify the key (HTTP ${verdict.status}). Nothing was saved to this tab.`
              : "Could not reach the coordinator to verify the key. Nothing was saved to this tab.",
        );
        setShowConnect(true);
        return;
      }

      disconnectedRef.current = false;
      setDashboardKey(key);
      setApiKey(key);
      setDraftKey("");
      setAuthRequired(false);
      setShowConnect(false);
      await refresh();
    } finally {
      setVerifying(false);
    }
  }

  function disconnect() {
    disconnectedRef.current = true;
    // Invalidate anything already in flight so a slow response cannot restore
    // farm/diagnostics state after the key has been cleared.
    refreshSequence.current += 1;
    refreshController.current?.abort();
    refreshController.current = null;
    setRefreshing(false);
    clearDashboardKey();
    setApiKey("");
    setHealth({ ok: false, error: "Authentication required" });
    setFarm(null);
    setFarmGpuTelemetry(null);
    setFarmGpuHistory({});
    setDiagnostics(null);
    setLatestMedia(null);
    setLatestBlobUrl(null);
    setSignOutError("");
  }

  const checkDashboardSession = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/dashboard/session", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.status === 200) setDashboardSession("active");
      else if (response.status === 404) setDashboardSession("unavailable");
      else setDashboardSession("inactive");
    } catch {
      setDashboardSession("unknown");
    }
  }, []);

  useEffect(() => {
    void checkDashboardSession();
  }, [checkDashboardSession]);

  useEffect(() => {
    // Re-probe whenever Settings opens so an expired session stops offering sign out.
    if (screen === "Settings") void checkDashboardSession();
  }, [screen, checkDashboardSession]);

  async function signOut() {
    if (signingOut) return;
    setSigningOut(true);
    setSignOutError("");
    try {
      // Only an active cookie session can be signed out; when the coordinator
      // runs without dashboard auth the endpoint answers 404 and this is hidden.
      const session = await fetch("/api/v1/dashboard/session", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (session.status === 404) {
        setDashboardSession("unavailable");
        setSignOutError("Sign out is unavailable: this coordinator does not require a dashboard login.");
        return;
      }
      if (session.status !== 200) {
        setDashboardSession("inactive");
        setSignOutError("This browser session has already ended. Reload the page to sign in again.");
        return;
      }

      const response = await fetch("/api/v1/dashboard/logout", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        setSignOutError(
          response.status === 403
            ? "The coordinator rejected the sign-out request (HTTP 403). Reload the page and try again."
            : `Sign out failed (HTTP ${response.status}). Your session is still active.`,
        );
        return;
      }

      disconnectedRef.current = true;
      refreshSequence.current += 1;
      refreshController.current?.abort();
      refreshController.current = null;
      setRefreshing(false);
      clearDashboardKey();
      setApiKey("");
      setFarm(null);
      setFarmGpuTelemetry(null);
      setFarmGpuHistory({});
      setDiagnostics(null);
      setLatestMedia(null);
      setLatestBlobUrl(null);
      window.location.assign("/login");
    } catch {
      setSignOutError("Could not reach the coordinator to sign out. Check the connection and try again.");
    } finally {
      setSigningOut(false);
    }
  }

  return (
    <div className="studio-shell">
      {mobileMenuOpen && (
        <button
          type="button"
          className="studio-drawer-backdrop"
          aria-label="Close navigation menu"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}
      <div className="studio-mobile-bar">
        <strong className="studio-mobile-title">{screen}</strong>
        <button
          type="button"
          className="studio-menu-trigger"
          aria-label="Open navigation menu"
          aria-controls="studio-mobile-drawer"
          aria-expanded={mobileMenuOpen}
          onClick={() => setMobileMenuOpen(true)}
        >
          <span aria-hidden="true" className="studio-menu-glyph">
            ☰
          </span>
        </button>
      </div>
      <aside
        id="studio-mobile-drawer"
        className={`studio-sidebar${mobileMenuOpen ? " mobile-menu-open" : ""}`}
        aria-label="Studio navigation"
      >
        <a className="studio-brand" href="/" aria-label="Blender Farm overview">
          <span className="brand-mark">
            <Box size={16} strokeWidth={1.7} />
          </span>
          <span className="studio-brand-copy">
            <strong>BLENDER FARM</strong>
            <small>RENDER PIPELINE</small>
          </span>
        </a>
        <nav className="studio-rail-nav" aria-label="Main navigation">
          {NAV_SECTIONS.map((section) => (
            <div className="studio-rail-section" key={section.label}>
              <div className="studio-rail-label">{section.label}</div>
              {section.items.map(({ label, icon: Icon }) => (
                <button
                  key={label}
                  type="button"
                  title={label}
                  aria-label={label}
                  className={`studio-rail-item${screen === label ? " active" : ""}`}
                  onClick={() => selectScreen(label)}
                  aria-current={screen === label ? "page" : undefined}
                >
                  <Icon size={15} strokeWidth={1.8} />
                  <span>{label}</span>
                  {(() => {
                    const count = label === "Workers" ? workers.length : label === "Jobs" ? activeJobs.length : 0;
                    return count > 0 ? <span className="rail-badge">{count}</span> : null;
                  })()}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <button
          type="button"
          className="studio-rail-add"
          aria-label="New render job"
          onClick={() => {
            selectScreen("Jobs");
          }}
        >
          <Plus size={14} strokeWidth={2} />
          <span>New render job</span>
        </button>
        <button
          type="button"
          className="studio-rail-conn"
          onClick={() => selectScreen("Settings")}
          aria-label={`BLENDER MCP. Coordinator status: ${backendReady ? "online" : needsAuth ? "authentication needed" : "offline"}. Open Settings`}
        >
          <span className={`connection-indicator ${backendReady ? "on" : "off"}`} aria-hidden="true" />
          <strong>{window.location.hostname || "coordinator"}</strong>
          <small aria-hidden="true">
            {backendReady
              ? `coordinator · connected${health?.version ? ` · v${health.version.trim()}` : ""}`
              : needsAuth
                ? "Connect in Settings"
                : "Waiting for server"}
          </small>
        </button>
      </aside>

      <main className="studio-main" ref={mainRef}>
        <header className="studio-head">
          <div className="studio-head-copy">
            {screen === "Overview" ? (
              <span className="studio-head-screen">
                Overview<span className="head-period" aria-hidden="true">.</span>
              </span>
            ) : (
              <span className="studio-head-screen">{screen}<span className="head-period" aria-hidden="true">.</span></span>
            )}
          </div>
          <div className="studio-head-actions">
            <div className="studio-notif-wrap" ref={notificationsRef}>
              <button
                type="button"
                className={`icon-button studio-bell${unreadAlertCount > 0 ? " has-unread" : ""}`}
                onClick={() => setNotificationsOpen((open) => !open)}
                aria-expanded={notificationsOpen}
                aria-controls="studio-notifications"
                aria-label={`Notifications, ${unreadAlertCount} unread`}
              >
                <Bell size={16} />
                {unreadAlertCount > 0 && (
                  <span className={`studio-bell-badge${hasCriticalAlert ? " is-critical" : ""}`} aria-hidden="true">
                    {unreadAlertCount}
                  </span>
                )}
              </button>
              {notificationsOpen && (
                <div className="studio-notif-panel" id="studio-notifications" role="dialog" aria-label="Notifications">
                  <div className="studio-notif-head">
                    <span>Notifications</span>
                    <span className="studio-notif-head-actions">
                      <span>
                        {visibleAlerts.length} event{visibleAlerts.length === 1 ? "" : "s"}
                      </span>
                      <button
                        type="button"
                        onClick={clearEvents}
                        disabled={visibleAlerts.length === 0 && eventLog.length === 0 && toasts.length === 0}
                      >
                        Clear
                      </button>
                    </span>
                  </div>
                  <div className="studio-notif-list">
                    {visibleAlerts.slice(0, 12).map((entry) => (
                      <StudioAlertRow
                        key={entry.id}
                        entry={entry}
                        onAction={() => setNotificationsOpen(false)}
                      />
                    ))}
                  </div>
                  <div className="studio-notif-foot">
                    <button type="button" onClick={openEventLog}>
                      View all events <ArrowUpRight size={13} />
                    </button>
                  </div>
                </div>
              )}
            </div>
            <button
              type="button"
              className="icon-button"
              onClick={() => void refresh()}
              aria-label="Refresh dashboard"
              data-tooltip="Refresh now"
            >
              <RefreshCw size={16} className={refreshing ? "spin" : ""} />
            </button>
            <StudioThemeToggle />
            <button
              type="button"
              className="profile-chip"
              onClick={() => (apiKey ? disconnect() : setShowConnect(true))}
              data-tooltip={apiKey ? "Disconnect backend" : "Connect backend"}
              aria-label={apiKey ? "Disconnect backend" : "Connect backend"}
            >
              {apiKey ? <ShieldCheck size={16} /> : <LockKeyhole size={15} />}
            </button>
          </div>
        </header>
        <div className="overview-content">
          {screen === "Overview" ? (
            <>
              {/* Top-level stat cards matching prototype layout */}
              <div className="v2-stat-row">
                <div className={`v2-stat${activeJobs.length > 0 ? " is-active" : ""}`}>
                  <div className="v2-stat-label">Active Jobs</div>
                  <div className="v2-stat-value">
                    {backendReady ? activeJobs.length : "—"}
                    {activeJobs.length > 0 && <small>rendering</small>}
                  </div>
                  <div className="v2-stat-foot">
                    {backendReady ? `${queuedJobs.length} queued · ${failedJobs.length} failed` : "not connected"}
                  </div>
                </div>
                <div className="v2-stat">
                  <div className="v2-stat-label">Workers Online</div>
                  <div className="v2-stat-value">
                    {backendReady ? onlineWorkers : "—"}
                    {backendReady && workers.length > 0 && <small>/ {workers.length}</small>}
                  </div>
                  <div className="v2-stat-foot">
                    {backendReady
                      ? onlineWorkers === workers.length && workers.length > 0
                        ? "All responding"
                        : `${workers.length - onlineWorkers} offline`
                      : "not connected"}
                  </div>
                </div>
                <div className="v2-stat">
                  <div className="v2-stat-label">VRAM Used</div>
                  <div className="v2-stat-value">
                    {gpus.length > 0 ? formatMib(vramUsed) : "—"}
                    {gpus.length > 0 && <small>GB</small>}
                  </div>
                  <div className="v2-stat-foot">
                    {gpus.length > 0
                      ? `${vramPct}% of ${formatMib(vramTotal)} GB farm total`
                      : "no devices"}
                  </div>
                </div>
                <div className="v2-stat">
                  <div className="v2-stat-label">GPU Devices</div>
                  <div className="v2-stat-value">
                    {gpus.length > 0 ? gpus.length : "—"}
                    {gpus.length > 0 && <small>{gpus.length === 1 ? "device" : "devices"}</small>}
                  </div>
                  <div className="v2-stat-foot">
                    {gpus.length > 0 ? `avg ${avgUtil}% utilisation` : "not connected"}
                  </div>
                </div>
              </div>
              {/* ── Prototype-style overview grid ───────────────────────────── */}
              <div className="overview-grid">
                <div className="overview-main">

                  {/* Active jobs card */}
                  <div className="card">
                    <div className="card-head">
                      <div>
                        <h2>Active jobs</h2>
                        <p>Currently dispatched to the farm.</p>
                      </div>
                      <span className="card-badge">{activeJobs.length} running</span>
                    </div>
                    {activeJobs.length === 0 ? (
                      <div className="job-inline">
                        <span style={{ color: "var(--nerve-muted)", font: "400 12px/1 Archivo,sans-serif" }}>
                          {backendReady ? "No active render jobs" : "Connect coordinator to view jobs"}
                        </span>
                      </div>
                    ) : (
                      activeJobs.slice(0, 5).map((job) => {
                        const name = shortName(job.project_path);
                        const pct = Math.round(jobProgress(job) ?? 0);
                        const done = job.completed_frames ?? job.frames_completed ?? 0;
                        const total = job.total_frames ?? job.frames_total ?? (job.frame_end && job.frame_start ? job.frame_end - job.frame_start + 1 : 0);
                        return (
                          <div className="job-inline" key={job.id ?? job.job_id}>
                            <span className="chip info" style={{ flex: "none" }}><i />Rendering</span>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ font: "600 12.5px/1.3 Archivo,sans-serif", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {name || job.id || job.job_id || "Job"}
                              </div>
                              <div style={{ font: "400 11px/1 Archivo,sans-serif", color: "var(--nerve-muted)", marginTop: 3 }}>
                                frames {job.frame_start ?? 1}–{job.frame_end ?? (total > 0 ? total : "?")}
                              </div>
                            </div>
                            {total > 0 && (
                              <div className="progress-bar" style={{ minWidth: 90 }}>
                                <div className="progress-track">
                                  <div className="progress-fill" style={{ width: `${pct}%` }} />
                                </div>
                                <div className="progress-pct">{done}/{total} — {pct}%</div>
                              </div>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>

                  {/* Activity feed */}
                  <div className="card">
                    <div className="card-head">
                      <div><h2>Activity</h2><p>Recent farm events.</p></div>
                    </div>
                    <div className="feed">
                      {visibleAlerts.length === 0 ? (
                        <div className="feed-item">
                          <div className="feed-dot" />
                          <div className="feed-msg">No recent events — farm is quiet.</div>
                        </div>
                      ) : (
                        visibleAlerts.slice(0, 6).map((entry) => {
                          const sev = entry.severity as string;
                          const dotCls = sev === "info" || sev === "log"
                            ? " blue"
                            : sev === "ok" || sev === "success"
                              ? " green"
                              : "";
                          return (
                            <div className="feed-item" key={entry.id}>
                              <div className={`feed-dot${dotCls}`} />
                              <div className="feed-msg">
                                <strong>{entry.title}</strong>
                                {entry.detail ? ` — ${entry.detail}` : ""}
                              </div>
                              <div className="feed-time">{entry.meta ?? ""}</div>
                            </div>
                          );
                        })
                      )}
                    </div>
                  </div>

                </div>{/* end overview-main */}

                <div className="overview-side">

                  {/* Workers card */}
                  <div className="card">
                    <div className="card-head">
                      <div><h2>Workers</h2></div>
                      <span className="card-badge">{onlineWorkers} / {workers.length} online</span>
                    </div>
                    {workers.length === 0 ? (
                      <div className="worker-mini">
                        <span className="status-dot" aria-hidden="true" />
                        <div style={{ flex: 1, color: "var(--nerve-muted)", font: "400 11.5px/1 Archivo,sans-serif" }}>
                          No workers enrolled
                        </div>
                      </div>
                    ) : (
                      workers.map((w, wi) => {
                        const online = w.online ?? false;
                        const gpuName = w.identity?.gpu_devices?.[0]?.split(",")[0] ?? "No GPU reported";
                        return (
                          <div className="worker-mini" key={w.worker_id ?? wi}>
                            <span className={`status-dot${online ? " on" : ""}`} aria-hidden="true" />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ font: "600 12px/1 Archivo,sans-serif" }}>
                                {w.name ?? w.identity?.hostname ?? w.worker_id ?? `Worker ${wi + 1}`}
                              </div>
                              <div style={{ font: "400 10.5px/1 Archivo,sans-serif", color: "var(--nerve-muted)", marginTop: 3 }}>
                                {gpuName}
                              </div>
                            </div>
                            <span className={`chip ${online ? "good" : "bad"}`} style={{ fontSize: "8.5px" }}>
                              <i />{online ? "Online" : "Offline"}
                            </span>
                          </div>
                        );
                      })
                    )}
                  </div>

                  {/* Queue card */}
                  <div className="card">
                    <div className="card-head">
                      <div><h2>Queue</h2><p>Waiting to dispatch.</p></div>
                      <span className="card-badge">{queuedJobs.length} jobs</span>
                    </div>
                    {queuedJobs.length === 0 ? (
                      <div className="worker-mini">
                        <span className="chip muted" style={{ fontSize: "8.5px", flex: "none" }}>Queue</span>
                        <div style={{ flex: 1, minWidth: 0, font: "500 11.5px/1 Archivo,sans-serif", paddingLeft: 8, color: "var(--nerve-muted)" }}>
                          Queue is empty
                        </div>
                      </div>
                    ) : (
                      queuedJobs.slice(0, 5).map((job, qi) => (
                        <div className="worker-mini" key={job.id ?? job.job_id ?? qi}>
                          <span className="chip muted" style={{ fontSize: "8.5px", flex: "none" }}>Queue</span>
                          <div style={{ flex: 1, minWidth: 0, font: "500 11.5px/1 Archivo,sans-serif", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", paddingLeft: 8 }}>
                            {shortName(job.project_path) || job.id || job.job_id || "Job"}
                          </div>
                          <div style={{ font: "400 10.5px/1 Archivo,sans-serif", color: "#3a3d47" }}>#{qi + 1}</div>
                        </div>
                      ))
                    )}
                  </div>

                </div>{/* end overview-side */}
              </div>{/* end overview-grid */}

              {/* ── Legacy nerve cards (data still live, visually hidden) ──── */}
              <div style={{ display: "none" }}>
              <div className="nerve-cards-row">
                <section className="nr-card" id="studio-nodes-card" aria-label="Infrastructure nodes">
                  <header className="nr-card-head">
                    <div>
                      <h2>Infrastructure Nodes</h2>
                      <p>
                        {backendReady
                          ? `Active Monitoring Across ${onlineWorkers} Distributed Workers`
                          : "Connect the coordinator to stream live node data"}
                      </p>
                    </div>
                  </header>
                  <div className="nerve-tile-grid">
                    {[
                      {
                        icon: <Server size={13} />,
                        label: "Workers online",
                        value: backendReady ? String(onlineWorkers) : "—",
                        unit: backendReady ? `of ${workers.length} enrolled` : "not connected",
                        delta: backendReady
                          ? workers.length === 0
                            ? { text: "no workers enrolled", tone: "" }
                            : onlineWorkers === workers.length
                              ? { text: "all online", tone: "good" }
                              : onlineWorkers === 0
                                ? { text: `${workers.length} offline`, tone: "bad" }
                                : { text: `${workers.length - onlineWorkers} offline`, tone: "warn" }
                          : { text: "waiting for server", tone: "" },
                      },
                      {
                        icon: <Film size={13} />,
                        label: "Active jobs",
                        value: backendReady ? String(activeJobs.length) : "—",
                        unit: backendReady ? `${queuedJobs.length} in queue` : "not connected",
                        delta:
                          activeJobs.length > 0 ? { text: "rendering now", tone: "info" } : { text: "idle", tone: "" },
                      },
                      {
                        icon: <Cpu size={13} />,
                        label: "GPU devices",
                        value: gpus.length > 0 ? String(gpus.length) : "—",
                        unit: gpus.length === 1 ? "device" : "devices",
                        delta: { text: `avg ${avgUtil}% util`, tone: avgUtil >= 75 ? "warn" : "info" },
                      },
                      {
                        icon: <MemoryStick size={13} />,
                        label: "VRAM in use",
                        value: gpus.length > 0 ? formatMib(vramUsed) : "—",
                        unit: gpus.length > 0 ? `of ${formatMib(vramTotal)} GB` : "GB",
                        delta: { text: gpus.length > 0 ? `${vramPct}% allocated` : "no devices", tone: "" },
                      },
                      {
                        icon: <Zap size={13} />,
                        label: "MCP tools",
                        value: health?.tools != null ? String(health.tools) : "—",
                        unit: "tools",
                        delta: {
                          text: health?.version ? `v${health.version}` : "not connected",
                          tone: health?.version ? "info" : "",
                        },
                      },
                      {
                        icon: <Activity size={13} />,
                        label: "Uptime",
                        value: health?.uptime != null ? formatDuration(health.uptime) : "—",
                        unit: "coordinator runtime",
                        delta: {
                          text: backendReady ? "since boot" : "offline",
                          tone: backendReady ? "good" : "bad",
                        },
                      },
                    ].map(({ icon, label, value, unit, delta }) => (
                      <div className="nerve-tile" key={label}>
                        <div className="nerve-tile-label">
                          {icon}
                          <span>{label}</span>
                        </div>
                        <div className="nerve-tile-value">
                          <span>{value}</span>
                          <span className="nerve-tile-unit">{unit}</span>
                        </div>
                        <div className={`nerve-tile-delta${delta.tone ? ` is-${delta.tone}` : ""}`}>{delta.text}</div>
                      </div>
                    ))}
                  </div>
                </section>

                <section className="nr-card" aria-label="Workload allocation">
                  <header className="nr-card-head">
                    <div>
                      <h2>Workload Allocation</h2>
                      <p>
                        {gpus.length > 0
                          ? `${gpus.length} GPU${gpus.length === 1 ? "" : "s"} Across ${workers.length} Render Nodes`
                          : "No farm GPUs reported"}
                      </p>
                    </div>
                  </header>
                  {gpus.length === 0 ? (
                    <div className="nr-inset">
                      <MemoryStick size={16} aria-hidden="true" />
                      <div>
                        <strong>No farm GPUs reported</strong>
                        <p>
                          {backendReady
                            ? `Coordinator probe: ${gpuEmptyMessage(diagnostics?.system?.gpu_probe)}`
                            : "GPU telemetry comes from the authenticated coordinator and online workers."}
                        </p>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="nerve-alloc">
                        <div className="nerve-alloc-donut">
                          <div className="nerve-donut">
                            <svg viewBox="0 0 140 140" role="img" aria-label={`Average GPU load ${avgUtil}%`}>
                              <circle
                                cx="70"
                                cy="70"
                                r="56"
                                fill="none"
                                stroke="var(--studio-border)"
                                strokeWidth="14"
                              />
                              {avgUtil > 0 ? (
                                <circle
                                  cx="70"
                                  cy="70"
                                  r="56"
                                  fill="none"
                                  stroke={donutColor}
                                  strokeWidth="14"
                                  strokeLinecap="round"
                                  strokeDasharray={`${(avgUtil / 100) * donutLength} ${donutLength}`}
                                  transform="rotate(-90 70 70)"
                                />
                              ) : null}
                            </svg>
                            <div className="nerve-donut-center">
                              <span className="nerve-donut-value">{avgUtil}%</span>
                              <span className="nerve-donut-label">Load Efficiency</span>
                            </div>
                          </div>
                          <div className="nerve-donut-legend">
                            {gpus.map((gpu, index) => (
                              <span className="nerve-legend-chip" key={gpuCardIdentity(gpu, index)}>
                                <i style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }} />
                                <span>{shortGpuName(gpu)}</span>
                              </span>
                            ))}
                          </div>
                        </div>
                        <div className="nerve-alloc-list">
                          {gpus.map((gpu, index) => {
                            const used = gpu.memory_used_mib ?? 0;
                            const total = gpu.memory_total_mib ?? 0;
                            const pct = total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
                            return (
                              <div className="nerve-alloc-row" key={gpuCardIdentity(gpu, index)}>
                                <div className="nerve-alloc-top">
                                  <span className="nerve-alloc-name">
                                    {shortGpuName(gpu)}
                                    <small>{gpu.worker_id ?? "Coordinator"}</small>
                                  </span>
                                  <span className="nerve-alloc-val">
                                    {total > 0 ? `${formatMib(used)} / ${formatMib(total)} GB` : "—"}
                                  </span>
                                </div>
                                <div
                                  className="nerve-alloc-track"
                                  role="meter"
                                  aria-label={`${gpu.name ?? `GPU ${index + 1}`} VRAM`}
                                  aria-valuemin={0}
                                  aria-valuemax={100}
                                  aria-valuenow={Math.round(pct)}
                                >
                                  <div
                                    className="nerve-alloc-fill"
                                    style={{
                                      width: `${pct}%`,
                                      background: SERIES_COLORS[index % SERIES_COLORS.length],
                                    }}
                                  />
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                      <div className="nerve-alloc-footer">
                        <span className="nerve-alloc-footer-icon">
                          <Zap size={14} aria-hidden="true" />
                        </span>
                        <div className="nerve-alloc-footer-copy">
                          <strong>Smart Resource Balancing</strong>
                          <p>
                            Auto routing load from {idleWorkerName ?? "idle nodes"} to{" "}
                            {busiestWorkerName ?? "active nodes"}.
                          </p>
                        </div>
                        <span className="nerve-pill">System Optimized</span>
                      </div>
                    </>
                  )}
                </section>

                <section className="nr-card" id="studio-alerts-card" aria-label="Realtime alerts and events">
                  <header className="nr-card-head">
                    <div>
                      <h2>Realtime alerts &amp; events</h2>
                      <p>Live System Events And Anomaly Feed</p>
                    </div>
                  </header>
                  <div className="nerve-alert-list">
                    {visibleAlerts.slice(0, 5).map((entry) => (
                      <StudioAlertRow key={entry.id} entry={entry} showMeta={false} />
                    ))}
                  </div>
                  <div className="nr-card-foot">
                    <span>
                      Last Incident :{" "}
                      {failedJobs.length + offlineWorkers.length > 0
                        ? `${failedJobs.length + offlineWorkers.length} in this session`
                        : "none in this session"}
                    </span>
                    <button type="button" onClick={openEventLog}>
                      View all events <ArrowUpRight size={12} />
                    </button>
                  </div>
                </section>
              </div>

              <div className="nerve-cards-row is-row-two">
                <section className="nr-card" aria-label="GPU gateway heatmap">
                  <header className="nr-card-head">
                    <div>
                      <h2>GPU Gateway Heatmap</h2>
                      <p>Per Device Utilization Intensity Analysis</p>
                    </div>
                  </header>
                  {gpus.length === 0 ? (
                    <div className="nr-empty">
                      <Cpu size={18} />
                      <strong>No GPU telemetry</strong>
                      <p>Coordinator GPU probes and online worker telemetry populate this grid.</p>
                    </div>
                  ) : (
                    <>
                      <div className="nerve-heatmap">
                        {gpus.map((gpu, index) => {
                          const util = gpu.utilization_percent ?? 0;
                          const bucket =
                            util >= 90
                              ? "is-l90"
                              : util >= 75
                                ? "is-l75"
                                : util >= 50
                                  ? "is-l50"
                                  : util >= 20
                                    ? "is-l20"
                                    : "is-l0";
                          return (
                            <span
                              className={`nerve-heat-cell ${bucket}`}
                              key={gpuCardIdentity(gpu, index)}
                              title={`${shortGpuName(gpu)} — ${util}% · ${gpu.temperature_c ?? "—"}°C`}
                            />
                          );
                        })}
                      </div>
                      <div className="nerve-heat-legend">
                        {[
                          { label: "0–19%", tone: "is-l0" },
                          { label: "20–49%", tone: "is-l20" },
                          { label: "50–74%", tone: "is-l50" },
                          { label: "75–89%", tone: "is-l75" },
                          { label: "90%+", tone: "is-l90" },
                        ].map(({ label, tone }) => (
                          <span className="nerve-heat-key" key={label}>
                            <i className={tone} />
                            {label}
                          </span>
                        ))}
                      </div>
                      <div className="nerve-heat-stats">
                        <span>
                          Peak load
                          <b>{Math.round(Math.max(...gpus.map((gpu) => gpu.utilization_percent ?? 0)))}%</b>
                        </span>
                        <span>
                          Hottest
                          <b>
                            {gpus.some((gpu) => typeof gpu.temperature_c === "number")
                              ? `${Math.round(Math.max(...gpus.map((gpu) => gpu.temperature_c ?? 0)))}°C`
                              : "—"}
                          </b>
                        </span>
                        <span>
                          Idle devices
                          <b>{gpus.filter((gpu) => (gpu.utilization_percent ?? 0) < 20).length}</b>
                        </span>
                      </div>
                    </>
                  )}
                </section>

                <section className="nr-card" aria-label="Render load history">
                  <header className="nr-card-head">
                    <div>
                      <h2>Render Load History</h2>
                      <p>Live Load Curves Across Active Devices</p>
                    </div>
                    <span className="nerve-live-chip">
                      <i aria-hidden="true" />
                      Live Tracking
                    </span>
                  </header>
                  {chartSeries.length === 0 ? (
                    <div className="nr-chart-empty">
                      <Activity size={18} />
                      <p>No history samples yet — curves appear as telemetry streams in.</p>
                    </div>
                  ) : (
                    <>
                      <div className="nerve-chart">
                        <div className="nerve-chart-y" aria-hidden="true">
                          {["100", "75", "50", "25", "0"].map((label) => (
                            <span key={label}>{label}</span>
                          ))}
                        </div>
                        <div className="nerve-chart-plot">
                          <svg
                            viewBox="0 0 560 200"
                            preserveAspectRatio="none"
                            role="img"
                            aria-label="GPU utilization over time"
                          >
                            {[0, 50, 100, 150, 200].map((y) => (
                              <line
                                key={y}
                                x1="0"
                                x2="560"
                                y1={y}
                                y2={y}
                                stroke="var(--studio-border)"
                                strokeWidth="1"
                                vectorEffect="non-scaling-stroke"
                              />
                            ))}
                            {chartSeries.map((series) => (
                              <polyline
                                key={series.key}
                                points={series.points}
                                fill="none"
                                stroke={series.color}
                                strokeWidth="2"
                                strokeLinejoin="round"
                                strokeLinecap="round"
                                vectorEffect="non-scaling-stroke"
                              />
                            ))}
                          </svg>
                        </div>
                      </div>
                      <div className="nerve-chart-legend">
                        {chartSeries.map((series) => (
                          <span className="nerve-legend-chip" key={series.key}>
                            <i style={{ background: series.color }} />
                            <span>{series.label}</span>
                            <b>{series.latest}%</b>
                          </span>
                        ))}
                      </div>
                    </>
                  )}
                </section>

                <section className="nr-card" aria-label="Render nodes">
                  <header className="nr-card-head">
                    <div>
                      <h2>Render nodes</h2>
                      <p>Cluster Health Monitoring</p>
                    </div>
                  </header>
                  {workers.length === 0 ? (
                    <div className="nr-empty">
                      <Server size={18} />
                      <strong>No workers registered</strong>
                      <p>The coordinator is online but has no render workers configured yet.</p>
                    </div>
                  ) : (
                    <div className="nerve-node-list">
                      {sortedWorkers.map((worker, i) => {
                        const live = liveWorkerGpu.get(worker.worker_id ?? "");
                        const online = live?.online ?? Boolean(worker.online);
                        const devices = online ? (live?.gpus ?? worker.identity?.gpu_stats ?? []) : [];
                        const names = devices.length
                          ? devices.map((gpu) => gpu.name ?? "GPU")
                          : online
                            ? (worker.identity?.gpu_devices ?? []).map((gpu) => gpu.split(",")[0])
                            : [];
                        const kind = [...new Set(names)].join(", ") || (online ? "GPU unavailable" : "offline");
                        const used = devices.reduce((sum, gpu) => sum + (gpu.memory_used_mib ?? 0), 0);
                        const total = devices.reduce((sum, gpu) => sum + (gpu.memory_total_mib ?? 0), 0);
                        const nodePct = total > 0 ? Math.round((used / total) * 100) : 0;
                        const nodeTone = nodePct >= 90 ? "is-hot" : nodePct >= 70 ? "is-warm" : "";
                        return (
                          <div className="nerve-node-row" key={worker.worker_id ?? i}>
                            <div className="nerve-node-top">
                              <span className={`nerve-node-dot${online ? " is-online" : ""}`} aria-hidden="true" />
                              <span className="nerve-node-name">
                                {worker.name ?? worker.identity?.hostname ?? worker.worker_id ?? `Worker ${i + 1}`}
                                <small>{kind}</small>
                              </span>
                              <span className="nerve-node-alloc">
                                {total > 0 ? `${nodePct}% GPU Allocation` : online ? "No VRAM data" : "Offline"}
                              </span>
                            </div>
                            <div
                              className="nerve-node-track"
                              role="meter"
                              aria-label={`${worker.name ?? worker.worker_id ?? `Worker ${i + 1}`} GPU allocation`}
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={nodePct}
                            >
                              <div className={`nerve-node-fill ${nodeTone}`} style={{ width: `${nodePct}%` }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>
              </div>

              <div className={`nerve-cards-row is-row-three${hasPreview ? "" : " is-single"}`}>
                <section className="nr-card" aria-label="Render activity">
                  <header className="nr-card-head">
                    <div>
                      <h2>Render activity</h2>
                      <p>{backendReady ? "Current job and recent queue" : "Connect to load the render queue"}</p>
                    </div>
                    <span className="nr-card-count">{backendReady ? `${queuedJobs.length} recent` : "—"}</span>
                  </header>
                  {currentJob ? (
                    <div className="nr-render">
                      <div className="nr-render-status">
                        <span className="nr-pulse" aria-hidden="true" />
                        {currentJob.status === "paused" ? "Paused" : "Rendering now"}
                        <span>{currentJob.id ?? currentJob.job_id ?? "ACTIVE JOB"}</span>
                      </div>
                      <div className="nr-render-title">{shortName(currentJob.project_path)}</div>
                      <div className="nr-render-bar">
                        <div style={{ width: `${currentProgress ?? 0}%` }} />
                      </div>
                      <div className="nr-render-meta">
                        <span>
                          Frames {currentJob.frame_start ?? "?"} — {currentJob.frame_end ?? "?"} ·{" "}
                          {currentProgress === null ? "—" : `${currentProgress}%`}
                        </span>
                        <span>{cadence === "manual" ? "Manual refresh" : `Updates every ${cadence}s`}</span>
                      </div>
                    </div>
                  ) : (
                    <div className="nr-empty">
                      <Film size={18} />
                      <strong>{backendReady ? "No render in progress" : "Connect to load render queue"}</strong>
                      <p>
                        {backendReady
                          ? "Submit a farm job to track its progress."
                          : "Farm and job data comes from the authenticated coordinator."}
                      </p>
                    </div>
                  )}
                  {backendReady && (
                    <div className="nr-queue-chips">
                      {[
                        { label: "Active", value: activeJobs.length, cls: "is-active" },
                        { label: "Queued", value: queuedJobs.filter((job) => job.status === "queued").length, cls: "" },
                        { label: "Failed", value: failedJobs.length, cls: "is-failed" },
                        { label: "Done", value: completedJobCount, cls: "is-done" },
                      ].map(({ label, value, cls }) => (
                        <div className={`nr-queue-chip ${cls}`} key={label}>
                          <strong>{value}</strong>
                          <span>{label}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                {hasPreview && (
                  <section
                    className="nr-card"
                    aria-label="Latest render output"
                    onClick={() => (latestOpenFile ? openFileInGallery(latestOpenFile) : setScreen("Gallery"))}
                  >
                    <header className="nr-card-head">
                      <div>
                        <h2>Latest output</h2>
                        <p>{latestMedia?.name ?? "Workspace render preview"}</p>
                      </div>
                      <span className="nr-card-count">Available</span>
                    </header>
                    <div className="preview-stage">
                      {latestMedia?.type === "video" ? (
                        // biome-ignore lint/a11y/useMediaCaption: workspace renders have no caption sidecars.
                        <video controls src={latestBlobUrl ?? undefined} className="preview-media" />
                      ) : (
                        <img src={latestBlobUrl ?? undefined} alt={latestMedia?.name ?? "Latest render preview"} />
                      )}
                    </div>
                    <div className="preview-meta">
                      <span>
                        <HardDrive size={13} /> {latestMedia?.name ?? "WORKSPACE PREVIEW"}
                      </span>
                      <button type="button" onClick={() => setScreen("Gallery")}>
                        View gallery <ArrowUpRight size={13} />
                      </button>
                    </div>
                  </section>
                )}
              </div>

              </div>{/* end hidden legacy cards */}

              <footer className="studio-footer nr-footer">
                <span>
                  {backendReady
                    ? `SERVER ${health?.version ?? "CONNECTED"} · ${health?.tools ?? "—"} TOOLS`
                    : "LIVE DATA REQUIRES AUTHENTICATED CONNECTION"}
                </span>
              </footer>
            </>
          ) : screen === "Jobs" ? (
            <StudioJobs key={apiKey ? "connected" : "disconnected"} />
          ) : screen === "Monitoring" ? (
            <MonitoringScreen
              diagnostics={diagnostics}
              farm={farm}
              health={health}
              samples={monitorSamples}
              authReady={authReady}
              onConnect={() => setShowConnect(true)}
              onOpenWorkers={() => setScreen("Workers")}
              farmGpuTelemetry={farmGpuTelemetry}
              gpuWindow={gpuWindow}
              onGpuWindowChange={changeGpuWindow}
              gpuHistory={gpuHistory.payload}
              gpuHistoryStatus={gpuHistory.status}
              farmGpuHistory={farmGpuHistory}
            />
          ) : screen === "Settings" ? (
            <SettingsScreen
              cadence={cadence}
              onCadenceChange={changeCadence}
              storageError={storageError}
              health={health}
              diagnostics={diagnostics}
              authReady={authReady}
              authError={authError}
              onConnect={() => setShowConnect(true)}
              onDisconnect={disconnect}
              onRefresh={() => void refresh()}
              refreshing={refreshing}
              onNavigate={setScreen}
              dashboardSession={dashboardSession}
              onSignOut={() => void signOut()}
              signingOut={signingOut}
              signOutError={signOutError}
              notificationPrefs={notificationPrefs}
              onNotificationPrefsChange={changeNotificationPrefs}
              notificationsSaveError={notificationsSaveError}
              eventLog={eventLog}
              onClearEvents={clearEvents}
            />
          ) : (
            <StudioScreens
              screen={screen}
              workerView={workerView}
              onWorkerViewChange={setWorkerView}
              openFile={openFile}
              onOpenFileHandled={handleOpenFileHandled}
            />
          )}
        </div>
      </main>

      {showConnect && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setShowConnect(false);
          }}
        >
          <section className="connect-modal" role="dialog" aria-modal="true" aria-labelledby="connect-title">
            <button type="button" className="modal-close" aria-label="Close" onClick={() => setShowConnect(false)}>
              <X size={17} />
            </button>
            <div className="modal-lock">
              <LockKeyhole size={19} />
            </div>
            <div className="panel-kicker">LOCAL API CONNECTION</div>
            <h2 id="connect-title">Connect coordinator</h2>
            <p>
              Enter the API key configured on the Blender MCP server. It is stored in this tab’s session storage only
              and sent only to this server. Enter it only over localhost or a trusted connection.
            </p>
            <form onSubmit={connect}>
              <label htmlFor="api-key">BLENDER MCP API KEY</label>
              <input
                id="api-key"
                type="password"
                autoComplete="off"
                value={draftKey}
                onChange={(event) => setDraftKey(event.target.value)}
                placeholder="Paste key"
              />
              <div className="modal-error" aria-live="polite">
                {authError}
              </div>
              <div className="modal-actions">
                <button type="button" className="cancel-button" onClick={() => setShowConnect(false)}>
                  Cancel
                </button>
                <button type="submit" className="connect-button" disabled={verifying}>
                  {verifying ? "Verifying…" : "Verify & connect"} <ChevronRight size={15} />
                </button>
              </div>
            </form>
            <div className="modal-security">
              <ShieldCheck size={14} /> Never included in the page URL or saved to local storage.
            </div>
          </section>
        </div>
      )}
      {notificationPrefs.enabled && <ToastStack toasts={toasts} onDismiss={dismissToast} />}
    </div>
  );
}