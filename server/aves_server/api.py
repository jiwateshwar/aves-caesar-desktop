"""HTTP API and web UI. Run with: uvicorn aves_server.api:app

Interactive API documentation is served at /docs.
"""

from __future__ import annotations

import base64
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession, selectinload

from . import brand, jobs as job_queue
from .catalog import SpeciesCatalog
from .dating import shift_session_date
from .config import ServerConfig
from .db import Database
from .geocoding import Geocoder
from .locations import UNPUBLISHED, clear_session_location, set_analyse_without_location, set_session_location
from .models import FileStatus, Job, JobStatus, MediaFile, Session, Shot, ShotStatus, WorkerState
from .previews import PreviewStore
from .publisher import ConflictChecker
from .review import (ReviewError, add_species, append_suffix, confirm_confident, confirm_shot, ignore_shot, reject_detection,
                     reopen_shot, restore_detection, set_species)
from .serializers import CATEGORIES, age_seconds, iso, session_dict, shot_category, shot_dict
from .settings import SettingsStore, WorkflowSettings


STATIC_DIR = Path(__file__).parent / "static"


class State:
    config: ServerConfig
    database: Database
    previews: PreviewStore
    catalog: SpeciesCatalog | None = None


state = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.config = ServerConfig.from_env()
    state.config.cache_dir.mkdir(parents=True, exist_ok=True)
    state.database = Database(state.config.database_url)
    state.database.migrate()
    state.previews = PreviewStore(state.config.cache_dir)
    yield


app = FastAPI(title=brand.NAME, description=brand.TAGLINE, version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    password = state.config.password if hasattr(state, "config") else None
    if password and request.url.path != "/api/health":
        header = request.headers.get("authorization", "")
        supplied = ""
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        if not secrets.compare_digest(supplied.encode(), password.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{brand.NAME}"'})
    return await call_next(request)


@app.exception_handler(ReviewError)
async def review_error(_request: Request, exc: ReviewError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def value_error(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def get_db():
    with state.database.session() as db:
        yield db


def catalog() -> SpeciesCatalog:
    if state.catalog is None:
        labels = state.config.models_dir / "species_mapping_v2.csv"
        if not labels.exists():
            raise HTTPException(503, "The species list isn't available yet; the models are still being downloaded")
        state.catalog = SpeciesCatalog.load(labels)
    return state.catalog


def _shot(db: OrmSession, shot_id: int) -> Shot:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(404, "Shot not found")
    return shot


def _shot_response(db: OrmSession, shot: Shot) -> dict:
    db.flush()
    settings = SettingsStore.load(db)
    conflicts = ConflictChecker(db).conflicts([f.id for f in shot.files])
    # Folder names as the photographer sees them over SMB (the container paths mean nothing to them).
    return {**shot_dict(shot, catalog(), settings, conflicts, detail=True),
            "dumpsite_name": state.config.dump_dir.name, "published_name": state.config.published_dir.name}


# ---------------------------------------------------------------- status

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status(db: OrmSession = Depends(get_db)) -> dict:
    worker = db.scalar(select(WorkerState).where(WorkerState.name == "worker"))
    settings = SettingsStore.load(db)
    totals = _category_counts(db, settings, None)
    active_jobs = db.scalars(select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running])).order_by(Job.id)).all()
    return {
        "worker": {
            "online": worker is not None and (age_seconds(worker.heartbeat_at) or 1e9) < 120,
            "activity": worker.activity if worker else None,
            "heartbeat_at": iso(worker.heartbeat_at) if worker else None,
            "providers": worker.providers if worker else {},
            "model_error": worker.model_error if worker else None,
            "last_scan": worker.last_scan if worker else {},
            "last_scan_at": iso(worker.last_scan_at) if worker else None,
        },
        "totals": totals,
        "duplicates": db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.status == FileStatus.duplicate)),
        "missing": db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.status == FileStatus.missing)),
        "jobs": [_job_dict(job) for job in active_jobs],
        "paths": {"dumpsite": str(state.config.dump_dir), "published": str(state.config.published_dir)},
    }


