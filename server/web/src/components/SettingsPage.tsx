import { useEffect, useState } from "preact/hooks";
import { api, type Dumpsite, type Dumpsites, type LifeList, type Settings } from "../api";
import { attempt, formatDate, plural, toast, useLoader } from "../util";

export function SettingsPage() {
  const loaded = useLoader(() => api.settings(), []);
  const [values, setValues] = useState<Settings | null>(null);
  useEffect(() => {
    if (loaded.data) setValues(loaded.data.values);
  }, [loaded.data]);

  if (loaded.error) return <div class="empty error">{loaded.error}</div>;
  if (!values || !loaded.data) return <div class="empty">Loading…</div>;
  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => setValues({ ...values, [key]: value });
  const defaults = loaded.data.defaults;

  return (
    <>
    <form
      class="page narrow settings"
      onSubmit={async (e) => {
        e.preventDefault();
        const changed = Object.fromEntries(Object.entries(values).filter(([k, v]) => loaded.data!.values[k as keyof Settings] !== v));
        if (!Object.keys(changed).length) return;
        const result = await attempt(() => api.updateSettings(changed), "Settings saved");
        if (result) loaded.setData(result);
      }}
    >
      <h1>Settings</h1>

      <fieldset>
        <legend>Identification</legend>
        <label>
          Low-confidence threshold: <b>{Math.round(values.confidence_threshold * 100)}%</b>
          <input type="range" min="0.05" max="0.99" step="0.01" value={values.confidence_threshold}
                 onInput={(e) => set("confidence_threshold", Number(e.currentTarget.value))} />
          <small>Photos where any bird's best guess is below this go to the “Low confidence” tab. Default {Math.round(defaults.confidence_threshold * 100)}%.</small>
        </label>
        <label>
          Species names in
          <select value={values.species_language} onChange={(e) => set("species_language", e.currentTarget.value)}>
            {loaded.data.languages.map((language) => <option key={language} value={language}>{language}</option>)}
          </select>
        </label>
      </fieldset>

      <fieldset>
        <legend>Published library</legend>
        <label>
          Folder pattern
          <input value={values.publish_pattern} onInput={(e) => set("publish_pattern", e.currentTarget.value)} />
          <small>
            Fields: <code>{"{country}"}</code> <code>{"{state}"}</code> <code>{"{city}"}</code> <code>{"{date}"}</code> <code>{"{year}"}</code>{" "}
            <code>{"{month}"}</code> <code>{"{filetype}"}</code>. Default <code>{defaults.publish_pattern}</code>. Filenames are never changed.
          </small>
        </label>
        <label>
          Folder name when the place is unknown
          <input value={values.unknown_placeholder} onInput={(e) => set("unknown_placeholder", e.currentTarget.value)} />
        </label>
        <label>
          Smallest place to use as the city
          <input type="number" min="0" step="1000" value={values.city_min_population}
                 onInput={(e) => set("city_min_population", Number(e.currentTarget.value))} />
          <small>Population. 0 uses the nearest named place; e.g. 15000 skips villages and suburbs in favour of the nearest town.</small>
        </label>
      </fieldset>

      <fieldset>
        <legend>Dumpsite watching</legend>
        <label class="check">
          <input type="checkbox" checked={values.auto_scan} onChange={(e) => set("auto_scan", e.currentTarget.checked)} />
          Pick up new files automatically
        </label>
        <label>
          Check every (seconds)
          <input type="number" min="5" value={values.poll_interval_seconds} onInput={(e) => set("poll_interval_seconds", Number(e.currentTarget.value))} />
        </label>
        <label>
          Wait until a file has stopped changing for (seconds)
          <input type="number" min="0" value={values.settle_seconds} onInput={(e) => set("settle_seconds", Number(e.currentTarget.value))} />
          <small>Prevents half-copied files from being read while a card is still being transferred.</small>
        </label>
      </fieldset>

      <div class="actions">
        <button class="primary">Save settings</button>
      </div>
    </form>
    <DumpsitesPanel />
    <LifeListPanel />
    </>
  );
}

