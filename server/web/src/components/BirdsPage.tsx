import { useState } from "preact/hooks";
import { api, thumbUrl, type Bird, type ShotSummary } from "../api";
import { formatDate, href, navigate, percent, plural, replaceRoute, useLoader, type Route } from "../util";
import { NewBadge } from "./NewBadge";
import { ShotViewer } from "./ShotViewer";

type Sort = "name" | "photos" | "recent" | "unconfirmed";
const SORT: Record<Sort, string> = { name: "Name", photos: "Most photos", recent: "Seen most recently", unconfirmed: "Most to confirm" };
const SORTERS: Record<Sort, (a: Bird, b: Bird) => number> = {
  name: (a, b) => a.species.name.localeCompare(b.species.name),
  photos: (a, b) => b.shots - a.shots,
  recent: (a, b) => (b.last_seen ?? "").localeCompare(a.last_seen ?? ""),
  unconfirmed: (a, b) => b.unconfirmed - a.unconfirmed,
};

const placeLabel = (p: Bird["places"][number]) => [p.city, p.state, p.country].filter(Boolean).join(", ") || "Place not known yet";

export function BirdsPage({ route, onChange }: { route: Route; onChange: () => void }) {
  const birds = useLoader(() => api.birds(), [], 30000);
  const params = route.params;
  const speciesId = params.get("species");

  if (birds.error) return <div class="empty error">Could not load the birds: {birds.error}</div>;
  if (!birds.data) return <div class="empty">Loading…</div>;
  if (speciesId) {
    const bird = birds.data.birds.find((b) => b.species.id === speciesId);
    return <SpeciesView speciesId={speciesId} bird={bird} route={route} onChange={() => { birds.reload(); onChange(); }} />;
  }

  const view = params.get("view") === "new" ? "new" : "all";
  const sort = (params.get("sort") as Sort | null) ?? "name";
  const q = params.get("q") ?? "";
  const country = params.get("country") ?? "";
  const set = (changes: Record<string, string | undefined>) => {
    const next = { view, sort, q, country, ...changes };
    replaceRoute("birds", { view: next.view === "all" ? undefined : next.view, sort: next.sort === "name" ? undefined : next.sort,
                            q: next.q || undefined, country: next.country || undefined });
  };

  const all = birds.data.birds;
  const lifeList = birds.data.life_list_loaded;
  const newBirds = all.filter((b) => b.species.new);
  const countries = [...new Set(all.flatMap((b) => b.places.map((p) => p.country)).filter(Boolean) as string[])].sort();
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = (view === "new" ? newBirds : all)
    .filter((b) => !country || b.places.some((p) => p.country === country))
    .filter((b) => words.every((w) => [b.species.name, b.species.scientific_name, b.species.family, ...b.places.map(placeLabel)]
      .join(" ").toLowerCase().includes(w)))
    .sort((a, b) => SORTERS[sort](a, b) || SORTERS.name(a, b));
  const confirmedSpecies = all.filter((b) => b.confirmed > 0).length;

  return (
    <div class="page">
      <h1>Birds</h1>
      <p class="hint">
        Every species identified so far: {plural(all.length, "species", "species")}, {confirmedSpecies} of them confirmed by you on at least one
        photo. Suggestions you haven't checked yet are included; open a species to see its photos and confirm them.
      </p>
      <div class="tabs">
        <button class={`tab ${view === "all" ? "active" : ""}`} onClick={() => set({ view: "all" })}>All birds <span>{all.length}</span></button>
        <button class={`tab ${view === "new" ? "active" : ""}`} onClick={() => set({ view: "new" })} disabled={!lifeList}
                title={lifeList ? "Species that aren't on your eBird life list" : "Upload your eBird life list in Settings first"}>
          New birds <span>{lifeList ? newBirds.length : "–"}</span>
        </button>
      </div>
      {!lifeList && (
        <p class="hint">Upload your eBird life list in <a href={href("settings")}>Settings</a> to see which birds are new to you.</p>
      )}
      {view === "new" && lifeList && (
        <p class="hint">Not on your life list yet. Confirm a photo to be sure, as an unconfirmed suggestion can be wrong.</p>
      )}
      <div class="session-filters">
        <input type="search" placeholder="Search species, family or place" value={q} aria-label="Search birds"
               onInput={(e) => set({ q: e.currentTarget.value })} />
        <select value={country} onChange={(e) => set({ country: e.currentTarget.value })} aria-label="Country">
          <option value="">All countries</option>
          {countries.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={sort} onChange={(e) => set({ sort: e.currentTarget.value })} aria-label="Sort">
          {(Object.keys(SORT) as Sort[]).map((k) => <option key={k} value={k}>{SORT[k]}</option>)}
        </select>
        <span class="muted">{plural(shown.length, "species", "species")}</span>
      </div>

      {shown.length === 0 ? (
        <div class="empty">{all.length ? "No birds match." : "No birds identified yet."}</div>
      ) : (
        <div class="bird-list">
          {shown.map((bird) => <BirdCard key={bird.species.id} bird={bird} />)}
        </div>
      )}
    </div>
  );
}

function BirdCard({ bird }: { bird: Bird }) {
  const places = bird.places.slice(0, 3);
  return (
    <a class={`bird-card ${bird.species.new ? "is-new" : ""}`} href={href("birds", { species: bird.species.id })}>
      <div class="bird-thumb">
        {bird.photo_shot_id ? <img loading="lazy" src={thumbUrl(bird.photo_shot_id)} alt="" /> : <div class="placeholder">No preview</div>}
      </div>
      <div class="bird-body">
        <div class="bird-name"><b>{bird.species.name}</b> <NewBadge species={bird.species} /></div>
        <div class="muted"><i>{bird.species.scientific_name}</i></div>
        <div class="session-counts">
          <span class="chip">{plural(bird.shots, "photo")}</span>
          {bird.confirmed > 0 && <span class="chip green">{bird.confirmed} confirmed</span>}
          {bird.unconfirmed > 0 && <span class="chip amber">{bird.unconfirmed} to confirm</span>}
        </div>
        <div class="bird-places">
          📍 {places.map(placeLabel).join(" · ")}
          {bird.places.length > 3 && <span class="muted"> and {plural(bird.places.length - 3, "more place")}</span>}
        </div>
        {bird.last_seen && <small>Last seen {formatDate(bird.last_seen.slice(0, 10), false)}</small>}
      </div>
    </a>
  );
}

const PAGE_SIZE = 120;

function SpeciesView({ speciesId, bird, route, onChange }: { speciesId: string; bird: Bird | undefined; route: Route; onChange: () => void }) {
  const unconfirmed = route.params.get("unconfirmed") === "1";
  const openShot = route.params.get("shot") ? Number(route.params.get("shot")) : null;
  const [limit, setLimit] = useState(PAGE_SIZE);
  const shots = useLoader(() => api.shots({ species_id: speciesId, unconfirmed, newest_first: true, limit }), [speciesId, unconfirmed, limit]);
  const go = (changes: Record<string, string | number | undefined>) =>
    navigate("birds", { species: speciesId, unconfirmed: unconfirmed ? "1" : undefined, shot: openShot ?? undefined, ...changes });

  const items = shots.data?.items ?? [];
  return (
    <div class="page">
      <a class="back" href={href("birds")}>← All birds</a>
      <div class="session-header">
        <div>
          <h1>{bird?.species.name ?? speciesId} <NewBadge species={bird?.species} /></h1>
          {bird && <div class="muted"><i>{bird.species.scientific_name}</i>{bird.species.family && ` · ${bird.species.family}`}</div>}
        </div>
        {bird && (
          <div class="session-counts">
            <span class="chip">{plural(bird.shots, "photo")}</span>
            {bird.confirmed > 0 && <span class="chip green">{bird.confirmed} confirmed</span>}
            {bird.unconfirmed > 0 && <span class="chip amber">{bird.unconfirmed} to confirm</span>}
          </div>
        )}
      </div>
      {bird && (
        <div class="bird-place-list">
          {bird.places.map((p) => <span key={placeLabel(p)} class="chip">📍 {placeLabel(p)} · {p.shots}</span>)}
          {bird.first_seen && <span class="muted">First seen {formatDate(bird.first_seen.slice(0, 10), false)}</span>}
        </div>
      )}
      <div class="toolbar">
        <label class="check">
          <input type="checkbox" checked={unconfirmed} onChange={(e) => go({ unconfirmed: e.currentTarget.checked ? "1" : undefined, shot: undefined })} />
          Only photos still to confirm
        </label>
        {shots.data && <span class="muted">{plural(shots.data.total, "photo")}</span>}
      </div>

      {shots.error ? (
        <div class="empty error">{shots.error}</div>
      ) : !shots.data ? (
        <div class="empty">Loading…</div>
      ) : items.length === 0 ? (
        <div class="empty">{unconfirmed ? "Every photo of this bird is confirmed." : "No photos of this bird."}</div>
      ) : (
        <>
          <div class="grid">
            {items.map((shot) => <BirdShotCard key={shot.id} shot={shot} speciesId={speciesId} onOpen={() => go({ shot: shot.id })} />)}
          </div>
          {shots.data.total > items.length && (
            <div class="more"><button onClick={() => setLimit(limit + PAGE_SIZE)}>Show more ({shots.data.total - items.length} left)</button></div>
          )}
        </>
      )}

      {openShot !== null && (
        <ShotViewer
          shotId={openShot}
          shotIds={items.map((s) => s.id)}
          onNavigate={(shotId) => go({ shot: shotId })}
          onClose={() => { go({ shot: undefined }); shots.reload(); }}
          onChanged={onChange}
        />
      )}
    </div>
  );
}

const STATUS: Record<string, [string, string]> = {
  review: ["To confirm", "amber"], confirmed: ["Confirmed", "green"], published: ["Published", "green"],
  pending: ["Being identified", ""], awaiting_location: ["Needs a location", "amber"],
};

function BirdShotCard({ shot, speciesId, onOpen }: { shot: ShotSummary; speciesId: string; onOpen: () => void }) {
  const place = [shot.location.city, shot.location.state, shot.location.country].filter(Boolean).join(", ");
  const [label, tone] = STATUS[shot.status] ?? [shot.status, ""];
  const others = shot.species.filter((s) => s.id !== speciesId);
  return (
    <div class="card">
      <button class="thumb" onClick={onOpen}>
        {shot.preview_ready ? <img loading="lazy" src={thumbUrl(shot.id)} alt="" /> : <div class="placeholder">Preparing preview…</div>}
        {shot.bird_count > 1 && <span class="count">{shot.bird_count} birds</span>}
      </button>
      <div class="card-body" onClick={onOpen}>
        <div class="meta">
          <span class={`chip ${tone}`}>{label}</span>
          {shot.min_confidence !== null && <span class="conf">{percent(shot.min_confidence)}</span>}
        </div>
        <div class="meta"><span>{shot.captured_at ? formatDate(shot.captured_at.slice(0, 10), false) : "Undated"}</span></div>
        <div class="meta"><span>📍 {place || "Place not known yet"}</span></div>
        {others.length > 0 && <div class="meta muted">Also: {others.map((s) => s.name).join(", ")}</div>}
      </div>
    </div>
  );
}
