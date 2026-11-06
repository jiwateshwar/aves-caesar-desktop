import { api, type SessionSummary, type Status } from "../api";
import { BRAND } from "../brand";
import { formatDate, href, navigate, plural, replaceRoute, useLoader, type Route } from "../util";

type Show = "all" | "attention" | "location" | "date" | "review" | "identifying" | "ready" | "done";
type Sort = "newest" | "oldest" | "review" | "photos" | "added";

const SHOW: Record<Show, string> = {
  all: "All sessions",
  attention: "Needs location or date",
  location: "Needs a location",
  date: "Date guessed",
  review: "Waiting for review",
  identifying: "Being identified",
  ready: "Ready to publish",
  done: "Fully published",
};
const SORT: Record<Sort, string> = {
  newest: "Newest first",
  oldest: "Oldest first",
  review: "Most to review",
  photos: "Most photos",
  added: "Recently added",
};

const toReview = (c: SessionSummary["counts"]) => c.review + c.low_confidence + c.no_bird;
const needsLocation = (s: SessionSummary) => s.counts.awaiting_location > 0;
const needsDate = (s: SessionSummary) => !s.date || (s.date_guessed ?? 0) > 0;
const isDone = (s: SessionSummary) => s.counts.total > 0 && s.counts.published + s.counts.ignored === s.counts.total;

const MATCHES: Record<Show, (s: SessionSummary) => boolean> = {
  all: () => true,
  attention: (s) => needsLocation(s) || needsDate(s),
  location: needsLocation,
  date: needsDate,
  review: (s) => toReview(s.counts) > 0,
  identifying: (s) => s.counts.pending > 0,
  ready: (s) => s.counts.confirmed > 0,
  done: isDone,
};

const SORTERS: Record<Sort, (a: SessionSummary, b: SessionSummary) => number> = {
  newest: (a, b) => (b.date ?? "").localeCompare(a.date ?? ""),
  oldest: (a, b) => (a.date ?? "9999").localeCompare(b.date ?? "9999"),
  review: (a, b) => toReview(b.counts) - toReview(a.counts),
  photos: (a, b) => b.counts.total - a.counts.total,
  added: (a, b) => (b.last_added_at ?? "").localeCompare(a.last_added_at ?? ""),
};

