"""The photographer's eBird life list, used to mark species they have never recorded as "(New)".

eBird exports it as CSV (columns include Category, Common Name, Scientific Name, Location, S/P, Date,
Countable). Only countable rows count, as on eBird itself: "duck sp.", slashes and hybrids are skipped.
Each species is matched to the model's taxonomy by scientific name, then by common name, which catches
genus changes such as Little Bunting (eBird Schoeniclus pusillus, model Emberiza pusilla).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from .catalog import SpeciesCatalog
from .models import LifeListEntry, LifeListImport


REQUIRED_COLUMNS = {"Common Name", "Scientific Name"}
MAX_BYTES = 10 * 1024 * 1024


class LifeListError(ValueError):
    pass


@dataclass
class LifeListRow:
    common_name: str
    scientific_name: str
    first_seen: date | None
    location: str | None
    region: str | None


@dataclass
class ImportSummary:
    filename: str
    species: int
    matched: int
    unmatched: list[dict] = field(default_factory=list)


def parse_ebird_csv(content: bytes) -> list[LifeListRow]:
    if len(content) > MAX_BYTES:
        raise LifeListError("That file is too large for an eBird life list")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_COLUMNS <= set(reader.fieldnames):
        raise LifeListError("This doesn't look like an eBird life list: it needs 'Common Name' and 'Scientific Name' columns")
    rows, seen = [], set()
    for record in reader:
        countable = (record.get("Countable") or "1").strip()
        category = (record.get("Category") or "species").strip().lower()
        if countable != "1" or category in ("spuh", "slash", "hybrid", "intergrade", "domestic"):
            continue
        scientific = " ".join((record.get("Scientific Name") or "").split()[:2])  # subspecies groups count as their species
        if not scientific or scientific.casefold() in seen:
            continue
        seen.add(scientific.casefold())
        rows.append(LifeListRow(
            common_name=(record.get("Common Name") or "").strip(),
            scientific_name=scientific,
            first_seen=_date(record.get("Date")),
            location=(record.get("Location") or "").strip() or None,
            region=(record.get("S/P") or "").strip() or None,
        ))
    if not rows:
        raise LifeListError("No countable species were found in this file")
    return rows


def import_life_list(db: OrmSession, filename: str, rows: list[LifeListRow], catalog: SpeciesCatalog) -> ImportSummary:
    """Replaces the stored life list with this one."""
    by_scientific = {entry.scientific_name.casefold(): entry.species_id for entry in catalog.entries.values()}
    by_common: dict[str, str] = {}
    for entry in catalog.entries.values():
        for name in {entry.common_name, *entry.names.values()}:
            if name:
                by_common.setdefault(name.casefold(), entry.species_id)

    db.execute(delete(LifeListEntry))
    db.execute(delete(LifeListImport))
    life_list = LifeListImport(filename=filename[:300], species_count=len(rows))
    summary = ImportSummary(filename=life_list.filename, species=len(rows), matched=0)
    for row in rows:
        species_id, matched_by = by_scientific.get(row.scientific_name.casefold()), "scientific"
        if species_id is None:
            species_id, matched_by = by_common.get(row.common_name.casefold()), "common"
        if species_id is None:
            matched_by = None
            summary.unmatched.append({"common_name": row.common_name, "scientific_name": row.scientific_name})
        else:
            summary.matched += 1
        life_list.entries.append(LifeListEntry(species_id=species_id, common_name=row.common_name[:200], scientific_name=row.scientific_name[:200],
                                               first_seen=row.first_seen, location=row.location[:300] if row.location else None,
                                               region=row.region[:40] if row.region else None, matched_by=matched_by))
    life_list.matched_count = summary.matched
    db.add(life_list)
    db.flush()
    return summary


def life_species_ids(db: OrmSession) -> set[str] | None:
    """Species ids on the life list, or None when no life list has been uploaded (then nothing is marked new)."""
    if db.scalar(select(LifeListImport.id).limit(1)) is None:
        return None
    return set(db.scalars(select(LifeListEntry.species_id).where(LifeListEntry.species_id.is_not(None))))


def _date(value: str | None) -> date | None:
    text = (value or "").strip()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
