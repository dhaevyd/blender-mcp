import { ArrowRight, Box, Copy, Download, Folder, Network, RefreshCw, Server, Tag, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { dashboardAuthHeaders, getDashboardKey } from "../api/dashboardAuth";

type WorkerView = "roster" | "onboarding";

type Worker = {
  worker_id: string;
  name?: string;
  url?: string;
  workspace?: string;
  enabled?: boolean;
  online?: boolean;
  status?: string;
  identity?: {
    hostname?: string;
    image_tag?: string | null;
    workspace_writable?: boolean;
    blender_version?: string | null;
    gpu_devices?: string[];
    gpu_stats?: Array<{ name?: string; memory_total_mib?: number | null; memory_free_mib?: number | null }>;
    platform?: string;
  } | null;
};
type Enrollment = {
  worker: Worker;
  worker_env: string;
  compose_yaml?: string;
  compose_variants?: Record<string, string>;
  setup_script?: string;
  gpu_vendor?: "auto" | "nvidia" | "amd" | "intel";
};

const NEW_WORKER_HIGHLIGHT_MS = 1600;
const CARD_EXIT_MS = 180;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Effective fleet state: an explicit pending/disabled status outranks liveness. */
function workerStatus(worker: Worker): "pending" | "disabled" | "online" | "offline" {
  if (worker.status === "pending" || worker.status === "disabled") return worker.status;
  return worker.online ? "online" : "offline";
}

function gb(mib: number): string {
  return (mib / 1024).toFixed(1);
}

async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...dashboardAuthHeaders(),
      ...init?.headers,
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error ?? `Request failed (HTTP ${response.status}).`);
  return data;
}

