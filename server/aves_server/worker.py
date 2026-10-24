"""Background worker: watches the dumpsite, builds previews, runs the models and publishes.

Run with: python -m aves_server.worker
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

from sqlalchemy import select

from bird_desktop.media.exiftool import ExifTool
from bird_desktop.media.image_io import ImageLoader

from .catalog import SpeciesCatalog
from .config import ServerConfig
from .db import Database
from .jobs import claim_next, recover_interrupted
from .media import MetadataReader, RestartingExifTool
from .models import Job, JobStatus, Shot, ShotStatus, WorkerState
from .previews import PreviewStore
from .publisher import LibraryIndexer, Publisher
from .scanner import DumpScanner
from .settings import SettingsStore
from .shots import primary_file


logger = logging.getLogger("aves_server.worker")

MODEL_RETRY_SECONDS = 300
LIBRARY_REINDEX_SECONDS = 6 * 3600
IDLE_SLEEP_SECONDS = 2.0
ERROR_SLEEP_SECONDS = 10.0


class Worker:
    def __init__(self, config: ServerConfig):
        self.config = config
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(config.database_url)
        self.database.create_schema()
        executable = ExifTool.find()
        if executable is None:
            raise RuntimeError("ExifTool is not installed")
        self.exiftool = RestartingExifTool(executable, timeout=180)
        self.previews = PreviewStore(config.cache_dir)
        self.scanner = DumpScanner(config, self.database, MetadataReader(self.exiftool))
        self.publisher = Publisher(config, self.database)
        self.indexer = LibraryIndexer(config, self.database)
        self.analyzer = None
        self._model_error: str | None = None
        self._model_attempt_at = 0.0
        self._last_poll = float("-inf")  # so the first scan and index run at startup, whatever the uptime
        self._last_reindex = float("-inf")
        self._stopping = False
        self._idle_activity: str | None = None

    def stop(self, *_args) -> None:
        logger.info("Stopping after the current step")
        self._stopping = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        with self.database.session() as db:
            recover_interrupted(db)
            if self._labels_path().exists():
                SpeciesCatalog.load(self._labels_path()).seed(db)
        self._heartbeat("Starting")
        try:
            while not self._stopping:
                try:
                    busy = self.tick()
                except Exception as exc:
                    # Never let one failing step (database hiccup, full disk, ...) take the whole worker down.
                    logger.exception("Worker step failed")
                    self._safe_heartbeat(f"Error: {exc}"[:300])
                    time.sleep(ERROR_SLEEP_SECONDS)
                    continue
                if not busy:
                    self._heartbeat(self._idle_activity or "Idle")
                    time.sleep(IDLE_SLEEP_SECONDS)
        finally:
            self.exiftool.close()

    def tick(self) -> bool:
        """Does one unit of work. Returns False when there was nothing to do."""
        with self.database.session() as db:
            settings = SettingsStore.load(db)
            job = claim_next(db)
        if job is not None:
            self._run_job(job)
            return True

        now = time.monotonic()
        if now - self._last_reindex >= LIBRARY_REINDEX_SECONDS:
            self._last_reindex = now
            self._safely("Indexing the published library", self.indexer.reindex)
        if settings.auto_scan and now - self._last_poll >= settings.poll_interval_seconds:
            self._last_poll = now
            self._scan(quick=False)

        if self._build_previews():
            return True
        return self._analyse()

    def _run_job(self, job: Job) -> None:
        self._heartbeat(f"Running {job.kind.replace('_', ' ')}")
        try:
            if job.kind == "scan":
                result = self._scan(quick=True, job_id=job.id)
            elif job.kind == "publish":
                def progress(done: int, total: int) -> None:
                    self._job_progress(job.id, done, total)
                    self._heartbeat(f"Publishing ({done}/{total})")

                result = self.publisher.publish(self._publish_targets(job), progress=progress).to_dict()
            elif job.kind == "reindex_library":
                result = self.indexer.reindex()
            elif job.kind == "reanalyse":
                result = self._requeue(job.payload.get("shot_ids", []))
            else:
                raise ValueError(f"Unknown job kind {job.kind}")
            self._finish(job.id, JobStatus.done, result)
        except Exception as exc:
            logger.exception("Job %s (%s) failed", job.id, job.kind)
            self._finish(job.id, JobStatus.failed, {}, str(exc))

    def _publish_targets(self, job: Job) -> list[int]:
        with self.database.session() as db:
            query = select(Shot.id).where(Shot.status == ShotStatus.confirmed).order_by(Shot.captured_at, Shot.id)
            if job.payload.get("shot_ids"):
                query = query.where(Shot.id.in_(job.payload["shot_ids"]))
            elif job.payload.get("session_id"):
                query = query.where(Shot.session_id == job.payload["session_id"])
            return list(db.scalars(query))

    def _requeue(self, shot_ids: list[int]) -> dict:
        with self.database.session() as db:
            shots = list(db.scalars(select(Shot).where(Shot.id.in_(shot_ids), Shot.status.in_([ShotStatus.review, ShotStatus.error]))))
            for shot in shots:
                shot.status = ShotStatus.pending
                shot.analysis_attempts = 0
            return {"queued": len(shots)}

    def _scan(self, quick: bool, job_id: int | None = None) -> dict:
        self._heartbeat("Scanning the dumpsite")

        def progress(done: int, total: int) -> None:
            self._job_progress(job_id, done, total)
            self._heartbeat(f"Adding new photos ({done}/{total})")

        try:
            result = self.scanner.scan(quick=quick, progress=progress).to_dict()
        except Exception as exc:
            logger.exception("Scan failed")
            self._record_scan({"errors": [str(exc)]})
            if job_id:
                raise
            return {}
        self._record_scan(result)
        if result.get("ingested") or result.get("duplicates"):
            logger.info("Scan: %s", result)
        return result

    def _record_scan(self, result: dict) -> None:
        with self.database.session() as db:
            state = self._state(db)
            state.last_scan_at = datetime.now(timezone.utc)
            state.last_scan = result

    def _build_previews(self, limit: int = 12) -> bool:
        """Previews for shots that won't be analysed soon (e.g. waiting for a location), so you can see them."""
        with self.database.session() as db:
            shots = list(db.scalars(select(Shot).where(Shot.preview_ready.is_(False), Shot.status.in_(
                [ShotStatus.awaiting_location, ShotStatus.review, ShotStatus.confirmed, ShotStatus.ignored])).limit(limit)))
            if not shots:
                return False
            self._heartbeat(f"Building previews ({len(shots)})")
            for shot in shots:
                file = primary_file(shot)
                if file is None:
                    shot.preview_ready = True
                    continue
                try:
                    loaded = ImageLoader.load(self.config.dump_dir / file.dump_relpath, session=self.exiftool)
                except Exception as exc:
                    logger.warning("Preview failed for %s: %s", file.dump_relpath, exc)
                    shot.preview_ready = True  # don't retry forever; the UI shows a placeholder
                    continue
                try:
                    shot.image_width, shot.image_height = self.previews.save(shot.id, loaded.image)
                    shot.preview_ready = True
                finally:
                    ImageLoader.cleanup(loaded)
        return True

    def _analyse(self) -> bool:
        analyzer = self._ensure_analyzer()
        if analyzer is None:
            return False
        with self.database.session() as db:
            pending = db.scalar(select(Shot.id).where(Shot.status == ShotStatus.pending).limit(1))
        if pending is None:
            return False
        from .analysis import BackendFailed
        from .engine import RemoteUnavailable

        self._heartbeat("Identifying birds")
        try:
            outcome = analyzer.analyse_pending(limit=8)
        except RemoteUnavailable as exc:
            self._idle_activity = f"Waiting for the GPU inference node: {exc}"[:300]
            return False
        except BackendFailed as exc:
            # A whole batch failed: assume the models or GPU broke. Reloading re-runs the GPU check,
            # which falls back to the CPU if the card is gone; photos that keep failing end up in Errors.
            logger.error("Every photo in the batch failed (%s); reloading the models", exc)
            self.analyzer = None
            self._model_attempt_at = 0.0
            return True
        self._idle_activity = None
        return outcome.analysed + outcome.failed > 0

    def _ensure_analyzer(self):
        if self.analyzer is not None:
            return self.analyzer
        if time.monotonic() - self._model_attempt_at < MODEL_RETRY_SECONDS and self._model_attempt_at:
            return None
        self._model_attempt_at = time.monotonic()
        self._heartbeat("Loading the Aves Caesar models")
        try:
            from .analysis import Analyzer

            self.analyzer = Analyzer(self.config, self.database, self.previews, self.exiftool)
            with self.database.session() as db:  # detections reference the species table
                SpeciesCatalog.load(self._labels_path()).seed(db)
            self._model_error = None
            logger.info("Models loaded, providers: %s", self.analyzer.providers)
        except Exception as exc:
            logger.exception("Could not load the models")
            self._model_error = str(exc)
        self._heartbeat("Models loaded" if self.analyzer else "Models unavailable")
        return self.analyzer

    def _labels_path(self):
        return self.config.models_dir / "species_mapping_v2.csv"

    def _safely(self, activity: str, action) -> None:
        self._heartbeat(activity)
        try:
            action()
        except Exception:
            logger.exception("%s failed", activity)

    def _job_progress(self, job_id: int | None, done: int, total: int) -> None:
        if job_id is None:
            return
        with self.database.session() as db:
            job = db.get(Job, job_id)
            job.progress, job.total = done, total

    def _finish(self, job_id: int, status: JobStatus, result: dict, message: str | None = None) -> None:
        with self.database.session() as db:
            job = db.get(Job, job_id)
            job.status, job.result, job.message = status, result, message
            job.finished_at = datetime.now(timezone.utc)

    def _state(self, db) -> WorkerState:
        state = db.scalar(select(WorkerState).where(WorkerState.name == "worker"))
        if state is None:
            state = WorkerState(name="worker")
            db.add(state)
        return state

    def _heartbeat(self, activity: str) -> None:
        with self.database.session() as db:
            state = self._state(db)
            state.heartbeat_at = datetime.now(timezone.utc)
            state.activity = activity
            state.model_error = self._model_error
            if self.analyzer is not None:
                state.providers = self.analyzer.providers

    def _safe_heartbeat(self, activity: str) -> None:
        try:
            self._heartbeat(activity)
        except Exception:
            logger.exception("Could not record the worker status")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Worker(ServerConfig.from_env()).run_forever()


if __name__ == "__main__":
    main()
