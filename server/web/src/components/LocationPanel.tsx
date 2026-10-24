import { useEffect, useState } from "preact/hooks";
import { api, type Place, type SessionSummary } from "../api";
import { attempt, useDebounced } from "../util";

export function LocationPanel({ session, onChange }: { session: SessionSummary; onChange: () => void }) {
  const waiting = session.counts.awaiting_location;
  const [editing, setEditing] = useState(false);
  const open = editing || (waiting > 0 && !session.location);

  const save = async (place: { latitude: number; longitude: number; label?: string }) => {
    const result = await attempt(() => api.updateSession(session.id, { location: place }),
      (r) => (r.queued ? `Location set; ${r.queued} photos queued for identification` : "Location set"));
    if (result) {
      setEditing(false);
      onChange();
    }
  };

  return (
    <section class={`location-panel ${waiting > 0 ? "attention" : ""}`}>
      <div class="location-current">
        <span class="icon">📍</span>
        <div>
          {session.location ? (
            <>
              <b>{session.location.label}</b>
              <small>
                Used for photos without GPS · {session.location.latitude.toFixed(4)}, {session.location.longitude.toFixed(4)}
              </small>
            </>
          ) : waiting > 0 ? (
            <>
              <b>{waiting} photos have no GPS. Where were they taken?</b>
              <small>A location makes identification noticeably more accurate, and decides the country/state/city folder.</small>
            </>
          ) : (
            <b class="muted">All photos in this session carry their own GPS location.</b>
          )}
        </div>
        <div class="location-actions">
          {session.location && !editing && <button onClick={() => setEditing(true)}>Change</button>}
          {session.location && (
            <button onClick={() => attempt(() => api.updateSession(session.id, { clear_location: true }), "Location removed").then(onChange)}>
              Remove
            </button>
          )}
          {!session.location && !editing && waiting === 0 && <button onClick={() => setEditing(true)}>Set location</button>}
          {waiting > 0 && !session.analyse_without_location && (
            <button
              onClick={() =>
                attempt(() => api.updateSession(session.id, { analyse_without_location: true }), (r) => `${r.queued} photos queued without a location`).then(onChange)
              }
              title="The model still works without a location, just less accurately"
            >
              Identify without location
            </button>
          )}
        </div>
      </div>
      {open && <PlacePicker onPick={save} onCancel={session.location || waiting === 0 ? () => setEditing(false) : undefined} />}
    </section>
  );
}

const COORDINATES = /^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$/;

export function PlacePicker({ onPick, onCancel }: { onPick: (place: { latitude: number; longitude: number; label?: string }) => void; onCancel?: () => void }) {
  const [text, setText] = useState("");
  const [results, setResults] = useState<Place[]>([]);
  const [coordinatePlace, setCoordinatePlace] = useState<{ latitude: number; longitude: number; place: Place | null } | null>(null);
  const query = useDebounced(text, 250);

  useEffect(() => {
    const match = COORDINATES.exec(query);
    if (match) {
      const latitude = Number(match[1]);
      const longitude = Number(match[2]);
      setResults([]);
      if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
        setCoordinatePlace(null);
        return;
      }
      api.reversePlace(latitude, longitude).then((place) => setCoordinatePlace({ latitude, longitude, place }), () => setCoordinatePlace({ latitude, longitude, place: null }));
      return;
    }
    setCoordinatePlace(null);
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    api.searchPlaces(query).then(setResults, () => setResults([]));
  }, [query]);

  return (
    <div class="place-picker">
      <input
        autoFocus
        value={text}
        onInput={(e) => setText(e.currentTarget.value)}
        placeholder="Search a town or city (e.g. “Ooty” or “Ooty, India”), or paste coordinates like 11.4102, 76.6950"
      />
      {coordinatePlace && (
        <button class="place-option" onClick={() => onPick({ latitude: coordinatePlace.latitude, longitude: coordinatePlace.longitude })}>
          <b>
            {coordinatePlace.latitude.toFixed(5)}, {coordinatePlace.longitude.toFixed(5)}
          </b>
          <small>
            {coordinatePlace.place ? `Near ${coordinatePlace.place.label} (${coordinatePlace.place.distance_km} km)` : "Unknown area"}
          </small>
        </button>
      )}
      {results.map((place) => (
        <button key={`${place.latitude},${place.longitude}`} class="place-option"
                onClick={() => onPick({ latitude: place.latitude, longitude: place.longitude, label: place.label })}>
          <b>{place.city}</b>
          <small>
            {[place.state, place.country].filter(Boolean).join(", ")}
            {place.population ? ` · pop. ${place.population.toLocaleString()}` : ""}
          </small>
        </button>
      ))}
      <p class="hint">
        Tip: for a reserve or trail without a nearby town, drop a pin in any map app and paste its coordinates here.
        {onCancel && (
          <>
            {" "}
            <button class="link" onClick={onCancel}>Cancel</button>
          </>
        )}
      </p>
    </div>
  );
}
