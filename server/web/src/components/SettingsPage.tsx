import { useEffect, useState } from "preact/hooks";
import { api, type Settings } from "../api";
import { attempt, useLoader } from "../util";

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
  );
}