export default function StudioWorkers({
  view = "roster",
  onViewChange,
}: {
  view?: WorkerView;
  onViewChange?: (view: WorkerView) => void;
}) {
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [mode, setMode] = useState<"compose" | "existing">("compose");
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("/workspace");
  const [gpuVendor, setGpuVendor] = useState<"auto" | "nvidia" | "amd" | "intel">("auto");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [editId, setEditId] = useState("");
  const [edit, setEdit] = useState({ name: "", workspace: "" });
  const [newWorkerId, setNewWorkerId] = useState("");
  const [setupBannerDismissed, setSetupBannerDismissed] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Worker | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteShake, setDeleteShake] = useState(0);
  const [deleteNameCopied, setDeleteNameCopied] = useState(false);
  const [removingId, setRemovingId] = useState("");
  // The page is normally controlled by the dashboard; keep a local fallback so
  // the segment still switches when mounted without an onViewChange handler.
  const [fallbackView, setFallbackView] = useState<WorkerView>("roster");
  const activeView: WorkerView = onViewChange ? view : fallbackView;
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const scrolledNewWorker = useRef("");
  const deleteTrigger = useRef<HTMLButtonElement | null>(null);
  const deleteInput = useRef<HTMLInputElement | null>(null);

  function switchView(next: WorkerView) {
    setFallbackView(next);
    onViewChange?.(next);
  }

  async function refresh() {
    if (!getDashboardKey()) {
      setLoading(false);
      setError("Connect the coordinator before managing workers.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await request("/api/v1/workers");
      setWorkers(data.workers ?? []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load workers.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void refresh();
  }, []);

  function closeEnrollment() {
    setEnrollment(null);
    setRevealed(false);
  }

  async function enroll(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const data = await request("/api/v1/workers/enroll", {
        method: "POST",
        body: JSON.stringify({ mode, url, name, workspace, gpu_vendor: gpuVendor }),
      });
      setEnrollment(data);
      setRevealed(false);
      setUrl("");
      setName("");
      setNewWorkerId(data.worker?.worker_id ?? "");
      scrolledNewWorker.current = "";
      setSetupBannerDismissed(false);
      setNotice(
        "Worker enrolled as pending. Apply the configuration and restart the worker, then test the connection here.",
      );
      await refresh();
      switchView("roster");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Enrollment failed.");
    } finally {
      setBusy(false);
    }
  }
  async function test(worker: Worker) {
    setBusy(true);
    setTesting(worker.worker_id);
    setError("");
    setNotice("");
    try {
      const data = await request(`/api/v1/workers/${encodeURIComponent(worker.worker_id)}/test`, { method: "POST" });
      if (enrollment?.worker.worker_id === worker.worker_id) closeEnrollment();
      if (data.worker)
        setWorkers((current) => current.map((node) => (node.worker_id === worker.worker_id ? data.worker : node)));
      setNotice(`${worker.name || worker.worker_id} is connected.`);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Connection test failed.");
    } finally {
      setTesting("");
      setBusy(false);
    }
  }
  async function saveEdit(worker: Worker) {
    setBusy(true);
    setError("");
    try {
      await request(`/api/v1/workers/${encodeURIComponent(worker.worker_id)}`, {
        method: "PATCH",
        body: JSON.stringify(edit),
      });
      setEditId("");
      setNotice("Worker settings saved. Workspace changes may require a connection re-test.");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save worker settings.");
    } finally {
      setBusy(false);
    }
  }
  async function remove(worker: Worker) {
    setBusy(true);
    setError("");
    try {
      await request(`/api/v1/workers/${encodeURIComponent(worker.worker_id)}`, { method: "DELETE" });
      if (enrollment?.worker.worker_id === worker.worker_id) closeEnrollment();
      setDeleteTarget(null);
      setDeleteConfirmation("");
      setRemovingId(worker.worker_id);
      await new Promise((resolve) => window.setTimeout(resolve, prefersReducedMotion() ? 0 : CARD_EXIT_MS));
      setNotice("Worker removed.");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not remove worker.");
    } finally {
      setRemovingId("");
      setBusy(false);
    }
  }
  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setNotice("Copied to clipboard.");
    } catch {
      setRevealed(true);
      setError("Clipboard access failed. Select and copy the displayed code instead.");
    }
  }
  function download(filename: string, value: string) {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([value], { type: "text/plain" }));
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  const deleteExpected = deleteTarget
    ? deleteTarget.name || deleteTarget.identity?.hostname || deleteTarget.worker_id
    : "";
  const deleteConfirmed = deleteTarget !== null && deleteConfirmation === deleteExpected;
  const deleteShakeClass = deleteShake === 0 ? "" : deleteShake % 2 === 1 ? "is-shaking" : "is-shaking-alt";

  function openDelete(worker: Worker, trigger: HTMLButtonElement) {
    deleteTrigger.current = trigger;
    setDeleteTarget(worker);
    setDeleteConfirmation("");
    setDeleteShake(0);
    setDeleteNameCopied(false);
    setError("");
  }
  function closeDelete() {
    setDeleteTarget(null);
    setDeleteConfirmation("");
    setDeleteShake(0);
    setDeleteNameCopied(false);
  }
  function shakeDeleteInput() {
    setDeleteShake((value) => value + 1);
  }
  function confirmDelete() {
    if (!deleteTarget || busy) return;
    if (deleteConfirmation !== deleteExpected) {
      shakeDeleteInput();
      return;
    }
    void remove(deleteTarget);
  }
  async function copyDeleteName() {
    if (!deleteExpected) return;
    try {
      await navigator.clipboard.writeText(deleteExpected);
      setDeleteNameCopied(true);
    } catch {
      setError("Clipboard access failed. Type the worker name instead.");
    }
  }

  // Escape closes the dialog; focus returns to the button that opened it.
  useEffect(() => {
    if (!deleteTarget) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    deleteInput.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setDeleteTarget(null);
        setDeleteConfirmation("");
        setDeleteShake(0);
        setDeleteNameCopied(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      // Deferred so the browser's own focus reset (mousedown/Escape) cannot win.
      window.setTimeout(() => deleteTrigger.current?.focus(), 0);
    };
  }, [deleteTarget]);

  // Highlight the freshly enrolled card, then let the ring fade on its own.
  useEffect(() => {
    if (!newWorkerId) return;
    if (activeView === "roster" && scrolledNewWorker.current !== newWorkerId) {
      const node = cardRefs.current.get(newWorkerId);
      if (node && !prefersReducedMotion()) {
        node.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      scrolledNewWorker.current = newWorkerId;
    }
    const timer = window.setTimeout(() => setNewWorkerId(""), NEW_WORKER_HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [newWorkerId, activeView, workers.length]);

  const composeFiles = enrollment?.compose_variants
    ? Object.entries(enrollment.compose_variants).map(([name, content]) => ({ name, content }))
    : enrollment?.compose_yaml
      ? [{ name: "compose.yaml", content: enrollment.compose_yaml }]
      : [];
  const configFiles = enrollment
    ? [
        ...composeFiles,
        ...(enrollment.setup_script ? [{ name: "start-worker.sh", content: enrollment.setup_script }] : []),
        {
          name: enrollment.compose_yaml || enrollment.compose_variants ? ".env" : "worker.env",
          content: enrollment.worker_env,
        },
      ]
    : [];

  // Fleet telemetry for the summary strip — derived from the roster payload.
  const onlineCount = workers.filter((worker) => workerStatus(worker) === "online").length;
  const pendingCount = workers.filter((worker) => worker.status === "pending").length;
  const offlineCount = workers.length - onlineCount;
  const gpuDevices = workers.reduce((total, worker) => total + (worker.identity?.gpu_stats?.length ?? 0), 0);
  const vram = workers.reduce(
    (total, worker) => {
      for (const gpu of worker.identity?.gpu_stats ?? []) {
        const gpuTotal = gpu.memory_total_mib;
        const free = gpu.memory_free_mib;
        if (typeof gpuTotal === "number" && gpuTotal > 0) {
          total.total += gpuTotal;
          if (typeof free === "number") total.free += Math.max(0, Math.min(free, gpuTotal));
        }
      }
      return total;
    },
    { total: 0, free: 0 },
  );
  const vramUsed = vram.total - vram.free;
  const vramUsedPct = vram.total > 0 ? Math.round((100 * vramUsed) / vram.total) : 0;

  return (
    <div className="page studio-workers worker-connect-form-wrap" data-worker-view={activeView}>
      <div className="page-head">
        <div>
          <h1>
            Workers<span className="page-period">.</span>
          </h1>
          <p>Enrolled render nodes, live GPU telemetry and onboarding.</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="page-btn"
            onClick={() => void refresh()}
            disabled={busy || loading}
            aria-label="Refresh workers"
          >
            <RefreshCw size={14} aria-hidden="true" /> Refresh
          </button>
          <button
            type="button"
            className="page-btn is-primary"
            aria-pressed={activeView === "onboarding"}
            onClick={() => switchView(activeView === "onboarding" ? "roster" : "onboarding")}
          >
            {activeView === "onboarding" ? "Back to roster" : "Add worker"}
          </button>
        </div>
      </div>

      <div className="page-stats" role="group" aria-label="Fleet summary">
        <div className="page-stat">
          <div className="page-stat-label">Online</div>
          <div className="page-stat-value">
            {onlineCount}
            <small>of {workers.length}</small>
          </div>
          <div className="page-stat-foot">
            {pendingCount
              ? `${pendingCount} pending enrolment`
              : workers.length
                ? "Coordinator reachable"
                : "No nodes enrolled"}
          </div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">GPU devices</div>
          <div className="page-stat-value">
            {gpuDevices}
            <small>{gpuDevices === 1 ? "device" : "devices"}</small>
          </div>
          <div className="page-stat-foot">Reported by enrolled nodes</div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">VRAM in use</div>
          <div className="page-stat-value">
            {vram.total > 0 ? gb(vramUsed) : "—"}
            <small>{vram.total > 0 ? `of ${gb(vram.total)} GB` : "no telemetry"}</small>
          </div>
          <div className="page-stat-foot">
            {vram.total > 0 ? `${vramUsedPct}% allocated` : "Awaiting GPU telemetry"}
          </div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">Offline</div>
          <div className="page-stat-value">
            {offlineCount}
            <small>{offlineCount === 1 ? "node" : "nodes"}</small>
          </div>
          <div className="page-stat-foot">
            {offlineCount ? "Needs attention" : workers.length ? "Every node online" : "No nodes enrolled"}
          </div>
        </div>
      </div>

      {error && !deleteTarget && (
        <p className="studio-error page-note is-bad worker-alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="page-note worker-notice" role="status" aria-live="polite">
          {notice}
        </p>
      )}

      {activeView === "onboarding" && (
        <div className="page-grid-2">
          <section className="nr-card worker-add-card">
            <div className="nr-card-head">
              <div>
                <h2>Add a render worker</h2>
                <p>Configure a worker node, then verify its coordinator connection.</p>
              </div>
            </div>
            <ol className="worker-enroll-steps" aria-label="Enrollment steps">
              <li aria-current={enrollment ? undefined : "step"}>1. Worker details</li>
              <li aria-current={enrollment ? "step" : undefined}>2. Configure &amp; connect</li>
            </ol>
            {!enrollment && (
              <form className="studio-form worker-connect-form" onSubmit={enroll}>
                <fieldset className="worker-setup-options" disabled={busy}>
                  <legend>Worker setup</legend>
                  <label>
                    <input
                      type="radio"
                      name="worker-mode"
                      checked={mode === "compose"}
                      onChange={() => setMode("compose")}
                    />
                    <Box size={17} aria-hidden="true" />
                    <span>
                      <strong>Docker Compose</strong>
                      <small>Generate Compose and environment files</small>
                    </span>
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="worker-mode"
                      checked={mode === "existing"}
                      onChange={() => setMode("existing")}
                    />
                    <Server size={17} aria-hidden="true" />
                    <span>
                      <strong>Existing worker</strong>
                      <small>Use your deployment or Kubernetes</small>
                    </span>
                  </label>
                </fieldset>
                {mode === "compose" && (
                  <label className="page-field worker-gpu-vendor-field">
                    <span>GPU vendor</span>
                    <select
                      className="page-select"
                      value={gpuVendor}
                      onChange={(event) => setGpuVendor(event.target.value as typeof gpuVendor)}
                    >
                      <option value="auto">Auto-detect on worker host</option>
                      <option value="nvidia">NVIDIA</option>
                      <option value="amd">AMD / Radeon</option>
                      <option value="intel">Intel integrated / Arc</option>
                    </select>
                    <small className="page-muted">
                      Auto-detect creates one compose.yaml with NVIDIA, AMD, and Intel profiles. A Linux setup script
                      checks the host GPU and starts the matching profile.
                    </small>
                  </label>
                )}
                <label className="page-field worker-endpoint-field">
                  <span>
                    <Network size={13} aria-hidden="true" /> Private IP or MCP URL
                  </span>
                  <input
                    className="page-input"
                    required
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder="100.64.0.10"
                    aria-describedby="worker-url-help"
                    spellCheck={false}
                  />
                  <small id="worker-url-help" className="page-muted">
                    {mode === "compose"
                      ? "Use a private or Tailscale IP. Compose uses HTTP on port 10849."
                      : "Use a private or Tailscale IP, or a full HTTP(S) MCP URL with that IP."}
                  </small>
                </label>
                <label className="page-field">
                  <span>
                    <Tag size={13} aria-hidden="true" /> Worker label
                  </span>
                  <input
                    className="page-input"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="e.g. Render VM 2 (optional)"
                    maxLength={80}
                  />
                </label>
                <label className="page-field worker-path-field">
                  <span>
                    <Folder size={13} aria-hidden="true" /> Workspace path
                  </span>
                  <input
                    className="page-input"
                    required
                    value={workspace}
                    onChange={(event) => setWorkspace(event.target.value)}
                    spellCheck={false}
                  />
                </label>
                <div className="page-form-actions worker-form-actions">
                  <button className="page-btn is-primary" disabled={busy}>
                    {busy ? "Preparing…" : "Generate configuration"} <ArrowRight size={15} aria-hidden="true" />
                  </button>
                </div>
              </form>
            )}
          </section>

          <div className="page-stack">
            {enrollment ? (
              <section className="nr-card worker-credentials" aria-label="One-time worker configuration">
                <div className="nr-card-head">
                  <div>
                    <h3 className="page-title-sm">Connect {enrollment.worker.name || enrollment.worker.worker_id}</h3>
                    <p>Save the generated files, then test the coordinator connection.</p>
                  </div>
                  <span className="nr-card-count">One-time</span>
                </div>
                <p>
                  {enrollment.gpu_vendor === "auto" && enrollment.compose_yaml ? (
                    <>
                      Save compose.yaml, .env, and start-worker.sh together on the Linux worker host, then run chmod +x
                      start-worker.sh and ./start-worker.sh. The script detects the host GPU (NVIDIA, AMD, or Intel) and
                      starts the matching Compose profile. The host needs its driver runtime, GPU device nodes, and
                      external Docker network named proxy. Set VIDEO_GID and RENDER_GID in .env if the host group IDs
                      differ from the defaults.
                    </>
                  ) : enrollment.compose_variants ? (
                    <>
                      Save all files together on the Linux worker host, then run chmod +x start-worker.sh and
                      ./start-worker.sh. The script selects NVIDIA first, then AMD, then Intel. The selected host needs
                      its matching driver runtime, GPU device nodes, and external Docker network named proxy. Set
                      VIDEO_GID and RENDER_GID in .env if the host group IDs differ from the defaults.
                    </>
                  ) : enrollment.compose_yaml ? (
                    <>
                      Place compose.yaml and .env together on the worker and start it with docker compose up -d.{" "}
                      {enrollment.gpu_vendor === "amd"
                        ? "The host needs ROCm at /opt/rocm and access to /dev/kfd and /dev/dri. Set VIDEO_GID and RENDER_GID in .env if the host uses different group IDs."
                        : enrollment.gpu_vendor === "intel"
                          ? "The host needs its Intel GPU and Level Zero runtime installed, with /dev/dri available. Set VIDEO_GID and RENDER_GID in .env if the host uses different group IDs."
                          : "The host needs the NVIDIA Container Toolkit and an NVIDIA GPU."}
                    </>
                  ) : (
                    "Apply these environment variables to your worker deployment and restart it."
                  )}{" "}
                  Then test the connection here.
                </p>
                <div className="page-note is-warn">
                  These files contain a one-time worker key. Save them before closing setup.
                </div>
                <div className="worker-config-files">
                  {configFiles.map(({ name: fileName, content }) => (
                    <div className="worker-config-file nr-inset" key={fileName}>
                      <code>{fileName}</code>
                      <div className="worker-config-file-actions">
                        <button type="button" className="page-btn is-sm" onClick={() => void copy(content)}>
                          <Copy size={13} aria-hidden="true" /> Copy
                        </button>
                        <button
                          type="button"
                          className="page-btn is-sm is-ghost"
                          onClick={() => download(fileName, content)}
                        >
                          <Download size={13} aria-hidden="true" /> Download
                        </button>
                      </div>
                      {revealed && (
                        <textarea
                          className="page-textarea worker-config-code"
                          aria-label={fileName}
                          readOnly
                          rows={4}
                          value={content}
                        />
                      )}
                    </div>
                  ))}
                </div>
                <div className="page-form-actions worker-form-actions">
                  <button
                    className="page-btn is-primary is-sm"
                    type="button"
                    disabled={busy}
                    onClick={() => void test(enrollment.worker)}
                  >
                    {testing ? "Testing…" : "Test connection"}
                  </button>
                  <button
                    type="button"
                    className="page-btn is-sm is-ghost"
                    onClick={() => setRevealed((value) => !value)}
                    aria-pressed={revealed}
                  >
                    {revealed ? "Hide code" : "Show code"}
                  </button>
                  <button type="button" className="page-btn is-sm is-ghost" onClick={closeEnrollment} disabled={busy}>
                    Close setup
                  </button>
                </div>
              </section>
            ) : (
              <section className="nr-card">
                <div className="nr-card-head">
                  <div>
                    <h2>One-time credentials</h2>
                    <p>Enrollment output for the worker you are adding.</p>
                  </div>
                </div>
                <div className="nr-inset">
                  <div>
                    <strong>Secrets are shown once</strong>
                    <p>
                      Generating a configuration returns the worker key, environment variables and Compose files for
                      this session only. Copy or download them before leaving this screen.
                    </p>
                  </div>
                </div>
                <div className="page-note worker-credentials-hint">
                  The worker appears as pending until you apply the configuration and test the connection.
                </div>
              </section>
            )}
          </div>
        </div>
      )}

      {activeView === "roster" && (
        <>
          {enrollment && !setupBannerDismissed && (
            <div className="worker-setup-banner" role="status">
              <p>Worker enrolled — save the one-time setup files before they expire.</p>
              <button type="button" className="page-btn is-sm" onClick={() => switchView("onboarding")}>
                View setup files
              </button>
              <button
                type="button"
                className="page-btn is-sm is-ghost worker-setup-banner-dismiss"
                aria-label="Dismiss setup reminder"
                onClick={() => setSetupBannerDismissed(true)}
              >
                <X size={14} aria-hidden="true" />
              </button>
            </div>
          )}

          <section className="nr-card worker-roster-card" aria-label="Enrolled workers">
            <div className="nr-card-head">
              <div>
                <h2>Enrolled workers</h2>
                <p>Connection, GPU capacity, workspace access, and software version for each render node.</p>
              </div>
              <span className="nr-card-count">{onlineCount} online</span>
            </div>

            <div className="page-table worker-table">
              <div className="page-table-head">
                <span>Node</span>
                <span>GPUs</span>
                <span>VRAM</span>
                <span>Status</span>
                <span className="worker-head-actions">Actions</span>
              </div>
              <div className="worker-table-body" role="list" aria-label="Enrolled workers">
                {loading ? (
                  <p className="page-muted worker-loading" role="status">
                    Loading workers…
                  </p>
                ) : workers.length ? (
                  workers.map((worker, index) => {
                    const gpuStats = worker.identity?.gpu_stats ?? [];
                    const gpuVisible = worker.identity?.gpu_stats
                      ? gpuStats.some((gpu) => typeof gpu.memory_total_mib === "number" && gpu.memory_total_mib > 0)
                      : undefined;
                    const writable = worker.identity?.workspace_writable;
                    const ready = worker.online && gpuVisible === true && writable === true;
                    const status = workerStatus(worker);
                    const chipTone =
                      ready || status === "online" ? "is-good" : status === "pending" ? "is-warn" : "is-bad";
                    const isNew = newWorkerId === worker.worker_id;
                    const isRemoving = removingId === worker.worker_id;
                    return (
                      <article
                        className={`page-table-row worker-row${isNew ? " is-new" : ""}${isRemoving ? " is-removing" : ""}`}
                        role="listitem"
                        key={worker.worker_id}
                        ref={(node) => {
                          if (node) cardRefs.current.set(worker.worker_id, node);
                          else cardRefs.current.delete(worker.worker_id);
                        }}
                        style={{ animationDelay: isRemoving ? "0ms" : `${Math.min(index * 40, 200)}ms` }}
                      >
                        <div className="worker-node-cell">
                          <span className={`nerve-node-dot${worker.online ? " is-online" : ""}`} aria-hidden="true" />
                          <span className="worker-node-id">
                            <strong>{worker.name || worker.identity?.hostname || worker.worker_id}</strong>
                            <small>{worker.identity?.hostname ?? worker.worker_id}</small>
                          </span>
                        </div>
                        <div className="worker-gpu-cell page-table-cell-muted">
                          {gpuStats.length ? gpuStats.map((gpu) => gpu.name ?? "GPU").join(", ") : "Not reported"}
                        </div>
                        <div className="worker-vram-cell">
                          {gpuStats.length ? (
                            gpuStats.map((gpu, gpuIndex) => {
                              const total = gpu.memory_total_mib;
                              const free = gpu.memory_free_mib;
                              const percent =
                                typeof total === "number" && total > 0 && typeof free === "number"
                                  ? Math.max(0, Math.min(100, (100 * free) / total))
                                  : 0;
                              return (
                                <div className="worker-vram-line" key={`${gpu.name ?? "gpu"}-${gpuIndex}`}>
                                  <span className="worker-vram-value">
                                    {typeof free === "number" && typeof total === "number"
                                      ? `${gb(free)} / ${gb(total)} GB`
                                      : "Unavailable"}
                                  </span>
                                  {typeof total === "number" && total > 0 && (
                                    <span
                                      className="page-meter is-thin worker-vram-track"
                                      role="meter"
                                      aria-label={`${gpu.name ?? `GPU ${gpuIndex}`} free memory`}
                                      aria-valuemin={0}
                                      aria-valuemax={100}
                                      aria-valuenow={Math.round(percent)}
                                    >
                                      <span style={{ width: `${percent}%` }} />
                                    </span>
                                  )}
                                </div>
                              );
                            })
                          ) : (
                            <span className="worker-vram-empty">Not reported</span>
                          )}
                        </div>
                        <div className="worker-status-cell">
                          <span className={`page-chip ${chipTone}`}>
                            <i aria-hidden="true" />
                            {ready ? "ready" : status}
                          </span>
                        </div>
                        <div className="worker-row-actions">
                          {editId === worker.worker_id ? (
                            <>
                              <button
                                type="button"
                                className="page-btn is-sm is-primary"
                                disabled={busy}
                                onClick={() => void saveEdit(worker)}
                              >
                                Save changes
                              </button>
                              <button type="button" className="page-btn is-sm is-ghost" onClick={() => setEditId("")}>
                                Cancel
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="page-btn is-sm"
                                disabled={busy}
                                onClick={() => void test(worker)}
                              >
                                {testing === worker.worker_id ? "Testing…" : "Test"}
                              </button>
                              <button
                                type="button"
                                className="page-btn is-sm is-ghost"
                                onClick={() => {
                                  setEditId(worker.worker_id);
                                  setEdit({
                                    name: worker.name ?? "",
                                    workspace: worker.workspace ?? "/workspace",
                                  });
                                }}
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                className="page-btn is-sm is-ghost worker-delete-trigger"
                                disabled={busy}
                                aria-haspopup="dialog"
                                aria-label={`Delete ${worker.name || worker.identity?.hostname || worker.worker_id}`}
                                onClick={(event) => openDelete(worker, event.currentTarget)}
                              >
                                <X size={12} aria-hidden="true" />
                              </button>
                            </>
                          )}
                        </div>
                        {editId === worker.worker_id && (
                          <div className="worker-edit">
                            <label className="page-field">
                              Label
                              <input
                                className="page-input"
                                value={edit.name}
                                onChange={(e) => setEdit({ ...edit, name: e.target.value })}
                              />
                            </label>
                            <label className="page-field">
                              Workspace
                              <input
                                className="page-input"
                                value={edit.workspace}
                                onChange={(e) => setEdit({ ...edit, workspace: e.target.value })}
                              />
                            </label>
                            <p className="page-muted">Workspace changes may require a connection re-test.</p>
                          </div>
                        )}
                        {status === "pending" && (
                          <p className="worker-row-note">
                            Pending — not active or verified. Apply configuration, restart the worker, then test.
                          </p>
                        )}
                        {status === "disabled" && (
                          <p className="worker-row-note">Disabled — test the connection to activate this worker.</p>
                        )}
                        <details className="worker-connection-details">
                          <summary>Connection details</summary>
                          <code>{worker.url ?? worker.worker_id}</code>
                        </details>
                      </article>
                    );
                  })
                ) : (
                  <div className="nr-empty">
                    <strong>No workers enrolled yet.</strong>
                    <p>Add a render worker to generate its configuration and start routing jobs to it.</p>
                    <button
                      type="button"
                      className="page-btn is-sm is-primary"
                      onClick={() => switchView("onboarding")}
                    >
                      Add worker
                    </button>
                  </div>
                )}
              </div>
            </div>
          </section>
        </>
      )}

      {deleteTarget && (
        <div
          className="worker-delete-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDelete();
          }}
        >
          <section
            className="worker-delete-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="worker-delete-title"
            aria-describedby="worker-delete-desc"
          >
            <div className="worker-delete-head">
              <h3 id="worker-delete-title">Delete worker</h3>
              <button
                type="button"
                className="page-btn is-sm is-ghost worker-delete-close"
                aria-label="Close"
                onClick={closeDelete}
              >
                <X size={15} aria-hidden="true" />
              </button>
            </div>
            <div className="worker-delete-name-row">
              <code className="worker-delete-target">{deleteExpected}</code>
              <button
                type="button"
                className="page-btn is-sm is-ghost worker-delete-copy"
                title="Copy worker name"
                onClick={() => void copyDeleteName()}
                aria-label={`Copy worker name ${deleteExpected}`}
              >
                <Copy size={13} aria-hidden="true" />
              </button>
            </div>
            <p id="worker-delete-desc">
              This removes the worker from the fleet and stops job routing to it. Jobs already running on the node will
              not be reassigned.
            </p>
            <label className="worker-delete-label" htmlFor="worker-delete-confirm">
              Type the worker name to confirm
            </label>
            <div className={`worker-delete-row ${deleteShakeClass}`}>
              <input
                className="page-input"
                id="worker-delete-confirm"
                ref={deleteInput}
                autoComplete="off"
                spellCheck={false}
                value={deleteConfirmation}
                onChange={(event) => setDeleteConfirmation(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter") return;
                  event.preventDefault();
                  if (deleteConfirmed) confirmDelete();
                  else shakeDeleteInput();
                }}
                aria-describedby="worker-delete-desc"
              />
            </div>
            {deleteNameCopied && (
              <p className="worker-delete-notice" role="status" aria-live="polite">
                Name copied — paste it to confirm
              </p>
            )}
            {error && (
              <p className="studio-error page-note is-bad worker-alert" role="alert">
                {error}
              </p>
            )}
            <div className="worker-delete-actions">
              <button type="button" className="page-btn" onClick={closeDelete} disabled={busy}>
                Cancel
              </button>
              <div
                className="worker-delete-armed"
                onMouseDown={() => {
                  if (!deleteConfirmed) shakeDeleteInput();
                }}
              >
                <button
                  type="button"
                  className="page-btn is-danger worker-delete-confirm"
                  disabled={!deleteConfirmed || busy}
                  onClick={confirmDelete}
                >
                  {busy ? "Deleting…" : "Delete worker"}
                </button>
              </div>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}