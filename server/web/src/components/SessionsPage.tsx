import { api, type SessionSummary, type Status } from "../api";
import { formatDate, href, useLoader } from "../util";

export function SessionsPage({ status }: { status: Status | null }) {
  const sessions = useLoader(() => api.sessions(), [], 15000);

  if (sessions.error) return <div class="empty error">Could not load sessions: {sessions.error}</div>;
  if (!sessions.data) return <div class="empty">Loading…</div>;
  if (!sessions.data.length) {
    return (
      <div class="empty">
        <h2>No photos yet</h2>
        <p>
          Copy photos from your camera into <code>{status?.paths.dumpsite ?? "the dumpsite folder"}</code>. They'll appear here, grouped by the day
          they were taken. New files are picked up automatically, or press <b>Scan now</b>.
        </p>
      </div>
    );
  }

  const totals = status?.totals;
  return (
    <div class="page">
      {totals && (
        <div class="summary">
          <Stat value={totals.review + totals.low_confidence + totals.no_bird} label="waiting for review" />
          <Stat value={totals.awaiting_location} label="need a location" warn />
          <Stat value={totals.pending} label="being identified" />
          <Stat value={totals.conflicts} label="name conflicts" warn />
          <Stat value={totals.confirmed} label="ready to publish" />
        </div>
      )}
      <div class="session-list">
        {sessions.data.map((session) => (
          <SessionCard key={session.id} session={session} />
        ))}
      </div>
    </div>
  );
}

function Stat({ value, label, warn }: { value: number; label: string; warn?: boolean }) {
  return (
    <div class={`stat ${warn && value ? "warn" : ""}`}>
      <b>{value}</b> {label}
    </div>
  );
}

function SessionCard({ session }: { session: SessionSummary }) {
  const c = session.counts;
  const toReview = c.review + c.low_confidence + c.no_bird;
  const done = c.published + c.ignored;
  return (
    <a class="session-card" href={href(`sessions/${session.id}`)}>
      <div class="session-date">
        <b>{formatDate(session.date)}</b>
        {session.title && <span>{session.title}</span>}
      </div>
      <div class="session-place">
        {session.location ? session.location.label : c.awaiting_location ? <span class="warn">📍 Location needed for {c.awaiting_location} photos</span> : <span class="muted">Located from GPS</span>}
      </div>
      <div class="session-counts">
        {toReview > 0 && <span class="chip">{toReview} to review</span>}
        {c.low_confidence > 0 && <span class="chip amber">{c.low_confidence} low confidence</span>}
        {c.pending > 0 && <span class="chip">{c.pending} queued</span>}
        {c.conflicts > 0 && <span class="chip red">{c.conflicts} conflicts</span>}
        {c.confirmed > 0 && <span class="chip green">{c.confirmed} ready</span>}
        {c.error > 0 && <span class="chip red">{c.error} errors</span>}
        <span class="chip muted">{done}/{c.total} done</span>
      </div>
      <div class="progress"><i style={{ width: `${c.total ? (done / c.total) * 100 : 0}%` }} /></div>
    </a>
  );
}
