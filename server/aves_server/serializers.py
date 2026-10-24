from __future__ import annotations

from datetime import datetime, timezone
from .catalog import SpeciesCatalog
from .models import Detection, DetectionStatus, MediaFile, Session, Shot, ShotStatus
from .publisher import target_folder
from .settings import WorkflowSettings


CATEGORIES = ["awaiting_location", "pending", "review", "low_confidence", "no_bird", "conflicts", "confirmed", "published", "ignored", "error"]


def shot_category(shot: Shot, threshold: float) -> str:
    if shot.status != ShotStatus.review:
        return shot.status.value
    if shot.bird_count == 0:
        return "no_bird"
    if shot.min_confidence is not None and shot.min_confidence < threshold:
        return "low_confidence"
    return "review"


def species_dict(catalog: SpeciesCatalog, species_id: str | None, language: str) -> dict | None:
    entry = catalog.get(species_id)
    if entry is None:
        return None if species_id is None else {"id": species_id, "name": species_id, "scientific_name": "", "family": None}
    return {"id": entry.species_id, "name": entry.name(language), "scientific_name": entry.scientific_name, "family": entry.family}


def detection_dict(detection: Detection, catalog: SpeciesCatalog, language: str) -> dict:
    return {
        "id": detection.id,
        "box": detection.box,
        "box_confidence": detection.box_confidence,
        "species": species_dict(catalog, detection.species_id, language),
        "confidence": detection.confidence,
        "source": detection.source.value,
        "status": detection.status.value,
        "alternatives": [{**(species_dict(catalog, p["species_id"], language) or {}), "confidence": p["confidence"]} for p in detection.predictions],
    }


def file_dict(file: MediaFile, conflicts: dict[int, str], detail: bool = False) -> dict:
    data = {
        "id": file.id,
        "original_filename": file.original_filename,
        "final_filename": file.final_filename,
        "file_type": file.file_type,
        "is_raw": file.is_raw,
        "size_bytes": file.size_bytes,
        "status": file.status.value,
        "dump_relpath": file.dump_relpath,
        "published_relpath": file.published_relpath,
        "conflict": conflicts.get(file.id),
    }
    if detail:
        data.update({
            "camera": " ".join(part for part in (file.camera_make, file.camera_model) if part) or None,
            "lens": file.lens_model, "focal_length": file.focal_length, "f_number": file.f_number,
            "exposure_time": file.exposure_time, "iso": file.iso, "width": file.width, "height": file.height,
            "sha256": file.sha256, "exif": file.exif,
        })
    return data


def shot_dict(shot: Shot, catalog: SpeciesCatalog, settings: WorkflowSettings, conflicts: dict[int, str], detail: bool = False) -> dict:
    language = settings.species_language
    active = [d for d in shot.detections if d.status != DetectionStatus.rejected]
    files = [f for f in shot.files if f.status.value != "duplicate"]
    data = {
        "id": shot.id,
        "session_id": shot.session_id,
        "folder": shot.dump_folder,
        "captured_at": shot.captured_at.isoformat() if shot.captured_at else None,
        "captured_at_source": shot.captured_at_source,
        "status": shot.status.value,
        "category": shot_category(shot, settings.confidence_threshold),
        "bird_count": shot.bird_count,
        "min_confidence": shot.min_confidence,
        "species": [species_dict(catalog, d.species_id, language) for d in active if d.species_id],
        "location": {"latitude": shot.gps_lat, "longitude": shot.gps_lon, "source": shot.location_source,
                     "country": shot.country, "state": shot.state, "city": shot.city},
        "preview_ready": shot.preview_ready,
        "has_conflict": any(f.id in conflicts for f in files),
        "files": [file_dict(f, conflicts) for f in files],
    }
    if detail:
        data.update({
            "detections": [detection_dict(d, catalog, language) for d in shot.detections],
            "files": [file_dict(f, conflicts, detail=True) | {"publish_to": _publish_to(shot, f, settings)} for f in files],
            "image_width": shot.image_width, "image_height": shot.image_height,
            "analysed_at": iso(shot.analysed_at), "analysed_with_location": shot.analysed_with_location,
            "analysis_provider": shot.analysis_provider, "analysis_error": shot.analysis_error,
            "reviewed_at": iso(shot.reviewed_at),
        })
    return data


def session_dict(session: Session, counts: dict[str, int] | None = None, folders: list[dict] | None = None) -> dict:
    data = {
        "id": session.id,
        "date": session.shoot_date.isoformat() if session.shoot_date else None,
        "title": session.title,
        "location": {"latitude": session.location_lat, "longitude": session.location_lon, "label": session.location_label,
                     "country": session.country, "state": session.state, "city": session.city} if session.has_location else None,
        "analyse_without_location": session.analyse_without_location,
        "counts": counts or {},
    }
    if folders is not None:
        data["folders"] = folders
    return data


def _publish_to(shot: Shot, file: MediaFile, settings: WorkflowSettings) -> str | None:
    if file.status.value == "published":
        return file.published_relpath
    return (target_folder(shot, file, settings) / file.final_filename).as_posix()


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def age_seconds(value: datetime | None) -> float | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()
