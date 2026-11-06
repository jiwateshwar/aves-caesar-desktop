from __future__ import annotations

import io
import json

from aves_server import placesearch
from aves_server.geocoding import Geocoder, english
from aves_server.placesearch import PlaceSearch

from test_api import client  # noqa: F401  (fixture)


def _feature(name, key, value, lat, lon, state="Uttarakhand", country="India", code="IN"):
    return {"geometry": {"coordinates": [lon, lat]},
            "properties": {"name": name, "osm_key": key, "osm_value": value, "state": state, "country": country, "countrycode": code}}


PHOTON = {"features": [
    _feature("Jim Corbett National Park", "leisure", "nature_reserve", 29.53, 78.77),
    _feature("Jim Corbett Heritage Bungalow", "tourism", "guest_house", 29.39, 79.45),
    _feature("Jim Corbett Entry office", "information", "office", 29.39, 79.13),
    _feature("Sattal", "place", "hamlet", 29.35, 79.53),
    _feature("Sattal", "place", "village", 34.9, 72.3, "Khyber Pakhtunkhwa", "Pakistan", "PK"),
]}


def _fake_photon(monkeypatch, payload=PHOTON, calls=None):
    def urlopen(request, timeout):
        if calls is not None:
            calls.append(request.full_url)
        if payload is None:
            raise OSError("no network")
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(placesearch.urllib.request, "urlopen", urlopen)


def test_place_names_are_in_plain_english():
    assert [english(n) for n in ("Bhīm Tāl", "Kālāgarh Project Colony", "Łódź", "Ålesund")] == ["Bhim Tal", "Kalagarh Project Colony", "Lodz", "Alesund"]
    geocoder = Geocoder.shared()
    assert geocoder.search("bhimtal")[0].label == "Bhim Tal, Uttarakhand, India"   # spaces and accents don't matter
    assert geocoder.reverse(29.35, 79.55).city.isascii()


def test_online_search_finds_parks_and_villages_but_not_buildings(monkeypatch):
    calls = []
    _fake_photon(monkeypatch, calls=calls)
    places = PlaceSearch(online=True).search("corbett", 10)
    online = [p for p in places if p.source == "online"]
    assert [(p.city, p.kind, p.country) for p in online] == [
        ("Jim Corbett National Park", "nature reserve", "India"), ("Sattal", "hamlet", "India"), ("Sattal", "village", "Pakistan")]
    assert "lang=en" in calls[0]
    assert {p.country for p in PlaceSearch(online=True).search("sattal, india", 10) if p.source == "online"} == {"India"}  # ", india" narrows


def test_without_internet_the_offline_list_is_used(monkeypatch):
    _fake_photon(monkeypatch, payload=None)
    search = PlaceSearch(online=True)
    assert all(p.source == "offline" for p in search.search("ooty", 5)) and search.search("ooty", 5)


def test_a_picked_park_becomes_the_session_folder(client, scanner, clock, config, make_photo, database):  # noqa: F811
    make_photo(config.dump_dir / "card" / "DSC_1.JPG")
    scanner.scan(quick=True)
    session_id = client.get("/api/sessions").json()[0]["id"]
    updated = client.patch(f"/api/sessions/{session_id}", json={"location": {
        "latitude": 29.53, "longitude": 78.77, "label": "Jim Corbett National Park, Uttarakhand, India",
        "city": "Jim Corbett National Park", "state": "Uttarakhand", "country": "India"}}).json()
    assert (updated["location"]["city"], updated["location"]["state"]) == ("Jim Corbett National Park", "Uttarakhand")
    shot = client.get("/api/shots", params={"session_id": session_id}).json()["items"][0]
    detail = client.get(f"/api/shots/{shot['id']}").json()
    assert detail["files"][0]["publish_to"].startswith("India/Uttarakhand/Jim Corbett National Park/")

    # Pasted coordinates still use the nearest town, in English.
    updated = client.patch(f"/api/sessions/{session_id}", json={"location": {"latitude": 29.35, "longitude": 79.55}}).json()
    assert updated["location"]["city"].isascii()
