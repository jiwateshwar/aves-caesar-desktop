import ctypes
from ctypes import wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bird_desktop.runtime.worker_lifecycle import WorkerProcesses
from bird_desktop.collection.jobs import CollectionProcess


@pytest.fixture
def workers():
    registry = WorkerProcesses()
    yield registry
    registry.shutdown()


def process_handle(pid):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000, False, pid)
    assert handle, ctypes.get_last_error()
    return api, handle


TREE = """
import subprocess, sys, time, json
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], creationflags=0x08000000)
print(json.dumps({'pid': child.pid}), flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree containment")
@pytest.mark.parametrize("shutdown", [False, True])
def test_stop_waits_for_descendants_and_is_idempotent(workers, shutdown):
    process = workers.start([sys.executable, "-u", "-c", TREE], stdout=subprocess.PIPE, text=True)
    api, child = process_handle(json.loads(process.stdout.readline())["pid"])
    try:
        started = time.monotonic()
        workers.shutdown() if shutdown else workers.stop(process)
        assert api.WaitForSingleObject(child, 0) == 0
        assert process.poll() is not None
        assert time.monotonic() - started < 5
        workers.stop(process)
        if shutdown:
            with pytest.raises(RuntimeError, match="closing"):
                workers.start([sys.executable, "-c", "pass"])
    finally:
        api.CloseHandle(child)
        process.stdout.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree containment")
def test_collection_stop_waits_for_reader_and_children(workers, monkeypatch):
    monkeypatch.setattr("bird_desktop.collection.jobs.workers", workers)
    process = workers.start([sys.executable, "-u", "-c", TREE], stdout=subprocess.PIPE, text=True)
    api, child = process_handle(json.loads(process.stdout.readline())["pid"])
    collection = CollectionProcess(process)
    try:
        assert collection.stop()["state"] == "stopped"
        assert not collection._reader.is_alive()
        assert api.WaitForSingleObject(child, 0) == 0
        assert collection.status()["state"] == "stopped"
        assert collection.stop()["state"] == "stopped"
    finally:
        api.CloseHandle(child)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job closes when owner dies")
def test_worker_dies_even_if_owner_exits_without_cleanup(tmp_path):
    backend = str(Path(__file__).resolve().parents[1] / "backend")
    script = f"""
import sys, subprocess, os
sys.path.insert(0, {backend!r})
from bird_desktop.runtime.worker_lifecycle import WorkerProcesses
registry = WorkerProcesses()
p = registry.start([sys.executable, '-u', '-c', {TREE!r}], stdout=subprocess.PIPE, text=True)
print(p.stdout.readline(), end='', flush=True)
sys.stdin.readline()
os._exit(0)
"""
    owner = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, creationflags=0x08000000)
    api, child = process_handle(json.loads(owner.stdout.readline())["pid"])
    try:
        owner.stdin.write("exit\n")
        owner.stdin.flush()
        owner.wait(timeout=5)
        assert api.WaitForSingleObject(child, 5000) == 0
    finally:
        api.CloseHandle(child)
        owner.stdin.close()
        owner.stdout.close()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
