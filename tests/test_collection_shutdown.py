from pathlib import Path
from types import SimpleNamespace

import pytest

from bird_desktop.collection.jobs import CollectionJobs, CollectionProcess
from bird_desktop import main


def test_collection_manager_rejects_duplicate_scan(monkeypatch):
    launches = []
    job = SimpleNamespace(running=lambda: True)
    monkeypatch.setattr(CollectionProcess, "start", lambda *args: launches.append(args) or job)
    runtime = SimpleNamespace(selection=SimpleNamespace(selected=[]), detector_selection=SimpleNamespace(selected=[]), classifier_selection=SimpleNamespace(selected=[]))
    jobs = CollectionJobs(SimpleNamespace(runtime=lambda: runtime))
    jobs.start({})
    with pytest.raises(RuntimeError, match="already running"):
        jobs.start({})
    assert len(launches) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_app_shuts_workers_down_on_window_close_and_start_failure(monkeypatch, tmp_path, fail):
    index = tmp_path / "index.html"
    index.write_text("")
    events = SimpleNamespace(closed=[])
    calls = []
    monkeypatch.setattr(main.AppPaths, "frontend_index", lambda: index)
    monkeypatch.setattr(main, "BirdDesktopApi", lambda: None)
    monkeypatch.setattr(main.webview, "create_window", lambda *args, **kwargs: SimpleNamespace(events=events))
    monkeypatch.setattr(main.AppRunner, "_window_icon", lambda _: Path("missing.ico"))
    monkeypatch.setattr(main, "workers", SimpleNamespace(shutdown=lambda: calls.append("shutdown")))

    # pywebview events implement += for handlers.
    class ClosedEvent:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    events.closed = ClosedEvent()
    def start(**kwargs):
        if fail:
            raise RuntimeError("webview failed")
        events.closed.callback()
        assert calls == ["shutdown"]
    monkeypatch.setattr(main.webview, "start", start)
    if fail:
        with pytest.raises(RuntimeError, match="webview failed"):
            main.AppRunner().run()
        assert calls == ["shutdown"]
    else:
        main.AppRunner().run()
        assert calls == ["shutdown", "shutdown"]
