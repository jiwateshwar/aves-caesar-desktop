"""Correcting a session's date when the camera's clock was wrong.

Files are never changed: the correction is a whole number of days recorded on each shot, and the corrected
time is what Featherbase uses for sessions, publish folders and identification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .locations import _untouched, apply_shot_location
from .models import Session, Shot, ShotStatus
from .review import ReviewError
from .settings import WorkflowSettings


@dataclass
class DateShift:
    session_id: int      # where the shots are now (the same session, or the one already on that day)
    days: int
    moved: int
    kept_published: int  # already in the library under the old date; their files never move
    requeued: int        # untouched shots identified again, since the season feeds the model


def shift_session_date(db: OrmSession, session: Session, new_date: date, settings: WorkflowSettings) -> DateShift:
    if session.shoot_date is None:
        raise ReviewError("This session has no date to correct")
    days = (new_date - session.shoot_date).days
    if days == 0:
        return DateShift(session.id, 0, 0, 0, 0)
    shots = list(session.shots)
    movable = [shot for shot in shots if shot.status != ShotStatus.published]
    kept = len(shots) - len(movable)
    if not movable:
        raise ReviewError("Every photo in this session is already published; published files keep their date and folder")

    existing = db.scalar(select(Session).where(Session.shoot_date == new_date))
    if existing is None and kept == 0:
        session.shoot_date = new_date  # the whole day moves: keep its title and location as they are
        target = session
    else:
        target = existing or Session(shoot_date=new_date, title=session.title, analyse_without_location=session.analyse_without_location)
        if existing is None:
            db.add(target)
        if not target.has_location and session.has_location:
            target.location_lat, target.location_lon = session.location_lat, session.location_lon
            target.location_label, target.country, target.state, target.city = session.location_label, session.country, session.state, session.city
        target.analyse_without_location = target.analyse_without_location or session.analyse_without_location
        for shot in movable:
            shot.session = target
            if not shot.has_gps:
                apply_shot_location(shot, target, settings)

    requeued = 0
    for shot in movable:
        if shot.captured_at is not None:
            shot.captured_at = shot.captured_at + timedelta(days=days)
        shot.captured_at_offset_days += days
        if shot.status == ShotStatus.review and _untouched(shot):
            shot.status = ShotStatus.pending
            requeued += 1
        elif shot.status == ShotStatus.awaiting_location and (target.has_location or target.analyse_without_location):
            shot.status = ShotStatus.pending

    db.flush()
    if target is not session and not kept:
        db.delete(session)
    return DateShift(target.id, days, len(movable), kept, requeued)


def model_datetime(shot: Shot) -> str | None:
    """The capture time handed to the classifier (it uses the day of year): corrected when the clock was wrong."""
    if shot.captured_at_offset_days and shot.captured_at is not None:
        return shot.captured_at.strftime("%Y:%m:%d %H:%M:%S")
    return shot.exif_datetime
