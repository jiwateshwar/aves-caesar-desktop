from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ShotStatus(str, enum.Enum):
    awaiting_location = "awaiting_location"  # no GPS and the session has no location yet
    pending = "pending"                      # queued for analysis
    review = "review"                        # analysed, waiting for the user
    confirmed = "confirmed"                  # user confirmed, ready to publish
    published = "published"
    ignored = "ignored"                      # user chose to leave it in the dumpsite
    error = "error"


class FileStatus(str, enum.Enum):
    in_dump = "in_dump"
    published = "published"
    duplicate = "duplicate"  # same content as another known file, not part of the workflow
    missing = "missing"      # disappeared from the dumpsite before being published


class DetectionStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class DetectionSource(str, enum.Enum):
    model = "model"
    manual = "manual"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


def _enum(cls):
    return Enum(cls, native_enum=False, length=32, validate_strings=True)


class Session(Base):
    """All shots taken on one calendar day (camera local time)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    shoot_date: Mapped[date | None] = mapped_column(Date, unique=True)  # None groups shots without any date
    title: Mapped[str | None] = mapped_column(String(200))
    location_lat: Mapped[float | None] = mapped_column(Float)
    location_lon: Mapped[float | None] = mapped_column(Float)
    location_label: Mapped[str | None] = mapped_column(String(300))
    country: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    analyse_without_location: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    shots: Mapped[list[Shot]] = relationship(back_populates="session")

    @property
    def has_location(self) -> bool:
        return self.location_lat is not None and self.location_lon is not None


class Shot(Base):
    """One captured frame: a single file, or a RAW+JPEG pair reviewed together."""

    __tablename__ = "shots"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    dump_folder: Mapped[str] = mapped_column(String(1000), index=True)  # folder relative to the dumpsite
    group_key: Mapped[str] = mapped_column(String(1200), unique=True)   # folder + stem + capture time
    captured_at: Mapped[datetime | None] = mapped_column(DateTime)       # camera local time, naive
    captured_at_source: Mapped[str] = mapped_column(String(16), default="exif")  # exif | file_mtime
    exif_datetime: Mapped[str | None] = mapped_column(String(40))        # raw EXIF string fed to the model
    gps_lat: Mapped[float | None] = mapped_column(Float)
    gps_lon: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[str | None] = mapped_column(String(16))      # exif | session | None
    country: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[ShotStatus] = mapped_column(_enum(ShotStatus), default=ShotStatus.pending, index=True)
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysed_with_location: Mapped[bool | None] = mapped_column(Boolean)
    analysis_provider: Mapped[str | None] = mapped_column(String(64))
    analysis_error: Mapped[str | None] = mapped_column(Text)
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0)  # reset on success; 3 failures -> error
    image_width: Mapped[int | None] = mapped_column(Integer)   # size of the analysed image, for box overlays
    image_height: Mapped[int | None] = mapped_column(Integer)
    min_confidence: Mapped[float | None] = mapped_column(Float)   # weakest top-1 confidence across detections
    bird_count: Mapped[int] = mapped_column(Integer, default=0)    # detections not rejected by the user
    preview_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    session: Mapped[Session] = relationship(back_populates="shots")
    files: Mapped[list[MediaFile]] = relationship(back_populates="shot", order_by="MediaFile.id")
    detections: Mapped[list[Detection]] = relationship(back_populates="shot", order_by="Detection.id", cascade="all, delete-orphan")

    @property
    def has_gps(self) -> bool:
        return self.gps_lat is not None and self.gps_lon is not None


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (Index("ix_media_files_dump_relpath_status", "dump_relpath", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shot_id: Mapped[int | None] = mapped_column(ForeignKey("shots.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    final_filename: Mapped[str] = mapped_column(String(600))  # original name, plus the user's suffix after a conflict
    filename_key: Mapped[str] = mapped_column(String(600), index=True)  # casefolded final_filename, for conflict checks
    extension: Mapped[str] = mapped_column(String(16))
    file_type: Mapped[str] = mapped_column(String(16))        # RAW, JPG, HEIC, TIFF, PNG
    is_raw: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[FileStatus] = mapped_column(_enum(FileStatus), default=FileStatus.in_dump, index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id"))
    dump_relpath: Mapped[str | None] = mapped_column(String(1500))
    published_relpath: Mapped[str | None] = mapped_column(String(1500))
    camera_make: Mapped[str | None] = mapped_column(String(120))
    camera_model: Mapped[str | None] = mapped_column(String(120))
    lens_model: Mapped[str | None] = mapped_column(String(200))
    focal_length: Mapped[float | None] = mapped_column(Float)
    f_number: Mapped[float | None] = mapped_column(Float)
    exposure_time: Mapped[float | None] = mapped_column(Float)
    iso: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    exif: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shot: Mapped[Shot | None] = relationship(back_populates="files")


class Detection(Base):
    """One bird found in a shot, or a species the user added by hand (no box)."""

    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(primary_key=True)
    shot_id: Mapped[int] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    box: Mapped[list[float] | None] = mapped_column(JsonType)       # [x0, y0, x1, y1] in analysed-image pixels
    box_confidence: Mapped[float | None] = mapped_column(Float)
    predictions: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)  # model top-k
    species_id: Mapped[str | None] = mapped_column(ForeignKey("species.species_id"), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)          # model confidence of the current species
    source: Mapped[DetectionSource] = mapped_column(_enum(DetectionSource), default=DetectionSource.model)
    status: Mapped[DetectionStatus] = mapped_column(_enum(DetectionStatus), default=DetectionStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    shot: Mapped[Shot] = relationship(back_populates="detections")
    species: Mapped[Species | None] = relationship()


class Species(Base):
    __tablename__ = "species"

    species_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    scientific_name: Mapped[str] = mapped_column(String(200), index=True)
    common_name: Mapped[str] = mapped_column(String(200), index=True)  # English
    family: Mapped[str | None] = mapped_column(String(200))
    order: Mapped[str | None] = mapped_column("order_name", String(200))
    names: Mapped[dict[str, str]] = mapped_column(JsonType, default=dict)  # language code -> common name


class LibraryFile(Base):
    """Every filename present in the published library, including files the app didn't put there."""

    __tablename__ = "library_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    relpath: Mapped[str] = mapped_column(String(1500), unique=True)
    filename_key: Mapped[str] = mapped_column(String(600), index=True)  # casefolded filename
    media_file_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id"))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # scan | publish | reanalyse | reindex_library
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.queued, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JsonType)


class WorkerState(Base):
    """Single row heartbeat the web app shows in its status bar."""

    __tablename__ = "worker_state"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), default="worker")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activity: Mapped[str | None] = mapped_column(String(300))
    providers: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    model_error: Mapped[str | None] = mapped_column(Text)
    last_scan: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
