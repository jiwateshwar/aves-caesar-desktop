from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .catalog import SpeciesCatalog
from .models import Detection, DetectionSource, DetectionStatus, Shot, ShotStatus
from .publisher import ConflictChecker
from .shots import dump_files, refresh_shot_summary


class ReviewError(ValueError):
    pass


REVIEWABLE = {ShotStatus.review, ShotStatus.confirmed, ShotStatus.ignored}
SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9 _\-.()+]{1,40}$")


def _require(shot: Shot, allowed: set[ShotStatus]) -> None:
    if shot.status not in allowed:
        raise ReviewError(f"This shot is {shot.status.value.replace('_', ' ')} and can't be changed right now")


def _detection(shot: Shot, detection_id: int) -> Detection:
    for detection in shot.detections:
        if detection.id == detection_id:
            return detection
    raise ReviewError("Detection not found on this shot")


def confirm_shot(shot: Shot) -> None:
    """Accepts the current species of every remaining detection. A shot without birds is published without species."""
    _require(shot, {ShotStatus.review, ShotStatus.confirmed})
    for detection in shot.detections:
        if detection.status == DetectionStatus.rejected:
            continue
        if detection.species_id is None:
            raise ReviewError("Every bird needs a species before the shot can be confirmed")
        detection.status = DetectionStatus.confirmed
    refresh_shot_summary(shot)
    shot.status = ShotStatus.confirmed
    shot.reviewed_at = datetime.now(timezone.utc)


def set_species(shot: Shot, detection_id: int, species_id: str, catalog: SpeciesCatalog) -> None:
    _require(shot, REVIEWABLE)
    if catalog.get(species_id) is None:
        raise ReviewError(f"Unknown species {species_id}")
    detection = _detection(shot, detection_id)
    predicted = {p["species_id"]: p["confidence"] for p in detection.predictions}
    detection.species_id = species_id
    detection.confidence = predicted.get(species_id)
    if detection.box is None:
        detection.source = DetectionSource.manual
    detection.status = DetectionStatus.pending
    _back_to_review(shot)


def reject_detection(shot: Shot, detection_id: int) -> None:
    """Marks a detection as not a bird (or not worth keeping). Hand-added species are removed outright."""
    _require(shot, REVIEWABLE)
    detection = _detection(shot, detection_id)
    if detection.source == DetectionSource.manual and detection.box is None:
        shot.detections.remove(detection)
    else:
        detection.status = DetectionStatus.rejected
    refresh_shot_summary(shot)
    _back_to_review(shot)


def restore_detection(shot: Shot, detection_id: int) -> None:
    _require(shot, REVIEWABLE)
    _detection(shot, detection_id).status = DetectionStatus.pending
    refresh_shot_summary(shot)
    _back_to_review(shot)


def add_species(shot: Shot, species_id: str, catalog: SpeciesCatalog) -> Detection:
    """Tags a species the detector missed (no bounding box)."""
    _require(shot, REVIEWABLE)
    if catalog.get(species_id) is None:
        raise ReviewError(f"Unknown species {species_id}")
    detection = Detection(species_id=species_id, source=DetectionSource.manual, status=DetectionStatus.pending, predictions=[])
    shot.detections.append(detection)
    refresh_shot_summary(shot)
    _back_to_review(shot)
    return detection


def ignore_shot(shot: Shot) -> None:
    """Leave this shot in the dumpsite; it won't be published."""
    _require(shot, {ShotStatus.review, ShotStatus.confirmed, ShotStatus.error})
    shot.status = ShotStatus.ignored
    shot.reviewed_at = datetime.now(timezone.utc)


def reopen_shot(shot: Shot) -> None:
    _require(shot, {ShotStatus.confirmed, ShotStatus.ignored, ShotStatus.error})
    if shot.status == ShotStatus.error or shot.analysed_at is None:
        shot.status = ShotStatus.pending
        shot.analysis_attempts = 0
        return
    for detection in shot.detections:
        if detection.status == DetectionStatus.confirmed:
            detection.status = DetectionStatus.pending
    shot.status = ShotStatus.review


def confirm_confident(db: OrmSession, shots: list[Shot], threshold: float) -> int:
    """Bulk-confirms shots where every bird's top prediction meets the threshold and nothing was edited."""
    confirmed = 0
    for shot in shots:
        if shot.status != ShotStatus.review or shot.bird_count == 0 or shot.min_confidence is None or shot.min_confidence < threshold:
            continue
        if any(d.source == DetectionSource.manual or (d.predictions and d.species_id != d.predictions[0]["species_id"])
               for d in shot.detections if d.status != DetectionStatus.rejected):
            continue
        confirm_shot(shot)
        confirmed += 1
    return confirmed


def append_suffix(db: OrmSession, shot: Shot, suffix: str, checker: ConflictChecker) -> list[str]:
    """Resolves a filename conflict by appending text to the name (before the extension) of every file in the shot.

    RAW+JPEG pairs get the same suffix so they keep matching names. Passing an empty suffix restores original names.
    """
    if shot.status in (ShotStatus.published,):
        raise ReviewError("Published files can't be renamed")
    if suffix and not SUFFIX_PATTERN.match(suffix):
        raise ReviewError("Use up to 40 letters, digits, spaces or _ - . ( ) +")
    files = dump_files(shot)
    proposed = {}
    for file in files:
        original = PurePosixPath(file.original_filename)
        proposed[file.id] = f"{original.stem}{suffix}{original.suffix}" if suffix else file.original_filename
    clashes = checker.clashes(proposed)
    if clashes:
        raise ReviewError("Still conflicts: " + ", ".join(sorted(clashes.values())))
    for file in files:
        file.final_filename = proposed[file.id]
        file.filename_key = proposed[file.id].casefold()
    return [proposed[file.id] for file in files]


def _back_to_review(shot: Shot) -> None:
    if shot.status in (ShotStatus.confirmed, ShotStatus.ignored):
        shot.status = ShotStatus.review
