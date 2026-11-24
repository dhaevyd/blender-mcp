import { useId } from "react";
import { Area, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

/**
 * A tiny time series with a hover readout.
 *
 * Shared by the GPU metric sparklines and the CPU/RAM trend rows so every small
 * chart in the dashboard answers a hover with the same chip: swatch, sample
 * time, value. It renders no axes and no in-plot text — the surrounding card
 * owns the label and the caption.
 */

export type MetricSeriesPoint = { tMs: number; value: number | null };

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

export function formatSampleTime(tMs: number): string {
  const date = new Date(tMs);
  if (Number.isNaN(date.getTime())) return "—";
  return `${WEEKDAYS[date.getDay()]} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function timeDomain(points: ReadonlyArray<MetricSeriesPoint>): [number, number] {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    if (point.tMs < min) min = point.tMs;
    if (point.tMs > max) max = point.tMs;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) {
    const pad = 30_000;
    return [min - pad, max + pad];
  }
  return [min, max];
}

function SparkTooltip({
  active,
  payload,
  color,
  formatValue,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: MetricSeriesPoint | null }>;
  color: string;
  formatValue: (value: number) => string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="spark-tooltip">
      <i className="spark-tooltip-swatch" style={{ background: color }} aria-hidden="true" />
      <span className="spark-tooltip-time">{formatSampleTime(row.tMs)}</span>
      <strong>{row.value === null ? "—" : formatValue(row.value)}</strong>
    </div>
  );
}

export function MetricSparkline({
  points,
  color,
  domain,
  formatValue,
  height = 30,
  ariaLabel,
  caption,
  emptyLabel = "No samples yet",
  emptyClassName = "spark-empty",
}: {
  points: ReadonlyArray<MetricSeriesPoint>;
  color: string;
  domain: [number, number];
  formatValue: (value: number) => string;
  height?: number;
  ariaLabel: string;
  caption?: string;
  emptyLabel?: string;
  emptyClassName?: string;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");

  let hasValue = false;
  for (const point of points) {
    if (point.value !== null) {
      hasValue = true;
      break;
    }
  }

  if (!hasValue || points.length < 2) {
    return (
      <span className={emptyClassName} role="img" aria-label={`${ariaLabel}: ${emptyLabel}`}>
        {emptyLabel}
      </span>
    );
  }

  const singlePoint = points.length === 1;

  return (
    <div className="spark-metric">
      <div className="spark-metric-plot" role="img" aria-label={ariaLabel}>
        <ResponsiveContainer width="100%" height={height}>
          <ComposedChart data={points as MetricSeriesPoint[]} margin={{ top: 2, right: 2, bottom: 0, left: 2 }}>
            <defs>
              <linearGradient id={`spark-fill-${uid}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.36} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <XAxis dataKey="tMs" type="number" scale="time" domain={timeDomain(points)} hide />
            <YAxis domain={domain} hide />
            <Area
              dataKey="value"
              type="linear"
              stroke="none"
              fill={`url(#spark-fill-${uid})`}
              connectNulls={false}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              dataKey="value"
              type="linear"
              stroke={color}
              strokeWidth={1.6}
              connectNulls={false}
              dot={singlePoint ? { r: 2.2, strokeWidth: 0, fill: color } : false}
              activeDot={{ r: 3, strokeWidth: 0, fill: color }}
              isAnimationActive={false}
            />
            <Tooltip
              content={<SparkTooltip color={color} formatValue={formatValue} />}
              cursor={{ stroke: "var(--border)", strokeWidth: 1 }}
              wrapperClassName="spark-tooltip-wrap"
              allowEscapeViewBox={{ x: false, y: true }}
              offset={12}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {caption ? <span className="spark-metric-caption">{caption}</span> : null}
    </div>
  );
}
