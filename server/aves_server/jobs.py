from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .models import Job, JobStatus


JOB_KINDS = {"scan", "publish", "reindex_library", "reanalyse"}


def enqueue(db: OrmSession, kind: str, payload: dict[str, Any] | None = None) -> Job:
    if kind not in JOB_KINDS:
        raise ValueError(f"Unknown job kind {kind}")
    if kind in ("scan", "reindex_library"):
        # Pressing "Scan now" twice shouldn't queue two scans.
        existing = db.scalar(select(Job).where(Job.kind == kind, Job.status.in_([JobStatus.queued, JobStatus.running])).limit(1))
        if existing is not None:
            return existing
    job = Job(kind=kind, payload=payload or {})
    db.add(job)
    db.flush()
    return job


def claim_next(db: OrmSession) -> Job | None:
    query = select(Job).where(Job.status == JobStatus.queued).order_by(Job.id).limit(1)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = db.scalar(query)
    if job is not None:
        job.status = JobStatus.running
        job.started_at = datetime.now(timezone.utc)
    return job


def recover_interrupted(db: OrmSession) -> int:
    """Jobs left 'running' by a worker that was stopped mid-way are retried."""
    jobs = list(db.scalars(select(Job).where(Job.status == JobStatus.running)))
    for job in jobs:
        job.status = JobStatus.queued
        job.message = "Restarted after the worker was interrupted"
    return len(jobs)
