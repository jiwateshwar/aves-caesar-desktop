from __future__ import annotations

import shutil

import pytest
from sqlalchemy import select

from aves_server.models import Dumpsite, FileStatus, MediaFile, Shot, ShotStatus
from aves_server.publisher import Publisher

from test_api import _bird, client  # noqa: F401  (fixture)
from aves_server.analysis import Analyzer


@pytest.fixture
def slots(tmp_path, monkeypatch):
    """Two shares mounted by the stack: slot 2 with a host path, slot 3 configured but left empty."""
    root = tmp_path / "slots"
    (root / "2").mkdir(parents=True)
    monkeypatch.setenv("AVES_DUMPSITE_SLOTS_DIR", str(root))
    monkeypatch.setenv("AVES_DUMPSITE_SLOTS", "2=/mnt/Tank/Imports;3=;4=/mnt/Tank/NotMounted")
    return root


def _settle(scanner, clock):
    scanner.scan()
    clock.advance(31)
    return scanner.scan()


def _add(database, slot: int, name: str) -> int:
    with database.session() as db:
        site = Dumpsite(name=name, slot=slot, enabled=True)
        db.add(site)
        db.flush()
        return site.id


def test_every_dumpsite_is_scanned_and_kept_apart(scanner, clock, config, make_photo, database, slots):
    site_id = _add(database, 2, "Imports")
    make_photo(config.dump_dir / "card" / "DSC_1.JPG")
    make_photo(slots / "2" / "card" / "DSC_1.JPG", color=(10, 20, 30))  # same path and time, different photo

    report = _settle(scanner, clock)

    assert report.ingested == 2
    with database.session() as db:
        shots = {s.dumpsite_id: s for s in db.scalars(select(Shot))}
        assert set(shots) == {1, site_id}
        assert (shots[1].dump_folder, shots[site_id].dump_folder) == ("card", "Imports/card")
        assert {f.dumpsite_id for f in db.scalars(select(MediaFile))} == {1, site_id}


def test_an_empty_or_unmounted_share_does_not_mark_its_files_missing(scanner, clock, config, make_photo, database, slots):
    _add(database, 2, "Imports")
    make_photo(slots / "2" / "DSC_2.JPG")
    make_photo(config.dump_dir / "DSC_3.JPG", color=(1, 2, 3))
    _settle(scanner, clock)

    (slots / "2" / "DSC_2.JPG").unlink()  # the share looks empty, as if it came unmounted
    report = scanner.scan()
    assert report.missing == 0
    shutil.rmtree(slots / "2")            # now really unmounted
    report = scanner.scan()
    assert report.missing == 0 and any("not mounted" in e for e in report.errors)
    with database.session() as db:
        assert all(f.status == FileStatus.in_dump for f in db.scalars(select(MediaFile)))


def test_disabled_dumpsites_are_not_scanned(scanner, clock, make_photo, database, slots):
    site_id = _add(database, 2, "Imports")
    with database.session() as db:
        db.get(Dumpsite, site_id).enabled = False
    make_photo(slots / "2" / "DSC_4.JPG")
    assert _settle(scanner, clock).ingested == 0


def test_photos_from_an_extra_dumpsite_publish_like_any_other(client, scanner, clock, config, make_photo, database, slots):  # noqa: F811
    _add(database, 2, "Imports")
    source = make_photo(slots / "2" / "trip" / "DSC_5.JPG", gps=(11.41, 76.70))
    _settle(scanner, clock)
    with database.session() as db:
        shot = db.scalar(select(Shot))
        Analyzer._store(shot, [_bird("101", 0.97)], (96, 64), True, ["CPUExecutionProvider"])
        shot.status = ShotStatus.confirmed
        shot_id = shot.id

    assert Publisher(config, database).publish([shot_id]).published_files == 1
    assert not source.exists()
    assert (config.published_dir / "India/Tamil Nadu/Ooty/2026-10-18/JPG/DSC_5.JPG").is_file()


