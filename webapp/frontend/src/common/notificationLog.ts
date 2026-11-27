import type { ToastMessage } from "./notificationEvents";

/** Every notification the dashboard has raised, newest first, kept in this browser. */
export const NOTIFICATION_LOG_KEY = "blender-mcp-notification-log";

/** Oldest entries are dropped beyond this size so storage stays small. */
export const NOTIFICATION_LOG_LIMIT = 200;

export type NotificationLogEntry = ToastMessage & {
  /** ISO timestamp of when the event was raised. */
  at: string;
};

export function readNotificationLog(): NotificationLogEntry[] {
  try {
    const raw = window.localStorage.getItem(NOTIFICATION_LOG_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (entry): entry is NotificationLogEntry =>
          typeof entry === "object" &&
          entry !== null &&
          typeof (entry as NotificationLogEntry).id === "string" &&
          typeof (entry as NotificationLogEntry).title === "string" &&
          typeof (entry as NotificationLogEntry).at === "string",
      )
      .slice(0, NOTIFICATION_LOG_LIMIT);
  } catch {
    return [];
  }
}

/** Saves the log. Returns false when browser storage is unavailable. */
export function writeNotificationLog(entries: NotificationLogEntry[]): boolean {
  try {
    window.localStorage.setItem(NOTIFICATION_LOG_KEY, JSON.stringify(entries.slice(0, NOTIFICATION_LOG_LIMIT)));
    return true;
  } catch {
    return false;
  }
}

/** Adds new events to the front of the log, newest first. */
export function prependToLog(
  current: NotificationLogEntry[],
  events: ToastMessage[],
  at: Date = new Date(),
): NotificationLogEntry[] {
  const stamp = at.toISOString();
  const stamped = events.map((event) => ({ ...event, at: stamp }));
  return [...stamped, ...current].slice(0, NOTIFICATION_LOG_LIMIT);
}
