import { useEffect, useState } from "preact/hooks";
import { api, type Species } from "../api";
import { useDebounced } from "../util";

export function SpeciesPicker({ placeholder, onPick, autoFocus }: { placeholder: string; onPick: (species: Species) => void; autoFocus?: boolean }) {
  const [text, setText] = useState("");
  const [results, setResults] = useState<Species[]>([]);
  const [active, setActive] = useState(0);
  const query = useDebounced(text, 200);

  useEffect(() => {
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    api.searchSpecies(query).then((found) => {
      setResults(found);
      setActive(0);
    }, () => setResults([]));
  }, [query]);

  const pick = (species: Species) => {
    onPick(species);
    setText("");
    setResults([]);
  };

  return (
    <div class="species-picker">
      <input
        value={text}
        autoFocus={autoFocus}
        placeholder={placeholder}
        onInput={(e) => setText(e.currentTarget.value)}
        onKeyDown={(e) => {
          e.stopPropagation(); // keep the viewer's shortcuts out of the search box
          if (e.key === "ArrowDown") setActive(Math.min(active + 1, results.length - 1));
          else if (e.key === "ArrowUp") setActive(Math.max(active - 1, 0));
          else if (e.key === "Enter" && results[active]) {
            e.preventDefault();
            pick(results[active]);
          } else if (e.key === "Escape") setText("");
        }}
      />
      {results.length > 0 && (
        <ul class="species-results">
          {results.map((species, index) => (
            <li key={species.id} class={index === active ? "active" : ""} onMouseDown={(e) => { e.preventDefault(); pick(species); }}>
              <b>{species.name}</b> <i>{species.scientific_name}</i>
              {species.family && <small>{species.family}</small>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