def test_dumpsites_are_added_renamed_and_removed_from_settings(client, scanner, clock, make_photo, database, slots):  # noqa: F811
    listing = client.get("/api/dumpsites").json()
    assert [d["name"] for d in listing["dumpsites"]] == ["dump"]
    assert [(s["slot"], s["mounted"]) for s in listing["available_slots"]] == [(2, True), (4, False)]

    assert client.post("/api/dumpsites", json={"slot": 4, "name": "Later"}).status_code == 400   # not mounted yet
    assert client.post("/api/dumpsites", json={"slot": 5, "name": "Nope"}).status_code == 400    # nothing in that slot
    assert client.post("/api/dumpsites", json={"slot": 2, "name": "a/b"}).status_code == 400
    listing = client.post("/api/dumpsites", json={"slot": 2, "name": "Imports"}).json()
    site = next(d for d in listing["dumpsites"] if d["slot"] == 2)
    assert (site["name"], site["host_path"], site["mounted"]) == ("Imports", "/mnt/Tank/Imports", True)
    assert [s["slot"] for s in listing["available_slots"]] == [4]

    make_photo(slots / "2" / "card" / "DSC_6.JPG")
    _settle(scanner, clock)
    renamed = client.patch(f"/api/dumpsites/{site['id']}", json={"name": "Phone"}).json()
    assert any(d["name"] == "Phone" and d["files"] == 1 for d in renamed["dumpsites"])
    shot = client.get("/api/shots").json()["items"][0]
    detail = client.get(f"/api/shots/{shot['id']}").json()
    assert detail["dumpsite_name"] == "Phone" and detail["folder"] == "Phone/card"
    assert client.get(f"/api/files/{detail['files'][0]['id']}/download").status_code == 200

    assert client.delete(f"/api/dumpsites/{site['id']}").status_code == 409  # its photo isn't published yet
    assert client.patch(f"/api/dumpsites/{site['id']}", json={"enabled": False}).json()["dumpsites"][1]["enabled"] is False
    assert client.patch("/api/dumpsites/1", json={"enabled": False}).status_code == 400
    assert client.delete("/api/dumpsites/1").status_code == 400


def test_a_read_only_dumpsite_is_copied_from_and_never_changed(client, scanner, clock, config, make_photo, database, slots):  # noqa: F811
    site_id = _add(database, 2, "Archive")
    with database.session() as db:
        db.get(Dumpsite, site_id).read_only = True
    source = make_photo(slots / "2" / "trip" / "DSC_7.JPG", gps=(11.41, 76.70))
    (slots / "2" / "trip" / "DSC_7.JPG.xmp").write_text("<x/>")
    before = source.read_bytes()
    _settle(scanner, clock)
    with database.session() as db:
        shot = db.scalar(select(Shot))
        Analyzer._store(shot, [_bird("101", 0.97)], (96, 64), True, ["CPUExecutionProvider"])
        shot.status = ShotStatus.confirmed
        shot_id = shot.id

    assert Publisher(config, database).publish([shot_id]).published_files == 1
    published = config.published_dir / "India/Tamil Nadu/Ooty/2026-10-18/JPG/DSC_7.JPG"
    assert published.read_bytes() == before and source.read_bytes() == before
    assert published.stat().st_ino != source.stat().st_ino            # a real copy, not a hard link
    assert (published.parent / "DSC_7.JPG.xmp").is_file() and (source.parent / "DSC_7.JPG.xmp").is_file()

    clock.advance(31)
    report = _settle(scanner, clock)                                   # the original is still there...
    assert (report.ingested, report.duplicates, report.missing) == (0, 0, 0)  # ...and is not picked up again
    with database.session() as db:
        assert db.scalar(select(Shot)).status == ShotStatus.published
        assert [f.status for f in db.scalars(select(MediaFile))] == [FileStatus.published]

    listing = client.get("/api/dumpsites").json()
    assert next(d for d in listing["dumpsites"] if d["id"] == site_id)["read_only"] is True
    assert client.patch(f"/api/dumpsites/{site_id}", json={"read_only": False}).json()["dumpsites"][1]["read_only"] is False


def test_duplicates_are_not_deleted_from_a_read_only_dumpsite(client, scanner, clock, config, make_photo, database, slots):  # noqa: F811
    site_id = _add(database, 2, "Archive")
    original = make_photo(config.dump_dir / "X.JPG")
    shutil.copy2(original, slots / "2" / "X.JPG")
    _settle(scanner, clock)
    client.patch(f"/api/dumpsites/{site_id}", json={"read_only": True})
    duplicate = client.get("/api/duplicates").json()[0]
    assert duplicate["dumpsite"] == "Archive"
    assert client.delete(f"/api/duplicates/{duplicate['id']}").status_code == 409
    assert (slots / "2" / "X.JPG").is_file()