def _category_counts(db: OrmSession, settings: WorkflowSettings, session_id: int | None, folder: str | None = None,
                     conflict_shots: set[int] | None = None) -> dict[str, int]:
    query = select(Shot.id, Shot.status, Shot.bird_count, Shot.min_confidence).where(Shot.status != ShotStatus.published)
    published = select(func.count()).select_from(Shot).where(Shot.status == ShotStatus.published)
    if session_id is not None:
        query, published = query.where(Shot.session_id == session_id), published.where(Shot.session_id == session_id)
    if folder is not None:
        query, published = query.where(Shot.dump_folder == folder), published.where(Shot.dump_folder == folder)
    rows = db.execute(query).all()
    counts = Counter(shot_category(row, settings.confidence_threshold) for row in rows)
    counts["published"] = db.scalar(published) or 0
    conflict_shots = _conflict_shot_ids(db) if conflict_shots is None else conflict_shots
    counts["conflicts"] = len(conflict_shots & {row.id for row in rows})
    counts["total"] = len(rows) + counts["published"]
    return {category: counts.get(category, 0) for category in [*CATEGORIES, "total"]}


def _conflict_shot_ids(db: OrmSession) -> set[int]:
    reasons = ConflictChecker(db).conflicts()
    if not reasons:
        return set()
    return set(db.scalars(select(MediaFile.shot_id).where(MediaFile.id.in_(list(reasons)))))


# ---------------------------------------------------------------- sessions

class LocationBody(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str | None = Field(default=None, max_length=300)


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    location: LocationBody | None = None
    clear_location: bool = False
    analyse_without_location: bool | None = None


@app.get("/api/sessions")
def list_sessions(db: OrmSession = Depends(get_db)) -> list[dict]:
    settings = SettingsStore.load(db)
    sessions = db.scalars(select(Session).order_by(Session.shoot_date.desc())).all()
    conflict_shots = _conflict_shot_ids(db)
    return [session_dict(session, _category_counts(db, settings, session.id, conflict_shots=conflict_shots)) for session in sessions]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: int, db: OrmSession = Depends(get_db)) -> dict:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    settings = SettingsStore.load(db)
    conflict_shots = _conflict_shot_ids(db)
    folders = [{"folder": folder, "counts": _category_counts(db, settings, session_id, folder, conflict_shots)}
               for folder in db.scalars(select(Shot.dump_folder).where(Shot.session_id == session_id).distinct().order_by(Shot.dump_folder))]
    return session_dict(session, _category_counts(db, settings, session_id, conflict_shots=conflict_shots), folders)


@app.patch("/api/sessions/{session_id}")
def update_session(session_id: int, body: SessionUpdate, db: OrmSession = Depends(get_db)) -> dict:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    settings = SettingsStore.load(db)
    queued = 0
    if body.title is not None:
        session.title = body.title.strip() or None
    if body.clear_location:
        queued += clear_session_location(session, settings)
    elif body.location is not None:
        queued += set_session_location(session, body.location.latitude, body.location.longitude, body.location.label, settings)
    if body.analyse_without_location is not None:
        queued += set_analyse_without_location(session, body.analyse_without_location)
    db.flush()
    return {**get_session(session_id, db), "queued": queued}


class DateShiftBody(BaseModel):
    date: date_type


@app.post("/api/sessions/{session_id}/shift-date")
def shift_date(session_id: int, body: DateShiftBody, db: OrmSession = Depends(get_db)) -> dict:
    """Corrects the date of a session's photos when the camera clock was wrong (files are not changed)."""
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return shift_session_date(db, session, body.date, SettingsStore.load(db)).__dict__


@app.post("/api/sessions/{session_id}/confirm-confident")
def confirm_session_confident(session_id: int, folder: str | None = None, db: OrmSession = Depends(get_db)) -> dict:
    settings = SettingsStore.load(db)
    query = select(Shot).where(Shot.session_id == session_id, Shot.status == ShotStatus.review)
    if folder is not None:
        query = query.where(Shot.dump_folder == folder)
    shots = list(db.scalars(query.options(selectinload(Shot.detections))))
    return {"confirmed": confirm_confident(db, shots, settings.confidence_threshold)}


# ---------------------------------------------------------------- shots

@app.get("/api/shots")
def list_shots(session_id: int | None = None, folder: str | None = None, category: str | None = None,
               offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=500), db: OrmSession = Depends(get_db)) -> dict:
    if category is not None and category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {', '.join(CATEGORIES)}")
    settings = SettingsStore.load(db)
    query = select(Shot).options(selectinload(Shot.files), selectinload(Shot.detections))
    if session_id is not None:
        query = query.where(Shot.session_id == session_id)
    if folder is not None:
        query = query.where(Shot.dump_folder == folder)
    threshold = settings.confidence_threshold
    if category == "conflicts":
        query = query.where(Shot.id.in_(_conflict_shot_ids(db) or {-1}))
    elif category == "review":
        query = query.where(Shot.status == ShotStatus.review, Shot.bird_count > 0, (Shot.min_confidence.is_(None)) | (Shot.min_confidence >= threshold))
    elif category == "low_confidence":
        query = query.where(Shot.status == ShotStatus.review, Shot.bird_count > 0, Shot.min_confidence < threshold)
    elif category == "no_bird":
        query = query.where(Shot.status == ShotStatus.review, Shot.bird_count == 0)
    elif category is not None:
        query = query.where(Shot.status == ShotStatus(category))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    shots = db.scalars(query.order_by(Shot.captured_at, Shot.id).offset(offset).limit(limit)).all()
    conflicts = ConflictChecker(db).conflicts([f.id for shot in shots for f in shot.files])
    return {"total": total, "items": [shot_dict(shot, catalog(), settings, conflicts) for shot in shots]}


