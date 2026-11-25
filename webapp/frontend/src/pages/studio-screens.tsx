import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ChevronDown,
  Download,
  File,
  FileCode,
  FileImage,
  FileVideo,
  Folder,
  LayoutGrid,
  List,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import {
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { dashboardAuthHeaders, getDashboardKey } from "../api/dashboardAuth";
import StudioWorkers from "./studio-workers";
import "./studio-gallery-browser.css";
import "./studio-gallery-bulk.css";

type WorkspaceEntry = {
  name: string;
  path: string;
  type: "folder" | "file";
  size: number | null;
  modified: string;
  added: string | null;
  format: string;
  preview: "image" | "video" | "text" | "none";
};

type SortKey = "name" | "modified" | "size" | "format" | "added";

type SortDirection = "asc" | "desc";

/** The gallery sort choice; the only gallery state that outlives a remount or reload. */
type GallerySort = { sortBy: SortKey; direction: SortDirection };

const SORT_KEYS: readonly SortKey[] = ["name", "modified", "size", "format", "added"];

/** Versioned key so a future payload change reads as "nothing saved" instead of misparsing. */
const GALLERY_SORT_STORAGE_KEY = "studio-gallery-sort.v1";

const DEFAULT_GALLERY_SORT: GallerySort = { sortBy: "name", direction: "asc" };

/** How long the undo toast stays before it dismisses itself. */
const TOAST_MS = 8000;

function isSortKey(value: unknown): value is SortKey {
  return typeof value === "string" && SORT_KEYS.includes(value as SortKey);
}

/**
 * Saved sort pair, validated field by field: an unknown key or direction falls back to the
 * defaults rather than leaving the select in a state it cannot represent. Missing, corrupt
 * JSON and a blocked store (private mode) all read as "nothing saved".
 */
function readStoredSort(): GallerySort {
  try {
    const raw = window.localStorage.getItem(GALLERY_SORT_STORAGE_KEY);
    if (!raw) return DEFAULT_GALLERY_SORT;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return DEFAULT_GALLERY_SORT;
    const { sortBy, direction } = parsed as { sortBy?: unknown; direction?: unknown };
    if (!isSortKey(sortBy) || (direction !== "asc" && direction !== "desc")) return DEFAULT_GALLERY_SORT;
    return { sortBy, direction };
  } catch {
    return DEFAULT_GALLERY_SORT;
  }
}

/** Icon grid or the dense row list; remembered per browser like the sort choice. */
type GalleryView = "list" | "grid";

const GALLERY_VIEW_STORAGE_KEY = "studio-gallery-view.v1";

function readStoredView(): GalleryView {
  try {
    return window.localStorage.getItem(GALLERY_VIEW_STORAGE_KEY) === "grid" ? "grid" : "list";
  } catch {
    return "list";
  }
}

function writeStoredView(view: GalleryView): void {
  try {
    window.localStorage.setItem(GALLERY_VIEW_STORAGE_KEY, view);
  } catch {
    // The toggle still works for this visit; the choice just will not survive a reload.
  }
}

/** Best-effort write: quota errors and disabled storage must never break sorting. */
function writeStoredSort(sort: GallerySort): void {
  try {
    window.localStorage.setItem(
      GALLERY_SORT_STORAGE_KEY,
      JSON.stringify({ sortBy: sort.sortBy, direction: sort.direction }),
    );
  } catch {
    // Sorting still works for this visit; the choice just will not survive a reload.
  }
}

function sameSort(a: GallerySort, b: GallerySort): boolean {
  return a.sortBy === b.sortBy && a.direction === b.direction;
}

/** Date shown in a row: first-seen when sorting by added, else last modified. */
function entryTimestamp(entry: WorkspaceEntry, sortBy: SortKey): string {
  return sortBy === "added" ? (entry.added ?? entry.modified) : entry.modified;
}

/** One icon per entry type, shared by the row list and the icon grid. */
function EntryIcon({ entry, size = 20 }: { entry: WorkspaceEntry; size?: number }) {
  if (entry.type === "folder") return <Folder size={size} aria-hidden="true" />;
  if (entry.preview === "image") return <FileImage size={size} aria-hidden="true" />;
  if (entry.preview === "video") return <FileVideo size={size} aria-hidden="true" />;
  if (entry.preview === "text") return <FileCode size={size} aria-hidden="true" />;
  return <File size={size} aria-hidden="true" />;
}

function sizeLabel(size: number | null): string {
  if (size === null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function fileUrl(path: string, preview = false): string {
  return `/api/v1/gallery/file?path=${encodeURIComponent(path)}${preview ? "&preview=1" : ""}`;
}

/** Platform hint for the workspace-palette shortcut chip (⌘ on Apple, Ctrl elsewhere). */
function searchShortcutLabel(): string {
  if (typeof navigator === "undefined") return "Ctrl K";
  // navigator.platform is deprecated but still the most reliable Mac signal in browsers.
  const platform = navigator.platform || navigator.userAgent;
  return /mac|iphone|ipad|ipod/i.test(platform) ? "⌘ K" : "Ctrl K";
}

const SEARCH_SHORTCUT = searchShortcutLabel();

const DIALOG_FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
  'textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])';

function dialogTargets(container: HTMLElement | null): HTMLElement[] {
  return Array.from(container?.querySelectorAll<HTMLElement>(DIALOG_FOCUSABLE) ?? []);
}

/** Keep Tab / Shift+Tab inside a modal dialog, wrapping at both ends. */
function trapTab(event: ReactKeyboardEvent<HTMLElement>, container: HTMLElement | null): void {
  const targets = dialogTargets(container);
  const first = targets[0];
  const last = targets[targets.length - 1];
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/** One workspace file the dashboard asks the gallery to open in its preview dialog. */
export type GalleryFileRequest = {
  path: string;
  /** Display name for messages; the path's basename when omitted. */
  name: string;
  /** Dashboard-issued id so a stale consumption cannot clear a newer request. */
  id?: number;
};

/** Split a workspace-relative path into its containing folder and basename. */
function splitWorkspacePath(path: string): { folder: string; name: string } {
  const index = path.lastIndexOf("/");
  return index < 0 ? { folder: "", name: path } : { folder: path.slice(0, index), name: path.slice(index + 1) };
}

/** Folder holding a path; the root when the path has no separator. */
function parentPath(path: string): string {
  return splitWorkspacePath(path).folder;
}

/** Folder / file summary under the gallery title, kept in step with local removals. */
function entryCountLabel(entries: WorkspaceEntry[]): string {
  if (entries.length === 0) return "Empty folder";
  const folders = entries.filter((entry) => entry.type === "folder").length;
  const files = entries.length - folders;
  const parts: string[] = [];
  if (folders) parts.push(`${folders} folder${folders === 1 ? "" : "s"}`);
  if (files) parts.push(`${files} file${files === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** Body of a successful POST /api/v1/gallery/delete (a soft delete into workspace/.trash). */
type DeleteResult = {
  trash_id: string;
  original_path: string;
  name: string;
  type: "folder" | "file";
};

type TrashEntry = {
  trash_id: string;
  name: string;
  original_path: string;
  type: "folder" | "file" | "symlink";
  deleted_at: string;
};

/** The undo toast: one soft-deleted item, restorable until the toast expires. */
type BulkResult = { path: string; ok: boolean; error?: string; already_gone?: boolean };

const GALLERY_DRAG_TYPE = "application/x-gallery-paths";

type GalleryToast = {
  /** Identity of this toast, so a late timer cannot dismiss a newer one. */
  key: number;
  message: string;
  /** Undo target; absent on notices that cannot be undone. */
  entry: WorkspaceEntry | null;
  trashId: string | null;
  /** Set once undo has run or failed: the action is single-shot. */
  undoUsed: boolean;
  error: string;
};

export default function StudioScreens({
  screen,
  workerView = "roster",
  onWorkerViewChange,
  openFile,
  onOpenFileHandled,
}: {
  screen: string;
  workerView?: "roster" | "onboarding";
  onWorkerViewChange?: (view: "roster" | "onboarding") => void;
  openFile?: GalleryFileRequest | null;
  onOpenFileHandled?: (id: number | undefined) => void;
}) {
  const [folder, setFolder] = useState("");
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [search, setSearch] = useState("");
  // Hydrated synchronously so the first paint already shows the saved order, with no re-sort after it.
  const [sort, setSort] = useState<GallerySort>(readStoredSort);
  // Hydrated synchronously too, so a grid user never sees a list flash on load.
  const [galleryView, setGalleryView] = useState<GalleryView>(readStoredView);
  const sortBy = sort.sortBy;
  const sortDirection = sort.direction;
  // Direction last chosen per key: returning to a key restores it instead of re-applying a default.
  const sortDirections = useRef<Partial<Record<SortKey, SortDirection>>>({});
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [paletteResults, setPaletteResults] = useState<WorkspaceEntry[]>([]);
  const [paletteTruncated, setPaletteTruncated] = useState(false);
  const [paletteLoading, setPaletteLoading] = useState(false);
  const [paletteError, setPaletteError] = useState("");
  const [activeResult, setActiveResult] = useState(0);
  const paletteInput = useRef<HTMLInputElement>(null);
  const paletteDialog = useRef<HTMLDivElement>(null);
  const focusBeforePalette = useRef<HTMLElement | null>(null);
  const searchTrigger = useRef<HTMLButtonElement>(null);
  // Set while focus is being handed back to the trigger; that focus event must not reopen the palette.
  const suppressTriggerOpen = useRef(false);
  const previewClose = useRef<HTMLButtonElement>(null);
  const previewDialog = useRef<HTMLDivElement>(null);
  const previewReturn = useRef<HTMLElement | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewText, setPreviewText] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [generation, setGeneration] = useState(0);
  const previewObject = useRef("");
  const authReady = Boolean(getDashboardKey());
  // Kept apart from `error` because reloading a folder clears `error`; a jump
  // notice must survive the folder change it triggers.
  const [openNotice, setOpenNotice] = useState("");
  const openRequestSequence = useRef(0);
  // Soft delete: the row awaiting confirmation, the request in flight, and the undo toast.
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceEntry | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  // Multi-select: checked paths, the last row touched (for shift-click ranges), and bulk dialogs.
  const [checked, setChecked] = useState<Set<string>>(() => new Set());
  const checkAnchor = useRef<string | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkMoveOpen, setBulkMoveOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState("");
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [toast, setToast] = useState<GalleryToast | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const [trashOpen, setTrashOpen] = useState(false);
  const [trashEntries, setTrashEntries] = useState<TrashEntry[]>([]);
  const [trashLoading, setTrashLoading] = useState(false);
  const [trashError, setTrashError] = useState("");
  const [trashActionId, setTrashActionId] = useState("");
  const [trashConfirmId, setTrashConfirmId] = useState("");
  const trashDialog = useRef<HTMLDivElement>(null);
  const deleteDialog = useRef<HTMLDivElement>(null);
  const deleteCancel = useRef<HTMLButtonElement>(null);
  // Control that opened the confirmation, so Cancel can hand focus back to the row.
  const deleteReturn = useRef<HTMLElement | null>(null);
  // undefined: nothing to do. null: no row left, fall back. string: focus that row's control.
  const pendingRowFocus = useRef<string | null | undefined>(undefined);
  const undoInFlight = useRef(false);
  const toastKey = useRef(0);
  const refreshButton = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (screen !== "Gallery") {
      closePreview();
      setPaletteOpen(false);
      setDeleteTarget(null);
      setDeleteBusy(false);
      setTrashOpen(false);
      setToast(null);
    }
  }, [screen]);

  // Remember the direction per key, then persist the pair whenever either half changes.
  useEffect(() => {
    sortDirections.current[sort.sortBy] = sort.direction;
    writeStoredSort(sort);
  }, [sort]);

  useEffect(() => {
    writeStoredView(galleryView);
  }, [galleryView]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== GALLERY_VIEW_STORAGE_KEY) return;
      setGalleryView(readStoredView());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Adopt a newer choice from another tab. Re-writing the adopted value is a no-op for the
  // other tab (same string, no storage event), so tabs converge without ping-ponging.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== GALLERY_SORT_STORAGE_KEY) return;
      const next = readStoredSort();
      setSort((current) => (sameSort(current, next) ? current : next));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    if (screen !== "Gallery") return;
    const onShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (!paletteOpen && !selected && !deleteTarget) openPalette();
        else if (paletteOpen) paletteInput.current?.focus();
      }
    };
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [screen, paletteOpen, selected, deleteTarget]);

  useEffect(() => {
    if (!paletteOpen) return;
    paletteInput.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [paletteOpen]);

  useEffect(() => {
    if (!paletteOpen || !paletteQuery.trim() || !authReady) {
      setPaletteResults([]);
      setPaletteLoading(false);
      setPaletteError("");
      setPaletteTruncated(false);
      return;
    }
    const controller = new AbortController();
    setPaletteLoading(true);
    setPaletteResults([]);
    setPaletteError("");
    setActiveResult(0);
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const response = await fetch(`/api/v1/gallery/search?q=${encodeURIComponent(paletteQuery.trim())}`, {
            headers: dashboardAuthHeaders(),
            signal: controller.signal,
          });
          if (!response.ok)
            throw new Error(
              response.status === 401
                ? "Coordinator key rejected; reconnect and retry."
                : `Search unavailable (HTTP ${response.status}).`,
            );
          const data = await response.json();
          if (!controller.signal.aborted) {
            setPaletteResults(data.entries ?? []);
            setPaletteTruncated(Boolean(data.truncated));
          }
        } catch (cause) {
          if (!controller.signal.aborted)
            setPaletteError(cause instanceof Error ? cause.message : "Search unavailable.");
        } finally {
          if (!controller.signal.aborted) setPaletteLoading(false);
        }
      })();
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [paletteOpen, paletteQuery, authReady]);

  useEffect(() => {
    if (screen !== "Gallery") return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setEntries([]);
    if (!authReady) {
      setError("Connect the coordinator before browsing the workspace.");
      setLoading(false);
      return;
    }
    void (async () => {
      try {
        const response = await fetch(`/api/v1/gallery/browse?path=${encodeURIComponent(folder)}`, {
          headers: dashboardAuthHeaders(),
          signal: controller.signal,
        });
        if (!response.ok)
          throw new Error(
            response.status === 401
              ? "Coordinator key rejected; reconnect and retry."
              : `Could not open folder (HTTP ${response.status}).`,
          );
        const data = await response.json();
        if (!controller.signal.aborted) setEntries(data.entries ?? []);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not open folder.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [screen, folder, generation, authReady]);

  // Dashboard request: land in the folder that holds one file and open that
  // file's preview. The listing is fetched here because the folder may never
  // have been visited in this session; the match is the same entry the list
  // renders, so the existing dialog, download, and focus trap all apply.
  useEffect(() => {
    if (screen !== "Gallery" || !openFile || !authReady) return;
    const containing = splitWorkspacePath(openFile.path).folder;
    const requestedPath = openFile.path;
    const requestedName = openFile.name || splitWorkspacePath(openFile.path).name;
    // Every request owns a generation: a newer request, an unmount, or leaving
    // the Gallery supersedes it and any late response is discarded.
    openRequestSequence.current += 1;
    const request = openRequestSequence.current;
    const controller = new AbortController();
    const isCurrent = () => request === openRequestSequence.current && !controller.signal.aborted;
    const where = containing || "the workspace root";

    setOpenNotice("");
    void (async () => {
      try {
        const response = await fetch(`/api/v1/gallery/browse?path=${encodeURIComponent(containing)}`, {
          headers: dashboardAuthHeaders(),
          signal: controller.signal,
        });
        if (!response.ok)
          throw new Error(
            response.status === 401
              ? "Coordinator key rejected; reconnect and retry."
              : `Could not open ${where} (HTTP ${response.status}).`,
          );
        const data = await response.json();
        if (!isCurrent()) return;
        const match: WorkspaceEntry | undefined = (data.entries ?? []).find(
          (entry: WorkspaceEntry) => entry.type === "file" && entry.path === requestedPath,
        );
        // Show the containing folder either way (the jump resets the filter, as
        // navigate() does) but never open a folder in the file dialog.
        setSearch("");
        setFolder(containing);
        if (!match) {
          setOpenNotice(`${requestedName} is not in ${where} any more.`);
          return;
        }
        setError("");
        // There is no row behind this jump, so closing must not restore focus.
        previewReturn.current = null;
        clearPreview();
        setSelected(match);
      } catch (cause) {
        if (!isCurrent()) return;
        // A failed lookup keeps the user's current folder; the notice says why.
        setOpenNotice(cause instanceof Error ? cause.message : `Could not open ${requestedName}.`);
      } finally {
        if (isCurrent()) onOpenFileHandled?.(openFile.id);
      }
    })();

    return () => controller.abort();
  }, [screen, openFile, authReady, onOpenFileHandled]);

  useEffect(() => {
    if (screen !== "Gallery" || !selected || selected.preview === "none") return;
    const controller = new AbortController();
    setPreviewLoading(true);
    setPreviewError("");
    if (selected.preview === "text" && (selected.size ?? 0) > 65536) {
      setPreviewError("Text preview is limited to 64 KB. Download this file instead.");
      setPreviewLoading(false);
      return;
    }
    void (async () => {
      try {
        const response = await fetch(fileUrl(selected.path, true), {
          headers: dashboardAuthHeaders(),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Preview unavailable (HTTP ${response.status}).`);
        if (selected.preview === "text") {
          const buffer = await response.arrayBuffer();
          if (buffer.byteLength > 65536) throw new Error("Text preview exceeds 64 KB.");
          const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
          if (!controller.signal.aborted) setPreviewText(text);
        } else {
          const blob = await response.blob();
          if (controller.signal.aborted) return;
          const objectUrl = URL.createObjectURL(blob);
          previewObject.current = objectUrl;
          setPreviewUrl(objectUrl);
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setPreviewError(cause instanceof Error ? cause.message : "Preview unavailable.");
      } finally {
        if (!controller.signal.aborted) setPreviewLoading(false);
      }
    })();
    return () => controller.abort();
  }, [screen, selected]);

  useEffect(() => {
    return () => {
      if (previewObject.current) URL.revokeObjectURL(previewObject.current);
    };
  }, []);

  useEffect(() => {
    if (!selected) return;
    previewClose.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closePreview();
        return;
      }
      // Focus starts inside the dialog; if anything moves it out, pull it back.
      if (event.key === "Tab" && previewDialog.current && !previewDialog.current.contains(document.activeElement)) {
        event.preventDefault();
        const targets = dialogTargets(previewDialog.current);
        (event.shiftKey ? targets[targets.length - 1] : targets[0])?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = oldOverflow;
    };
  }, [selected]);

  // Confirmation dialog: focus starts on Cancel, the safe choice for a destructive action.
  // Kept apart from the key handler so an in-flight request does not move focus off the button.
  useEffect(() => {
    if (deleteTarget) deleteCancel.current?.focus();
  }, [deleteTarget]);

  // Escape cancels, and the page behind a modal dialog does not scroll.
  useEffect(() => {
    if (!deleteTarget) return;
    const onKey = (event: KeyboardEvent) => {
      // A request in flight keeps the dialog open so its result can be reported.
      if (event.key === "Escape" && !deleteBusy) {
        event.preventDefault();
        closeDeleteConfirm();
      }
    };
    window.addEventListener("keydown", onKey);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = oldOverflow;
    };
  }, [deleteTarget, deleteBusy]);

  // The toast expires on its own; undo stays available for that whole window.
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => {
      setToast((current) => (current && current.key === toast.key ? null : current));
    }, TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // After a row leaves, focus the control that took its place rather than dropping to <body>.
  useEffect(() => {
    if (pendingRowFocus.current === undefined) return;
    const wanted = pendingRowFocus.current;
    pendingRowFocus.current = undefined;
    window.requestAnimationFrame(() => {
      const match = Array.from(document.querySelectorAll<HTMLButtonElement>(".gallery-browser-delete")).find(
        (button) => button.dataset.entryPath === wanted,
      );
      (match ?? listRef.current ?? refreshButton.current)?.focus();
    });
  }, [entries]);

  const visible = entries
    .filter((entry) => entry.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
    .sort((a, b) => {
      // Folder grouping is invariant; ties are deterministic in both directions.
      if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
      const value =
        sortBy === "name" || sortBy === "format"
          ? a[sortBy].localeCompare(b[sortBy], undefined, { sensitivity: "base", numeric: true })
          : sortBy === "added"
            ? Date.parse(a.added ?? a.modified) - Date.parse(b.added ?? b.modified)
            : sortBy === "modified"
              ? Date.parse(a.modified) - Date.parse(b.modified)
              : (a.size ?? 0) - (b.size ?? 0);
      return (sortDirection === "asc" ? value : -value) || a.name.localeCompare(b.name) || a.path.localeCompare(b.path);
    });
  const folderParts = folder ? folder.split("/") : [];

  function changeSortBy(next: SortKey) {
    setSort((current) => {
      if (next === current.sortBy) return current;
      // Other keys keep the current direction. Date added opens newest first, unless a direction
      // was already chosen for it (this session, including a save restored on mount) - that wins.
      if (next !== "added") return { sortBy: next, direction: current.direction };
      return { sortBy: next, direction: sortDirections.current.added ?? "desc" };
    });
  }

  function toggleSortDirection() {
    setSort((current) => ({ ...current, direction: current.direction === "asc" ? "desc" : "asc" }));
  }

  function clearPreview() {
    if (previewObject.current) URL.revokeObjectURL(previewObject.current);
    previewObject.current = "";
    setPreviewUrl("");
    setPreviewText("");
    setPreviewError("");
  }

  function closePreview() {
    setSelected(null);
    clearPreview();
    const returnTo = previewReturn.current;
    previewReturn.current = null;
    if (returnTo) window.requestAnimationFrame(() => returnTo.focus());
  }

  function openPalette() {
    // Focus and click can both fire on one activation; never reset an already-open palette.
    if (paletteOpen) {
      paletteInput.current?.focus();
      return;
    }
    focusBeforePalette.current =
      document.activeElement instanceof HTMLElement && document.activeElement !== document.body
        ? document.activeElement
        : searchTrigger.current;
    setPaletteQuery("");
    setPaletteOpen(true);
  }

  function closePalette(returnFocus = true) {
    setPaletteOpen(false);
    if (!returnFocus) return;
    window.requestAnimationFrame(() => {
      const returnTo = focusBeforePalette.current;
      if (!returnTo) return;
      // The trigger opens the palette on focus; skip the one focus event that restores it.
      suppressTriggerOpen.current = returnTo === searchTrigger.current;
      returnTo.focus();
      suppressTriggerOpen.current = false;
    });
  }

  function handleTriggerFocus() {
    if (paletteOpen || selected || suppressTriggerOpen.current) return;
    openPalette();
  }

  function chooseResult(entry: WorkspaceEntry) {
    closePalette(entry.type === "folder");
    if (entry.type === "folder") navigate(entry.path);
    else {
      previewReturn.current = focusBeforePalette.current;
      clearPreview();
      setSelected(entry);
    }
  }

  function navigate(path: string) {
    closePreview();
    setSearch("");
    setOpenNotice("");
    setChecked(new Set());
    checkAnchor.current = null;
    setFolder(path);
  }

  async function download(entry: WorkspaceEntry) {
    try {
      const response = await fetch(fileUrl(entry.path), { headers: dashboardAuthHeaders() });
      if (!response.ok) throw new Error(`Download failed (HTTP ${response.status}).`);
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = entry.name;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } catch (cause) {
      setPreviewError(cause instanceof Error ? cause.message : "Download failed.");
    }
  }

  function showToast(message: string, undo: { trashId: string; entry: WorkspaceEntry } | null) {
    toastKey.current += 1;
    setToast({
      key: toastKey.current,
      message,
      entry: undo?.entry ?? null,
      trashId: undo?.trashId ?? null,
      undoUsed: false,
      error: "",
    });
  }

  /** Ctrl/Cmd or Shift click, and the checkbox, all toggle selection; Shift extends a range. */
  function toggleChecked(entry: WorkspaceEntry, modifiers?: { shiftKey: boolean }) {
    const anchor = checkAnchor.current;
    setChecked((current) => {
      const next = new Set(current);
      const from = anchor ? visible.findIndex((row) => row.path === anchor) : -1;
      const to = visible.findIndex((row) => row.path === entry.path);
      if (modifiers?.shiftKey && from >= 0 && to >= 0) {
        for (const row of visible.slice(Math.min(from, to), Math.max(from, to) + 1)) next.add(row.path);
      } else if (next.has(entry.path)) {
        next.delete(entry.path);
      } else {
        next.add(entry.path);
      }
      return next;
    });
    checkAnchor.current = entry.path;
  }

  function startDrag(event: ReactDragEvent<HTMLElement>, entry: WorkspaceEntry) {
    const paths = dragPaths(entry);
    event.dataTransfer.setData(GALLERY_DRAG_TYPE, JSON.stringify(paths));
    event.dataTransfer.setData("text/plain", paths.join("\n"));
    event.dataTransfer.effectAllowed = "move";
  }

  function dragOverFolder(event: ReactDragEvent<HTMLElement>, entry: WorkspaceEntry) {
    if (!Array.from(event.dataTransfer.types).includes(GALLERY_DRAG_TYPE)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (dropTarget !== entry.path) setDropTarget(entry.path);
  }

  function dropOnFolder(event: ReactDragEvent<HTMLElement>, entry: WorkspaceEntry) {
    if (!Array.from(event.dataTransfer.types).includes(GALLERY_DRAG_TYPE)) return;
    event.preventDefault();
    setDropTarget(null);
    const paths = readDragPaths(event);
    if (paths.length > 0) void runBulkMove(paths, entry.path);
  }

  /** Dragging a checked row moves the whole selection; dragging any other row moves just that row. */
  function dragPaths(entry: WorkspaceEntry): string[] {
    return checked.has(entry.path) ? Array.from(checked) : [entry.path];
  }

  function readDragPaths(event: ReactDragEvent<HTMLElement>): string[] {
    try {
      const parsed: unknown = JSON.parse(event.dataTransfer.getData(GALLERY_DRAG_TYPE) || "[]");
      return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
    } catch {
      return [];
    }
  }

  function removeEntries(paths: string[]) {
    const gone = new Set(paths);
    if (selected && gone.has(selected.path)) closePreview();
    setEntries((current) => current.filter((row) => !gone.has(row.path)));
    setChecked((current) => new Set(Array.from(current).filter((path) => !gone.has(path))));
  }

  function describeCount(count: number): string {
    return `${count} item${count === 1 ? "" : "s"}`;
  }

  async function runBulkDelete(paths: string[]) {
    setBulkBusy(true);
    setBulkError("");
    try {
      const response = await fetch("/api/v1/gallery/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ paths }),
      });
      if (response.status === 401 || response.status === 403) {
        setBulkDeleteOpen(false);
        setError(
          response.status === 401
            ? "Coordinator key rejected; reconnect and retry."
            : "Coordinator refused the request (HTTP 403); reconnect and retry.",
        );
        return;
      }
      if (!response.ok) {
        setBulkError(`Could not move these to the trash (HTTP ${response.status}). Try again.`);
        return;
      }
      const results = ((await response.json()) as { results?: BulkResult[] }).results ?? [];
      const moved = results.filter((row) => row.ok).map((row) => row.path);
      const failed = results.filter((row) => !row.ok);
      removeEntries(moved);
      setBulkDeleteOpen(false);
      if (failed.length > 0) {
        setChecked(new Set(failed.map((row) => row.path)));
        setBulkError(`${describeCount(failed.length)} could not be deleted. ${failed[0].error ?? ""}`.trim());
      }
      if (moved.length > 0) showToast(`${describeCount(moved.length)} moved to trash`, null);
    } catch {
      setBulkError("Could not reach the coordinator. Check the connection, then try again.");
    } finally {
      setBulkBusy(false);
    }
  }

  async function runBulkMove(paths: string[], destination: string) {
    // Dropping a folder onto itself or into its own subtree is refused by the server; drop it here too.
    const safe = paths.filter((path) => path !== destination && !destination.startsWith(`${path}/`));
    if (safe.length === 0) return;
    setBulkBusy(true);
    setBulkError("");
    try {
      const response = await fetch("/api/v1/gallery/bulk-move", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ paths: safe, destination }),
      });
      if (response.status === 401 || response.status === 403) {
        setBulkMoveOpen(false);
        setError(
          response.status === 401
            ? "Coordinator key rejected; reconnect and retry."
            : "Coordinator refused the request (HTTP 403); reconnect and retry.",
        );
        return;
      }
      if (!response.ok) {
        setBulkError(`Could not move these items (HTTP ${response.status}). Try again.`);
        return;
      }
      const results = ((await response.json()) as { results?: BulkResult[] }).results ?? [];
      const moved = results.filter((row) => row.ok).map((row) => row.path);
      const failed = results.filter((row) => !row.ok);
      removeEntries(moved);
      setBulkMoveOpen(false);
      if (failed.length > 0) {
        setChecked(new Set(failed.map((row) => row.path)));
        setBulkError(`${describeCount(failed.length)} could not be moved. ${failed[0].error ?? ""}`.trim());
      }
      if (moved.length > 0) {
        showToast(`${describeCount(moved.length)} moved to ${destination || "the workspace root"}`, null);
      }
    } catch {
      setBulkError("Could not reach the coordinator. Check the connection, then try again.");
    } finally {
      setBulkBusy(false);
    }
  }

  function openDeleteConfirm(entry: WorkspaceEntry, trigger: HTMLElement | null) {
    deleteReturn.current = trigger;
    setDeleteError("");
    setDeleteBusy(false);
    setDeleteTarget(entry);
  }

  function closeDeleteConfirm(returnFocus = true) {
    setDeleteTarget(null);
    setDeleteError("");
    setDeleteBusy(false);
    const returnTo = deleteReturn.current;
    deleteReturn.current = null;
    if (returnFocus && returnTo) window.requestAnimationFrame(() => returnTo.focus());
  }

  /**
   * Drop one entry from the local listing. The visible order is derived from `entries` on every
   * render, so the list stays sorted and no refetch is needed.
   */
  function removeEntry(entry: WorkspaceEntry) {
    if (selected?.path === entry.path) closePreview();
    const order = visible.map((row) => row.path);
    const index = order.indexOf(entry.path);
    pendingRowFocus.current = index < 0 ? null : (order[index + 1] ?? order[index - 1] ?? null);
    // Deeper folders cannot outlive the folder that held them: step up before it disappears.
    if (folder === entry.path || folder.startsWith(`${entry.path}/`)) navigate(parentPath(entry.path));
    setEntries((current) => current.filter((row) => row.path !== entry.path));
  }

  async function confirmDelete() {
    const entry = deleteTarget;
    if (!entry || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError("");
    try {
      const response = await fetch("/api/v1/gallery/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ path: entry.path }),
      });
      // Auth failures take the same path as every other Gallery request: the page alert.
      if (response.status === 401 || response.status === 403) {
        closeDeleteConfirm(false);
        setError(
          response.status === 401
            ? "Coordinator key rejected; reconnect and retry."
            : "Coordinator refused the request (HTTP 403); reconnect and retry.",
        );
        return;
      }
      if (response.status === 404) {
        // Another session removed it first: the row is stale, so drop it and say so.
        removeEntry(entry);
        closeDeleteConfirm(false);
        showToast(`${entry.name} was already gone; the list is up to date.`, null);
        return;
      }
      if (response.status === 409) {
        setDeleteError("The coordinator refused this item. The workspace root and the trash itself cannot be deleted.");
        return;
      }
      if (!response.ok) {
        setDeleteError(`Could not move this to the trash (HTTP ${response.status}). Try again.`);
        return;
      }
      const data = (await response.json()) as Partial<DeleteResult>;
      const name = typeof data.name === "string" && data.name ? data.name : entry.name;
      removeEntry(entry);
      closeDeleteConfirm(false);
      // No trash id means nothing to restore; the notice still says where the item went.
      showToast(
        `${name} moved to trash`,
        typeof data.trash_id === "string" && data.trash_id ? { trashId: data.trash_id, entry } : null,
      );
    } catch {
      // Network failure: nothing moved, the row stays, and the same button retries.
      setDeleteError("Could not reach the coordinator. Check the connection, then try again.");
    } finally {
      setDeleteBusy(false);
    }
  }

  async function loadTrash() {
    setTrashLoading(true);
    setTrashError("");
    try {
      const response = await fetch("/api/v1/gallery/trash", { headers: dashboardAuthHeaders() });
      if (!response.ok) throw new Error(`Could not load recycle bin (HTTP ${response.status}).`);
      const data = (await response.json()) as { entries?: TrashEntry[] };
      setTrashEntries(Array.isArray(data.entries) ? data.entries : []);
    } catch (cause) {
      setTrashError(cause instanceof Error ? cause.message : "Could not load recycle bin.");
    } finally {
      setTrashLoading(false);
    }
  }

  async function openTrash() {
    setTrashOpen(true);
    setTrashConfirmId("");
    await loadTrash();
  }

  async function restoreTrashEntry(entry: TrashEntry) {
    setTrashActionId(entry.trash_id);
    setTrashError("");
    try {
      const response = await fetch("/api/v1/gallery/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ trash_id: entry.trash_id }),
      });
      if (!response.ok)
        throw new Error(
          response.status === 409 ? "The original location is occupied." : `Restore failed (HTTP ${response.status}).`,
        );
      const data = (await response.json()) as { restored_path?: string };
      setTrashEntries((current) => current.filter((item) => item.trash_id !== entry.trash_id));
      setGeneration((value) => value + 1);
      setTrashConfirmId("");
      showToast(`${entry.name} restored to ${data.restored_path || entry.original_path}`, null);
    } catch (cause) {
      setTrashError(cause instanceof Error ? cause.message : "Could not restore this item.");
    } finally {
      setTrashActionId("");
    }
  }

  async function permanentlyDeleteTrashEntry(entry: TrashEntry) {
    if (trashConfirmId !== entry.trash_id) {
      setTrashConfirmId(entry.trash_id);
      return;
    }
    setTrashActionId(entry.trash_id);
    setTrashError("");
    try {
      const response = await fetch("/api/v1/gallery/trash/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ trash_id: entry.trash_id }),
      });
      if (!response.ok) throw new Error(`Permanent delete failed (HTTP ${response.status}).`);
      setTrashEntries((current) => current.filter((item) => item.trash_id !== entry.trash_id));
      setTrashConfirmId("");
    } catch (cause) {
      setTrashError(cause instanceof Error ? cause.message : "Could not permanently delete this item.");
    } finally {
      setTrashActionId("");
    }
  }

  async function undoDelete() {
    const current = toast;
    if (!current?.trashId || !current.entry || undoInFlight.current) return;
    undoInFlight.current = true;
    setUndoBusy(true);
    try {
      const response = await fetch("/api/v1/gallery/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...dashboardAuthHeaders() },
        body: JSON.stringify({ trash_id: current.trashId }),
      });
      if (response.status === 401 || response.status === 403) {
        setToast(null);
        setError(
          response.status === 401
            ? "Coordinator key rejected; reconnect and retry."
            : "Coordinator refused the request (HTTP 403); reconnect and retry.",
        );
        return;
      }
      if (!response.ok) {
        // Single-shot: the button is spent either way, so say why the item stays in the trash.
        setToast((active) =>
          active
            ? {
                ...active,
                undoUsed: true,
                error:
                  response.status === 404
                    ? "That item is no longer in the trash."
                    : response.status === 409
                      ? "Something already exists at that location; the item stays in the trash."
                      : `Restore failed (HTTP ${response.status}).`,
              }
            : active,
        );
        return;
      }
      const data = (await response.json()) as { restored_path?: unknown };
      const restoredPath =
        typeof data.restored_path === "string" && data.restored_path ? data.restored_path : current.entry.path;
      const restored: WorkspaceEntry = { ...current.entry, path: restoredPath };
      // Only the open folder can show the row again; a restore elsewhere is just acknowledged.
      const here = parentPath(restoredPath) === folder;
      if (here) {
        setEntries((rows) => (rows.some((row) => row.path === restored.path) ? rows : [...rows, restored]));
      }
      showToast(
        here
          ? `${restored.name} restored`
          : `${restored.name} restored to ${parentPath(restoredPath) || "the workspace root"}`,
        null,
      );
    } catch {
      setToast((active) =>
        active
          ? { ...active, undoUsed: true, error: "Could not reach the coordinator; the item is still in the trash." }
          : active,
      );
    } finally {
      undoInFlight.current = false;
      setUndoBusy(false);
    }
  }

  return (
    <div className="overview-content">
      {screen === "Workers" && <StudioWorkers view={workerView} onViewChange={onWorkerViewChange} />}
      {screen === "Gallery" && (
        <section className="page gallery-page gallery-browser">
          <header className="page-head gallery-browser-heading">
            <div>
              <h1>
                Gallery<span className="page-period">.</span>
              </h1>
              <p className="overview-subtitle">Browse, search and manage render output in the workspace.</p>
            </div>
            <div className="page-actions gallery-heading-actions">
              {!loading && !error && (
                <p className="gallery-browser-count page-chip" data-testid="gallery-count">
                  {entryCountLabel(entries)}
                </p>
              )}
              <button type="button" className="page-btn is-ghost is-sm gallery-trash-button" onClick={() => void openTrash()}>
                <Trash2 size={14} aria-hidden="true" /> Recycle bin
              </button>
              <button
                className="page-btn is-ghost is-sm studio-refresh-button"
                type="button"
                ref={refreshButton}
                onClick={() => setGeneration((value) => value + 1)}
              >
                <RefreshCw size={14} aria-hidden="true" /> Refresh
              </button>
            </div>
          </header>
          <div className="nr-card gallery-browser-panel">
            <div className="gallery-browser-pathbar">
              <nav className="gallery-browser-crumbs page-kicker" aria-label="Workspace breadcrumb">
                <button type="button" onClick={() => navigate("")} aria-current={!folder ? "page" : undefined}>
                  Workspace
                </button>
                {folderParts.map((part, index) => (
                  <span key={`${index}-${part}`}>
                    <span aria-hidden="true">/</span>
                    <button
                      type="button"
                      aria-current={index === folderParts.length - 1 ? "page" : undefined}
                      onClick={() => navigate(folderParts.slice(0, index + 1).join("/"))}
                    >
                      {part}
                    </button>
                  </span>
                ))}
              </nav>
              {folder && (
                <button
                  type="button"
                  className="page-btn is-ghost is-sm gallery-browser-up"
                  onClick={() => navigate(folderParts.slice(0, -1).join("/"))}
                >
                  <ArrowLeft size={14} aria-hidden="true" /> Back
                </button>
              )}
            </div>
            <div className="page-toolbar gallery-browser-tools">
              <div className="page-field gallery-search-field">
                <span>Search</span>
                <div className="gallery-search-control">
                  <Search size={14} aria-hidden="true" />
                  <input
                    className="page-input gallery-search-input"
                    type="search"
                    aria-label="Filter current folder"
                    placeholder="Search this folder…"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                  <button
                    type="button"
                    className="gallery-search-kbd"
                    ref={searchTrigger}
                    aria-label="Search all workspace files"
                    title="Search all workspace files"
                    onClick={openPalette}
                    onFocus={handleTriggerFocus}
                  >
                    <kbd>{SEARCH_SHORTCUT}</kbd>
                    <Search size={13} className="gallery-search-kbd-icon" aria-hidden="true" />
                  </button>
                </div>
              </div>
              <div className="gallery-sort-group">
                <label className="page-field gallery-sort-control">
                  <span>Sort</span>
                  <span className="gallery-sort-select">
                    <select
                      className="page-select"
                      aria-label="Sort files by"
                      value={sortBy}
                      onChange={(event) => changeSortBy(event.target.value as SortKey)}
                    >
                      <option value="name">Name</option>
                      <option value="modified">Modified</option>
                      <option value="added">Date added</option>
                      <option value="size">Size</option>
                      <option value="format">Format</option>
                    </select>
                    <ChevronDown size={13} aria-hidden="true" />
                  </span>
                </label>
                <button
                  className="page-btn is-ghost gallery-sort-direction"
                  type="button"
                  aria-label={`Sort ${sortDirection === "asc" ? "ascending" : "descending"}; toggle direction`}
                  title={`Sort ${sortDirection === "asc" ? "ascending" : "descending"}; toggle direction`}
                  onClick={toggleSortDirection}
                >
                  {sortDirection === "asc" ? <ArrowUp size={14} aria-hidden="true" /> : <ArrowDown size={14} aria-hidden="true" />}
                </button>
              </div>
              <div className="page-segment gallery-view-toggle" role="group" aria-label="Gallery layout">
                <button
                  type="button"
                  className="gallery-view-option"
                  aria-pressed={galleryView === "list"}
                  aria-label="List view"
                  title="List view"
                  onClick={() => setGalleryView("list")}
                >
                  <List size={14} aria-hidden="true" /> List
                </button>
                <button
                  type="button"
                  className="gallery-view-option"
                  aria-pressed={galleryView === "grid"}
                  aria-label="Icon grid view"
                  title="Icon grid view"
                  onClick={() => setGalleryView("grid")}
                >
                  <LayoutGrid size={14} aria-hidden="true" /> Grid
                </button>
              </div>
            </div>
          </div>
          <div className="nr-card gallery-browser-results">
            {checked.size > 0 && (
              <div className="gallery-bulk-bar" role="toolbar" aria-label="Selected items">
                <span className="gallery-bulk-count" aria-live="polite">
                  {checked.size} selected
                </span>
                <button type="button" className="gallery-bulk-button" onClick={() => setChecked(new Set(visible.map((row) => row.path)))} disabled={bulkBusy}>
                  Select all
                </button>
                <button type="button" className="gallery-bulk-button" onClick={() => setBulkMoveOpen(true)} disabled={bulkBusy}>
                  Move to…
                </button>
                <button type="button" className="gallery-bulk-button is-danger" onClick={() => setBulkDeleteOpen(true)} disabled={bulkBusy}>
                  Delete
                </button>
                <button type="button" className="gallery-bulk-button is-ghost" onClick={() => setChecked(new Set())} disabled={bulkBusy}>
                  Clear
                </button>
                {bulkError && (
                  <span className="gallery-bulk-error" role="alert">
                    {bulkError}
                  </span>
                )}
              </div>
            )}
            {openNotice && (
              <p className="studio-error page-note is-bad" role="alert">
                {openNotice}
              </p>
            )}
            {error && (
              <p className="studio-error page-note is-bad" role="alert">
                {error}
              </p>
            )}
            {loading ? (
              <div className="gallery-browser-state nr-empty" role="status">
                <RefreshCw size={17} className="gallery-browser-spinner" aria-hidden="true" />
                <strong>Loading folder…</strong>
              </div>
            ) : !error && visible.length === 0 ? (
              <div className="gallery-browser-state nr-empty">
                <Folder size={17} aria-hidden="true" />
                <strong>{search ? "No matching files or folders." : "This folder is empty."}</strong>
              </div>
            ) : visible.length === 0 ? null : galleryView === "grid" ? (
              <div
                className="gallery-browser-grid"
                role="group"
                aria-label="Files and folders"
                ref={listRef}
                tabIndex={-1}
              >
                {visible.map((entry) => (
                  <div
                    className={`gallery-browser-tilewrap${dropTarget === entry.path ? " is-drop-target" : ""}${checked.has(entry.path) ? " is-checked" : ""}`}
                    key={entry.path}
                    onDragOver={entry.type === "folder" ? (event) => dragOverFolder(event, entry) : undefined}
                    onDragLeave={entry.type === "folder" ? () => setDropTarget(null) : undefined}
                    onDrop={entry.type === "folder" ? (event) => dropOnFolder(event, entry) : undefined}
                  >
                    <input
                      type="checkbox"
                      className="gallery-browser-check"
                      checked={checked.has(entry.path)}
                      aria-label={`Select ${entry.name}`}
                      onChange={() => undefined}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleChecked(entry, event);
                      }}
                    />
                    <button
                      className="gallery-browser-tile"
                      type="button"
                      title={entry.name}
                      draggable
                      onDragStart={(event) => startDrag(event, entry)}
                      onClick={(event) => {
                        if (event.ctrlKey || event.metaKey || event.shiftKey) {
                          toggleChecked(entry, event);
                          return;
                        }
                        if (entry.type === "folder") navigate(entry.path);
                        else {
                          previewReturn.current = event.currentTarget;
                          clearPreview();
                          setSelected(entry);
                        }
                      }}
                    >
                      <span className="gallery-browser-tile-icon">
                        <EntryIcon entry={entry} size={22} />
                      </span>
                      <span className="gallery-browser-tile-name">{entry.name}</span>
                      <span className="gallery-browser-tile-meta">
                        <span>{entry.format}</span>
                        <span>{sizeLabel(entry.size)}</span>
                      </span>
                    </button>
                    <button
                      className="gallery-browser-delete"
                      type="button"
                      data-entry-path={entry.path}
                      aria-label={`Delete ${entry.name}`}
                      title={`Delete ${entry.name}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        openDeleteConfirm(entry, event.currentTarget);
                      }}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div
                className="gallery-browser-list page-table"
                role="group"
                aria-label="Files and folders"
                ref={listRef}
                tabIndex={-1}
              >
                <div className="gallery-browser-thead page-table-head" aria-hidden="true">
                  <span />
                  <span>Name</span>
                  <span>Format</span>
                  <span>Size</span>
                  <span>Modified</span>
                </div>
                {visible.map((entry) => (
                  // The delete control is a sibling, not a child: a button inside a button is invalid
                  // markup, and this way the row keeps opening the item on its own.
                  <div
                    className={`gallery-browser-rowwrap${dropTarget === entry.path ? " is-drop-target" : ""}${checked.has(entry.path) ? " is-checked" : ""}`}
                    key={entry.path}
                    onDragOver={entry.type === "folder" ? (event) => dragOverFolder(event, entry) : undefined}
                    onDragLeave={entry.type === "folder" ? () => setDropTarget(null) : undefined}
                    onDrop={entry.type === "folder" ? (event) => dropOnFolder(event, entry) : undefined}
                  >
                    <input
                      type="checkbox"
                      className="gallery-browser-check"
                      checked={checked.has(entry.path)}
                      aria-label={`Select ${entry.name}`}
                      onChange={() => undefined}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleChecked(entry, event);
                      }}
                    />
                    <button
                      className="gallery-browser-row page-table-row"
                      type="button"
                      draggable
                      onDragStart={(event) => startDrag(event, entry)}
                      onClick={(event) => {
                        if (event.ctrlKey || event.metaKey || event.shiftKey) {
                          toggleChecked(entry, event);
                          return;
                        }
                        if (entry.type === "folder") navigate(entry.path);
                        else {
                          previewReturn.current = event.currentTarget;
                          clearPreview();
                          setSelected(entry);
                        }
                      }}
                    >
                      <span className="gallery-browser-icon">
                        <EntryIcon entry={entry} />
                      </span>
                      <span className="gallery-browser-name page-table-cell-strong" title={entry.name}>
                        {entry.name}
                      </span>
                      <span className="gallery-browser-format">{entry.format}</span>
                      <span className="gallery-browser-size page-table-cell-muted">{sizeLabel(entry.size)}</span>
                      <time
                        className="gallery-browser-date page-table-cell-muted"
                        dateTime={entryTimestamp(entry, sortBy)}
                        title={sortBy === "added" ? "Date added" : "Last modified"}
                      >
                        {new Date(entryTimestamp(entry, sortBy)).toLocaleString()}
                      </time>
                    </button>
                    <button
                      className="gallery-browser-delete"
                      type="button"
                      data-entry-path={entry.path}
                      aria-label={`Delete ${entry.name}`}
                      title={`Delete ${entry.name}`}
                      onClick={(event) => {
                        // Required even as a sibling: deleting must never also open the item.
                        event.stopPropagation();
                        openDeleteConfirm(entry, event.currentTarget);
                      }}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      )}
      {screen === "Gallery" && paletteOpen && (
        <div
          className="gallery-browser-overlay"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closePalette();
          }}
        >
          <div
            className="gallery-search-dialog"
            ref={paletteDialog}
            role="dialog"
            aria-modal="true"
            aria-label="Search workspace"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                closePalette();
              }
              if (event.key === "ArrowDown" && paletteResults.length) {
                event.preventDefault();
                setActiveResult((index) => (index + 1) % paletteResults.length);
                paletteInput.current?.focus();
              }
              if (event.key === "ArrowUp" && paletteResults.length) {
                event.preventDefault();
                setActiveResult((index) => (index - 1 + paletteResults.length) % paletteResults.length);
                paletteInput.current?.focus();
              }
              if (
                event.key === "Enter" &&
                paletteResults[activeResult] &&
                document.activeElement === paletteInput.current
              ) {
                event.preventDefault();
                chooseResult(paletteResults[activeResult]);
              }
              if (event.key === "Tab") trapTab(event, paletteDialog.current);
            }}
          >
            <header>
              <Search size={19} aria-hidden="true" />
              <input
                ref={paletteInput}
                type="search"
                role="combobox"
                aria-autocomplete="list"
                aria-expanded="true"
                aria-label="Search files and folders in workspace"
                placeholder="Search all workspace folders…"
                maxLength={100}
                value={paletteQuery}
                onChange={(event) => setPaletteQuery(event.target.value)}
                aria-controls="gallery-search-results"
                aria-activedescendant={paletteResults.length ? `gallery-search-result-${activeResult}` : undefined}
              />
              <button type="button" aria-label="Close search" onClick={() => closePalette()}>
                <X size={18} />
              </button>
            </header>
            <p className="gallery-search-summary" role="status" aria-live="polite">
              {!authReady
                ? "Connect the coordinator to search."
                : !paletteQuery.trim()
                  ? "Type a file or folder name to search the workspace."
                  : paletteLoading
                    ? "Searching workspace…"
                    : paletteError ||
                      `${paletteResults.length} result${paletteResults.length === 1 ? "" : "s"}${paletteTruncated ? " (more matches may exist; refine your search)" : ""}`}
            </p>
            <div
              className="gallery-search-results"
              id="gallery-search-results"
              role="listbox"
              aria-label="Workspace search results"
            >
              {!paletteLoading && paletteQuery.trim() && !paletteError && !paletteResults.length && (
                <p>No matching files or folders.</p>
              )}
              {paletteResults.map((entry, index) => (
                <button
                  id={`gallery-search-result-${index}`}
                  role="option"
                  aria-selected={index === activeResult}
                  className="gallery-search-result"
                  key={entry.path}
                  type="button"
                  onMouseEnter={() => setActiveResult(index)}
                  onClick={() => chooseResult(entry)}
                >
                  {entry.type === "folder" ? <Folder size={18} /> : <File size={18} />}
                  <span>
                    <strong>{entry.name}</strong>
                    <small>{entry.path}</small>
                  </span>
                  <em>{entry.format}</em>
                </button>
              ))}
            </div>
            <footer>
              ↑ ↓ Select <span>Enter Open</span> <span>Esc Close</span>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && selected && (
        <div className="gallery-browser-overlay" onClick={closePreview}>
          <div
            className="gallery-browser-dialog"
            ref={previewDialog}
            role="dialog"
            aria-modal="true"
            aria-label={`${selected.format} file: ${selected.name}`}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === "Tab") trapTab(event, previewDialog.current);
            }}
          >
            <header>
              <div>
                <span className="panel-kicker">{selected.format} FILE</span>
                <h2>{selected.name}</h2>
              </div>
              <button type="button" ref={previewClose} onClick={closePreview} aria-label="Close preview">
                <X size={20} />
              </button>
            </header>
            <div className="gallery-browser-preview">
              {previewLoading && <p role="status">Loading preview…</p>}
              {previewError && <p role="alert">{previewError}</p>}
              {selected.preview === "none" && (
                <p>Preview unavailable for this file format. Download to open it locally.</p>
              )}
              {!previewLoading && previewUrl && selected.preview === "image" && (
                <img src={previewUrl} alt={selected.name} />
              )}
              {!previewLoading && previewUrl && selected.preview === "video" && (
                // biome-ignore lint/a11y/useMediaCaption: workspace videos do not have associated captions.
                <video src={previewUrl} controls preload="metadata" />
              )}
              {!previewLoading && !previewError && selected.preview === "text" && <pre>{previewText}</pre>}
            </div>
            <footer>
              <span>
                {sizeLabel(selected.size)} · Modified {new Date(selected.modified).toLocaleString()}
              </span>
              <button type="button" className="connect-button" onClick={() => void download(selected)}>
                <Download size={16} /> Download
              </button>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && trashOpen && (
        <div
          className="gallery-browser-overlay"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !trashActionId) setTrashOpen(false);
          }}
        >
          <div
            className="gallery-confirm-dialog gallery-trash-dialog"
            ref={trashDialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="gallery-trash-title"
            onKeyDown={(event) => {
              if (event.key === "Tab") trapTab(event, trashDialog.current);
              if (event.key === "Escape" && !trashActionId) setTrashOpen(false);
            }}
          >
            <header className="gallery-trash-heading">
              <div>
                <span className="panel-kicker">GALLERY / RECOVER OR REMOVE</span>
                <h2 id="gallery-trash-title">Recycle bin</h2>
                <p>Restore items to their original location, or permanently delete them.</p>
              </div>
              <button
                type="button"
                className="gallery-trash-close"
                aria-label="Close recycle bin"
                onClick={() => setTrashOpen(false)}
              >
                <X size={17} aria-hidden="true" />
              </button>
            </header>
            {trashError && (
              <p className="studio-error" role="alert">
                {trashError}
              </p>
            )}
            {trashLoading ? (
              <p className="gallery-browser-state" role="status">
                Loading recycle bin…
              </p>
            ) : trashEntries.length === 0 ? (
              <p className="gallery-browser-state">Recycle bin is empty.</p>
            ) : (
              <div className="gallery-trash-list">
                {trashEntries.map((entry) => (
                  <article className="gallery-trash-row" key={entry.trash_id}>
                    <div className="gallery-trash-info">
                      <strong>{entry.name}</strong>
                      <span>
                        {entry.original_path} · {entry.type} · Deleted {new Date(entry.deleted_at).toLocaleString()}
                      </span>
                    </div>
                    <div className="gallery-trash-actions">
                      <button
                        type="button"
                        className="gallery-trash-restore"
                        onClick={() => void restoreTrashEntry(entry)}
                        disabled={Boolean(trashActionId)}
                      >
                        <RotateCcw size={14} aria-hidden="true" />{" "}
                        {trashActionId === entry.trash_id ? "Working…" : "Restore"}
                      </button>
                      <button
                        type="button"
                        className="gallery-trash-delete"
                        onClick={() => void permanentlyDeleteTrashEntry(entry)}
                        disabled={Boolean(trashActionId)}
                        aria-label={`${trashConfirmId === entry.trash_id ? "Confirm permanent deletion of" : "Permanently delete"} ${entry.name}`}
                      >
                        <Trash2 size={14} aria-hidden="true" />
                        {trashActionId === entry.trash_id
                          ? "Deleting…"
                          : trashConfirmId === entry.trash_id
                            ? "Confirm delete"
                            : "Delete forever"}
                      </button>
                      {trashConfirmId === entry.trash_id && (
                        <span className="gallery-trash-warning">This cannot be undone.</span>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            )}
            <footer className="gallery-trash-footer">
              <span>
                {trashEntries.length} {trashEntries.length === 1 ? "item" : "items"}
              </span>
              <button
                type="button"
                className="gallery-confirm-cancel"
                onClick={() => setTrashOpen(false)}
                disabled={Boolean(trashActionId)}
              >
                Close
              </button>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && bulkDeleteOpen && (
        <div
          className="gallery-browser-overlay"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !bulkBusy) setBulkDeleteOpen(false);
          }}
        >
          <div className="gallery-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="gallery-bulk-delete-title">
            <div>
              <span className="panel-kicker">MOVE TO TRASH</span>
              <h2 id="gallery-bulk-delete-title">Delete {describeCount(checked.size)}?</h2>
            </div>
            <p>
              Everything selected moves to the workspace trash. Nothing is erased. You can restore items from the Recycle bin until
              the trash is emptied.
            </p>
            <footer>
              <button type="button" className="gallery-confirm-cancel" onClick={() => setBulkDeleteOpen(false)} disabled={bulkBusy}>
                Cancel
              </button>
              <button
                type="button"
                className="gallery-confirm-delete"
                onClick={() => void runBulkDelete(Array.from(checked))}
                disabled={bulkBusy}
                aria-busy={bulkBusy}
              >
                {bulkBusy ? "Moving…" : "Move to trash"}
              </button>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && bulkMoveOpen && (
        <div
          className="gallery-browser-overlay"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !bulkBusy) setBulkMoveOpen(false);
          }}
        >
          <div className="gallery-confirm-dialog gallery-move-dialog" role="dialog" aria-modal="true" aria-labelledby="gallery-bulk-move-title">
            <div>
              <span className="panel-kicker">MOVE</span>
              <h2 id="gallery-bulk-move-title">Move {describeCount(checked.size)} to…</h2>
            </div>
            <div className="gallery-move-options" role="group" aria-label="Destination folders">
              {folder !== "" && (
                <button type="button" className="gallery-move-option" onClick={() => void runBulkMove(Array.from(checked), parentPath(folder))} disabled={bulkBusy}>
                  ↑ Up one level
                </button>
              )}
              <button type="button" className="gallery-move-option" onClick={() => void runBulkMove(Array.from(checked), "")} disabled={bulkBusy}>
                Workspace root
              </button>
              {entries
                .filter((row) => row.type === "folder" && !checked.has(row.path))
                .map((row) => (
                  <button
                    key={row.path}
                    type="button"
                    className="gallery-move-option"
                    onClick={() => void runBulkMove(Array.from(checked), row.path)}
                    disabled={bulkBusy}
                  >
                    {row.name}
                  </button>
                ))}
            </div>
            {bulkError && (
              <p className="studio-error" role="alert">
                {bulkError}
              </p>
            )}
            <footer>
              <button type="button" className="gallery-confirm-cancel" onClick={() => setBulkMoveOpen(false)} disabled={bulkBusy}>
                Cancel
              </button>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && deleteTarget && (
        <div
          className="gallery-browser-overlay"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !deleteBusy) closeDeleteConfirm();
          }}
        >
          <div
            className="gallery-confirm-dialog"
            ref={deleteDialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="gallery-delete-title"
            aria-describedby="gallery-delete-body"
            onKeyDown={(event) => {
              if (event.key === "Tab") trapTab(event, deleteDialog.current);
            }}
          >
            <div>
              <span className="panel-kicker">MOVE TO TRASH</span>
              <h2 id="gallery-delete-title">Delete {deleteTarget.name}?</h2>
            </div>
            <p id="gallery-delete-body">
              {deleteTarget.type === "folder"
                ? "The folder and everything inside it moves to the workspace trash. Nothing is erased, and you can undo this straight away."
                : "The file moves to the workspace trash. Nothing is erased, and you can undo this straight away."}
            </p>
            {deleteError && (
              <p className="studio-error" role="alert">
                {deleteError}
              </p>
            )}
            <footer>
              <button
                type="button"
                className="gallery-confirm-cancel"
                ref={deleteCancel}
                onClick={() => closeDeleteConfirm()}
                disabled={deleteBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="gallery-confirm-delete"
                onClick={() => void confirmDelete()}
                disabled={deleteBusy}
                aria-busy={deleteBusy}
              >
                {deleteBusy ? "Moving…" : "Move to trash"}
              </button>
            </footer>
          </div>
        </div>
      )}
      {screen === "Gallery" && toast && (
        <div className="gallery-toast" role="status" aria-live="polite">
          <span className="gallery-toast-text">
            {toast.message}
            {toast.error && <span className="gallery-toast-error">{toast.error}</span>}
          </span>
          {toast.entry && toast.trashId && (
            <button
              type="button"
              className="gallery-toast-undo"
              onClick={() => void undoDelete()}
              disabled={undoBusy || toast.undoUsed}
            >
              {undoBusy ? "Restoring…" : "Undo"}
            </button>
          )}
          <button type="button" className="gallery-toast-close" aria-label="Dismiss" onClick={() => setToast(null)}>
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  );
}