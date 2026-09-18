from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Iterator

import numpy as np

from ..media.image_io import ImageLoader
from .metrics import InferenceMetrics
from .pipeline import PipelineInput, PipelineResult
from .preprocess import BirdImagePreprocessor, ClassifierCropJob
from .sessions import stable_classifier_batch_size


@dataclass
class PredictionEvent:
    index: int
    image_path: Path
    result: dict | None = None
    error: Exception | None = None
    thumbnails: dict[str, str] = field(default_factory=dict)


@dataclass
class ImageContext:
    index: int
    prepared: Any
    loaded: Any
    tensor: np.ndarray
    scale: tuple[float, float]
    width: int
    height: int
    birds: dict[int, dict] = field(default_factory=dict)
    thumbnails: dict[str, str] = field(default_factory=dict)
    remaining_preprocessing: int = 0
    remaining_classifications: int = 0
    error: Exception | None = None
    providers: list[str] = field(default_factory=list)
    closed: bool = False


class PredictionStream:
    """One bounded pipeline per call. Only the consumer persists predictions.

    Admission bounds ordered result buffering; image_slots separately bounds decoded
    images. Every queue operation is cancellable. Loaded images outlive all crop
    readers, even when a crop or a model fails.
    """

    def __init__(self, service, requests: list[dict], threshold: float, fingerprint: str,
                 thumbnail_factory: Callable | None = None, metadata_loader=None, image_loader=None):
        self.service = service
        self.config = service.config
        self.requests = requests
        self.threshold = threshold
        self.fingerprint = fingerprint
        self.thumbnail_factory = thumbnail_factory
        self.metadata_loader = metadata_loader
        self.image_loader = image_loader or ImageLoader.load
        self.metrics = InferenceMetrics()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.contexts: dict[int, ImageContext] = {}
        self.workers = max(1, self.config.preparation_workers)
        self.window = max(32, self.config.classifier_batch_size * 2, self.config.detector_batch_size * 4)
        self.admission = threading.Semaphore(self.window)
        self.image_slots = threading.Semaphore(max(4, self.config.detector_batch_size * 2))
        self.detect_queue = queue.Queue(maxsize=max(2, self.config.detector_batch_size * 2))
        self.crop_queue = queue.Queue(maxsize=max(8, self.config.classifier_batch_size))
        self.classify_queue = queue.Queue(maxsize=max(8, self.config.classifier_batch_size * 2))
        self.finished_queue = queue.Queue(maxsize=self.window)
        self.sentinel = object()
        self.pipeline = None
        self.fatal_error: Exception | None = None

    def _put(self, target, value) -> bool:
        with self.metrics.measure("queue_wait"):
            while not self.cancelled.is_set():
                try:
                    target.put(value, timeout=0.05)
                    return True
                except queue.Full:
                    pass
        return False

    def _acquire(self, semaphore) -> bool:
        while not self.cancelled.is_set():
            if semaphore.acquire(timeout=0.05):
                return True
        return False

    def _guard(self, function):
        try:
            function()
        except Exception as exc:
            self.fatal_error = exc
            self.cancelled.set()

    def run(self) -> Iterator[PredictionEvent]:
        threads = [threading.Thread(target=self._guard, args=(function,), name=f"bird-stream-{name}")
                   for name, function in [("source", self._produce), ("detect", self._detect),
                                           ("classify", self._classify),
                                           *[(f"crop-{i}", self._crop) for i in range(self.workers)]]]
        for thread in threads:
            thread.start()
        ready: dict[int, PredictionEvent] = {}
        next_index = 0
        try:
            while next_index < len(self.requests):
                if self.fatal_error:
                    raise self.fatal_error
                try:
                    item = self.finished_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(item, PredictionEvent):
                    event = item
                else:
                    with self.lock:
                        self.contexts.pop(item.index, None)
                    try:
                        if item.error:
                            raise item.error
                        with self.metrics.measure("persistence"):
                            result = self.service._prediction_result_from_values(
                                item.prepared, PipelineResult(item.providers, [item.birds[i] for i in sorted(item.birds)]),
                                item.width, item.height, self.threshold)
                        event = PredictionEvent(item.index, item.prepared.source_path, result, thumbnails=item.thumbnails)
                    except Exception as exc:
                        event = PredictionEvent(item.index, item.prepared.source_path, error=exc)
                ready[event.index] = event
                while next_index in ready:
                    event = ready.pop(next_index)
                    next_index += 1
                    self.admission.release()
                    yield event
        finally:
            self.cancelled.set()
            # Native inference cannot be interrupted safely. Wait for its return
            # before releasing image buffers or permitting another run.
            for thread in threads:
                thread.join()
            for context in list(self.contexts.values()):
                self._close_image(context)
            self.contexts.clear()
            self.service.last_stream_metrics = self.metrics.snapshot()

    def _load(self, index, request, metadata):
        path = Path(str(request.get("imagePath", "")))
        loaded = None
        acquired = False
        try:
            with self.metrics.measure("metadata_and_cache"):
                prepared = self.service._prepare_prediction(request, self.fingerprint, metadata)
                cached = self.service._cached_result(request, prepared, self.threshold)
            if cached is not None:
                return PredictionEvent(index, path, cached)
            acquired = self._acquire(self.image_slots)
            if not acquired:
                return None
            with self.metrics.measure("decode"):
                loaded = self.image_loader(prepared.source_path)
            with self.metrics.measure("detector_preprocessing"):
                tensor, _, scale = BirdImagePreprocessor.detector_image(loaded.image)
            context = ImageContext(index, prepared, loaded, tensor[0], scale, loaded.image.width, loaded.image.height)
            with self.lock:
                self.contexts[index] = context
            return context
        except Exception as exc:
            if loaded is not None:
                ImageLoader.cleanup(loaded)
            if acquired:
                self.image_slots.release()
            return PredictionEvent(index, path, error=exc)

    def _produce(self):
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="bird-load") as pool:
            for start in range(0, len(self.requests), 16):
                chunk = self.requests[start:start + 16]
                metadata = {}
                if self.metadata_loader:
                    with self.metrics.measure("metadata"):
                        metadata = self.metadata_loader([Path(str(r["imagePath"])) for r in chunk if r.get("imagePath")])
                futures = []
                for offset, request in enumerate(chunk):
                    if not self._acquire(self.admission):
                        return
                    futures.append(pool.submit(self._load, start + offset, request, metadata.get(str(Path(str(request.get("imagePath", "")))))))
                for future in futures:
                    if self.cancelled.is_set():
                        return
                    item = future.result()
                    if item is not None:
                        self._put(self.finished_queue if isinstance(item, PredictionEvent) else self.detect_queue, item)
        self._put(self.detect_queue, self.sentinel)

    def _close_image(self, context):
        with self.lock:
            if context.closed:
                return
            context.closed = True
        ImageLoader.cleanup(context.loaded)
        context.tensor = None
        self.image_slots.release()

    def _batches(self, source, size, sentinels=1, followup_wait=None, initial_wait=None):
        batch = []
        deadline = 0.
        finished = 0
        first_batch = True
        while not self.cancelled.is_set():
            timeout = max(0., min(0.05, deadline - time.monotonic())) if batch else 0.05
            try:
                item = source.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is self.sentinel:
                finished += 1
            elif item is not None:
                if not batch:
                    wait = self.config.batch_wait_seconds if first_batch or followup_wait is None else followup_wait
                    if callable(wait):
                        wait = wait()
                    if first_batch and initial_wait is not None:
                        wait = initial_wait
                    deadline = time.monotonic() + wait
                batch.append(item)
            limit = size() if callable(size) else size
            if batch and (len(batch) >= limit or time.monotonic() >= deadline or finished == sentinels):
                yield batch
                batch = []
                first_batch = False
            if finished == sentinels:
                return

    def _detect(self):
        for batch in self._batches(self.detect_queue, self.config.detector_batch_size,
                                  followup_wait=self._detector_batch_wait,
                                  initial_wait=self._initial_detector_batch_wait()):
            if self.pipeline is None:
                self.pipeline = self.service._get_pipeline()
            self._detect_batch(batch)
        for _ in range(self.workers):
            self._put(self.crop_queue, self.sentinel)

    def _initial_detector_batch_wait(self):
        # Avoid compiling an extra GPU shape just because decoding the first few
        # images exceeded the steady-state deadline. EOF still flushes immediately.
        return max(1.0, self.config.batch_wait_seconds)

    def _detector_batch_wait(self):
        session = getattr(self.pipeline, "detector_session", None)
        if session is not None and "DmlExecutionProvider" in session.get_providers():
            inputs = session.get_inputs()
            if inputs and inputs[0].shape and not isinstance(inputs[0].shape[0], int):
                return max(1.0, self.config.batch_wait_seconds)
        return self.config.batch_wait_seconds

    def _stable_classifier_batch_size(self):
        session = getattr(self.pipeline, "classifier_session", None)
        return stable_classifier_batch_size(session, self.config) if session is not None else None

    def _classifier_batch_size(self):
        # Resolve lazily: cached-only scans must never create an ONNX session.
        return self._stable_classifier_batch_size() or self.config.classifier_batch_size

    def _classifier_batch_wait(self):
        if self.config.classifier_batch_wait_seconds is not None:
            return self.config.classifier_batch_wait_seconds
        if (self._stable_classifier_batch_size() or 0) > 1:
            # Cross detector boundaries to reduce padding, but bound sparse-bird latency.
            return max(2.0, self.config.batch_wait_seconds)
        return self.config.batch_wait_seconds

    def _detect_batch(self, batch):
        if self.cancelled.is_set():
            return
        try:
            if not hasattr(self.pipeline, "_detect_birds_many"):
                inputs = [PipelineInput(c.loaded.image, c.prepared.latitude, c.prepared.longitude, c.prepared.datetime_text) for c in batch]
                predictions = self.pipeline.predict_many(inputs) if hasattr(self.pipeline, "predict_many") else [self.pipeline.predict(i.image, i.latitude, i.longitude, i.datetime_text) for i in inputs]
                for context, prediction in zip(batch, predictions):
                    context.birds = dict(enumerate(prediction.birds))
                    context.providers = prediction.provider
                    self._close_image(context)
                    self._put(self.finished_queue, context)
                return
            with self.metrics.measure("detection"):
                if hasattr(self.pipeline, "detect_prepared"):
                    boxes = self.pipeline.detect_prepared([c.tensor for c in batch], [c.scale for c in batch])
                else:
                    boxes = self.pipeline._detect_birds_many([c.loaded.image for c in batch])
            if len(boxes) != len(batch):
                raise RuntimeError("Detector returned an incorrect result count")
        except Exception as exc:
            exc.__traceback__ = None
            if len(batch) > 1:
                midpoint = len(batch) // 2
                self._detect_batch(batch[:midpoint])
                self._detect_batch(batch[midpoint:])
            else:
                context = batch[0]
                context.error = exc
                self._close_image(context)
                self._put(self.finished_queue, context)
            return
        for context, image_boxes in zip(batch, boxes):
            context.tensor = None
            context.providers = self.pipeline._active_providers()
            context.remaining_preprocessing = context.remaining_classifications = len(image_boxes)
            if not image_boxes:
                self._close_image(context)
                self._put(self.finished_queue, context)
            for index, box in enumerate(image_boxes):
                if not self._put(self.crop_queue, (context, index, box)):
                    return

    def _crop(self):
        while not self.cancelled.is_set():
            try:
                item = self.crop_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is self.sentinel:
                self._put(self.classify_queue, self.sentinel)
                return
            context, index, box = item
            tensor = None
            error = None
            try:
                with self.metrics.measure("classifier_preprocessing"):
                    tensor = BirdImagePreprocessor._classifier_tensor_from_box(ClassifierCropJob(context.loaded.image, box), self.config.crop_size)
                if self.thumbnail_factory:
                    with self.metrics.measure("thumbnails"):
                        thumbnail = self.thumbnail_factory(context.loaded.image, box.box)
                    with self.lock:
                        context.thumbnails[json.dumps([float(value) for value in box.box])] = thumbnail
            except Exception as exc:
                error = exc
            finally:
                with self.lock:
                    context.remaining_preprocessing -= 1
                    close = context.remaining_preprocessing == 0
                if close:
                    self._close_image(context)
            if error:
                self._classified(context, index, box, None, error)
            else:
                self._put(self.classify_queue, (context, index, box, tensor))

    def _classified(self, context, index, box, classification, error=None):
        with self.lock:
            if error is not None:
                context.error = error
            if classification:
                context.birds[index] = {"box": box.box, "box_confidence": box.score, "classification": classification}
            context.remaining_classifications -= 1
            finished = context.remaining_classifications == 0
        if finished:
            self._put(self.finished_queue, context)

    def _classify(self):
        for batch in self._batches(self.classify_queue, self._classifier_batch_size, self.workers,
                                  self._classifier_batch_wait):
            self._classify_batch(batch)

    def _classify_batch(self, batch):
        if self.cancelled.is_set():
            return
        try:
            tensors = np.empty((len(batch), *batch[0][3].shape), dtype=np.float32)
            for index, item in enumerate(batch):
                tensors[index] = item[3]
            metadata = [(c.prepared.latitude, c.prepared.longitude, c.prepared.datetime_text) for c, *_ in batch]
            with self.metrics.measure("classification"):
                results = self.pipeline._run_classifier_batch(tensors, metadata)
            if len(results) != len(batch):
                raise RuntimeError("Classifier returned an incorrect result count")
        except Exception as exc:
            exc.__traceback__ = None
            if len(batch) > 1:
                midpoint = len(batch) // 2
                self._classify_batch(batch[:midpoint])
                self._classify_batch(batch[midpoint:])
            else:
                c, index, box, _ = batch[0]
                self._classified(c, index, box, None, exc)
            return
        for (context, index, box, _), classification in zip(batch, results):
            self._classified(context, index, box, classification)
