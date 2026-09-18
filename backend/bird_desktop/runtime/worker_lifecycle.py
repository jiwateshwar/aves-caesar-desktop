"""App-owned workers cannot outlive Stop or application shutdown."""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading

from .worker_process import WorkerProcessCommand


class WorkerProcesses:
    def __init__(self):
        self._lock = threading.RLock()
        self._workers = {}
        self._closing = False

    def start(self, command, **kwargs):
        with self._lock:
            if self._closing:
                raise RuntimeError("Application is closing")
            options = WorkerProcessCommand.hidden_window_options()
            job = None
            if sys.platform == "win32":
                from .windows_job import WindowsJob
                job = WindowsJob()
                options["creationflags"] |= WindowsJob.CREATE_SUSPENDED
            else:
                options["start_new_session"] = True
            process = None
            try:
                process = subprocess.Popen(command, **kwargs, **options)
                if job is not None:
                    job.attach_and_resume(process)
                self._workers[process] = job
                return process
            except BaseException:
                if job is not None:
                    job.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                raise

    def stop(self, process):
        with self._lock:
            if process not in self._workers:
                return
            job = self._workers[process]
            if job is not None:
                job.terminate()
            elif process.returncode is None:
                # Do not target a numeric process group after its leader has
                # been reaped: its id could already belong to another process.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            del self._workers[process]

    def shutdown(self):
        with self._lock:
            self._closing = True
            errors = []
            for process in list(self._workers):
                try:
                    self.stop(process)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise RuntimeError("Failed to stop application workers") from errors[0]


workers = WorkerProcesses()
atexit.register(workers.shutdown)
