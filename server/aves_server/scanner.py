from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from bird_desktop.media.exiftool import ExifTool

from .config import ServerConfig
from .db import Database
from .dumpsites import MAIN_ID, DumpsiteRoot, display_path, folder_label, roots
from .geocoding import Geocoder
from .locations import UNPUBLISHED, apply_shot_location, initial_status
from .media import MetadataReader, file_type_for, is_ignored_dir, is_ingestible, sha256_of
from .models import FileStatus, MediaFile, Session, Shot, ShotStatus
from .settings import SettingsStore


logger = logging.getLogger(__name__)

Key = tuple[int, str]  # (dumpsite id, path relative to that dumpsite)


@dataclass
class ScanReport:
    found: int = 0
    ingested: int = 0
    duplicates: int = 0
    waiting: int = 0      # still being copied, picked up by a later scan
    missing: int = 0
    restored: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**self.__dict__, "errors": self.errors[:50]}


@dataclass(frozen=True)
class _Candidate:
    path: Path
    size: int
    mtime: float


class DumpScanner:
    """Finds new photos in the dumpsite and records them. Never moves or deletes anything."""

    BATCH_SIZE = 50
    QUICK_SETTLE_SECONDS = 3.0

    def __init__(self, config: ServerConfig, database: Database, metadata: MetadataReader,
                 geocoder: Geocoder | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.database = database
        self.metadata = metadata
        self.geocoder = geocoder
        self.clock = clock
        self.sleep = sleep
        self._observed: dict[Key, tuple[int, float, float]] = {}  # (dumpsite, relpath) -> (size, mtime, first seen)

    def scan(self, quick: bool = False, progress: Callable[[int, int], None] | None = None) -> ScanReport:
        """quick=True is a manual scan: files that hold still for a few seconds are ingested right away."""
        report = ScanReport()
        with self.database.session() as db:
            settings = SettingsStore.load(db)
            sites = roots(db, self.config)
        main = sites[MAIN_ID]
        if main.enabled and not main.root.is_dir():
            raise RuntimeError(f"Dumpsite folder not found: {main.root}")

        found: dict[Key, _Candidate] = {}
        scanned: set[int] = set()
        for site in sites.values():
            if not site.enabled:
                continue
            if not site.mounted:
                report.errors.append(f"Dumpsite '{site.name}' is not mounted ({site.host_path or site.root}); skipped")
                continue
            scanned.add(site.id)
            found.update({(site.id, rel): candidate for rel, candidate in self._walk(site.root).items()})
        report.found = len(found)
        with self.database.session() as db:
            known = {(row.dumpsite_id, row.dump_relpath): row for row in db.execute(
                select(MediaFile.id, MediaFile.dumpsite_id, MediaFile.dump_relpath, MediaFile.status, MediaFile.size_bytes)
                .where(MediaFile.status.in_([FileStatus.in_dump, FileStatus.duplicate, FileStatus.missing]))
            )}
            self._reconcile_known(db, known, found, scanned, sites, report)

        ready = self._ready_files({key: c for key, c in found.items() if key not in known}, settings.settle_seconds, quick, report)
        for start in range(0, len(ready), self.BATCH_SIZE):
            batch = ready[start:start + self.BATCH_SIZE]
            try:
                self._ingest(batch, found, sites, report)
            except Exception as exc:  # one unreadable batch must not stop the rest
                logger.exception("Ingest batch failed")
                report.errors.append(f"{display_path(sites, *batch[0])}: {exc}")
            if progress:
                progress(min(start + len(batch), len(ready)), len(ready))
        return report

    def _walk(self, root: Path) -> dict[str, _Candidate]:
        found: dict[str, _Candidate] = {}
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not is_ignored_dir(name)]
            for name in filenames:
                path = Path(directory, name)
                if not is_ingestible(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found[PurePosixPath(path.relative_to(root)).as_posix()] = _Candidate(path, stat.st_size, stat.st_mtime)
        return found

    def _reconcile_known(self, db: OrmSession, known: dict, found: dict[Key, _Candidate], scanned: set[int],
                         sites: dict, report: ScanReport) -> None:
        for site_id in scanned:
            rows = [row for key, row in known.items() if key[0] == site_id]
            vanished = [row for row in rows if row.status == FileStatus.in_dump and (site_id, row.dump_relpath) not in found]
            if vanished and not any(key[0] == site_id for key in found):
                # An empty dumpsite with files still on record looks like an unmounted share, not mass deletion.
                logger.warning("Dumpsite '%s' looks empty but %d files are on record; skipping missing-file check",
                               sites[site_id].name, len(vanished))
                continue
            for row in vanished:
                db.get(MediaFile, row.id).status = FileStatus.missing
                report.missing += 1
            for row in rows:
                candidate = found.get((site_id, row.dump_relpath))
                if row.status == FileStatus.missing and candidate is not None and candidate.size == row.size_bytes:
                    db.get(MediaFile, row.id).status = FileStatus.in_dump
                    report.restored += 1

    def _ready_files(self, new: dict[Key, _Candidate], settle_seconds: int, quick: bool, report: ScanReport) -> list[Key]:
        now = self.clock()
        ready, unsettled = [], []
        for key in list(self._observed):
            if key not in new:
                del self._observed[key]
        for key, candidate in new.items():
            previous = self._observed.get(key)
            if candidate.size > 0 and previous and previous[:2] == (candidate.size, candidate.mtime):
                if now - previous[2] >= settle_seconds:
                    ready.append(key)
                    continue
            else:
                self._observed[key] = (candidate.size, candidate.mtime, now)
            unsettled.append(key)

        if quick and unsettled:
            self.sleep(self.QUICK_SETTLE_SECONDS)
            for key in unsettled:
                candidate = new[key]
                try:
                    stat = candidate.path.stat()
                except OSError:
                    continue
                if stat.st_size > 0 and (stat.st_size, stat.st_mtime) == (candidate.size, candidate.mtime):
                    ready.append(key)
            unsettled = [key for key in unsettled if key not in set(ready)]

        report.waiting = len(unsettled)
        return sorted(ready)

    def _ingest(self, batch: list[Key], found: dict[Key, _Candidate], sites: dict, report: ScanReport) -> None:
        metadata = self.metadata.read_many([found[key].path for key in batch])
        with self.database.session() as db:
            settings = SettingsStore.load(db)
            sessions: dict[date | None, Session] = {}
            for key in batch:
                site_id, rel = key
                candidate = found[key]
                path = candidate.path
                try:
                    digest = sha256_of(path)
                except OSError as exc:
                    report.errors.append(f"{display_path(sites, site_id, rel)}: {exc}")
                    continue
                meta = metadata[path]
                file = MediaFile(
                    original_filename=path.name, final_filename=path.name, filename_key=path.name.casefold(),
                    extension=path.suffix.lower(), file_type=file_type_for(path), is_raw=ExifTool.is_raw_file(path),
                    size_bytes=candidate.size, mtime=candidate.mtime, sha256=digest, dumpsite_id=site_id, dump_relpath=rel,
                    exif=meta.exif, camera_make=meta.camera_make, camera_model=meta.camera_model, lens_model=meta.lens_model,
                    focal_length=meta.focal_length, f_number=meta.f_number, exposure_time=meta.exposure_time,
                    iso=meta.iso, width=meta.width, height=meta.height,
                )
                original = db.scalar(select(MediaFile).where(MediaFile.sha256 == digest, MediaFile.status.in_([FileStatus.in_dump, FileStatus.published])).limit(1))
                if original is not None:
                    file.status, file.duplicate_of_id = FileStatus.duplicate, original.id
                    db.add(file)
                    db.flush()
                    report.duplicates += 1
                    continue

                captured_at, source = (meta.captured_at, "exif") if meta.captured_at else (datetime.fromtimestamp(candidate.mtime), "file_mtime")
                shot = self._shot_for(db, sites[site_id], rel, captured_at, source, meta, sessions, settings)
                file.shot = shot
                db.add(file)
                db.flush()
                report.ingested += 1

    def _shot_for(self, db: OrmSession, site: DumpsiteRoot, rel: str, captured_at: datetime, source: str, meta, sessions: dict, settings) -> Shot:
        rel_path = PurePosixPath(rel)
        folder = rel_path.parent.as_posix() if rel_path.parent.as_posix() != "." else ""
        # The main dumpsite keeps the original key format, so shots ingested before multiple dumpsites still match.
        prefix = "" if site.id == MAIN_ID else f"@{site.id}:"
        group_key = f"{prefix}{folder}|{rel_path.stem.casefold()}|{captured_at.replace(microsecond=0).isoformat()}"
        shot = db.scalar(select(Shot).where(Shot.group_key == group_key))
        if shot is not None:
            # The other half of a RAW+JPEG pair, possibly arriving in a later dump.
            if not shot.has_gps and meta.latitude is not None and shot.status in UNPUBLISHED:
                shot.gps_lat, shot.gps_lon = meta.latitude, meta.longitude
                apply_shot_location(shot, shot.session, settings, self.geocoder)
            if shot.status == ShotStatus.published:
                shot.status = ShotStatus.confirmed
            return shot

        session = self._session_for(db, captured_at.date(), sessions)
        shot = Shot(session=session, dumpsite_id=site.id, dump_folder=folder_label(site, folder), group_key=group_key,
                    captured_at=captured_at.replace(microsecond=0), captured_at_source=source, exif_datetime=meta.exif_datetime,
                    gps_lat=meta.latitude, gps_lon=meta.longitude)
        apply_shot_location(shot, session, settings, self.geocoder)
        shot.status = initial_status(shot, session)
        db.add(shot)
        db.flush()
        return shot

    @staticmethod
    def _session_for(db: OrmSession, shoot_date: date, cache: dict) -> Session:
        if shoot_date not in cache:
            session = db.scalar(select(Session).where(Session.shoot_date == shoot_date))
            if session is None:
                session = Session(shoot_date=shoot_date)
                db.add(session)
                db.flush()
            cache[shoot_date] = session
        return cache[shoot_date]
