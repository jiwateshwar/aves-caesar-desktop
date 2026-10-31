from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from bird_desktop.inference.pipeline import PipelineInput
from bird_desktop.inference.result_filter import PredictionResultFilter
from bird_desktop.media.image_io import ImageLoader

from .config import ServerConfig
from .db import Database
from .engine import RemoteUnavailable, backend_from_env, load_runtime_config
from .dating import model_datetime
from .locations import analysis_coordinates
from .models import Detection, DetectionSource, DetectionStatus, Shot, ShotStatus
from .previews import PreviewStore
from .shots import primary_file, refresh_shot_summary


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class BackendFailed(RuntimeError):
    """Every photo of a batch failed: the models (or the GPU) are broken, not the photos."""


@dataclass
class AnalysisOutcome:
    analysed: int = 0
    failed: int = 0


class Analyzer:
    """Runs the Aves Caesar detector and classifier on queued shots, locally or on an inference node."""

    def __init__(self, config: ServerConfig, database: Database, previews: PreviewStore, exiftool_session=None, backend=None):
        self.config = config
        self.database = database
        self.previews = previews
        self.exiftool_session = exiftool_session
        self.runtime_config = load_runtime_config(config.models_dir)
        self.backend = backend or backend_from_env(config.models_dir)
        self.filter = PredictionResultFilter(self.runtime_config.min_classification_confidence)

    @property
    def providers(self) -> dict:
        return self.backend.providers

    def analyse_pending(self, limit: int = 8) -> AnalysisOutcome:
        outcome = AnalysisOutcome()
        if getattr(self.backend, "waiting", lambda: False)():
            raise RemoteUnavailable(self.backend.remote_error or "Waiting for the inference node")
        loaded, inputs = [], []
        with self.database.session() as db:
            shots = list(db.scalars(select(Shot).where(Shot.status == ShotStatus.pending).order_by(Shot.captured_at, Shot.id).limit(limit)))
            for shot in shots:
                if shot.analysis_attempts >= MAX_ATTEMPTS:
                    self._give_up(shot, shot.analysis_error or "Identification failed repeatedly")
                    outcome.failed += 1
                    continue
                file = primary_file(shot)
                if file is None:
                    shot.status, shot.analysis_error = ShotStatus.error, "No file of this shot is left in the dumpsite"
                    outcome.failed += 1
                    continue
                try:
                    image = ImageLoader.load(self.config.dump_dir / file.dump_relpath, session=self.exiftool_session)
                except Exception as exc:
                    logger.exception("Could not open %s", file.dump_relpath)
                    shot.status, shot.analysis_error = ShotStatus.error, f"Could not open {file.original_filename}: {exc}"
                    outcome.failed += 1
                    continue
                # Counted before inference, so a photo that crashes the whole process is still caught after a restart.
                shot.analysis_attempts += 1
                latitude, longitude = analysis_coordinates(shot, shot.session)
                loaded.append((shot.id, image, latitude is not None))
                inputs.append(PipelineInput(image.image, latitude, longitude, model_datetime(shot)))

        try:
            try:
                results = self._predict_isolated(inputs) if inputs else []
            except RemoteUnavailable:
                self._uncount([shot_id for shot_id, _image, _loc in loaded])
                raise
            with self.database.session() as db:
                for (shot_id, image, with_location), result in zip(loaded, results):
                    shot = db.get(Shot, shot_id)
                    if isinstance(result, Exception):
                        shot.analysis_error = f"Identification failed: {result}"
                        if shot.analysis_attempts >= MAX_ATTEMPTS:
                            self._give_up(shot, shot.analysis_error)
                        outcome.failed += 1
                        continue
                    birds = self.filter.apply({"birds": result.birds})["birds"]
                    if not shot.preview_ready:
                        self.previews.save(shot_id, image.image)
                        shot.preview_ready = True
                    self._store(shot, birds, image.image.size, with_location, result.provider)
                    outcome.analysed += 1
        finally:
            for _shot_id, image, _with_location in loaded:
                ImageLoader.cleanup(image)

        # One photo failing on its own says nothing about the GPU; several failing together does.
        if len(inputs) > 1 and all(isinstance(result, Exception) for result in results):
            raise BackendFailed(str(results[0]))
        return outcome

    def _predict_isolated(self, inputs: list[PipelineInput]) -> list:
        """Predicts a batch; if it fails, retries photo by photo so one bad photo can't block the others."""
        try:
            return self.backend.predict_many(inputs)
        except RemoteUnavailable:
            raise
        except Exception as exc:
            if len(inputs) == 1:
                logger.exception("Identification failed")
                return [exc]
            logger.warning("A batch failed (%s); retrying photo by photo", exc)
        results = []
        for item in inputs:
            try:
                results.append(self.backend.predict_many([item])[0])
            except RemoteUnavailable:
                raise
            except Exception as exc:
                logger.exception("Identification failed")
                results.append(exc)
        return results

    def _uncount(self, shot_ids: list[int]) -> None:
        # Waiting for the inference node isn't the photo's fault.
        with self.database.session() as db:
            for shot in db.scalars(select(Shot).where(Shot.id.in_(shot_ids))):
                shot.analysis_attempts = max(0, shot.analysis_attempts - 1)

    @staticmethod
    def _give_up(shot: Shot, message: str) -> None:
        shot.status = ShotStatus.error
        shot.analysis_error = f"{message} (gave up after {MAX_ATTEMPTS} attempts; use “Try again” to retry)"

    @staticmethod
    def _store(shot: Shot, birds: list[dict], size: tuple[int, int], with_location: bool, providers: list[str]) -> None:
        # Re-analysis replaces model detections but keeps species the user added by hand.
        shot.detections = [d for d in shot.detections if d.source == DetectionSource.manual]
        for bird in birds:
            predictions = [{"species_id": c["species_id"], "confidence": round(float(c["confidence"]), 5)} for c in bird["classification"]]
            top = predictions[0]
            shot.detections.append(Detection(box=[round(v, 1) for v in bird["box"]], box_confidence=round(float(bird["box_confidence"]), 4),
                                             predictions=predictions, species_id=top["species_id"], confidence=top["confidence"],
                                             source=DetectionSource.model, status=DetectionStatus.pending))
        refresh_shot_summary(shot)
        shot.image_width, shot.image_height = size
        shot.analysed_at = datetime.now(timezone.utc)
        shot.analysed_with_location = with_location
        shot.analysis_provider = ",".join(p.replace("ExecutionProvider", "") for p in providers)
        shot.analysis_error = None
        shot.analysis_attempts = 0
        shot.status = ShotStatus.review
