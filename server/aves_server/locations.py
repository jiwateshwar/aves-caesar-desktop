from __future__ import annotations

from .geocoding import Geocoder
from .models import DetectionSource, DetectionStatus, Session, Shot, ShotStatus
from .settings import WorkflowSettings


UNPUBLISHED = {ShotStatus.awaiting_location, ShotStatus.pending, ShotStatus.review, ShotStatus.confirmed, ShotStatus.error, ShotStatus.ignored}


def apply_shot_location(shot: Shot, session: Session, settings: WorkflowSettings, geocoder: Geocoder | None = None) -> None:
    """A shot's own GPS wins; otherwise it inherits the location you set for its session."""
    if shot.has_gps:
        place = (geocoder or Geocoder.shared()).reverse(shot.gps_lat, shot.gps_lon, settings.city_min_population)
        shot.location_source = "exif"
        shot.country, shot.state, shot.city = (place.country, place.state, place.city) if place else (None, None, None)
    elif session.has_location:
        shot.location_source = "session"
        shot.country, shot.state, shot.city = session.country, session.state, session.city
    else:
        shot.location_source = None
        shot.country = shot.state = shot.city = None


def analysis_coordinates(shot: Shot, session: Session) -> tuple[float | None, float | None]:
    if shot.has_gps:
        return shot.gps_lat, shot.gps_lon
    if session.has_location:
        return session.location_lat, session.location_lon
    return None, None


def initial_status(shot: Shot, session: Session) -> ShotStatus:
    if shot.has_gps or session.has_location or session.analyse_without_location:
        return ShotStatus.pending
    return ShotStatus.awaiting_location


def set_session_location(session: Session, latitude: float, longitude: float, label: str | None,
                         settings: WorkflowSettings, geocoder: Geocoder | None = None) -> int:
    """Sets where a day's photos were taken. Returns how many shots were queued for (re)analysis."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Coordinates are out of range")
    place = (geocoder or Geocoder.shared()).reverse(latitude, longitude, settings.city_min_population)
    session.location_lat, session.location_lon = latitude, longitude
    session.country, session.state, session.city = (place.country, place.state, place.city) if place else (None, None, None)
    session.location_label = label or (place.label if place else f"{latitude:.4f}, {longitude:.4f}")
    return _refresh_shots(session, settings, geocoder)


def clear_session_location(session: Session, settings: WorkflowSettings) -> int:
    session.location_lat = session.location_lon = None
    session.location_label = session.country = session.state = session.city = None
    return _refresh_shots(session, settings, None)


def set_analyse_without_location(session: Session, enabled: bool) -> int:
    session.analyse_without_location = enabled
    queued = 0
    if enabled:
        for shot in session.shots:
            if shot.status == ShotStatus.awaiting_location:
                shot.status = ShotStatus.pending
                queued += 1
    return queued


def _refresh_shots(session: Session, settings: WorkflowSettings, geocoder: Geocoder | None) -> int:
    queued = 0
    for shot in session.shots:
        if shot.status not in UNPUBLISHED or shot.has_gps:
            continue
        apply_shot_location(shot, session, settings, geocoder)
        if shot.status == ShotStatus.awaiting_location and (session.has_location or session.analyse_without_location):
            shot.status = ShotStatus.pending
            queued += 1
        elif shot.status == ShotStatus.review and session.has_location and not shot.analysed_with_location and _untouched(shot):
            # The model is noticeably more accurate with a location; redo shots nobody has reviewed yet.
            shot.status = ShotStatus.pending
            queued += 1
        elif not session.has_location and not session.analyse_without_location and shot.status == ShotStatus.pending:
            shot.status = ShotStatus.awaiting_location
    return queued


def _untouched(shot: Shot) -> bool:
    return all(d.source == DetectionSource.model and d.status == DetectionStatus.pending and d.species_id == _top_species(d)
               for d in shot.detections)


def _top_species(detection) -> str | None:
    return detection.predictions[0]["species_id"] if detection.predictions else None
