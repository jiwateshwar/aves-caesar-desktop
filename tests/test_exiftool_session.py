import subprocess
import sys

import pytest

from bird_desktop.media.exiftool_session import ExifToolSession


def fake_executable(monkeypatch, body):
    popen = subprocess.Popen
    monkeypatch.setattr("bird_desktop.media.exiftool_session.subprocess.Popen",
                        lambda command, **kwargs: popen([sys.executable, "-u", "-c", body], **kwargs))


def test_persistent_session_frames_binary_and_reuses_process(monkeypatch):
    fake_executable(monkeypatch, '''
import sys
for line in sys.stdin.buffer:
    if line.startswith(b'-execute'):
        marker = b'{ready' + line.strip()[8:] + b'}\\n'
        sys.stdout.buffer.write(b'jpeg\\x00\\n{ready999}\\n\\xff' + marker)
        sys.stdout.buffer.flush()
''')
    with ExifToolSession("fake") as session:
        pid = session.process.pid
        assert session.execute(["-b", "file.jpg"]) == b"jpeg\x00\n{ready999}\n\xff"
        assert session.execute(["-b", "other.jpg"]) == b"jpeg\x00\n{ready999}\n\xff"
        assert session.process.pid == pid
    assert session.process.poll() is not None
    assert not session.reader.is_alive()


def test_timeout_terminates_process_and_reader(monkeypatch):
    fake_executable(monkeypatch, "import time; time.sleep(60)")
    with ExifToolSession("fake", timeout=.05) as session:
        with pytest.raises(RuntimeError, match="timed out"):
            session.execute(["file.jpg"])
    assert session.process.poll() is not None
    assert not session.reader.is_alive()


def test_newline_in_path_is_rejected_without_injecting_commands(monkeypatch):
    fake_executable(monkeypatch, "import time; time.sleep(60)")
    with ExifToolSession("fake") as session:
        with pytest.raises(ValueError, match="newline"):
            session.execute(["bad\n-execute\nfile.jpg"])
