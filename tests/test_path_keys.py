import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bird_desktop.runtime.path_keys import canonical_file_keys


def test_regular_files_share_parent_resolution_and_match_complete_resolver(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("Bird.JPG", "été.jpeg", "another.jpg")]
    for path in paths:
        path.write_bytes(b"jpeg")
    missing = tmp_path / "missing.jpg"
    paths += [missing, tmp_path]
    expected = {path: str(path.resolve()).casefold() for path in paths}
    resolve = Path.resolve
    seen = []
    def track(path, *a, **kw):
        seen.append(path)
        return resolve(path, *a, **kw)
    monkeypatch.setattr(Path, "resolve", track)
    assert canonical_file_keys(iter(paths)) == expected
    assert seen == [tmp_path, missing, tmp_path]


def test_reparse_file_uses_complete_resolution(tmp_path, monkeypatch):
    path = tmp_path / "bird.jpg"
    path.write_bytes(b"jpeg")
    info = path.lstat()
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400))
    seen = []
    def resolve(value):
        seen.append(value)
        return tmp_path / "target.jpg"
    monkeypatch.setattr(Path, "resolve", resolve)
    assert canonical_file_keys([path]) == {path: str(tmp_path / "target.jpg").casefold()}
    assert seen == [path]


def test_file_symlink_uses_target_key(tmp_path):
    target = tmp_path / "target.jpg"
    target.write_bytes(b"jpeg")
    link = tmp_path / "link.jpg"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("File symlink creation requires a Windows privilege")
    assert canonical_file_keys([link, target]) == {path: str(path.resolve()).casefold() for path in (link, target)}
    target.unlink()
    assert canonical_file_keys([link]) == {link: str(link.resolve()).casefold()}


def test_directory_alias_is_resolved_again_after_retargeting(tmp_path):
    first, second, link = (tmp_path / name for name in ("first", "second", "alias"))
    for parent in (first, second):
        parent.mkdir()
        (parent / "bird.jpg").write_bytes(b"jpeg")
        (parent / "other.jpg").write_bytes(b"jpeg")
    def create(target):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    def remove():
        link.rmdir() if os.name == "nt" else link.unlink()
    path = link / "bird.jpg"
    for target in (first, second):
        create(target)
        try:
            assert canonical_file_keys([path])[path] == str(path.resolve()).casefold()
            assert canonical_file_keys([path])[path] == str(target / "bird.jpg").casefold()
        finally:
            remove()
    assert (first / "bird.jpg").exists() and (second / "bird.jpg").exists()
    create(first)
    other = link / "other.jpg"
    def retargeted_paths():
        yield path
        remove()
        create(second)
        yield other
    try:
        assert canonical_file_keys(retargeted_paths()) == {
            path: str(first / "bird.jpg").casefold(), other: str(second / "other.jpg").casefold()}
    finally:
        remove()


@pytest.mark.skipif(os.name != "nt", reason="Win32 path normalization")
@pytest.mark.parametrize("name", ["bird.jpg.", "bird.jpg ", "BIRD~1.JPG", "bird.jpg:extra"])
def test_windows_alias_spellings_use_complete_resolution(tmp_path, monkeypatch, name):
    path = tmp_path / name
    expected = str(path.resolve()).casefold()
    seen = []
    resolve = Path.resolve
    def track(value):
        seen.append(value)
        return resolve(value)
    monkeypatch.setattr(Path, "resolve", track)
    assert canonical_file_keys([path]) == {path: expected}
    assert seen == [path]
