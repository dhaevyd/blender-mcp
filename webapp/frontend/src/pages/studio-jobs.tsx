import { type FormEvent, useEffect, useState } from "react";
import { dashboardAuthHeaders } from "../api/dashboardAuth";
/* farm.css loaded by studio-dashboard.tsx */

type Frame = {
  frame: number;
  status: string;
  worker_id?: string;
  gpu_name?: string;
  gpu_index?: number;
  error?: string;
  output_path?: string;
};
type Job = {
  id: string;
  project_path: string;
  status: string;
  completed: number;
  total_frames: number;
  failed: number;
  wait_reason?: string;
  frames?: Frame[];
};
type Operations = {
  alerts: string[];
  audit: Array<{ id: number; at: number; actor: string; action: string; target: string }>;
};
const root = "/api/v1/farm";

async function call<T>(path: string, signal?: AbortSignal, payload?: Record<string, unknown>): Promise<T> {
  const r = await fetch(`${root}${path}`, {
    signal,
    cache: "no-store",
    headers: { ...dashboardAuthHeaders(), ...(payload ? { "Content-Type": "application/json" } : {}) },
    ...(payload ? { method: "POST", body: JSON.stringify(payload) } : {}),
  });
  if (!r.ok) {
    const response = (await r.json().catch(() => ({}))) as { error?: string };
    throw new Error(response.error ?? `Farm request failed (${r.status}).`);
  }
  return r.json() as Promise<T>;
}

/** Status → chip tone. `pending` stays neutral, terminal failures read coral. */
function tone(status: string): string {
  if (status === "running") return "is-info";
  if (status === "paused") return "is-warn";
  if (status === "completed") return "is-good";
  if (status === "failed" || status === "cancelled") return "is-bad";
  return "";
}

function progressOf(job: Job): number {
  if (!job.total_frames) return 0;
  return Math.max(0, Math.min(100, Math.round((job.completed / job.total_frames) * 100)));
}

const AlertIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
    <path
      d="M8 2.2 14 13H2L8 2.2Z"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinejoin="round"
    />
    <path d="M8 6.4v3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    <circle cx="8" cy="11.2" r="0.7" fill="currentColor" />
  </svg>
);

