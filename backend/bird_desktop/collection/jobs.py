from __future__ import annotations

import json
import subprocess
import threading
import uuid
from typing import Any

from ..inference.providers import RuntimeProbe
from ..runtime.paths import AppPaths
from ..runtime.worker_process import WorkerProcessCommand
from ..runtime.worker_lifecycle import workers
from .transport import CollectionStatusDecoder


class CollectionJobs:
    def __init__(self, runtime_probe: RuntimeProbe):
        self._runtime_probe = runtime_probe
        self._jobs: dict[str, CollectionProcess] = {}
        self._start_lock = threading.Lock()


    def start(self, request: dict[str, Any]) -> dict[str, str]:
        with self._start_lock:
            if self.has_running():
                raise RuntimeError("A collection scan is already running.")
            return self._start(request)


    def _start(self, request: dict[str, Any]) -> dict[str, str]:
        job_id = uuid.uuid4().hex
        runtime = self._runtime_probe.runtime()
        request = dict(request)
        request["_runtimeSelection"] = runtime.selection.__dict__
        request["_detectorSelection"] = runtime.detector_selection.__dict__
        request["_classifierSelection"] = runtime.classifier_selection.__dict__
        self._jobs[job_id] = CollectionProcess.start(job_id, request)
        return {"jobId": job_id}


    def status(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return self._missing_status()

        return job.status()


    def stop(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return self._missing_status()

        return job.stop()


    def has_running(self) -> bool:
        return any(job.running() for job in self._jobs.values())


    def _missing_status(self) -> dict[str, Any]:
        return {"state": "missing", "total": 0, "completed": 0, "errors": 0, "currentFile": "", "message": "Collection job not found.", "error": "Collection job not found.", "species": []}


class CollectionProcess:
    def __init__(self, process: subprocess.Popen[str]):
        self._process = process
        self._lock = threading.Lock()
        self._stop_requested = False
        self._transport = CollectionStatusDecoder()
        self._status: dict[str, Any] = {"state": "running", "total": 0, "completed": 0, "errors": 0, "currentFile": "", "message": "Starting collection scan...", "species": []}
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()


    @staticmethod
    def start(job_id: str, request: dict[str, Any]) -> "CollectionProcess":
        log_path = AppPaths.logs_dir() / f"collection-{job_id}.log"
        command = WorkerProcessCommand("collection.worker").with_log_path(log_path)
        process = workers.start(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        try:
            if process.stdin is None:
                raise RuntimeError("Collection worker stdin is not available.")
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.close()
            return CollectionProcess(process)
        except BaseException:
            workers.stop(process)
            raise


    def status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._status)

        if status.get("state") in ("done", "stopped", "error", "missing"):
            return status

        if self._stop_requested:
            return status  # stop() publishes completion only after the entire tree exits.

        returncode = self._process.poll()
        if returncode is None:
            return status

        if returncode != 0:
            return self._terminal_status("error", "Collection worker stopped unexpectedly.")

        return status


    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_requested = True
        workers.stop(self._process)
        self._reader.join(timeout=5)
        if self._reader.is_alive():
            raise TimeoutError("Collection status reader did not stop")
        return self._terminal_status("stopped", "Collection scan stopped.")


    def running(self) -> bool:
        return self.status().get("state") == "running"


    def _read_output(self) -> None:
        try:
            self._consume_output()
        finally:
            workers.stop(self._process)
            if self._process.stdout is not None:
                self._process.stdout.close()


    def _consume_output(self) -> None:
        if self._process.stdout is None:
            self._terminal_status("error", "Collection worker stdout is not available.")
            return

        for line in self._process.stdout:
            if self._stop_requested:
                return
            if not line.startswith("{"):
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            try:
                payload = self._transport.decode(payload)
            except (ValueError, TypeError, KeyError):
                self._terminal_status("error", "Invalid collection worker status.")
                return

            with self._lock:
                if not self._stop_requested:
                    self._status.update(payload)


    def _terminal_status(self, state: str, message: str) -> dict[str, Any]:
        with self._lock:
            self._status["state"] = state
            self._status["message"] = message
            status = dict(self._status)

        return status