export function SessionsPage({ status, route }: { status: Status | null; route: Route }) {
  const sessions = useLoader(() => api.sessions(), [], 15000);
  const dumpsites = useLoader(() => api.dumpsites(), []);

  const params = route.params;
  const show = (params.get("show") as Show | null) ?? "all";
  const sort = (params.get("sort") as Sort | null) ?? "newest";
  const q = params.get("q") ?? "";
  const year = params.get("year") ?? "";
  const site = params.get("dumpsite") ?? "";
  const set = (changes: Record<string, string | undefined>) => {
    const next = { show, sort, q, year, dumpsite: site, ...changes };
    replaceRoute("", {
      show: next.show === "all" ? undefined : next.show, sort: next.sort === "newest" ? undefined : next.sort,
      q: next.q || undefined, year: next.year || undefined, dumpsite: next.dumpsite || undefined,
    });
  };

  if (sessions.error) return <div class="empty error">Could not load sessions: {sessions.error}</div>;
  if (!sessions.data) return <div class="empty">Loading…</div>;
  if (!sessions.data.length) {
    return (
      <div class="empty">
        <div class="tagline">{BRAND.tagline}</div>
        <p>
          Copy photos from your camera into <code>{status?.paths.dumpsite ?? "the dumpsite folder"}</code>. {BRAND.name} picks them up,
          groups them by the day they were taken and suggests a species for every bird, ready for you to review. Or press <b>Scan now</b>.
        </p>
      </div>
    );
  }

  const all = sessions.data;
  const years = [...new Set(all.map((s) => s.date?.slice(0, 4)).filter(Boolean) as string[])].sort().reverse();
  const sites = dumpsites.data?.dumpsites ?? [];
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const base = all.filter((s) =>
    (!year || s.date?.startsWith(year)) &&
    (!site || (s.dumpsites ?? []).includes(Number(site))) &&
    words.every((w) => [s.title, s.location?.label, s.date, s.date ? formatDate(s.date) : "Undated"].join(" ").toLowerCase().includes(w)));
  const counts = Object.fromEntries((Object.keys(SHOW) as Show[]).map((k) => [k, base.filter(MATCHES[k]).length])) as Record<Show, number>;
  const shown = base.filter(MATCHES[show]).sort((a, b) => SORTERS[sort](a, b) || SORTERS.newest(a, b));
  const filtered = show !== "all" || q || year || site;

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

      <div class="session-filters">
        <input type="search" placeholder="Search title, place or date" value={q} aria-label="Search sessions"
               onInput={(e) => set({ q: e.currentTarget.value })} />
        <select value={show} onChange={(e) => set({ show: e.currentTarget.value })} aria-label="Show">
          {(Object.keys(SHOW) as Show[]).map((k) => <option key={k} value={k}>{SHOW[k]} ({counts[k]})</option>)}
        </select>
        <select value={year} onChange={(e) => set({ year: e.currentTarget.value })} aria-label="Year">
          <option value="">All years</option>
          {years.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        {sites.length > 1 && (
          <select value={site} onChange={(e) => set({ dumpsite: e.currentTarget.value })} aria-label="Dumpsite">
            <option value="">All dumpsites</option>
            {sites.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        )}
        <select value={sort} onChange={(e) => set({ sort: e.currentTarget.value })} aria-label="Sort">
          {(Object.keys(SORT) as Sort[]).map((k) => <option key={k} value={k}>{SORT[k]}</option>)}
        </select>
        <span class="muted">{shown.length} of {plural(all.length, "session")}</span>
        {filtered && <button type="button" class="link" onClick={() => navigate("")}>Clear</button>}
      </div>
      <div class="session-legend">
        <button type="button" class={`legend location ${show === "location" ? "active" : ""}`}
                onClick={() => set({ show: show === "location" ? "all" : "location" })}>
          <i /> Needs a location ({counts.location})
        </button>
        <button type="button" class={`legend date ${show === "date" ? "active" : ""}`}
                onClick={() => set({ show: show === "date" ? "all" : "date" })}>
          <i /> Date guessed, check it ({counts.date})
        </button>
      </div>

      {shown.length === 0 ? (
        <div class="empty">No sessions match. <button type="button" class="link" onClick={() => navigate("")}>Show all</button></div>
      ) : (
        <div class="session-list">
          {shown.map((session) => <SessionCard key={session.id} session={session} sites={sites} />)}
        </div>
      )}
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

function SessionCard({ session, sites }: { session: SessionSummary; sites: { id: number; name: string; main: boolean }[] }) {
  const c = session.counts;
  const review = toReview(c);
  const done = c.published + c.ignored;
  const location = needsLocation(session);
  const date = needsDate(session);
  const from = (session.dumpsites ?? []).map((id) => sites.find((s) => s.id === id)).filter((s) => s && !s.main);
  return (
    <a class={`session-card ${location ? "needs-location" : ""} ${date ? "needs-date" : ""}`} href={href(`sessions/${session.id}`)}>
      <div class="session-date">
        <b>{formatDate(session.date)}</b>
        {session.title && <span>{session.title}</span>}
      </div>
      <div class="session-place">
        {session.location ? session.location.label : location ? <span class="warn">📍 Location needed for {plural(c.awaiting_location, "photo")}</span> : <span class="muted">Located from GPS</span>}
      </div>
      {date && (
        <div class="date-warning">
          📅 {session.date ? `Date guessed from the file for ${plural(session.date_guessed ?? 0, "photo")}; check it` : "No date"}
        </div>
      )}
      <div class="session-counts">
        {review > 0 && <span class="chip">{review} to review</span>}
        {c.low_confidence > 0 && <span class="chip amber">{c.low_confidence} low confidence</span>}
        {c.pending > 0 && <span class="chip">{c.pending} queued</span>}
        {c.conflicts > 0 && <span class="chip red">{plural(c.conflicts, "conflict")}</span>}
        {c.confirmed > 0 && <span class="chip green">{c.confirmed} ready</span>}
        {c.error > 0 && <span class="chip red">{plural(c.error, "error")}</span>}
        {from.map((s) => <span key={s!.id} class="chip muted">{s!.name}</span>)}
        <span class="chip muted">{done}/{c.total} done</span>
      </div>
      <div class="progress"><i style={{ width: `${c.total ? (done / c.total) * 100 : 0}%` }} /></div>
    </a>
  );
}
