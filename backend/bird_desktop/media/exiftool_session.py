from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import threading

from ..runtime.worker_process import WorkerProcessCommand


class ExifToolSession:
    """Serialized, timeout-bounded commands to one scan-owned ExifTool process."""

    def __init__(self, executable: str, timeout: float = 60):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.sequence = 0
        self.process = subprocess.Popen(
            [executable, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **WorkerProcessCommand.hidden_window_options())
        self.output = queue.Queue()
        self.reader = threading.Thread(target=self._read, name="bird-exiftool-reader", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while data := self.process.stdout.read1(65536):
                self.output.put(data)
        finally:
            self.output.put(None)

    def execute(self, arguments: list[str]) -> bytes:
        if any("\n" in arg or "\r" in arg for arg in arguments):
            raise ValueError("ExifTool argument contains a newline")
        with self.lock:
            self.sequence += 1
            marker = f"{{ready{self.sequence}}}".encode()
            command = ["-charset", "filename=UTF8", *arguments, f"-execute{self.sequence}"]
            try:
                self.process.stdin.write(("\n".join(command) + "\n").encode("utf-8"))
                self.process.stdin.flush()
                chunks = bytearray()
                import time
                deadline = time.monotonic() + self.timeout
                while True:
                    data = self.output.get(timeout=max(0., deadline - time.monotonic()))
                    if data is None:
                        raise RuntimeError("ExifTool exited before completing a command")
                    chunks.extend(data)
                    # The marker is at the end of a response, including binary output.
                    if chunks.endswith(marker + b"\n") or chunks.endswith(marker + b"\r\n"):
                        suffix = len(marker) + (2 if chunks.endswith(b"\r\n") else 1)
                        return bytes(chunks[:-suffix])
            except (queue.Empty, BrokenPipeError, OSError) as exc:
                self.close()
                raise RuntimeError("ExifTool command failed or timed out") from exc

    def read_tags_many(self, paths: list[Path]) -> dict[str, dict]:
        if not paths:
            return {}
        output = self.execute(["-json", "-n", "-GPSLatitude", "-GPSLatitudeRef", "-GPSLongitude", "-GPSLongitudeRef",
                               "-DateTimeOriginal", "-CreateDate", "-DateTimeCreated", *[str(p.resolve()) for p in paths]])
        records = json.loads(output.decode("utf-8", errors="replace"))
        by_path = {str(Path(r["SourceFile"]).resolve()).casefold(): r for r in records if "SourceFile" in r}
        return {str(p): by_path.get(str(p.resolve()).casefold(), {}) for p in paths}

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=2)
        for handle in (self.process.stdin, self.process.stdout):
            if handle is not None:
                handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