export default function StudioJobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [selected, setSelected] = useState("");
  const [operations, setOperations] = useState<Operations | null>(null);
  const [role, setRole] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [page, setPage] = useState(0);
  const [projectPath, setProjectPath] = useState("");
  const [frameStart, setFrameStart] = useState(1);
  const [frameEnd, setFrameEnd] = useState(1);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const [list, access, detail] = await Promise.all([
          call<{ jobs: Job[] }>("/jobs", signal),
          call<{ role: string }>("/access", signal),
          selected ? call<Job>(`/jobs/${encodeURIComponent(selected)}`, signal) : null,
        ]);
        if (signal.aborted) return;
        setJobs(list.jobs);
        setRole(access.role);
        setJob(detail);
        setError("");
      } catch (e) {
        if (!signal.aborted) setError(e instanceof Error ? e.message : "Could not load jobs.");
      } finally {
        if (!signal.aborted) {
          setLoaded(true);
          timer = window.setTimeout(poll, 5000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [selected, refresh]);

  // SSE: stream live frame progress for the selected job via fetch (supports auth headers)
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    const url = `${root}/jobs/${encodeURIComponent(selected)}/stream`;
    const terminal = new Set(["done", "failed", "cancelled"]);

    (async () => {
      try {
        const res = await fetch(url, {
          headers: dashboardAuthHeaders() as Record<string, string>,
          signal: controller.signal,
        });
        if (!res.ok || !res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            try {
              const data = JSON.parse(line.slice(5).trim()) as {
                status: string;
                total_frames: number;
                completed_frames: number;
                failed_frames: number;
              };
              setJob((prev) =>
                prev
                  ? {
                      ...prev,
                      status: data.status,
                      total_frames: data.total_frames,
                      completed: data.completed_frames,
                      failed: data.failed_frames,
                    }
                  : prev,
              );
              if (terminal.has(data.status)) return;
            } catch {
              // ignore malformed event
            }
          }
        }
      } catch {
        // aborted or network error — silent
      }
    })();

    return () => controller.abort();
  }, [selected]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const data = await call<Operations>("/operations", controller.signal);
        if (!controller.signal.aborted) setOperations(data);
      } catch {
        if (!controller.signal.aborted) setOperations(null);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 15000);
      }
    };
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [refresh]);

  async function act(action: string) {
    if (action === "cancel" && !window.confirm("Cancel this render job?")) return;
    setBusy(true);
    try {
      await call(`/jobs/${encodeURIComponent(selected)}/actions`, undefined, { action });
      setMessage("Job updated.");
      setRefresh((n) => n + 1);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    setError("");
    try {
      const result = await call<{ job_id: string; status: string }>("/jobs", undefined, {
        project_path: projectPath.trim(),
        frame_start: frameStart,
        frame_end: frameEnd,
      });
      setSelected(result.job_id);
      setPage(0);
      setProjectPath("");
      setMessage(`Job ${result.job_id} submitted.`);
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not submit the job.");
    } finally {
      setBusy(false);
    }
  }

  async function download(frame: Frame) {
    try {
      const response = await fetch(`${root}/jobs/${encodeURIComponent(selected)}/frames/${frame.frame}/download`, {
        headers: dashboardAuthHeaders(),
      });
      if (!response.ok) throw new Error("Output unavailable.");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = frame.output_path?.split("/").at(-1) ?? `frame-${frame.frame}.png`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setMessage("Could not download this frame.");
    }
  }

  const detail = job?.id === selected ? job : null;
  const frames = detail?.frames ?? [];
  const running = jobs.filter((item) => item.status === "running").length;
  const paused = jobs.filter((item) => item.status === "paused").length;
  const queued = jobs.filter((item) => item.status === "pending").length;
  const failed = jobs.filter((item) => item.status === "failed").length;
  const done = jobs.filter((item) => item.status === "completed").length;
  const failedFrames = jobs.reduce((sum, item) => sum + (item.failed || 0), 0);
  const active = running + paused;
  const canSubmit = role === "admin" || role === "operator";

  return (
    <section className="page studio-jobs" aria-label="Farm jobs">
      <header className="page-head">
        <div>
          <h1>
            Jobs<span className="page-period">.</span>
          </h1>
          <p>Submit, monitor and steer renders across the farm.</p>
        </div>
        <div className="page-actions">
          {canSubmit && (
            <button
              type="button"
              className="page-btn is-primary"
              onClick={() => setSelected("__submit__")}
            >
              + Submit job
            </button>
          )}
          <button type="button" className="page-btn" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>
            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
              <path
                d="M13.2 8a5.2 5.2 0 1 1-1.6-3.7M13.4 2.4v3.2h-3.2"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.35"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Refresh
          </button>
        </div>
      </header>

      <div className="page-stats">
        <div className="page-stat">
          <div className="page-stat-label">
            <i className="studio-jobs-dot is-live" aria-hidden="true" />
            Active
          </div>
          <div className="page-stat-value">
            {active}
            <small>{active === 1 ? "job" : "jobs"}</small>
          </div>
          <div className="page-stat-foot">
            {running} running · {paused} paused
          </div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">
            <i className="studio-jobs-dot" aria-hidden="true" />
            Queued
          </div>
          <div className="page-stat-value">
            {queued}
            <small>{queued === 1 ? "job" : "jobs"}</small>
          </div>
          <div className="page-stat-foot">awaiting a free worker</div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">
            <i className="studio-jobs-dot is-bad" aria-hidden="true" />
            Failed
          </div>
          <div className="page-stat-value">
            {failed}
            <small>{failed === 1 ? "job" : "jobs"}</small>
          </div>
          <div className="page-stat-foot">{failedFrames} frames need a retry</div>
        </div>
        <div className="page-stat">
          <div className="page-stat-label">
            <i className="studio-jobs-dot is-good" aria-hidden="true" />
            Done
          </div>
          <div className="page-stat-value">
            {done}
            <small>{done === 1 ? "job" : "jobs"}</small>
          </div>
          <div className="page-stat-foot">{jobs.length ? Math.round((done / jobs.length) * 100) : 0}% of all jobs</div>
        </div>
      </div>

      {error && (
        <p className="page-note is-bad" role="alert">
          {error}
        </p>
      )}

      {/* Submit modal */}
      {selected === "__submit__" && canSubmit && (
        <div className="jobs-modal-backdrop" onClick={() => setSelected("")}>
          <form
            className="nr-card jobs-submit-modal"
            onSubmit={(event) => void submit(event)}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="nr-card-head">
              <div>
                <h2>Submit a render</h2>
                <p>Choose a .blend or .zip file inside the coordinator workspace and a frame range.</p>
              </div>
              <button type="button" className="jobs-modal-close" onClick={() => setSelected("")} aria-label="Close">×</button>
            </div>
            <div className="studio-job-fields">
              <label className="page-field">
                <span>Project path</span>
                <input
                  className="page-input studio-job-path-input"
                  required
                  value={projectPath}
                  onChange={(event) => setProjectPath(event.target.value)}
                  placeholder="projects/shot.blend"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <div className="studio-job-frame-range">
                <label className="page-field">
                  <span>First frame</span>
                  <input
                    className="page-input"
                    type="number"
                    min={0}
                    max={1_000_000}
                    value={frameStart}
                    onChange={(e) => setFrameStart(Number(e.target.value))}
                  />
                </label>
                <label className="page-field">
                  <span>Last frame</span>
                  <input
                    className="page-input"
                    type="number"
                    min={0}
                    max={1_000_000}
                    value={frameEnd}
                    onChange={(e) => setFrameEnd(Number(e.target.value))}
                  />
                </label>
              </div>
            </div>
            <div className="page-form-actions">
              <button
                type="submit"
                className="page-btn is-primary"
                disabled={busy || !loaded || frameEnd < frameStart}
              >
                {busy ? "Submitting…" : "Submit to farm"}
              </button>
              <span className="page-muted">
                {frameEnd < frameStart ? "Last frame must be ≥ first frame." : "Queued jobs start as soon as a GPU is free."}
              </span>
            </div>
          </form>
        </div>
      )}

      {/* All jobs table */}
      <section className="nr-card">
        <div className="nr-card-head">
          <div>
            <h2>All jobs</h2>
            <p>Click a row to inspect frames and control execution.</p>
          </div>
          <span className="nr-card-count">{jobs.length} total</span>
        </div>
        {!loaded ? (
          <p className="page-muted studio-jobs-placeholder">Loading jobs…</p>
        ) : !jobs.length && !error ? (
          <div className="nr-empty">
            <strong>No jobs submitted</strong>
            <p>Once a render is queued it shows up here with live frame progress.</p>
          </div>
        ) : null}
        {jobs.length > 0 && (
          <div className="page-table jobs-all-table">
            <div className="page-table-head jobs-all-row" aria-hidden="true">
              <span>Project</span>
              <span>Frames</span>
              <span>Workers</span>
              <span>Progress</span>
              <span>ETA</span>
              <span>Actions</span>
            </div>
            {jobs.map((item) => {
              const percent = progressOf(item);
              const isSelected = selected === item.id;
              return (
                <div key={item.id} className={`page-table-row jobs-all-row${isSelected ? " is-selected" : ""}`}>
                  <button
                    type="button"
                    className="jobs-row-project"
                    onClick={() => { setSelected(isSelected ? "" : item.id); setPage(0); setMessage(""); }}
                  >
                    <span className="page-table-cell-strong" title={item.project_path}>{item.project_path.split(/[\\/]/).pop() || item.project_path}</span>
                    <span className={`page-chip ${tone(item.status)}`}><i />{item.status}</span>
                  </button>
                  <span className="page-table-cell-muted">
                    {item.completed}/{item.total_frames}
                    {item.failed > 0 && <span className="jobs-fail-count"> · {item.failed} fail</span>}
                  </span>
                  <span className="page-table-cell-muted">—</span>
                  <span className="jobs-meter-cell">
                    <span className={`page-meter is-thin${item.failed ? " is-hot" : ""}`}>
                      <span style={{ width: `${percent}%` }} />
                    </span>
                    <span className="jobs-meter-pct">{percent}%</span>
                  </span>
                  <span className="page-table-cell-muted">—</span>
                  <span className="jobs-actions-cell">
                    {role !== "viewer" && role && item.status === "running" && (
                      <button type="button" className="page-btn is-sm" disabled={busy && selected === item.id}
                        onClick={() => { setSelected(item.id); void act("pause"); }}>Pause</button>
                    )}
                    {role !== "viewer" && role && item.status === "paused" && (
                      <button type="button" className="page-btn is-sm" disabled={busy && selected === item.id}
                        onClick={() => { setSelected(item.id); void act("resume"); }}>Resume</button>
                    )}
                    {role !== "viewer" && role && item.status === "failed" && item.failed > 0 && (
                      <button type="button" className="page-btn is-sm" disabled={busy && selected === item.id}
                        onClick={() => { setSelected(item.id); void act("retry_failed"); }}>Retry</button>
                    )}
                    {role !== "viewer" && role && ["running", "paused"].includes(item.status) && (
                      <button type="button" className="page-btn is-sm is-danger" disabled={busy && selected === item.id}
                        onClick={() => { setSelected(item.id); void act("cancel"); }}>Cancel</button>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        )}

        {/* Expanded detail for selected job */}
        {detail && (
          <div className="jobs-detail-drawer">
            <div className="jobs-detail-head">
              <div>
                <span className="page-kicker">Job detail</span>
                <strong className="jobs-detail-path">{detail.project_path}</strong>
                <span className="page-muted">{detail.completed}/{detail.total_frames} frames · {detail.failed} failed</span>
              </div>
              <button type="button" className="jobs-modal-close" onClick={() => setSelected("")} aria-label="Close detail">×</button>
            </div>
            <div className="jobs-detail-progress">
              <div
                className="page-meter"
                role="progressbar"
                aria-label="Frames completed"
                aria-valuenow={detail.completed}
                aria-valuemin={0}
                aria-valuemax={detail.total_frames || 1}
              >
                <span style={{ width: `${progressOf(detail)}%` }} />
              </div>
              <span className="page-muted">{progressOf(detail)}%</span>
            </div>
            {detail.status === "running" && detail.wait_reason && (
              <p className="page-note is-warn">{detail.wait_reason}</p>
            )}
            {message && <p className="page-note" role="status">{message}</p>}
            <div className="page-kicker" style={{ marginTop: "16px" }}>Frames and outputs</div>
            <div className="page-table studio-frame-table">
              <div className="page-table-head studio-frame-row" aria-hidden="true">
                <span>Frame</span>
                <span>Status</span>
                <span>Worker / GPU</span>
                <span>Output</span>
              </div>
              {frames.slice(page * 50, (page + 1) * 50).map((frame) => (
                <div key={frame.frame} className="page-table-row studio-frame-row">
                  <span className="page-table-cell-strong">{frame.frame}</span>
                  <span className={`page-chip ${tone(frame.status)}`}><i />{frame.status}</span>
                  <span className="studio-frame-worker">
                    {frame.worker_id ?? "—"}
                    {frame.gpu_name ? ` / ${frame.gpu_name}${frame.gpu_index == null ? "" : ` #${frame.gpu_index}`}` : ""}
                  </span>
                  <span className="studio-frame-out">
                    {frame.status === "completed" && frame.output_path ? (
                      <button type="button" className="page-btn is-sm" aria-label={`Download frame ${frame.frame}`}
                        onClick={() => void download(frame)}>Download</button>
                    ) : (frame.error ?? "—")}
                  </span>
                </div>
              ))}
            </div>
            {frames.length > 50 && (
              <div className="studio-jobs-actions studio-jobs-pager">
                <button type="button" className="page-btn is-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
                <span className="page-muted">Page {page + 1} of {Math.ceil(frames.length / 50)}</span>
                <button type="button" className="page-btn is-sm" disabled={(page + 1) * 50 >= frames.length} onClick={() => setPage(page + 1)}>Next</button>
              </div>
            )}
          </div>
        )}

        <div className="nr-card-foot">
          <span>{detail ? `Inspecting ${detail.id.slice(0, 8)}` : "Click a row to inspect"}</span>
          <span>Polling every 5s</span>
        </div>
      </section>

      {/* Alerts row */}
      {operations?.alerts && operations.alerts.length > 0 && (
        <section className="nr-card">
          <div className="nr-card-head">
            <div><h2>Farm alerts</h2><p>Worker and queue anomalies from the coordinator.</p></div>
            <span className="nr-card-count">{operations.alerts.length} open</span>
          </div>
          <div className="page-list">
            {operations.alerts.map((alert) => (
              <div className="page-list-row studio-jobs-alert" key={alert}>
                <span className="studio-jobs-alert-icon"><AlertIcon /></span>
                <span className="studio-jobs-alert-text">{alert}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </section>
  );
}
