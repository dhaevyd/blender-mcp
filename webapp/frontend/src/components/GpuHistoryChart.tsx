/**
 * GPU history charts — Recharts rendering, no hand-rolled SVG geometry.
 *
 * The backend buckets nvidia-smi samples server-side (`GET /api/v1/monitoring/gpu`),
 * so this module only renders what it is given. Time is the x axis (unix seconds
 * converted to milliseconds), never the array index: uneven buckets and missing
 * samples therefore keep their real spacing. Null samples stay in the data array and
 * `connectNulls={false}` turns them into visible breaks instead of interpolated lines.
 * Utilisation is the primary area on a 0..100 axis, VRAM used a secondary line on a
 * right-hand 0..memory_total_mib axis.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatSampleTime, MetricSparkline } from "./MetricSparkline";

export type GpuWindow = "live" | "5m" | "15m" | "30m" | "1h" | "3h" | "6h" | "12h" | "1d" | "3d";

export const GPU_WINDOWS: ReadonlyArray<{ value: GpuWindow; label: string }> = [
  { value: "live", label: "Live" },
  { value: "5m", label: "5m" },
  { value: "15m", label: "15m" },
  { value: "30m", label: "30m" },
  { value: "1h", label: "1h" },
  { value: "3h", label: "3h" },
  { value: "6h", label: "6h" },
  { value: "12h", label: "12h" },
  { value: "1d", label: "1d" },
  { value: "3d", label: "3d" },
];

export function isGpuWindow(value: unknown): value is GpuWindow {
  return typeof value === "string" && GPU_WINDOWS.some((option) => option.value === value);
}

export type GpuHistoryPoint = {
  t?: number | null;
  util?: number | null;
  mem_used_mib?: number | null;
  temp_c?: number | null;
  power_w?: number | null;
};

export type GpuHistoryDevice = {
  index?: number | null;
  name?: string | null;
  memory_total_mib?: number | null;
  series?: GpuHistoryPoint[] | null;
};

export type GpuHistoryPayload = {
  window?: string | null;
  bucket_seconds?: number | null;
  retention_days?: number | null;
  sampled_at?: string | null;
  gpus?: GpuHistoryDevice[] | null;
};

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const COMPACT_HEIGHT = 34;
const DEFAULT_HEIGHT = 168;
const METRIC_HEIGHT = 62;

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** Ticks read as clock times up to a day, then as day markers so a 3d axis never crowds. */
export function formatGpuTick(timestamp: number, window: GpuWindow, narrow = false): string {
  const date = new Date(timestamp * 1000);
  const hours = pad2(date.getHours());
  const minutes = pad2(date.getMinutes());
  const seconds = pad2(date.getSeconds());
  if (window === "live" || window === "5m") return `${hours}:${minutes}:${seconds}`;
  if (window === "1d")
    return narrow ? `${WEEKDAYS[date.getDay()]} ${hours}` : `${WEEKDAYS[date.getDay()]} ${hours}:${minutes}`;
  if (window === "3d") return `${date.getDate()} ${MONTHS[date.getMonth()]}`;
  return `${hours}:${minutes}`;
}

export function formatGpuWindowLabel(window: GpuWindow): string {
  return GPU_WINDOWS.find((option) => option.value === window)?.label ?? window;
}

