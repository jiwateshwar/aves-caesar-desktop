import { useState } from "preact/hooks";
import { plural } from "../util";

export function PlaceDialog({ count, onSave, onCancel }: {
  count: number;
  onSave: (place: { country?: string; state?: string; city?: string }) => void;
  onCancel: () => void;
}) {
  const [place, setPlace] = useState({ country: "", state: "", city: "" });
  const field = (key: keyof typeof place, label: string) => (
    <label>
      {label}
      <input value={place[key]} onInput={(e) => setPlace({ ...place, [key]: e.currentTarget.value })} placeholder="Leave empty to keep" />
    </label>
  );
  return (
    <div class="dialog-backdrop" onClick={onCancel}>
      <form
        class="dialog"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          const changes = Object.fromEntries(Object.entries(place).filter(([, v]) => v.trim())) as typeof place;
          onSave(changes);
        }}
      >
        <h3>Set the place of {plural(count, "shot")}</h3>
        <p class="hint">Overrides the folder names used when publishing, e.g. when GPS resolved to a neighbourhood instead of the city.</p>
        {field("country", "Country")}
        {field("state", "State / region")}
        {field("city", "City / town")}
        <div class="dialog-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button class="primary">Apply</button>
        </div>
      </form>
    </div>
  );
}
