import { useEffect, useMemo, useState } from "preact/hooks";
import { api, thumbUrl, type Category, type SessionSummary, type ShotSummary } from "../api";
import { attempt, CATEGORY_LABELS, CATEGORY_ORDER, formatDate, formatTime, navigate, percent, PRIMARY_CATEGORIES, useLoader, type Route } from "../util";
import { LocationPanel } from "./LocationPanel";
import { PlaceDialog } from "./PlaceDialog";
import { ShotViewer } from "./ShotViewer";

const PAGE_SIZE = 120;

export function SessionPage({ id, route, onChange }: { id: number; route: Route; onChange: () => void }) {
  const session = useLoader(() => api.session(id), [id], 10000);
  const settings = useLoader(() => api.settings(), []);
  const folder = route.params.get("folder") ?? undefined;
  const openShot = route.params.get("shot") ? Number(route.params.get("shot")) : null;
  const counts = folder ? session.data?.folders?.find((f) => f.folder === folder)?.counts : session.data?.counts;
  const category = (route.params.get("category") as Category | null) ?? (counts ? defaultCategory(counts) : null);

  const [limit, setLimit] = useState(PAGE_SIZE);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [placeDialog, setPlaceDialog] = useState(false);
  useEffect(() => {
    setLimit(PAGE_SIZE);
    setSelected(new Set());
  }, [id, category, folder]);

  const busy = category === "pending" || category === "awaiting_location";
  const shots = useLoader(
    () => (category ? api.shots({ session_id: id, folder, category, limit }) : Promise.resolve({ total: 0, items: [] })),
    [id, category, folder, limit],
    busy ? 5000 : 20000,
  );

  const refresh = () => {
    session.reload();
    shots.reload();
    onChange();
  };
  const go = (params: Record<string, string | number | undefined | null>) =>
    navigate(`sessions/${id}`, { category, folder, shot: undefined, ...params });

  const shotIds = useMemo(() => shots.data?.items.map((s) => s.id) ?? [], [shots.data]);
  if (session.error) return <div class="empty error">{session.error}</div>;
  if (!session.data || !counts) return <div class="empty">Loading…</div>;
  const s = session.data;
  const threshold = settings.data?.values.confidence_threshold ?? 0.6;

  const toggle = (shotId: number) =>
    setSelected((current) => {
      const next = new Set(current);
      next.has(shotId) ? next.delete(shotId) : next.add(shotId);
      return next;
    });
  const selectedIds = [...selected];

  return (
    <div class="page">
      <div class="session-header">
        <div>
          <a class="back" href="#/">← All sessions</a>
          <h1>{formatDate(s.date)}</h1>
          <TitleEditor session={s} onSaved={session.reload} />
        </div>
        <div class="header-actions">
          {counts.confirmed > 0 && (
            <button
              class="primary"
              onClick={() =>
                attempt(() => api.publish({ session_id: id }), `Publishing ${counts.confirmed} confirmed shots…`).then(() => window.setTimeout(refresh, 1500))
              }
            >
              Publish {counts.confirmed} confirmed
            </button>
          )}
        </div>
      </div>

      <LocationPanel session={s} onChange={refresh} />

      {s.folders && s.folders.length > 1 && (
        <div class="folders">
          <span class="muted">Folder</span>
          <button class={!folder ? "chip active" : "chip"} onClick={() => go({ folder: undefined })}>All ({s.counts.total})</button>
          {s.folders.map((f) => (
            <button key={f.folder} class={folder === f.folder ? "chip active" : "chip"} onClick={() => go({ folder: f.folder })}>
              {f.folder || "(top level)"} ({f.counts.total})
            </button>
          ))}
        </div>
      )}

      <div class="tabs">
        {CATEGORY_ORDER.filter((c) => PRIMARY_CATEGORIES.has(c) || counts[c] > 0).map((c) => (
          <button key={c} class={`tab ${c === category ? "active" : ""} cat-${c}`} onClick={() => go({ category: c })}>
            {CATEGORY_LABELS[c]} <span>{counts[c]}</span>
          </button>
        ))}
      </div>

      <div class="toolbar">
        {category === "review" && counts.review > 0 && (
          <button
            onClick={() =>
              attempt(() => api.confirmConfident(id, folder), (r) => `Confirmed ${r.confirmed} shots`).then(refresh)
            }
            title={`Confirms every shot where all birds are identified with at least ${percent(threshold)} confidence and nothing was changed`}
          >
            ✓ Confirm all confident
          </button>
        )}
        {shots.data && shots.data.items.length > 0 && (
          <button onClick={() => setSelected(selected.size === shotIds.length ? new Set() : new Set(shotIds))}>
            {selected.size === shotIds.length ? "Select none" : "Select all"}
          </button>
        )}
        {selected.size > 0 && (
          <>
            <span class="muted">{selected.size} selected</span>
            <button onClick={() => attempt(() => api.confirmMany(selectedIds), (r) => `Confirmed ${r.confirmed}${r.failed.length ? `, ${r.failed.length} need a species first` : ""}`).then(refresh)}>
              Confirm
            </button>
            <button onClick={() => setPlaceDialog(true)}>Set place…</button>
            <button onClick={() => attempt(() => api.reanalyse(selectedIds), "Queued for identification again").then(refresh)}>Identify again</button>
            {category === "confirmed" && (
              <button class="primary" onClick={() => attempt(() => api.publish({ shot_ids: selectedIds }), "Publishing…").then(() => window.setTimeout(refresh, 1500))}>
                Publish selected
              </button>
            )}
          </>
        )}
      </div>

      {category && <CategoryHint category={category} />}

      {shots.error ? (
        <div class="empty error">{shots.error}</div>
      ) : !shots.data ? (
        <div class="empty">Loading…</div>
      ) : shots.data.items.length === 0 ? (
        <div class="empty">Nothing here.</div>
      ) : (
        <>
          <div class="grid">
            {shots.data.items.map((shot) => (
              <ShotCard key={shot.id} shot={shot} threshold={threshold} selected={selected.has(shot.id)}
                        onSelect={() => toggle(shot.id)} onOpen={() => go({ shot: shot.id })} />
            ))}
          </div>
          {shots.data.total > shots.data.items.length && (
            <div class="more">
              <button onClick={() => setLimit(limit + PAGE_SIZE)}>Show more ({shots.data.total - shots.data.items.length} left)</button>
            </div>
          )}
        </>
      )}

      {openShot !== null && (
        <ShotViewer
          shotId={openShot}
          shotIds={shotIds}
          onNavigate={(shotId) => go({ shot: shotId })}
          onClose={() => {
            go({ shot: undefined });
            refresh();
          }}
          onChanged={() => {
            session.reload();
            onChange();
          }}
        />
      )}
      {placeDialog && (
        <PlaceDialog
          count={selected.size}
          onCancel={() => setPlaceDialog(false)}
          onSave={async (place) => {
            const result = await attempt(() => api.setPlace(selectedIds, place), (r) => `Updated the place of ${r.updated} shots`);
            if (result) {
              setPlaceDialog(false);
              refresh();
            }
          }}
        />
      )}
    </div>
  );
}

