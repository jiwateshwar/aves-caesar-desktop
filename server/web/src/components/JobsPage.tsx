import { api, type Job } from "../api";
import { attempt, useLoader } from "../util";

const KIND_LABELS: Record<string, string> = {
  scan: "Dumpsite scan",
  publish: "Publish",
  reindex_library: "Library index",
  reanalyse: "Identify again",
};

export function JobsPage() {
  const jobs = useLoader(() => api.jobs(), [], 4000);
  const status = useLoader(() => api.status(), [], 10000);
  const lastScan = status.data?.worker.last_scan;

  return (
    <div class="page narrow">
      <h1>Activity</h1>
      {status.data && (
        <section class="panel">
          {status.data.worker.providers.node && (
            <p class={status.data.worker.providers.remote_error ? "warn" : ""}>
              <b>GPU inference node:</b> {status.data.worker.providers.node} ·{" "}
              {status.data.worker.providers.remote_error ?? (status.data.worker.providers.via === "remote" ? "in use" : "not used yet")}
            </p>
          )}
          <p>
            <b>Worker:</b> {status.data.worker.online ? status.data.worker.activity : "offline"} ·{" "}
            <b>Detector:</b> {status.data.worker.providers.detector?.join(", ") ?? "not loaded"} ·{" "}
            <b>Classifier:</b> {status.data.worker.providers.classifier?.join(", ") ?? "not loaded"}
          </p>
          {lastScan && status.data.worker.last_scan_at && (
            <p class="muted">
              Last automatic scan {new Date(status.data.worker.last_scan_at).toLocaleTimeString()}: {lastScan.found ?? 0} photos in the dumpsite,{" "}
              {lastScan.ingested ?? 0} new, {lastScan.waiting ?? 0} still being copied
              {lastScan.errors?.length ? `, errors: ${lastScan.errors.join("; ")}` : ""}
            </p>
          )}
          {status.data.missing > 0 && <p class="warn">{status.data.missing} files disappeared from the dumpsite before they were published.</p>}
          <button onClick={() => attempt(() => api.reindexLibrary(), "Re-indexing the published library…").then(jobs.reload)}>
            Re-index published library
          </button>
        </section>
      )}
      <table class="list">
        <thead>
          <tr><th>When</th><th>Job</th><th>Status</th><th>Result</th></tr>
        </thead>
        <tbody>
          {jobs.data?.map((job) => (
            <tr key={job.id}>
              <td>{new Date(job.created_at).toLocaleString()}</td>
              <td>{KIND_LABELS[job.kind] ?? job.kind}</td>
              <td class={`job-${job.status}`}>{job.status}{job.status === "running" && job.total ? ` ${job.progress}/${job.total}` : ""}</td>
              <td><JobResult job={job} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobResult({ job }: { job: Job }) {
  if (job.status === "failed") return <span class="danger">{job.message}</span>;
  const r = job.result;
  if (job.kind === "publish" && job.status === "done") {
    return (
      <span>
        {r.published_files} files published
        {r.conflicts?.length ? <span class="warn"> · {r.conflicts.length} held back by name conflicts</span> : null}
        {r.errors?.length ? <span class="danger"> · {r.errors.join("; ")}</span> : null}
      </span>
    );
  }
  if (job.kind === "scan" && job.status === "done") {
    return <span>{r.ingested} new, {r.duplicates} duplicates, {r.waiting} still copying{r.errors?.length ? ` · ${r.errors.length} errors` : ""}</span>;
  }
  if (job.kind === "reindex_library" && job.status === "done") return <span>{r.files} files in the library ({r.added} new)</span>;
  if (job.kind === "reanalyse" && job.status === "done") return <span>{r.queued} shots queued</span>;
  return <span class="muted">{job.message ?? ""}</span>;
}
