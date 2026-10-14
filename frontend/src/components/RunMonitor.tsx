import { useEffect, useState } from "react";
import { API, api } from "../api";
import type { Job } from "../api";
import { useStore } from "../store";

/** Queue position, progress, ETA, cancel (FR-UI-05). Progress streams over SSE. */
export function RunMonitor() {
  const s = useStore();
  const queued = s.jobs.filter((j) => j.status === "queued" || j.status === "submitted");
  return (
    <div className="runs" data-testid="run-monitor">
      <h2>Runs</h2>
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Queue</th>
          </tr>
        </thead>
        <tbody>
          {s.jobs.map((j) => (
            <tr key={j.id} className={j.id === s.jobId ? "selected" : ""} onClick={() => s.setJobId(j.id)}>
              <td>{j.id}</td>
              <td data-testid={`status-${j.id}`}>{j.status}</td>
              <td>
                {j.progress_completed ?? 0}/{j.progress_planned ?? "?"}
              </td>
              <td>{queued.findIndex((q) => q.id === j.id) >= 0 ? queued.findIndex((q) => q.id === j.id) + 1 : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {s.jobId && <JobProgress jobId={s.jobId} />}
    </div>
  );
}

export function JobProgress({ jobId, compact = false }: { jobId: string; compact?: boolean }) {
  const s = useStore();
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    const es = new EventSource(`${API}/jobs/${jobId}/events`);
    const on = (e: MessageEvent) => setJob(JSON.parse(e.data));
    es.addEventListener("progress", on);
    es.addEventListener("done", (e) => {
      on(e as MessageEvent);
      es.close();
      s.refreshJobs();
    });
    es.onerror = () => es.close();
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);
  if (!job) return <div className="card muted">connecting to {jobId}…</div>;
  const done = job.progress_completed ?? 0;
  const planned = job.progress_planned || job.estimate?.runs || 1;
  const pct = Math.round((100 * done) / planned);
  const elapsed = job.started_at ? Date.now() / 1000 - job.started_at : 0;
  const eta = job.estimate ? Math.max(0, job.estimate.minutes * 60 - elapsed) : null;
  const finished = ["completed", "failed", "cancelled"].includes(job.status);
  return (
    <div className="card progress" data-testid={`job-${jobId}`}>
      <div className="row">
        <b>{job.id}</b> <span data-testid="job-status">{job.status}</span>
        {job.cached && <span className="badge">cached</span>}
      </div>
      <div className="bar">
        <div style={{ width: `${pct}%` }} />
      </div>
      <small>
        {done}/{planned} replications{!finished && eta !== null ? ` · ETA ~${Math.ceil(eta)} s` : ""}
      </small>
      {!compact && !finished && <button onClick={() => api.post(`/jobs/${jobId}/cancel`, {})}>Cancel</button>}
      {job.status === "completed" && (
        <button data-testid="open-results" onClick={() => (s.setJobId(jobId), s.setView("compare"))}>
          Open results
        </button>
      )}
      {job.error && <p className="error">{job.error}</p>}
    </div>
  );
}
