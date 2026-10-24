import { useEffect, useState } from "preact/hooks";
import { api } from "../api";
import { BRAND } from "../brand";
import { attempt, href, onToast, useLoader, useRoute, type Toast } from "../util";
import { DuplicatesPage } from "./DuplicatesPage";
import { JobsPage } from "./JobsPage";
import { SessionPage } from "./SessionPage";
import { SessionsPage } from "./SessionsPage";
import { SettingsPage } from "./SettingsPage";

export function App() {
  const route = useRoute();
  const status = useLoader(() => api.status(), [], 5000);
  const [page, id] = route.path;

  return (
    <div class="app">
      <header class="topbar">
        <a class="brand" href={href("")} aria-label={`${BRAND.name} home`}>
          <img src="/featherbase-mark.svg" alt="" width="28" height="28" />
          <span class="wordmark">{BRAND.wordmark}</span>
        </a>
        <nav>
          <a href={href("")} class={!page || page === "sessions" ? "active" : ""}>Sessions</a>
          <a href={href("duplicates")} class={page === "duplicates" ? "active" : ""}>
            Duplicates{status.data?.duplicates ? <span class="badge">{status.data.duplicates}</span> : null}
          </a>
          <a href={href("jobs")} class={page === "jobs" ? "active" : ""}>Activity</a>
          <a href={href("settings")} class={page === "settings" ? "active" : ""}>Settings</a>
        </nav>
        <WorkerPill status={status.data} error={status.error} />
        <button class="primary" onClick={() => attempt(() => api.scan(), "Scanning the dumpsite…").then(status.reload)}>
          Scan now
        </button>
      </header>
      {status.data?.worker.model_error && (
        <div class="banner error" role="alert">The species suggestion models could not be loaded: {status.data.worker.model_error}</div>
      )}
      <main>
        {page === "sessions" && id ? (
          <SessionPage id={Number(id)} route={route} onChange={status.reload} />
        ) : page === "duplicates" ? (
          <DuplicatesPage onChange={status.reload} />
        ) : page === "jobs" ? (
          <JobsPage />
        ) : page === "settings" ? (
          <SettingsPage />
        ) : (
          <SessionsPage status={status.data} />
        )}
      </main>
      <footer class="footer">
        <span>{BRAND.name} · {BRAND.tagline}</span>
        <span>
          <a href={BRAND.engineUrl} target="_blank" rel="noreferrer">{BRAND.engineCredit}</a>. AI-assisted suggestions that you review;
          analysis runs on your own hardware.
        </span>
      </footer>
      <Toasts />
    </div>
  );
}

function WorkerPill({ status, error }: { status: Awaited<ReturnType<typeof api.status>> | null; error: string | null }) {
  if (error) return <span class="pill offline" title={error}>Server unreachable</span>;
  if (!status) return <span class="pill">…</span>;
  const { worker, jobs } = status;
  if (!worker.online) return <span class="pill offline" title="No heartbeat from the worker in the last 2 minutes">Worker offline</span>;
  const running = jobs.find((job) => job.status === "running");
  const p = worker.providers;
  const gpu = [...(p.classifier ?? []), ...(p.detector ?? [])].find((name) => name !== "CPUExecutionProvider");
  const processor = gpu ? gpu.replace("ExecutionProvider", "").replace("Dml", "DirectML") : p.classifier ? "CPU" : "";
  const where = p.via === "remote" ? `PC · ${processor}` : p.node && p.remote_error ? `${processor} · PC offline` : processor;
  const progress = running && running.total ? ` ${running.progress}/${running.total}` : "";
  const title = [
    p.node ? `Inference node: ${p.node}` : "Models run on the NAS",
    `Detector: ${p.detector?.join(", ") ?? "?"}`,
    `Classifier: ${p.classifier?.join(", ") ?? "?"}`,
    p.remote_error ? `Node problem: ${p.remote_error}` : "",
  ].filter(Boolean).join("\n");
  return (
    <span class={`pill online ${p.remote_error ? "degraded" : ""}`} title={title}>
      <i class="dot" />
      {running ? `${running.kind.replace("_", " ")}${progress}` : worker.activity ?? "Idle"}
      <small>{where}</small>
    </span>
  );
}

function Toasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useEffect(
    () =>
      onToast((message) => {
        setToasts((current) => [...current, message]);
        window.setTimeout(() => setToasts((current) => current.filter((t) => t.id !== message.id)), message.kind === "error" ? 8000 : 3500);
      }),
    [],
  );
  return (
    <div class="toasts">
      {toasts.map((t) => (
        <div key={t.id} class={`toast ${t.kind}`}>{t.text}</div>
      ))}
    </div>
  );
}