@pytest.fixture
def nas(monkeypatch):
    """A fake TrueNAS (SMB shares) and Portainer (the stack's variables), and redeploys recorded instead of run."""
    from aves_server import stackadmin

    for key, value in {"AVES_PORTAINER_URL": "https://nas:31015", "AVES_PORTAINER_TOKEN": "ptr", "AVES_PORTAINER_STACK_ID": "37",
                       "AVES_TRUENAS_URL": "nas", "AVES_TRUENAS_API_KEY": "key", "AVES_PHOTOS_HOST_PATH": "/mnt/Pool/Wildlife",
                       "AVES_APPDATA_HOST_PATH": "/mnt/Pool/Wildlife/AppData"}.items():
        monkeypatch.setenv(key, value)
    fake = {"env": [{"name": "DUMPSITE_2", "value": "/mnt/Pool/WildHome"}, {"name": "TZ", "value": "UTC"}], "deploys": []}
    shares = [{"name": n, "path": p, "enabled": True} for n, p in (("WildHome", "/mnt/Pool/WildHome"), ("wildlife1", "/mnt/Pool/Wildlife"),
              ("MegaTank", "/mnt/Pool/MegaTank"), ("Pics", "/mnt/Pool/MegaTank/Pics"), ("Tank", "/mnt/Pool/Tank"))]

    def request(url, headers, method="GET", body=None, timeout=20):
        if "/sharing/smb" in url:
            return shares
        if method == "GET":
            return {"Env": fake["env"], "GitConfig": {"ReferenceName": "refs/heads/x"}}
        fake["env"] = body["Env"]
        fake["deploys"].append({e["name"]: e["value"] for e in body["Env"]})
        return {}

    monkeypatch.setattr(stackadmin.StackAdmin, "_request", staticmethod(request))
    monkeypatch.setattr(stackadmin.Redeployer, "start", lambda self, admin, change, what: admin._redeploy(change))
    return fake


def test_shares_on_the_nas_are_added_and_removed_from_settings(client, database, slots, nas):  # noqa: F811
    _add(database, 2, "WildHome")
    stack = client.get("/api/dumpsites").json()["stack"]
    assert stack["self_service"] is True
    blocked = {s["name"]: s["blocked"] for s in stack["shares"]}
    assert blocked["WildHome"] == "Already the dumpsite WildHome"
    assert "published library" in blocked["wildlife1"]
    assert blocked["MegaTank"] is None and blocked["Tank"] is None

    assert client.post("/api/dumpsites/shares", json={"path": "/mnt/Pool/Elsewhere", "name": "X"}).status_code == 400  # not a share
    assert client.post("/api/dumpsites/shares", json={"path": "/mnt/Pool/Wildlife", "name": "X"}).status_code == 409
    added = client.post("/api/dumpsites/shares", json={"path": "/mnt/Pool/MegaTank", "name": "Mega", "read_only": True}).json()
    assert added["restarting"] is True
    assert nas["deploys"][-1]["DUMPSITE_3"] == "/mnt/Pool/MegaTank" and nas["deploys"][-1]["TZ"] == "UTC"
    site = next(d for d in added["dumpsites"] if d["name"] == "Mega")
    assert (site["slot"], site["read_only"], site["mounted"]) == (3, True, False)  # mounted once the stack has restarted
    assert {s["name"]: s["blocked"] for s in added["stack"]["shares"]}["Pics"] == "Overlaps the dumpsite Mega (/mnt/Pool/MegaTank)"

    removed = client.delete(f"/api/dumpsites/{site['id']}").json()
    assert removed["restarting"] is True and "DUMPSITE_3" not in nas["deploys"][-1]


def test_without_stack_access_settings_only_explains(client, slots):  # noqa: F811
    stack = client.get("/api/dumpsites").json()["stack"]
    assert (stack["self_service"], stack["shares"]) == (False, [])
    assert client.post("/api/dumpsites/shares", json={"path": "/mnt/x", "name": "X"}).status_code == 400