function LifeListPanel() {
  const lifeList = useLoader(() => api.lifeList(), []);
  const [busy, setBusy] = useState(false);
  const data: LifeList | null = lifeList.data;

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    const result = await attempt(() => api.uploadLifeList(file),
      (r) => `Life list loaded: ${plural(r.species ?? 0, "species", "species")}` + (r.unmatched?.length ? `, ${r.unmatched.length} not recognised` : ""));
    setBusy(false);
    if (result) lifeList.setData(result);
  };

  return (
    <section class="page narrow settings">
      <fieldset>
        <legend>eBird life list</legend>
        <p class="hint">
          Species that aren't on your life list are marked <span class="new-badge">(New)</span> when you review photos. Download your life list
          from eBird as a CSV file and upload it here; each upload replaces the previous one.
        </p>
        {data?.loaded ? (
          <div class="life-list-summary">
            <b>{plural(data.species ?? 0, "species", "species")}</b> from <code>{data.filename}</code>
            <small class="muted"> · uploaded {data.imported_at ? formatDate(data.imported_at.slice(0, 10), false) : ""}</small>
            {data.unmatched && data.unmatched.length > 0 && (
              <details>
                <summary class="warn">{plural(data.unmatched.length, "species", "species")} not recognised by the identification model</summary>
                <p class="hint">
                  Usually recent taxonomy splits. Photos of these birds are named after the model's species, so they may still show as (New).
                </p>
                <ul>
                  {data.unmatched.map((u) => (
                    <li key={u.scientific_name}>{u.common_name} <i class="muted">{u.scientific_name}</i></li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        ) : (
          <p class="muted">No life list uploaded yet, so nothing is marked as new.</p>
        )}
        <div class="actions">
          <label class="button-like">
            {busy ? "Uploading…" : data?.loaded ? "Upload a newer life list" : "Upload life list (CSV)"}
            <input type="file" accept=".csv,text/csv" hidden disabled={busy}
                   onChange={(e) => { upload(e.currentTarget.files?.[0]); e.currentTarget.value = ""; }} />
          </label>
          {data?.loaded && (
            <button type="button" class="danger" onClick={async () => {
              if (!window.confirm("Remove the life list? Nothing will be marked as new until you upload one again.")) return;
              const result = await attempt(() => api.clearLifeList(), "Life list removed");
              if (result) lifeList.setData(result);
            }}>Remove</button>
          )}
        </div>
      </fieldset>
    </section>
  );
}

function DumpsitesPanel() {
  const loaded = useLoader(() => api.dumpsites(), []);
  const data: Dumpsites | null = loaded.data;
  const [names, setNames] = useState<Record<string, string>>({});
  const [readOnly, setReadOnly] = useState<Record<string, boolean>>({});
  // While the stack redeploys, the app restarts: wait for it to come back with the change in place.
  const [waiting, setWaiting] = useState<{ what: string; mountedName?: string } | null>(null);

  useEffect(() => {
    if (!waiting) return;
    let wentDown = false;
    const started = Date.now();
    const timer = window.setInterval(async () => {
      try {
        const result = await api.dumpsites();
        const mounted = waiting.mountedName ? result.dumpsites.some((d) => d.name === waiting.mountedName && d.mounted) : false;
        if (result.stack.error || mounted || (wentDown && !result.stack.busy) || Date.now() - started > 240_000) {
          loaded.setData(result);
          setWaiting(null);
          if (result.stack.error) toast(result.stack.error, "error");
          else if (mounted) toast(`${waiting.mountedName} is mounted; scanning it now`);
        }
      } catch {
        wentDown = true; // restarting
      }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [waiting]);

  if (loaded.error) return <section class="page narrow settings"><div class="empty error">{loaded.error}</div></section>;
  if (!data) return null;

  const run = async (action: () => Promise<Dumpsites>, message: string, mountedName?: string) => {
    const result = await attempt(action, message);
    if (result) {
      loaded.setData(result);
      if (result.restarting) setWaiting({ what: message, mountedName });
    }
    return result;
  };
  const free = data.available_slots;
  const stack = data.stack;
  const busy = waiting !== null || stack.busy !== null;
  const defaultName = (path: string) => path.split("/").filter(Boolean).pop() ?? "";

  return (
    <section class="page narrow settings">
      <fieldset>
        <legend>Dumpsites</legend>
        <p class="hint">
          Every dumpsite is watched for new photos, and they all go through the same review and publish into the same library.
          Folders from extra dumpsites appear under the dumpsite's name in a session's folder list. Publishing moves photos out of a
          dumpsite into the library; tick <b>Read-only</b> to copy them instead and leave the dumpsite exactly as it is.
        </p>
        {busy && (
          <p class="notice">
            <b>{waiting?.what ?? stack.busy}</b>. Featherbase is restarting to mount the change; this takes about a minute and the
            list below updates by itself.
          </p>
        )}
        {!busy && stack.error && <p class="warn">{stack.error}</p>}
        <table class="dumpsites">
          <tbody>
            {data.dumpsites.map((site) => <DumpsiteRow key={site.id} site={site} run={run} />)}
          </tbody>
        </table>

        <h4>Add a dumpsite</h4>
        {free.length > 0 && (
          <ul class="slot-list">
            {free.map((slot) => (
              <li key={slot.slot}>
                <code>{slot.host_path}</code>{" "}
                {slot.mounted ? (
                  <AddForm placeholder={defaultName(slot.host_path)} value={names[`slot${slot.slot}`]} readOnly={!!readOnly[`slot${slot.slot}`]}
                           onName={(v) => setNames({ ...names, [`slot${slot.slot}`]: v })}
                           onReadOnly={(v) => setReadOnly({ ...readOnly, [`slot${slot.slot}`]: v })}
                           onAdd={async (name, ro) => !!(await run(() => api.addDumpsite(slot.slot, name, ro), `Added ${name}; scanning it now`))} />
                ) : (
                  <small class="warn">set in the stack but not mounted yet; redeploy the stack</small>
                )}
              </li>
            ))}
          </ul>
        )}
        {stack.self_service ? (
          stack.shares_error ? (
            <p class="warn">Couldn't list the NAS's shares: {stack.shares_error}</p>
          ) : (
            <>
              <p class="hint">SMB shares on this NAS. Adding one restarts Featherbase for about a minute to mount it.</p>
              <ul class="share-list">
                {stack.shares.map((share) => (
                  <li key={share.path} class={share.blocked ? "muted" : ""}>
                    <div><b>{share.name}</b> <code>{share.path}</code>{!share.enabled && <small class="muted"> (SMB sharing off)</small>}</div>
                    {share.blocked ? (
                      <small>{share.blocked}</small>
                    ) : (
                      <AddForm placeholder={share.name} value={names[share.path]} readOnly={!!readOnly[share.path]} disabled={busy}
                               onName={(v) => setNames({ ...names, [share.path]: v })}
                               onReadOnly={(v) => setReadOnly({ ...readOnly, [share.path]: v })}
                               onAdd={async (name, ro) => {
                                 if (!window.confirm(`Add ${share.path} as the dumpsite "${name}"${ro ? " (read-only)" : ""}?\n\n` +
                                   (ro ? "Published photos are copied; nothing in the share is changed." :
                                         "Published photos are MOVED out of this share into the library.") +
                                   "\n\nFeatherbase restarts for about a minute.")) return false;
                                 return !!(await run(() => api.addShare(share.path, name, ro), `Mounting ${name}`, name));
                               }} />
                    )}
                  </li>
                ))}
              </ul>
            </>
          )
        ) : free.length === 0 && (
          <p class="muted">
            No share is waiting to be added. Set one of{" "}
            {data.slot_numbers.map((n, i) => <span key={n}>{i ? ", " : ""}<code>DUMPSITE_{n}</code></span>)} in the Portainer stack's
            environment to the share's folder (e.g. <code>/mnt/MasterPool/Wildlife/Phone</code>) and redeploy; it then shows up here.
            To add shares right here instead, give the stack the Portainer and TrueNAS settings described in <code>stack.env.example</code>.
          </p>
        )}
      </fieldset>
    </section>
  );
}

function AddForm({ placeholder, value, readOnly, disabled, onName, onReadOnly, onAdd }: {
  placeholder: string; value: string | undefined; readOnly: boolean; disabled?: boolean;
  onName: (value: string) => void; onReadOnly: (value: boolean) => void; onAdd: (name: string, readOnly: boolean) => Promise<boolean>;
}) {
  return (
    <form class="inline-form" onSubmit={async (e) => {
      e.preventDefault();
      if (await onAdd((value ?? "").trim() || placeholder, readOnly)) onName("");
    }}>
      <input placeholder={placeholder} value={value ?? ""} onInput={(e) => onName(e.currentTarget.value)} aria-label="Name" disabled={disabled} />
      <label class="check" title="Publishing copies photos into the library and leaves the originals here">
        <input type="checkbox" checked={readOnly} onChange={(e) => onReadOnly(e.currentTarget.checked)} disabled={disabled} />
        Read-only
      </label>
      <button class="primary" disabled={disabled}>Add</button>
    </form>
  );
}

function DumpsiteRow({ site, run }: { site: Dumpsite; run: (action: () => Promise<Dumpsites>, message: string, mountedName?: string) => Promise<Dumpsites | undefined> }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(site.name);
  return (
    <tr class={site.enabled ? "" : "muted"}>
      <td>
        {editing ? (
          <form class="inline-form" onSubmit={async (e) => {
            e.preventDefault();
            if (await run(() => api.updateDumpsite(site.id, { name }), "Dumpsite renamed")) setEditing(false);
          }}>
            <input value={name} onInput={(e) => setName(e.currentTarget.value)} aria-label="Name" />
            <button class="primary">Save</button>
            <button type="button" onClick={() => { setEditing(false); setName(site.name); }}>Cancel</button>
          </form>
        ) : (
          <b>{site.name}</b>
        )}
        <br />
        <small class="muted">
          {site.main ? "Main dumpsite" : <code>{site.host_path ?? `slot ${site.slot}`}</code>}
          {" · "}{plural(site.files, "file", "files")} not yet published
          {!site.mounted && <span class="warn"> · not mounted</span>}
        </small>
        <label class="check read-only">
          <input type="checkbox" checked={site.read_only} onChange={(e) => {
            const checked = e.currentTarget.checked;
            run(() => api.updateDumpsite(site.id, { read_only: checked }),
              checked ? `${site.name} is read-only: photos are copied, never moved` : `${site.name}: photos are moved when published`);
          }} />
          Read-only <small class="muted">(copy when publishing, leave originals in place)</small>
        </label>
      </td>
      <td class="actions">
        {!site.main && !editing && <button type="button" onClick={() => setEditing(true)}>Rename</button>}
        {!site.main && (
          <button type="button" onClick={() => run(() => api.updateDumpsite(site.id, { enabled: !site.enabled }),
            site.enabled ? `${site.name} is no longer watched` : `${site.name} is watched again`)}>
            {site.enabled ? "Turn off" : "Turn on"}
          </button>
        )}
        {!site.main && site.files === 0 && (
          <button type="button" class="danger" onClick={() => {
            if (window.confirm(`Remove ${site.name}? Photos already published stay in the library; files in the share are not touched. The share is unmounted, which restarts Featherbase for about a minute.`))
              run(() => api.removeDumpsite(site.id), `${site.name} removed`);
          }}>Remove</button>
        )}
      </td>
    </tr>
  );
}