function defaultCategory(counts: Record<string, number>): Category {
  for (const c of ["review", "low_confidence", "no_bird", "awaiting_location", "conflicts", "pending", "confirmed", "published"] as Category[]) {
    if (counts[c] > 0) return c;
  }
  return "review";
}

function CategoryHint({ category }: { category: Category }) {
  const hints: Partial<Record<Category, string>> = {
    low_confidence: "The model wasn't sure about at least one bird in these photos. Check the alternatives or search for the right species.",
    no_bird: "No bird was detected. Tag a species by hand, confirm to publish without a species, or leave the photo in the dump.",
    awaiting_location: "These photos have no GPS. Set where they were taken above, or choose to identify them without a location.",
    conflicts: "A file with the same name already exists in the library (or is waiting elsewhere). Open the photo and add a suffix to its name.",
    confirmed: "Ready to be moved into the published library.",
    ignored: "These stay in the dumpsite and won't be published. Open one to bring it back for review.",
  };
  return hints[category] ? <p class="hint">{hints[category]}</p> : null;
}

function ShotCard({ shot, threshold, selected, onSelect, onOpen }: {
  shot: ShotSummary; threshold: number; selected: boolean; onSelect: () => void; onOpen: () => void;
}) {
  const types = [...new Set(shot.files.map((f) => f.file_type))];
  const low = shot.min_confidence !== null && shot.min_confidence < threshold;
  return (
    <div class={`card ${selected ? "selected" : ""}`}>
      <button class="thumb" onClick={onOpen}>
        {shot.preview_ready ? <img loading="lazy" src={thumbUrl(shot.id)} alt="" /> : <div class="placeholder">Preparing preview…</div>}
        {shot.bird_count > 1 && <span class="count">{shot.bird_count} birds</span>}
      </button>
      <label class="select">
        <input type="checkbox" checked={selected} onChange={onSelect} />
      </label>
      <div class="card-body" onClick={onOpen}>
        <div class="species">
          {shot.species.length ? (
            shot.species.map((sp, i) => <span key={i}>{sp.name}</span>)
          ) : shot.category === "awaiting_location" || shot.category === "pending" ? (
            <span class="muted">Not identified yet</span>
          ) : (
            <span class="muted">No bird</span>
          )}
        </div>
        <div class="meta">
          {shot.min_confidence !== null && <span class={low ? "conf low" : "conf"}>{percent(shot.min_confidence)}</span>}
          <span>{formatTime(shot.captured_at)}</span>
          {types.map((t) => <span key={t} class="type">{t}</span>)}
          {shot.has_conflict && <span class="type red" title="Filename conflict">name!</span>}
        </div>
      </div>
    </div>
  );
}

function TitleEditor({ session, onSaved }: { session: SessionSummary; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(session.title ?? "");
  if (!editing) {
    return (
      <button class="link" onClick={() => setEditing(true)}>
        {session.title ? `“${session.title}” ✎` : "+ Add a title"}
      </button>
    );
  }
  return (
    <form
      class="inline-form"
      onSubmit={async (e) => {
        e.preventDefault();
        await attempt(() => api.updateSession(session.id, { title: value }));
        setEditing(false);
        onSaved();
      }}
    >
      <input value={value} onInput={(e) => setValue(e.currentTarget.value)} placeholder="e.g. Nilgiris trip" autoFocus maxLength={200} />
      <button class="primary">Save</button>
      <button type="button" onClick={() => setEditing(false)}>Cancel</button>
    </form>
  );
}
