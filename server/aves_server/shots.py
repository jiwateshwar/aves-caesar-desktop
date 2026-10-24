from __future__ import annotations

from .media import PRIMARY_PREFERENCE
from .models import DetectionSource, DetectionStatus, FileStatus, MediaFile, Shot


def refresh_shot_summary(shot: Shot) -> None:
    """Recomputes the bird count and the weakest model confidence after detections change."""
    active = [d for d in shot.detections if d.status != DetectionStatus.rejected]
    shot.bird_count = len(active)
    model_scores = [d.predictions[0]["confidence"] for d in active if d.source == DetectionSource.model and d.predictions]
    shot.min_confidence = min(model_scores) if model_scores else None


def workflow_files(shot: Shot) -> list[MediaFile]:
    """Files that belong to the shot's workflow (excludes duplicates and vanished files)."""
    return [file for file in shot.files if file.status in (FileStatus.in_dump, FileStatus.published)]


def dump_files(shot: Shot) -> list[MediaFile]:
    return [file for file in shot.files if file.status == FileStatus.in_dump]


def primary_file(shot: Shot) -> MediaFile | None:
    """The file used for analysis and previews: a JPEG beats the RAW it was shot alongside."""
    candidates = dump_files(shot) or workflow_files(shot)
    if not candidates:
        return None

    def rank(file: MediaFile) -> int:
        return PRIMARY_PREFERENCE.index(file.file_type) if file.file_type in PRIMARY_PREFERENCE else len(PRIMARY_PREFERENCE)

    return min(candidates, key=rank)