function formatGigabytes(mib: number): string {
  const gb = mib / 1024;
  if (!Number.isFinite(gb)) return "—";
  return gb >= 10 ? gb.toFixed(0) : gb.toFixed(1);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function positiveOrNull(value: number | null | undefined): number | null {
  return isFiniteNumber(value) && value > 0 ? value : null;
}

/** Fit the axis to actual observations; never imply coverage where none was sampled. */
function observedTimeDomain(rows: ChartRow[], selectedMetric?: GpuMetricKey): [number, number] | null {
  let first = Number.POSITIVE_INFINITY;
  let last = Number.NEGATIVE_INFINITY;
  for (const row of rows) {
    const plotted = selectedMetric
      ? row.selected !== null && row.selected !== undefined
      : row.util !== null || row.mem !== null;
    if (!plotted) continue;
    first = Math.min(first, row.tMs);
    last = Math.max(last, row.tMs);
  }
  if (!Number.isFinite(first) || !Number.isFinite(last)) return null;
  if (first === last) return [first - 500, last + 500];
  return [first, last];
}

/** Recharts hands tick values back as numbers, or as Dates once `scale="time"` kicks in. */
function tickValueToSeconds(value: unknown): number {
  if (typeof value === "number") return value / 1000;
  if (value instanceof Date) return value.getTime() / 1000;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

function niceCeil(value: number, step: number): number {
  const base = Number.isFinite(value) && value > 0 ? value : 0;
  return Math.max(step, Math.ceil(base / step) * step);
}

/** Evenly spaced axis ticks, deduped by label: a 3d window can land two ticks on one day. */
function gpuTicks(count: number, window: GpuWindow, narrow: boolean, domain: [number, number]): number[] {
  if (count < 2) return [];
  const [first, last] = domain;
  const span = last - first;
  const ticks: number[] = [];
  const seen = new Set<string>();
  for (let index = 0; index < count; index += 1) {
    const tMs = Math.round(first + (index / (count - 1)) * span);
    const label = formatGpuTick(tMs / 1000, window, narrow);
    if (seen.has(label)) continue;
    seen.add(label);
    ticks.push(tMs);
  }
  return ticks;
}

type ChartRow = {
  tMs: number;
  util: number | null;
  mem: number | null;
  temp: number | null;
  power: number | null;
  selected?: number | null;
};

function metricPointValue(point: GpuHistoryPoint, metric: GpuMetricKey): number | null {
  const value =
    metric === "vram"
      ? point.mem_used_mib
      : metric === "util"
        ? point.util
        : metric === "temp"
          ? point.temp_c
          : point.power_w;
  return isFiniteNumber(value) ? Math.max(0, metric === "util" ? clamp(value, 0, 100) : value) : null;
}

function toChartRows(device: GpuHistoryDevice, selectedMetric?: GpuMetricKey): ChartRow[] {
  const raw = Array.isArray(device.series) ? device.series : [];
  const rows: ChartRow[] = [];
  for (const point of raw) {
    if (!point || !isFiniteNumber(point.t)) continue;
    rows.push({
      tMs: Math.round(point.t * 1000),
      util: isFiniteNumber(point.util) ? clamp(point.util, 0, 100) : null,
      mem: isFiniteNumber(point.mem_used_mib) ? Math.max(0, point.mem_used_mib) : null,
      temp: isFiniteNumber(point.temp_c) ? Math.max(0, point.temp_c) : null,
      power: isFiniteNumber(point.power_w) ? Math.max(0, point.power_w) : null,
      selected: selectedMetric ? metricPointValue(point, selectedMetric) : undefined,
    });
  }
  rows.sort((a, b) => a.tMs - b.tMs);
  return rows;
}

/** Real buttons with aria-pressed: arrow keys come free from the tab order, Enter/Space from <button>. */
export function GpuWindowSelector({
  value,
  onChange,
  disabled = false,
}: {
  value: GpuWindow;
  onChange: (value: GpuWindow) => void;
  disabled?: boolean;
}) {
  return (
    <label className="gpu-window-control">
      GPU history range{" "}
      <select
        aria-label="GPU history range"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as GpuWindow)}
      >
        {GPU_WINDOWS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function GpuChartLegend({
  hasUtil,
  hasVram,
  note,
  metric,
}: {
  hasUtil: boolean;
  hasVram: boolean;
  note?: string;
  metric?: GpuMetricKey;
}) {
  return (
    <div className="gpu-chart-legend">
      {metric ? (
        <span className="gpu-legend-item">
          <i className={`gpu-legend-swatch ${metric}`} aria-hidden="true" /> {GPU_METRIC_SPECS[metric].label}
        </span>
      ) : (
        <>
          <span className={`gpu-legend-item ${hasUtil ? "" : "muted"}`}>
            <i className="gpu-legend-swatch util" aria-hidden="true" /> Utilisation %
          </span>
          <span className={`gpu-legend-item ${hasVram ? "" : "muted"}`}>
            <i className="gpu-legend-swatch vram" aria-hidden="true" /> VRAM used
          </span>
        </>
      )}
      {note ? <span className="gpu-chart-note">{note}</span> : null}
    </div>
  );
}

type GpuTooltipEntry = { payload?: ChartRow | null };

function GpuChartTooltip({
  active,
  payload,
  window,
  metric,
}: {
  active?: boolean;
  payload?: ReadonlyArray<GpuTooltipEntry>;
  window: GpuWindow;
  metric?: GpuMetricKey;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="gpu-chart-tooltip">
      <span className="gpu-chart-tooltip-time">
        {window === "3d"
          ? `${row.tMs ? `${new Date(row.tMs).getDate()} ${MONTHS[new Date(row.tMs).getMonth()]}` : "—"} ${formatSampleTime(row.tMs)}`
          : formatGpuTick(row.tMs / 1000, window)}
      </span>
      {metric ? (
        <span className="gpu-chart-tooltip-row">
          <i className={`gpu-legend-swatch ${metric}`} aria-hidden="true" />
          <span>{GPU_METRIC_SPECS[metric].label}</span>
          <strong>{row.selected == null ? "—" : formatMetricValue(metric, row.selected)}</strong>
        </span>
      ) : (
        <>
          <span className="gpu-chart-tooltip-row">
            <i className="gpu-legend-swatch util" aria-hidden="true" />
            <span>Utilisation</span>
            <strong>{row.util === null ? "—" : `${Math.round(row.util)}%`}</strong>
          </span>
          {row.mem === null ? null : (
            <span className="gpu-chart-tooltip-row">
              <i className="gpu-legend-swatch vram" aria-hidden="true" />
              <span>VRAM used</span>
              <strong>{formatGigabytes(row.mem)} GB</strong>
            </span>
          )}
        </>
      )}
    </div>
  );
}

export function GpuHistoryChart({
  device,
  window: gpuWindow,
  bucketSeconds,
  compact = false,
  height,
  selectedMetric,
}: {
  device: GpuHistoryDevice;
  window: GpuWindow;
  bucketSeconds?: number | null;
  compact?: boolean;
  height?: number;
  selectedMetric?: GpuMetricKey | null;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const shellRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const rows = useMemo(() => toChartRows(device, selectedMetric ?? undefined), [device, selectedMetric]);

  useLayoutEffect(() => {
    const element = shellRef.current;
    if (!element) return;
    const measure = () => {
      const next = Math.max(120, Math.round(element.clientWidth));
      setWidth((previous) => (previous === next ? previous : next));
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const chartHeight = height ?? (compact ? COMPACT_HEIGHT : DEFAULT_HEIGHT);
  const hasUtil = rows.some((row) => row.util !== null);
  const hasVram = rows.some((row) => row.mem !== null);
  const hasSelected = rows.some((row) => row.selected != null);
  const bucketLabel =
    typeof bucketSeconds === "number" && bucketSeconds > 0
      ? bucketSeconds >= 60
        ? `${Math.round(bucketSeconds / 60)}m buckets`
        : `${bucketSeconds}s buckets`
      : "";
  const note = `${rows.length} samples${bucketLabel ? ` · ${bucketLabel}` : ""}`;

  if (selectedMetric ? !hasSelected : !hasUtil && !hasVram) {
    return (
      <div className="gpu-chart-shell" ref={shellRef}>
        <div className={compact ? "gpu-chart-empty compact" : "gpu-chart-empty"} style={{ height: chartHeight }}>
          {selectedMetric
            ? `No ${GPU_METRIC_SPECS[selectedMetric].label.toLowerCase()} samples in this window.`
            : compact
              ? "No util samples yet"
              : "No samples in this window yet — the sampler needs time to accumulate."}
        </div>
      </div>
    );
  }

  const totalMib = positiveOrNull(device.memory_total_mib);
  const observedMemMax = rows.reduce((max, row) => (row.mem !== null && row.mem > max ? row.mem : max), 0);
  const vramMax = totalMib ?? niceCeil(observedMemMax, 512);
  const selectedHigh = rows.reduce((max, row) => (row.selected != null && row.selected > max ? row.selected : max), 0);
  const selectedDomain = selectedMetric ? metricDomain(selectedMetric, selectedHigh, totalMib) : null;
  const selectedColor = selectedMetric ? GPU_METRIC_SPECS[selectedMetric].color : "var(--primary)";
  const selectedAxisTicks =
    selectedMetric === "util"
      ? [0, 50, 100]
      : selectedDomain
        ? [0, selectedDomain[1] / 2, selectedDomain[1]]
        : [0, 50, 100];

  const narrow = width > 0 && width < 420;
  const tickCount = width < 380 ? 3 : width < 560 ? 4 : 5;
  const xDomain = observedTimeDomain(rows, selectedMetric ?? undefined) ?? [0, 1];
  const tickValues = gpuTicks(tickCount, gpuWindow, narrow, xDomain);
  const singlePoint = rows.length === 1;

  return (
    <div className="gpu-chart-shell" ref={shellRef}>
      <div
        className="gpu-chart-plot"
        role="img"
        aria-label={`${device.name ?? "GPU"} ${compact ? "utilisation sparkline" : "history"} over ${formatGpuWindowLabel(
          gpuWindow,
        )}: ${selectedMetric ? GPU_METRIC_SPECS[selectedMetric].label : `utilisation${hasVram && !compact ? " and VRAM used" : ""}`}, ${rows.length} ${
          rows.length === 1 ? "sample" : "samples"
        }`}
      >
        <ResponsiveContainer width="100%" height={chartHeight}>
          <ComposedChart
            data={rows}
            margin={{ top: compact ? 2 : 10, right: compact ? 0 : 10, bottom: 0, left: compact ? 0 : 4 }}
          >
            <defs>
              <linearGradient id={`gpu-util-${uid}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--primary)" stopOpacity={0.4} />
                <stop offset="100%" stopColor="var(--primary)" stopOpacity={0} />
              </linearGradient>
              {selectedMetric ? (
                <linearGradient id={`gpu-selected-${uid}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={selectedColor} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={selectedColor} stopOpacity={0} />
                </linearGradient>
              ) : null}
            </defs>
            <XAxis
              dataKey="tMs"
              type="number"
              scale="time"
              domain={xDomain}
              hide={compact}
              height={18}
              ticks={tickValues}
              interval={0}
              tickMargin={6}
              tickFormatter={(value: unknown) => formatGpuTick(tickValueToSeconds(value), gpuWindow, narrow)}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              yAxisId={selectedMetric ? "selected" : "util"}
              domain={selectedDomain ?? [0, 100]}
              ticks={selectedAxisTicks}
              tickFormatter={selectedMetric ? (value: number) => formatMetricValue(selectedMetric, value) : undefined}
              width={compact ? 0 : selectedMetric ? 48 : 28}
              hide={compact}
              tickMargin={2}
              axisLine={false}
              tickLine={false}
              allowDecimals={false}
            />
            {!selectedMetric && hasVram && !compact ? (
              <YAxis
                yAxisId="vram"
                orientation="right"
                domain={[0, vramMax]}
                ticks={[0, vramMax / 2, vramMax]}
                width={34}
                tickMargin={2}
                axisLine={false}
                tickLine={false}
                tickFormatter={(value: number) => (value <= 0 ? "0" : `${formatGigabytes(value)}G`)}
              />
            ) : null}
            {compact ? null : <CartesianGrid vertical={false} stroke="var(--border)" strokeOpacity={0.3} />}
            <Area
              yAxisId={selectedMetric ? "selected" : "util"}
              dataKey={selectedMetric ? "selected" : "util"}
              type="linear"
              stroke={selectedColor}
              strokeWidth={compact ? 1.25 : 1.6}
              fill={selectedMetric ? `url(#gpu-selected-${uid})` : `url(#gpu-util-${uid})`}
              connectNulls={false}
              dot={singlePoint ? { r: 2.4, strokeWidth: 0 } : false}
              activeDot={compact ? false : { r: 3, strokeWidth: 0 }}
              isAnimationActive={false}
            />
            {!selectedMetric && hasVram && !compact ? (
              <Line
                yAxisId="vram"
                dataKey="mem"
                type="linear"
                stroke="var(--primary)"
                strokeOpacity={0.55}
                strokeDasharray="3 3"
                strokeWidth={1.1}
                connectNulls={false}
                dot={singlePoint ? { r: 2, strokeWidth: 0 } : false}
                activeDot={{ r: 3, strokeWidth: 0 }}
                isAnimationActive={false}
              />
            ) : null}
            {compact ? null : (
              <Tooltip
                content={<GpuChartTooltip window={gpuWindow} metric={selectedMetric ?? undefined} />}
                cursor={{ stroke: "var(--border)", strokeWidth: 1 }}
                wrapperClassName="gpu-chart-tooltip-wrap"
                isAnimationActive={false}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {compact ? null : (
        <GpuChartLegend hasUtil={hasUtil} hasVram={hasVram} note={note} metric={selectedMetric ?? undefined} />
      )}
    </div>
  );
}

export type GpuMetricKey = "vram" | "util" | "temp" | "power";

const GPU_METRIC_POINT_KEYS: Record<GpuMetricKey, "util" | "mem_used_mib" | "temp_c" | "power_w"> = {
  vram: "mem_used_mib",
  util: "util",
  temp: "temp_c",
  power: "power_w",
};

const GPU_METRIC_SPECS: Record<GpuMetricKey, { label: string; unit: string; color: string }> = {
  vram: { label: "VRAM", unit: "MiB", color: "var(--primary)" },
  util: { label: "Utilisation", unit: "%", color: "var(--primary)" },
  temp: { label: "Temperature", unit: "°C", color: "var(--warning, #e7a83e)" },
  power: { label: "Power", unit: "W", color: "var(--success, #49b783)" },
};

type MetricRow = { tMs: number; value: number | null };

function toMetricRows(device: GpuHistoryDevice, metric: GpuMetricKey): MetricRow[] {
  const raw = Array.isArray(device.series) ? device.series : [];
  const key = GPU_METRIC_POINT_KEYS[metric];
  const rows: MetricRow[] = [];
  for (const point of raw) {
    if (!point || !isFiniteNumber(point.t)) continue;
    const value = point[key];
    const numeric = isFiniteNumber(value) ? (metric === "util" ? clamp(value, 0, 100) : Math.max(0, value)) : null;
    rows.push({ tMs: Math.round(point.t * 1000), value: numeric });
  }
  rows.sort((a, b) => a.tMs - b.tMs);
  return rows;
}

function metricDomain(metric: GpuMetricKey, high: number, totalMib: number | null): [number, number] {
  switch (metric) {
    case "util":
      return [0, 100];
    case "vram":
      return [0, totalMib ?? niceCeil(high, 512)];
    case "temp":
      // Zero-based with 100 °C as the top: the thermal limit is the meaningful
      // ceiling, so a cool card reads as a low line instead of a full-height one.
      return [0, Math.max(100, niceCeil(high, 10))];
    case "power":
      return [0, niceCeil(high, high <= 100 ? 10 : 25)];
  }
}

function formatMetricValue(metric: GpuMetricKey, value: number): string {
  switch (metric) {
    case "vram":
      return `${formatGigabytes(value)} GB`;
    case "util":
      return `${Math.round(value)}%`;
    case "temp":
      return `${Math.round(value)}\u00b0C`;
    case "power":
      return `${Math.round(value)} W`;
  }
}

/** A single-metric sparkline: no axes, no in-plot text, an honest metric-specific domain. */
export function GpuMetricChart({
  device,
  window: gpuWindow,
  metric,
  emptyLabel = "No samples yet",
}: {
  device: GpuHistoryDevice;
  window: GpuWindow;
  metric: GpuMetricKey;
  bucketSeconds?: number | null;
  /** Copy for a series with no usable points; a missing sensor says so here. */
  emptyLabel?: string;
}) {
  const spec = GPU_METRIC_SPECS[metric];
  const rows = useMemo(() => toMetricRows(device, metric), [device, metric]);

  let high = 0;
  let hasSample = false;
  for (const row of rows) {
    if (row.value === null) continue;
    hasSample = true;
    if (row.value > high) high = row.value;
  }

  const totalMib = positiveOrNull(device.memory_total_mib);
  const domain = metricDomain(metric, high, totalMib);
  const domainMax = domain[1];
  const caption =
    metric === "vram" ? `0 – ${formatGigabytes(domainMax)} GB` : `0 – ${Math.round(domainMax)} ${spec.unit}`;
  // The plot and its range caption only exist once a sample does; otherwise the
  // sparkline's own empty state carries the label.
  const windowLabel = formatGpuWindowLabel(gpuWindow);
  const baseLabel = `${device.name ?? "GPU"} ${spec.label} history over ${windowLabel}`;

  return (
    <MetricSparkline
      points={rows.map((row) => ({ tMs: row.tMs, value: row.value }))}
      color={spec.color}
      domain={domain}
      formatValue={(value) => formatMetricValue(metric, value)}
      height={METRIC_HEIGHT}
      caption={hasSample ? caption : undefined}
      emptyLabel={emptyLabel}
      emptyClassName="gpu-metric-chart-empty"
      ariaLabel={hasSample ? `${baseLabel}: ${caption}` : baseLabel}
    />
  );
}
