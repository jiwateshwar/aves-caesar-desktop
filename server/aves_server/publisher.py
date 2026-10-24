from __future__ import annotations

import errno
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as OrmSession

from .config import ServerConfig
from .db import Database
from .media import is_ignored_dir, sha256_of
from .models import FileStatus, LibraryFile, MediaFile, Shot, ShotStatus
from .settings import SettingsStore, WorkflowSettings
from .shots import dump_files


logger = logging.getLogger(__name__)

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PARTIAL_SUFFIX = ".aves-partial"


class ConflictChecker:
    """Filenames must be unique across the whole published library (case-insensitive)."""

    def __init__(self, db: OrmSession):
        self.db = db

    def clashes(self, proposed: dict[int, str], include_dump: bool = True) -> dict[int, str]:
        """Returns the subset of {file_id: filename} that collides with the library or other files in the workflow."""
        keys = {file_id: name.casefold() for file_id, name in proposed.items()}
        if not keys:
            return {}
        taken = set(self.db.scalars(select(LibraryFile.filename_key).where(
            LibraryFile.filename_key.in_(set(keys.values())),
            LibraryFile.media_file_id.is_(None) | LibraryFile.media_file_id.not_in(list(keys)))))
        if include_dump:
            taken |= set(self.db.scalars(select(MediaFile.filename_key).where(
                MediaFile.filename_key.in_(set(keys.values())), MediaFile.status == FileStatus.in_dump,
                MediaFile.shot_id.is_not(None), MediaFile.id.not_in(list(keys)))))
        return {file_id: proposed[file_id] for file_id, key in keys.items() if key in taken}

    def conflicts(self, file_ids: list[int] | None = None) -> dict[int, str]:
        """Why each in-dump file conflicts: 'library' (already published) or 'dump' (same name waiting elsewhere)."""
        query = select(MediaFile.id, MediaFile.filename_key, MediaFile.shot_id).where(MediaFile.status == FileStatus.in_dump, MediaFile.shot_id.is_not(None))
        if file_ids is not None:
            query = query.where(MediaFile.id.in_(file_ids))
        rows = self.db.execute(query).all()
        if not rows:
            return {}
        keys = {row.filename_key for row in rows}
        library = set(self.db.scalars(select(LibraryFile.filename_key).where(LibraryFile.filename_key.in_(keys))))
        dump_counts = dict(self.db.execute(
            select(MediaFile.filename_key, func.count(func.distinct(MediaFile.shot_id))).where(
                MediaFile.filename_key.in_(keys), MediaFile.status == FileStatus.in_dump, MediaFile.shot_id.is_not(None))
            .group_by(MediaFile.filename_key)).all())
        reasons = {}
        for row in rows:
            if row.filename_key in library:
                reasons[row.id] = "library"
            elif dump_counts.get(row.filename_key, 0) > 1:
                reasons[row.id] = "dump"
        return reasons


def target_folder(shot: Shot, file: MediaFile, settings: WorkflowSettings) -> PurePosixPath:
    shoot_date = shot.captured_at.date() if shot.captured_at else shot.session.shoot_date
    placeholder = settings.unknown_placeholder
    values = {
        "country": shot.country, "state": shot.state, "city": shot.city,
        "date": shoot_date.isoformat() if shoot_date else None,
        "year": f"{shoot_date.year:04d}" if shoot_date else None,
        "month": f"{shoot_date.month:02d}" if shoot_date else None,
        "filetype": file.file_type,
    }
    formatted = settings.publish_pattern.format(**{key: _component(value, placeholder) for key, value in values.items()})
    return PurePosixPath(*[_component(part, placeholder) for part in formatted.split("/") if part.strip()])


def _component(value: str | None, placeholder: str) -> str:
    text = _FORBIDDEN.sub("-", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text).rstrip(". ")
    return text or placeholder


