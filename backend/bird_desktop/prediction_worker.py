from __future__ import annotations

import json
import sys
import traceback

from .inference.providers import ProviderSelector, RuntimeResolution
from .inference.services import PredictionService
from .runtime.config import RuntimeConfig
from .runtime.settings import DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD
from .runtime.worker_io import WorkerLogRedirect, WorkerStatusWriter


class PredictionWorker:
    def __init__(self):
        self._prediction_service: PredictionService | None = None
        self._runtime_key: str | None = None
        self._status_writer = WorkerStatusWriter()


    def run(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue

            self._handle(line)


    def _handle(self, line: str) -> None:
        try:
            request = json.loads(line)
            runtime = self._runtime_from_request(request)
            threshold = self._threshold(request.get("acceptedClassificationThreshold"))
            request.pop("_runtimeSelection", None)
            request.pop("_detectorSelection", None)
            request.pop("_classifierSelection", None)
            with WorkerLogRedirect(sys.argv[1] if len(sys.argv) > 1 else None):
                result = self._prediction_service_for_runtime(runtime).predict(request, threshold)

            self._write({"state": "done", "result": result})
        except Exception as exc:
            self._write({"state": "error", "error": str(exc), "traceback": traceback.format_exc()})


    def _write(self, payload: dict) -> None:
        if not self._status_writer.write(payload):
            raise SystemExit(0)


    def _runtime_from_request(self, request: dict) -> RuntimeResolution:
        selection = request.get("_runtimeSelection")
        if not isinstance(selection, dict):
            return RuntimeResolution(selection=ProviderSelector.from_dict({"selected": ["CPUExecutionProvider"]}))

        detector = request.get("_detectorSelection")
        classifier = request.get("_classifierSelection")
        return RuntimeResolution(
            selection=ProviderSelector.from_dict(selection),
            detector_selection=ProviderSelector.from_dict(detector) if isinstance(detector, dict) else None,
            classifier_selection=ProviderSelector.from_dict(classifier) if isinstance(classifier, dict) else None,
        )


    def _prediction_service_for_runtime(self, runtime: RuntimeResolution) -> PredictionService:
        runtime_key = json.dumps({
            "selection": runtime.selection.__dict__,
            "detector": runtime.detector_selection.__dict__,
            "classifier": runtime.classifier_selection.__dict__,
        }, sort_keys=True)
        if self._prediction_service is None or self._runtime_key != runtime_key:
            self._prediction_service = PredictionService(RuntimeConfig.load(), forced_runtime=runtime)
            self._runtime_key = runtime_key

        return self._prediction_service


    def _threshold(self, value: object) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD

        if threshold < 0 or threshold > 1:
            return DEFAULT_ACCEPTED_CLASSIFICATION_THRESHOLD

        return threshold


if __name__ == "__main__":
    PredictionWorker().run()
