/** Per-browser notification preferences for the dashboard. */

export const NOTIFICATION_PREFS_KEY = "blender-mcp-notification-prefs";

export type NotificationPrefs = {
  /** Master switch: when off, no in-app toasts are shown. */
  enabled: boolean;
  /** A render job reached the completed state. */
  jobFinished: boolean;
  /** A render job reached the failed state. */
  jobFailed: boolean;
  /** A farm worker went offline or came back online. */
  workerChanges: boolean;
  /** A new critical alert appeared, such as the coordinator becoming unreachable. */
  serverErrors: boolean;
};

export const DEFAULT_NOTIFICATION_PREFS: NotificationPrefs = {
  enabled: true,
  jobFinished: true,
  jobFailed: true,
  workerChanges: true,
  serverErrors: true,
};

/** Reads saved preferences, falling back to defaults for missing or malformed values. */
export function readNotificationPrefs(): NotificationPrefs {
  try {
    const raw = window.localStorage.getItem(NOTIFICATION_PREFS_KEY);
    if (!raw) return DEFAULT_NOTIFICATION_PREFS;
    const parsed = JSON.parse(raw) as Partial<Record<keyof NotificationPrefs, unknown>>;
    const pick = (key: keyof NotificationPrefs) =>
      typeof parsed[key] === "boolean" ? (parsed[key] as boolean) : DEFAULT_NOTIFICATION_PREFS[key];
    return {
      enabled: pick("enabled"),
      jobFinished: pick("jobFinished"),
      jobFailed: pick("jobFailed"),
      workerChanges: pick("workerChanges"),
      serverErrors: pick("serverErrors"),
    };
  } catch {
    return DEFAULT_NOTIFICATION_PREFS;
  }
}

/** Saves preferences. Returns false when storage is unavailable, such as in private mode. */
export function writeNotificationPrefs(prefs: NotificationPrefs): boolean {
  try {
    window.localStorage.setItem(NOTIFICATION_PREFS_KEY, JSON.stringify(prefs));
    return true;
  } catch {
    return false;
  }
}