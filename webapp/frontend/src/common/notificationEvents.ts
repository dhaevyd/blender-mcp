import type { NotificationPrefs } from "./notificationPrefs";

export type ToastTone = "good" | "warn" | "bad" | "info";

export type ToastMessage = {
  /** Stable per event, so the same change is never shown twice. */
  id: string;
  tone: ToastTone;
  title: string;
  detail?: string;
};

/*
 * Each function compares the previous snapshot with the current one and returns
 * the toasts for changes only. A `null` previous snapshot means this is the first
 * one seen, so nothing is reported. Opening the dashboard should not replay history.
 */

/** Job status changes into finished or failed. `prev` maps job id to last known status. */
export function jobEvents(
  prev: ReadonlyMap<string, string> | null,
  jobs: ReadonlyArray<{ id: string; status: string; label: string }>,
  prefs: NotificationPrefs,
): ToastMessage[] {
  if (!prev || !prefs.enabled) return [];
  const events: ToastMessage[] = [];
  for (const job of jobs) {
    // A job first seen after the baseline counts as a change, so a short job that finished between polls still reports.
    if (prev.get(job.id) === job.status) continue;
    if (job.status === "completed" && prefs.jobFinished) {
      events.push({ id: `job-${job.id}-completed`, tone: "good", title: "Render finished", detail: job.label });
    } else if (job.status === "failed" && prefs.jobFailed) {
      events.push({ id: `job-${job.id}-failed`, tone: "bad", title: "Render failed", detail: job.label });
    }
  }
  return events;
}

/** Worker online/offline transitions. `prev` maps worker id to last known online state. */
export function workerEvents(
  prev: ReadonlyMap<string, boolean> | null,
  workers: ReadonlyArray<{ id: string; label: string; online: boolean }>,
  prefs: NotificationPrefs,
): ToastMessage[] {
  if (!prev || !prefs.enabled || !prefs.workerChanges) return [];
  const events: ToastMessage[] = [];
  for (const worker of workers) {
    const before = prev.get(worker.id);
    if (before === undefined || before === worker.online) continue;
    events.push(
      worker.online
        ? { id: `worker-${worker.id}-online`, tone: "good", title: "Worker back online", detail: worker.label }
        : { id: `worker-${worker.id}-offline`, tone: "warn", title: "Worker went offline", detail: worker.label },
    );
  }
  return events;
}

/** Newly appearing critical alerts. `prev` holds the critical alert ids seen last time. */
export function alertEvents(
  prev: ReadonlySet<string> | null,
  alerts: ReadonlyArray<{ id: string; severity: string; title: string; detail?: string }>,
  prefs: NotificationPrefs,
): ToastMessage[] {
  if (!prev || !prefs.enabled || !prefs.serverErrors) return [];
  return alerts
    .filter((alert) => alert.severity === "critical" && !prev.has(alert.id))
    .map((alert) => ({
      id: `alert-${alert.id}-${alert.title}`,
      tone: "bad" as const,
      title: alert.title,
      detail: alert.detail,
    }));
}
