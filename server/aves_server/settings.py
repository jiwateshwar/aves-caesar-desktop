from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from .models import Setting


PATTERN_FIELDS = {"country", "state", "city", "date", "year", "month", "filetype"}


@dataclass(frozen=True)
class WorkflowSettings:
    confidence_threshold: float = 0.6          # top-1 below this lands in "Low confidence"
    publish_pattern: str = "{country}/{state}/{city}/{date}/{filetype}"
    unknown_placeholder: str = "Unknown"       # used when country/state/city can't be determined
    auto_scan: bool = True                     # watch the dumpsite for new files
    poll_interval_seconds: int = 30
    settle_seconds: int = 30                   # a file must stop changing for this long before ingest
    species_language: str = "en"
    city_min_population: int = 0               # ignore smaller places when naming the city

    def validated(self) -> "WorkflowSettings":
        if not 0 < self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if self.poll_interval_seconds < 5:
            raise ValueError("poll_interval_seconds must be at least 5")
        if self.settle_seconds < 0:
            raise ValueError("settle_seconds can't be negative")
        if self.city_min_population < 0:
            raise ValueError("city_min_population can't be negative")
        validate_pattern(self.publish_pattern)
        if not self.unknown_placeholder.strip() or any(c in self.unknown_placeholder for c in "/\\"):
            raise ValueError("unknown_placeholder must be a non-empty folder name")
        return self


def validate_pattern(pattern: str) -> None:
    import string

    names = [name for _text, name, _spec, _conv in string.Formatter().parse(pattern) if name is not None]
    unknown = set(names) - PATTERN_FIELDS
    if unknown:
        raise ValueError(f"Unknown publish pattern field(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(PATTERN_FIELDS))}")
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise ValueError("publish_pattern must be a relative path")


class SettingsStore:
    @staticmethod
    def load(db: OrmSession) -> WorkflowSettings:
        stored = {row.key: row.value for row in db.query(Setting).all()}
        known = {field.name for field in fields(WorkflowSettings)}
        return WorkflowSettings(**{key: value for key, value in stored.items() if key in known})

    @staticmethod
    def update(db: OrmSession, changes: dict[str, Any]) -> WorkflowSettings:
        current = asdict(SettingsStore.load(db))
        unknown = set(changes) - set(current)
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            expected = type(current[key])
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ValueError(f"{key} must be of type {expected.__name__}")
            current[key] = value
        updated = WorkflowSettings(**current).validated()
        for key in changes:
            row = db.get(Setting, key) or Setting(key=key)
            row.value = getattr(updated, key)
            db.add(row)
        return updated
