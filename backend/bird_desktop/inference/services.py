from __future__ import annotations

import base64
from contextlib import nullcontext
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from PIL import Image

from ..data.bird_names import BirdNamesLoader
from ..media.image_io import ImageLoader, LoadedImage
from ..media.exiftool import ExifTool
from ..media.exiftool_session import ExifToolSession
from .streaming import PredictionStream, PredictionEvent
from ..runtime.config import RuntimeConfig
from .cache import PredictionCache
from .corrections import ManualCorrectionStore
from .pipeline import BirdOnnxPipeline, PipelineInput, PipelineResult
from .providers import RuntimeProbe, RuntimeResolution
from .result_filter import PredictionResultFilter


@dataclass
class PreparedPrediction:
    source_path: Path
    latitude: float | None
    longitude: float | None
    datetime_text: str | None
    include_preview: bool
    model_fingerprint: str
    location_fingerprint: str
    loaded: LoadedImage | None = None


class PredictionService:
    def __init__(self, config: RuntimeConfig, runtime_probe: RuntimeProbe | None = None, forced_runtime: RuntimeResolution | None = None):
        self.config = config
        self._runtime_probe = runtime_probe
        self._forced_runtime = forced_runtime
        self._pipeline: BirdOnnxPipeline | None = None
        self._prediction_cache = PredictionCache()
        self._names = BirdNamesLoader(config.labels_path)
        self._corrections = ManualCorrectionStore(names=self._names)


    def clear_cache(self) -> int:
        return self._prediction_cache.clear()


    def predict(self, request: dict[str, Any], accepted_classification_threshold: float) -> dict[str, Any]:
        return self.predict_many([request], accepted_classification_threshold)[0]


    def predict_many(self, requests: list[dict[str, Any]], accepted_classification_threshold: float) -> list[dict[str, Any]]:
        return list(self.iter_predict_many(requests, accepted_classification_threshold))


    def iter_predict_many(self, requests: list[dict[str, Any]], accepted_classification_threshold: float):
        if not requests:
            return

        if any(request.get("includePreview") is not False for request in requests):
            for chunk in self._chunks(requests, self._request_chunk_size()):
                for result in self._predict_many_chunk(chunk, accepted_classification_threshold):
                    yield result

            return

        events = self.iter_prediction_events(requests, accepted_classification_threshold)
        try:
            for event in events:
                if event.error is not None:
                    raise event.error
                yield event.result
        finally:
            events.close()


    def iter_prediction_events(self, requests: list[dict[str, Any]], accepted_classification_threshold: float,
                               model_fingerprint: str | None = None, thumbnail_factory=None,
                               persistent_exif: bool = True) -> Iterator[PredictionEvent]:
        if not requests:
            return
        fingerprint = model_fingerprint or PredictionCache.model_fingerprint(self.config)
        executable = ExifTool.find() if persistent_exif else None
        with ExifToolSession(executable) if executable else nullcontext(None) as session:
            metadata_loader = None
            image_loader = None
            if session is not None:
                metadata_loader = lambda paths: {key: ImageLoader.metadata_from_tags(tags)
                                                for key, tags in session.read_tags_many(paths).items()}
                image_loader = lambda path: ImageLoader.load(path, session=session)
            runner = PredictionStream(self, requests, accepted_classification_threshold, fingerprint,
                                      thumbnail_factory, metadata_loader, image_loader)
            iterator = runner.run()
            try:
                yield from iterator
            finally:
                iterator.close()


    def close(self) -> None:
        if self._pipeline is not None and hasattr(self._pipeline, "close"):
            self._pipeline.close()
        self._pipeline = None


    def performance_metrics(self) -> dict[str, Any]:
        metrics = getattr(self._pipeline, "metrics", None)
        return {"stream": getattr(self, "last_stream_metrics", {}),
                "onnx": metrics.snapshot() if metrics is not None else {}}


    def _predict_many_chunk(self, requests: list[dict[str, Any]], accepted_classification_threshold: float) -> list[dict[str, Any]]:
        model_fingerprint = PredictionCache.model_fingerprint(self.config)
        results: list[dict[str, Any] | None] = [None for _request in requests]
        pending: list[tuple[int, PreparedPrediction]] = []
        for index, request in enumerate(requests):
            prepared = self._prepare_prediction(request, model_fingerprint)
            cached_result = self._cached_result(request, prepared, accepted_classification_threshold)
            if cached_result is not None:
                results[index] = cached_result
            else:
                pending.append((index, prepared))

        if pending:
            loaded_predictions: list[PreparedPrediction] = []
            try:
                for _index, prepared in pending:
                    prepared.loaded = ImageLoader.load(prepared.source_path)
                    loaded_predictions.append(prepared)

                pipeline_inputs = [
                    PipelineInput(prepared.loaded.image, prepared.latitude, prepared.longitude, prepared.datetime_text)
                    for prepared in loaded_predictions
                    if prepared.loaded is not None
                ]
                pipeline = self._get_pipeline()
                predictions = pipeline.predict_many(pipeline_inputs) if hasattr(pipeline, "predict_many") else [pipeline.predict(item.image, item.latitude, item.longitude, item.datetime_text) for item in pipeline_inputs]
                for (index, prepared), prediction in zip(pending, predictions):
                    results[index] = self._prediction_result(prepared, prediction, accepted_classification_threshold)
            finally:
                for prepared in loaded_predictions:
                    if prepared.loaded is not None:
                        ImageLoader.cleanup(prepared.loaded)

        return [result if result is not None else {"birds": []} for result in results]


    def _prepare_prediction(self, request: dict[str, Any], model_fingerprint: str, metadata=None) -> PreparedPrediction:
        image_path = request.get("imagePath")
        if not image_path:
            raise ValueError("No image path provided.")

        source_path = Path(str(image_path))
        latitude = self._optional_float(request.get("latitude"))
        longitude = self._optional_float(request.get("longitude"))
        metadata = metadata if metadata is not None else ImageLoader.read_metadata(source_path)
        datetime_text = request.get("datetime") or metadata.datetime_text
        include_preview = request.get("includePreview") is not False
        if latitude is None:
            latitude = metadata.latitude

        if longitude is None:
            longitude = metadata.longitude

        location_fingerprint = PredictionCache.location_fingerprint(latitude, longitude, datetime_text)

        return PreparedPrediction(source_path=source_path, latitude=latitude, longitude=longitude, datetime_text=datetime_text, include_preview=include_preview, model_fingerprint=model_fingerprint, location_fingerprint=location_fingerprint)


    def _cached_result(self, request: dict[str, Any], prepared: PreparedPrediction, accepted_classification_threshold: float) -> dict[str, Any] | None:
        if request.get("reprocess") is True:
            return None

        cached = self._prediction_cache.lookup(prepared.source_path, prepared.model_fingerprint, prepared.location_fingerprint)
        if cached is None:
            return None

        result = self._filtered_result(cached.get("result", {}))
        result["acceptedClassificationThreshold"] = accepted_classification_threshold
        result["usedLatitude"] = prepared.latitude
        result["usedLongitude"] = prepared.longitude
        result["usedDatetime"] = prepared.datetime_text
        result["source"] = "cache"
        result = self._corrections.apply(prepared.source_path, result)
        if prepared.include_preview:
            return self._with_loaded_preview(prepared.source_path, result)

        return result


    def _prediction_result(self, prepared: PreparedPrediction, prediction: PipelineResult, accepted_classification_threshold: float) -> dict[str, Any]:
        if prepared.loaded is None:
            raise RuntimeError("Prediction image was not loaded.")

        result = self._prediction_result_from_values(prepared, prediction, prepared.loaded.image.width, prepared.loaded.image.height, accepted_classification_threshold)
        if prepared.include_preview:
            return self._with_preview(result, prepared.loaded.image)

        return result


    def _prediction_result_from_values(self, prepared: PreparedPrediction, prediction: PipelineResult, width: int, height: int, accepted_classification_threshold: float) -> dict[str, Any]:
        cached_result = {"provider": prediction.provider, "birds": prediction.birds, "lowConfidenceThreshold": self.config.low_confidence_threshold, "usedLatitude": prepared.latitude, "usedLongitude": prepared.longitude, "usedDatetime": prepared.datetime_text, "width": width, "height": height}
        self._prediction_cache.store(prepared.source_path, prepared.model_fingerprint, prepared.location_fingerprint, cached_result)
        result = self._filtered_result(cached_result)
        result = self._corrections.apply(prepared.source_path, result)
        result["acceptedClassificationThreshold"] = accepted_classification_threshold
        result["source"] = "prediction"
        return result


    def cached_prediction_preview(self, request: dict[str, Any], accepted_classification_threshold: float) -> dict[str, Any]:
        image_path = request.get("imagePath")
        if not image_path:
            raise ValueError("No image path provided.")

        source_path = Path(str(image_path))
        latitude = self._optional_float(request.get("latitude"))
        longitude = self._optional_float(request.get("longitude"))
        datetime_text = request.get("datetime") if "datetime" in request else None
        metadata = None
        if "latitude" not in request or "longitude" not in request or "datetime" not in request:
            metadata = ImageLoader.read_metadata(source_path)

        if latitude is None and "latitude" not in request and metadata is not None:
            latitude = metadata.latitude

        if longitude is None and "longitude" not in request and metadata is not None:
            longitude = metadata.longitude

        if "datetime" not in request and metadata is not None:
            datetime_text = metadata.datetime_text

        model_fingerprint = PredictionCache.model_fingerprint(self.config)
        location_fingerprint = PredictionCache.location_fingerprint(latitude, longitude, datetime_text)
        cached = self._prediction_cache.lookup(source_path, model_fingerprint, location_fingerprint)
        if cached is None:
            raise ValueError("Cached prediction not found. Scan the collection again.")

        result = self._filtered_result(cached.get("result", {}))
        result["acceptedClassificationThreshold"] = accepted_classification_threshold
        result["usedLatitude"] = latitude
        result["usedLongitude"] = longitude
        result["usedDatetime"] = datetime_text
        result["source"] = "cache"
        result = self._corrections.apply(source_path, result)
        return self._with_loaded_preview(source_path, result)


    def thumbnail_data_url(self, path: Path) -> str | None:
        try:
            loaded = ImageLoader.load(path)
            try:
                return self._image_data_url(loaded.image, 160, 82)
            finally:
                ImageLoader.cleanup(loaded)
        except Exception:
            return None


    def _with_loaded_preview(self, image_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        loaded = ImageLoader.load(image_path)
        try:
            return self._with_preview(result, loaded.image)
        finally:
            ImageLoader.cleanup(loaded)


    def _with_preview(self, result: dict[str, Any], image: Image.Image) -> dict[str, Any]:
        result["previewDataUrl"] = self._preview_data_url(image)
        result["width"] = image.width
        result["height"] = image.height
        return result


    def _get_pipeline(self) -> BirdOnnxPipeline:
        if self._pipeline is None:
            self._pipeline = BirdOnnxPipeline(self.config, self._forced_runtime or self._runtime())

        return self._pipeline


    def _filtered_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"birds": []}

        return PredictionResultFilter(self.config.min_classification_confidence).apply(result)


    def _runtime(self) -> RuntimeResolution:
        if self._runtime_probe is None:
            raise RuntimeError("Runtime probe is not available.")

        return self._runtime_probe.runtime()



    def _preview_data_url(self, image: Image.Image) -> str:
        return self._image_data_url(image, None, 92)


    def _image_data_url(self, image: Image.Image, max_size: int | None, quality: int) -> str:
        output = BytesIO()
        preview = image.copy()
        if max_size is not None:
            preview.thumbnail((max_size, max_size))

        preview.save(output, format="JPEG", quality=quality)
        preview.close()
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


    def _optional_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None

        return float(value)


    def _request_chunk_size(self) -> int:
        return max(1, int(getattr(self.config, "detector_batch_size", 1)))


    def _chunks(self, values: list[Any], size: int) -> list[list[Any]]:
        return [values[index:index + size] for index in range(0, len(values), size)]
