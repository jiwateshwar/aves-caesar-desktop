from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from .models import Species


@dataclass(frozen=True)
class SpeciesEntry:
    species_id: str
    scientific_name: str
    common_name: str
    family: str | None
    order: str | None
    names: dict[str, str]

    def name(self, language: str) -> str:
        return self.names.get(language) or self.common_name or self.scientific_name


class SpeciesCatalog:
    """The 10,859 species the classifier knows, loaded from the model's species mapping."""

    _cache: dict[Path, "SpeciesCatalog"] = {}
    _lock = threading.Lock()

    def __init__(self, labels_path: Path):
        self.entries: dict[str, SpeciesEntry] = {}
        with labels_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                names = {key[5:]: value for key, value in row.items() if key.startswith("name_") and value}
                self.entries[row["species_id"]] = SpeciesEntry(
                    species_id=row["species_id"],
                    scientific_name=row.get("scientific_name") or "",
                    common_name=names.get("en", ""),
                    family=row.get("family_name") or None,
                    order=row.get("order_name") or None,
                    names=names,
                )
        self._search_index: dict[str, list[tuple[str, str]]] = {}

    @classmethod
    def load(cls, labels_path: Path) -> "SpeciesCatalog":
        with cls._lock:
            if labels_path not in cls._cache:
                cls._cache[labels_path] = SpeciesCatalog(labels_path)
            return cls._cache[labels_path]

    def get(self, species_id: str | None) -> SpeciesEntry | None:
        return self.entries.get(species_id) if species_id is not None else None

    def languages(self) -> list[str]:
        first = next(iter(self.entries.values()), None)
        return sorted(first.names) if first else ["en"]

    def search(self, query: str, language: str = "en", limit: int = 20) -> list[SpeciesEntry]:
        needle = query.strip().casefold()
        if len(needle) < 2:
            return []
        ranked = []
        for species_id, keys in self._index(language):
            best = None
            for key in keys:
                if key == needle:
                    best = 0
                elif key.startswith(needle):
                    best = 1 if best is None else min(best, 1)
                elif needle in key:
                    best = 2 if best is None else min(best, 2)
            if best is not None:
                ranked.append((best, self.entries[species_id].name(language), species_id))
        ranked.sort()
        return [self.entries[species_id] for _rank, _name, species_id in ranked[:limit]]

    def _index(self, language: str):
        if language not in self._search_index:
            self._search_index[language] = [
                (entry.species_id, tuple({entry.name(language).casefold(), entry.common_name.casefold(), entry.scientific_name.casefold()}))
                for entry in self.entries.values()
            ]
        return self._search_index[language]

    def seed(self, db: OrmSession) -> int:
        """Keeps the species table in step with the model's mapping file."""
        existing = db.scalar(select(func.count()).select_from(Species)) or 0
        if existing == len(self.entries):
            return 0
        known = set(db.scalars(select(Species.species_id)))
        added = 0
        for entry in self.entries.values():
            if entry.species_id in known:
                continue
            db.add(Species(species_id=entry.species_id, scientific_name=entry.scientific_name, common_name=entry.common_name,
                           family=entry.family, order=entry.order, names=entry.names))
            added += 1
        return added