def move_file(source: Path, target: Path, expected_sha256: str) -> None:
    """Moves without ever overwriting. Same filesystem: hard link + unlink. Otherwise: copy, verify, then unlink."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    try:
        os.link(source, target)  # fails instead of replacing an existing file
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK, errno.EOPNOTSUPP):
            raise
        partial = target.with_name(f".{target.name}{PARTIAL_SUFFIX}")
        try:
            shutil.copy2(source, partial)
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            if sha256_of(partial) != expected_sha256:
                raise OSError(f"Copy of {source.name} did not match the original; nothing was moved")
            if target.exists():
                raise FileExistsError(target)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    source.unlink()


def _sidecars(source: Path) -> list[tuple[Path, str]]:
    """XMP sidecars next to a photo: 'NAME.EXT.xmp' or 'NAME.xmp'. Returns (path, kind) pairs."""
    found = []
    try:
        siblings = {entry.name.casefold(): entry for entry in source.parent.iterdir() if entry.is_file()}
    except OSError:
        return found
    for kind, name in (("full", f"{source.name}.xmp"), ("stem", f"{source.stem}.xmp")):
        entry = siblings.get(name.casefold())
        if entry is not None:
            found.append((entry, kind))
    return found


@dataclass
class PublishReport:
    published_shots: int = 0
    published_files: int = 0
    conflicts: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**self.__dict__, "conflicts": self.conflicts[:200], "errors": self.errors[:100]}


class Publisher:
    def __init__(self, config: ServerConfig, database: Database):
        self.config = config
        self.database = database

    def publish(self, shot_ids: list[int], progress: Callable[[int, int], None] | None = None) -> PublishReport:
        report = PublishReport()
        for index, shot_id in enumerate(shot_ids, start=1):
            try:
                self._publish_shot(shot_id, report)
            except Exception as exc:
                logger.exception("Publishing shot %s failed", shot_id)
                report.errors.append(f"Shot {shot_id}: {exc}")
            if progress:
                progress(index, len(shot_ids))
        return report

    def _publish_shot(self, shot_id: int, report: PublishReport) -> None:
        with self.database.session() as db:
            shot = db.get(Shot, shot_id)
            if shot is None or shot.status != ShotStatus.confirmed:
                return
            settings = SettingsStore.load(db)
            files = dump_files(shot)
            plan = [(file, target_folder(shot, file, settings) / file.final_filename) for file in files]

            for file, _relpath in plan:
                if not (self.config.dump_dir / file.dump_relpath).is_file():
                    file.status = FileStatus.missing
                    report.errors.append(f"{file.original_filename} is no longer in the dumpsite")
                    return
            # Check every file first so a RAW+JPEG pair never gets split across a conflict.
            clashes = ConflictChecker(db).clashes({file.id: file.final_filename for file, _ in plan}, include_dump=False)
            clashes |= {file.id: file.final_filename for file, relpath in plan if (self.config.published_dir / relpath.as_posix()).exists()}
            if clashes:
                report.conflicts.extend({"shot_id": shot.id, "file_id": file_id, "filename": name} for file_id, name in clashes.items())
                return

            for file, relpath in plan:
                source = self.config.dump_dir / file.dump_relpath
                target = self.config.published_dir / relpath.as_posix()
                move_file(source, target, file.sha256)
                self._move_sidecars(source, target, file, report)
                file.status = FileStatus.published
                file.published_relpath = relpath.as_posix()
                file.published_at = datetime.now(timezone.utc)
                db.add(LibraryFile(relpath=relpath.as_posix(), filename_key=file.filename_key, media_file_id=file.id))
                db.commit()  # record each move as it happens, so the database never lags behind the disk
                report.published_files += 1

            if not dump_files(shot):
                shot.status = ShotStatus.published
                report.published_shots += 1

    def _move_sidecars(self, source: Path, target: Path, file: MediaFile, report: PublishReport) -> None:
        for sidecar, kind in _sidecars(source):
            name = f"{target.name}.xmp" if kind == "full" else f"{target.stem}.xmp"
            destination = target.with_name(name)
            try:
                move_file(sidecar, destination, sha256_of(sidecar))
            except FileExistsError:
                report.errors.append(f"Sidecar {sidecar.name} left in the dumpsite: {name} already exists")
            except OSError as exc:
                report.errors.append(f"Sidecar {sidecar.name} could not be moved: {exc}")


class LibraryIndexer:
    """Records every filename already in the published folder, including files added outside this app."""

    def __init__(self, config: ServerConfig, database: Database):
        self.config = config
        self.database = database

    def reindex(self) -> dict:
        root = self.config.published_dir
        if not root.is_dir():
            raise RuntimeError(f"Published folder not found: {root}")
        found = set()
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not is_ignored_dir(name)]
            for name in filenames:
                if name.startswith(".") or name.endswith(PARTIAL_SUFFIX):
                    continue
                found.add(PurePosixPath(Path(directory, name).relative_to(root)).as_posix())
        with self.database.session() as db:
            known = set(db.scalars(select(LibraryFile.relpath)))
            added = found - known
            removed = known - found
            for relpath in added:
                db.add(LibraryFile(relpath=relpath, filename_key=PurePosixPath(relpath).name.casefold()))
            if removed and found:  # an empty listing is more likely an unmounted share than a wiped library
                for chunk in _chunks(sorted(removed), 500):
                    db.execute(delete(LibraryFile).where(LibraryFile.relpath.in_(chunk)))
        return {"files": len(found), "added": len(added), "removed": len(removed) if found else 0}


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]