@app.get("/api/shots/{shot_id}")
def get_shot(shot_id: int, db: OrmSession = Depends(get_db)) -> dict:
    return _shot_response(db, _shot(db, shot_id))


@app.get("/api/shots/{shot_id}/{kind}.jpg")
def shot_image(shot_id: int, kind: str):
    if kind not in ("thumb", "preview"):
        raise HTTPException(404)
    path = state.previews.path(shot_id, kind)
    if not path.exists():
        raise HTTPException(404, "Preview not ready")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/files/{file_id}/download")
def download_file(file_id: int, db: OrmSession = Depends(get_db)):
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404)
    if file.status == FileStatus.published and file.published_relpath:
        path = state.config.published_dir / file.published_relpath
    elif file.dump_relpath:
        path = state.config.dump_dir / file.dump_relpath
    else:
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404, "The file is no longer on disk")
    return FileResponse(path, filename=file.final_filename)


class SpeciesBody(BaseModel):
    species_id: str


class SuffixBody(BaseModel):
    suffix: str = Field(max_length=40)


class ShotIds(BaseModel):
    shot_ids: list[int] = Field(min_length=1, max_length=5000)


class PlaceBody(ShotIds):
    country: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)


@app.post("/api/shots/{shot_id}/confirm")
def confirm(shot_id: int, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    confirm_shot(shot)
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/ignore")
def ignore(shot_id: int, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    ignore_shot(shot)
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/reopen")
def reopen(shot_id: int, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    reopen_shot(shot)
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/detections/{detection_id}/species")
def change_species(shot_id: int, detection_id: int, body: SpeciesBody, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    set_species(shot, detection_id, body.species_id, catalog())
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/detections/{detection_id}/reject")
def reject(shot_id: int, detection_id: int, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    reject_detection(shot, detection_id)
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/detections/{detection_id}/restore")
def restore(shot_id: int, detection_id: int, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    restore_detection(shot, detection_id)
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/species")
def tag_species(shot_id: int, body: SpeciesBody, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    add_species(shot, body.species_id, catalog())
    return _shot_response(db, shot)


@app.post("/api/shots/{shot_id}/suffix")
def rename_with_suffix(shot_id: int, body: SuffixBody, db: OrmSession = Depends(get_db)) -> dict:
    shot = _shot(db, shot_id)
    append_suffix(db, shot, body.suffix.strip(), ConflictChecker(db))
    return _shot_response(db, shot)


@app.post("/api/shots/confirm")
def confirm_many(body: ShotIds, db: OrmSession = Depends(get_db)) -> dict:
    confirmed, failed = 0, []
    for shot in db.scalars(select(Shot).where(Shot.id.in_(body.shot_ids))):
        try:
            confirm_shot(shot)
            confirmed += 1
        except ReviewError as exc:
            failed.append({"shot_id": shot.id, "reason": str(exc)})
    return {"confirmed": confirmed, "failed": failed}


@app.post("/api/shots/place")
def override_place(body: PlaceBody, db: OrmSession = Depends(get_db)) -> dict:
    """Corrects country/state/city (e.g. a neighbourhood the geocoder picked) before publishing."""
    updated = 0
    for shot in db.scalars(select(Shot).where(Shot.id.in_(body.shot_ids), Shot.status.in_(UNPUBLISHED))):
        for field in ("country", "state", "city"):
            value = getattr(body, field)
            if value is not None:
                setattr(shot, field, value.strip() or None)
        updated += 1
    return {"updated": updated}


@app.post("/api/shots/reanalyse")
def reanalyse(body: ShotIds, db: OrmSession = Depends(get_db)) -> dict:
    return _job_dict(job_queue.enqueue(db, "reanalyse", {"shot_ids": body.shot_ids}))


# ---------------------------------------------------------------- jobs, publish, scan

class PublishBody(BaseModel):
    session_id: int | None = None
    shot_ids: list[int] | None = None


@app.post("/api/publish")
def publish(body: PublishBody, db: OrmSession = Depends(get_db)) -> dict:
    payload: dict[str, Any] = {}
    if body.shot_ids:
        payload["shot_ids"] = body.shot_ids
    elif body.session_id is not None:
        payload["session_id"] = body.session_id
    return _job_dict(job_queue.enqueue(db, "publish", payload))


@app.post("/api/scan")
def scan(db: OrmSession = Depends(get_db)) -> dict:
    return _job_dict(job_queue.enqueue(db, "scan"))


@app.post("/api/library/reindex")
def reindex_library(db: OrmSession = Depends(get_db)) -> dict:
    return _job_dict(job_queue.enqueue(db, "reindex_library"))


@app.get("/api/jobs")
def list_jobs(limit: int = Query(20, ge=1, le=200), db: OrmSession = Depends(get_db)) -> list[dict]:
    return [_job_dict(job) for job in db.scalars(select(Job).order_by(Job.id.desc()).limit(limit))]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: OrmSession = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    return _job_dict(job)


def _job_dict(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "status": job.status.value, "progress": job.progress, "total": job.total,
            "message": job.message, "result": job.result, "payload": job.payload,
            "created_at": iso(job.created_at), "finished_at": iso(job.finished_at)}


# ---------------------------------------------------------------- duplicates

@app.get("/api/duplicates")
def list_duplicates(db: OrmSession = Depends(get_db)) -> list[dict]:
    files = db.scalars(select(MediaFile).where(MediaFile.status == FileStatus.duplicate).order_by(MediaFile.dump_relpath)).all()
    originals = {f.id: f for f in db.scalars(select(MediaFile).where(MediaFile.id.in_({f.duplicate_of_id for f in files})))}
    return [{"id": f.id, "dump_relpath": f.dump_relpath, "size_bytes": f.size_bytes,
             "original": {"id": o.id, "status": o.status.value, "path": o.published_relpath or o.dump_relpath} if (o := originals.get(f.duplicate_of_id)) else None}
            for f in files]


@app.delete("/api/duplicates/{file_id}")
def delete_duplicate(file_id: int, db: OrmSession = Depends(get_db)) -> dict:
    """Deletes an identical extra copy from the dumpsite. Only ever touches files recorded as duplicates."""
    from .media import sha256_of

    file = db.get(MediaFile, file_id)
    if file is None or file.status != FileStatus.duplicate or not file.dump_relpath:
        raise HTTPException(404, "Not a known duplicate")
    path = (state.config.dump_dir / file.dump_relpath).resolve()
    if state.config.dump_dir.resolve() not in path.parents:
        raise HTTPException(400, "Refusing to delete outside the dumpsite")
    if path.is_file():
        if sha256_of(path) != file.sha256:
            raise HTTPException(409, "The file changed since it was scanned; it was not deleted")
        path.unlink()
    file.status = FileStatus.missing
    return {"deleted": file.dump_relpath}


# ---------------------------------------------------------------- lookups and settings

@app.get("/api/species")
def search_species(q: str = Query(min_length=2), limit: int = Query(20, ge=1, le=100), db: OrmSession = Depends(get_db)) -> list[dict]:
    language = SettingsStore.load(db).species_language
    return [{"id": e.species_id, "name": e.name(language), "scientific_name": e.scientific_name, "family": e.family}
            for e in catalog().search(q, language, limit)]


@app.get("/api/locations/search")
def search_locations(q: str = Query(min_length=2), limit: int = Query(10, ge=1, le=50)) -> list[dict]:
    return [place.to_dict() for place in Geocoder.shared().search(q, limit)]


@app.get("/api/locations/reverse")
def reverse_location(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180), db: OrmSession = Depends(get_db)) -> dict:
    place = Geocoder.shared().reverse(lat, lon, SettingsStore.load(db).city_min_population)
    if place is None:
        raise HTTPException(404)
    return place.to_dict()


@app.get("/api/settings")
def get_settings(db: OrmSession = Depends(get_db)) -> dict:
    settings = SettingsStore.load(db)
    try:
        languages = catalog().languages()
    except HTTPException:
        languages = ["en"]
    return {"values": settings.__dict__, "defaults": WorkflowSettings().__dict__, "languages": languages}


@app.put("/api/settings")
def update_settings(changes: dict[str, Any], db: OrmSession = Depends(get_db)) -> dict:
    SettingsStore.update(db, changes)
    return get_settings(db)


# ---------------------------------------------------------------- web UI

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
