import type { Species } from "../api";

/** "(New)" next to a species that isn't on the photographer's eBird life list. */
export function NewBadge({ species }: { species: Pick<Species, "new"> | null | undefined }) {
  if (!species?.new) return null;
  return <span class="new-badge" title="Not on your eBird life list yet">(New)</span>;
}
