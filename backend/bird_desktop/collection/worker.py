from __future__ import annotations

import json
import sys
import traceback
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..inference.cache import PredictionCache
from ..inference.corrections import ManualCorrectionStore
from ..inference.providers import ProviderSelector, RuntimeResolution
from ..inference.result_filter import PredictionResultFilter
from ..inference.services import PredictionService
from ..runtime.config import RuntimeConfig
from ..runtime.settings import DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD
from ..runtime.worker_io import WorkerLogRedirect, WorkerStatusWriter
from .services import COLLECTION_SCAN_MODE_RAW_JPEG, COLLECTION_SCAN_MODES, CollectionIndexer, CollectionScanner, CollectionStore
from .transport import CollectionStatusEncoder


class CollectionWorker:
    def __init__(self):
        self._prediction_service: PredictionService | None = None
        self._runtime_key: str | None = None
        self._log_path = sys.argv[1] if len(sys.argv) > 1 else None
        self._status_writer = WorkerStatusWriter()
        self._transport = CollectionStatusEncoder()


    def run(self) -> None:
        line = sys.stdin.readline()
        if not line.strip():
            self._write(self._error_status("No collection request received."))
            return

        try:
            request = json.loads(line)
            runtime = self._runtime_from_request(request)
            request.pop("_runtimeSelection", None)
            self._run_collection(request, runtime)
        except Exception as exc:
            payload = self._error_status(str(exc))
            payload["traceback"] = traceback.format_exc()
            self._write(payload)


    def _run_collection(self, request: dict[str, Any], runtime: RuntimeResolution) -> None:
        base_directory = Path(self._required_string(request, "baseDirectory"))
        scan_mode = self._scan_mode(request.get("scanMode"))
        threshold = self._threshold(request.get("acceptedClassificationThreshold"))
        images = CollectionScanner.scan_images(base_directory, scan_mode)
        self._write_log({"event": "scan_images", "baseDirectory": str(base_directory), "scanMode": scan_mode, "count": len(images)})
        store = CollectionStore()
        indexer = CollectionIndexer(store.thumbnail_data_url)
        config = RuntimeConfig.load()
        model_fingerprint = PredictionCache.model_fingerprint(config)
        image_keys = store.image_keys(images)
        cached_predictions_by_image = self._cached_predictions_by_image(images, model_fingerprint, config.min_classification_confidence, image_keys)
        processed_images: list[Path] = []
        total = len(images)
        completed = self._reuse_cached_predictions(images, cached_predictions_by_image, threshold, store, indexer, processed_images, image_keys)
        cached = completed
        errors = 0
        initial_status = {"state": "running", "total": total, "completed": completed, "cached": cached, "processed": 0, "processingElapsedSeconds": 0, "errors": errors, "currentFile": "", "message": f"Found {total} image(s).", "species": indexer.species()}
        self._write(initial_status)
        last_publication = time.monotonic()
        processing_started = last_publication

        def progress() -> dict[str, Any]:
            # Cache discovery/reuse is not representative of inference throughput.
            return {"cached": cached, "processed": completed - cached,
                    "processingElapsedSeconds": time.monotonic() - processing_started}

        service = self._prediction_service_for_runtime(runtime)
        self._model_fingerprint = model_fingerprint
        self._thumbnail_factory = indexer._thumbnail_data_url
        missing_images = [image_path for image_path in images if image_keys[image_path] not in cached_predictions_by_image]
        self._write_log({"event": "prediction_plan", "total": total, "cached": completed, "missing": len(missing_images), "providers": runtime.selection.selected})
        if missing_images:
            self._write({"state": "running", "total": total, "completed": completed, **progress(), "errors": errors, "currentFile": str(missing_images[0]), "message": f"Processing {len(missing_images)} image(s).", "species": indexer.species()})

        for image_path, result, error in self._prediction_results_for_chunk(missing_images, service, threshold):
            current_file = str(image_path)
            if error is None and result is not None:
                try:
                    records = indexer.add_prediction(image_path, result, threshold, self._event_thumbnails)
                    store.save_thumbnail_records(records)
                    processed_images.append(image_path)
                except Exception as exc:
                    error = exc
                    self._write_log({"event": "indexing_error", "path": current_file, "message": str(exc)})
            if error is not None:
                errors += 1
            completed += 1
            if time.monotonic() - last_publication >= 0.4:
                status = {"state": "running", "total": total, "completed": completed, **progress(), "errors": errors, "currentFile": current_file, "message": str(error) if error else "Collection scan running.", "species": indexer.species()}
                self._write(status)
                last_publication = time.monotonic()

        status = {"state": "done", "total": total, "completed": completed, **progress(), "errors": errors, "currentFile": "", "message": "Collection scan complete.", "species": indexer.species()}
        self._write(status)


    def _prediction_results_for_chunk(self, image_paths: list[Path], service: PredictionService, threshold: float) -> Iterator[tuple[Path, dict[str, Any] | None, Exception | None]]:
        requests = [{"imagePath": str(image_path), "includePreview": False} for image_path in image_paths]
        events = service.iter_prediction_events(requests, threshold, getattr(self, "_model_fingerprint", None),
                                                getattr(self, "_thumbnail_factory", None))
        try:
            while True:
                try:
                    with WorkerLogRedirect(self._log_path):
                        event = next(events)
                except StopIteration:
                    break
                self._event_thumbnails = event.thumbnails
                if event.error is not None:
                    self._write_log({"event": "prediction_error", "path": str(event.image_path), "message": str(event.error)})
                yield event.image_path, event.result, event.error
        finally:
            events.close()
            self._write_log({"event": "prediction_metrics", "stages": service.performance_metrics()})
            service.close()


    def _cached_predictions_by_image(self, images: list[Path], model_fingerprint: str, min_classification_confidence: float,
                                     image_keys: dict[Path, str] | None = None) -> dict[str, dict[str, Any]]:
        cache = PredictionCache()
        result_filter = PredictionResultFilter(min_classification_confidence)
        cached: dict[str, dict[str, Any]] = {}
        corrections = ManualCorrectionStore()
        entries_by_image = cache.lookup_many_for_model(images, model_fingerprint, path_keys=image_keys)
        entries = [(key, entry) for key, entry in entries_by_image.items() if isinstance(entry.get("result"), dict)]
        corrected = corrections.apply_many([(Path(entry["path"]), result_filter.apply(entry["result"])) for _, entry in entries],
                                           path_keys=image_keys, copy_results=False)
        cached.update({key: result for (key, _), result in zip(entries, corrected)})

        return cached


    def _reuse_cached_predictions(self, images: list[Path], cached_predictions_by_image: dict[str, dict[str, Any]], threshold: float, store: CollectionStore, indexer: CollectionIndexer, processed_images: list[Path], image_keys=None) -> int:
        completed = 0
        image_keys = image_keys if image_keys is not None else {path: store.image_key(path) for path in images}
        prepared = []
        for image_path in images:
            image_key = image_keys[image_path]
            cached_prediction = cached_predictions_by_image.get(image_key)
            if cached_prediction is not None:
                candidates = indexer._valid_occurrences(image_path, cached_prediction, threshold)
                keys = indexer.thumbnail_keys(image_path, [item["box"] for item in candidates], path_key=image_key)
                prepared.append((image_path, candidates, keys))
        store.prefetch_thumbnails([key for _, _, keys in prepared for key in keys])
        for image_path, candidates, keys in prepared:
            records = indexer._index_occurrences(image_path, candidates, keys)
            store.save_thumbnail_records(records)
            processed_images.append(image_path)
            completed += 1

        return completed


    def _required_string(self, request: dict[str, Any], key: str) -> str:
        value = request.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing collection field: {key}.")

        return value


    def _threshold(self, value: Any) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD

        if threshold < 0 or threshold > 1:
            return DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD

        return threshold


    def _scan_mode(self, value: Any) -> str:
        return value if value in COLLECTION_SCAN_MODES else COLLECTION_SCAN_MODE_RAW_JPEG


    def _runtime_from_request(self, request: dict[str, Any]) -> RuntimeResolution:
        selection = request.get("_runtimeSelection")
        if isinstance(selection, dict):
            return RuntimeResolution(selection=ProviderSelector.from_dict(selection))

        return RuntimeResolution(selection=ProviderSelector.from_dict({"selected": ["CPUExecutionProvider"]}))


    def _prediction_service_for_runtime(self, runtime: RuntimeResolution) -> PredictionService:
        runtime_key = json.dumps(runtime.selection.__dict__, sort_keys=True)
        if self._prediction_service is None or self._runtime_key != runtime_key:
            self._prediction_service = PredictionService(RuntimeConfig.load(), forced_runtime=runtime)
            self._runtime_key = runtime_key

        return self._prediction_service


    def _error_status(self, message: str) -> dict[str, Any]:
        return {"state": "error", "total": 0, "completed": 0, "errors": 1, "currentFile": "", "message": message, "error": message, "species": []}


    def _write(self, payload: dict[str, Any]) -> None:
        if not self._status_writer.write(self._transport.encode(payload)):
            raise SystemExit(0)


    def _write_log(self, payload: dict[str, Any]) -> None:
        if not self._log_path:
            return

        try:
            log_path = Path(self._log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **payload}
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        except OSError:
            return


if __name__ == "__main__":
    CollectionWorker().run()
