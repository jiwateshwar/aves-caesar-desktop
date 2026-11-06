"""Searching places by name: OpenStreetMap through Photon (photon.komoot.io), with the offline list as fallback.

The offline list only has towns, spelt locally; OpenStreetMap also knows villages, lakes and national parks
("Sattal", "Jim Corbett National Park") and gives English names. Only the typed text is sent, never photos or
coordinates. AVES_PLACE_SEARCH=offline turns the online search off; AVES_PHOTON_URL points it at your own Photon.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

from .geocoding import Geocoder, Place, english, search_key

logger = logging.getLogger(__name__)

USER_AGENT = "Featherbase/1.0 (self-hosted bird photo organiser)"

# Places a photographer means; not shops, buildings or bus stops.
_KEEP = {
    "place": None,  # any city, town, village, hamlet, locality...
    "boundary": {"national_park", "protected_area", "administrative"},
    "leisure": {"nature_reserve", "park", "garden"},
    "natural": None,
    "water": None,
    "waterway": {"river"},
    "tourism": {"attraction", "viewpoint", "zoo"},
    "landuse": {"forest", "reservoir"},
    "mountain_pass": None,
}


def _wanted(key: str | None, value: str | None) -> bool:
    if key not in _KEEP:
        return False
    allowed = _KEEP[key]
    return allowed is None or value in allowed


class PlaceSearch:
    def __init__(self, geocoder: Geocoder | None = None, url: str | None = None, online: bool | None = None, timeout: float = 4.0):
        self.geocoder = geocoder or Geocoder.shared()
        self.url = (url or os.environ.get("AVES_PHOTON_URL") or "https://photon.komoot.io").rstrip("/")
        self.online = online if online is not None else os.environ.get("AVES_PLACE_SEARCH", "online").lower() != "offline"
        self.timeout = timeout
        self._failed_until = 0.0  # after a failure, don't make every keystroke wait for the timeout

    def search(self, query: str, limit: int = 10) -> list[Place]:
        offline = self.geocoder.search(query, limit)
        if not self.online or time.monotonic() < self._failed_until:
            return offline
        try:
            online = self._photon(query, limit)
        except Exception as exc:  # no internet, service down: the offline list still works
            logger.warning("Online place search failed, using the offline list: %s", exc)
            self._failed_until = time.monotonic() + 60
            return offline
        # Offline towns the online search didn't return (by name and country) go after the online results.
        seen = {(search_key(p.city), p.country_code) for p in online}
        extra = [p for p in offline if (search_key(p.city), p.country_code) not in seen]
        return (online + extra)[:limit]

    def _photon(self, query: str, limit: int) -> list[Place]:
        name, _, qualifier = query.partition(",")
        params = urllib.parse.urlencode({"q": query.replace(",", " "), "limit": max(limit * 3, 15), "lang": "en"})
        request = urllib.request.Request(f"{self.url}/api/?{params}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            features = json.loads(response.read())["features"]
        places, seen = [], set()
        for feature in features:
            props = feature.get("properties", {})
            if not _wanted(props.get("osm_key"), props.get("osm_value")) or not props.get("name"):
                continue
            longitude, latitude = feature["geometry"]["coordinates"][:2]
            code = (props.get("countrycode") or "").upper()
            country = self.geocoder.country_name(code) or english(props.get("country")) or code
            state = english(props.get("state"))
            if qualifier.strip() and not any(search_key(qualifier) in search_key(v or "") for v in (country, state, code)):
                continue
            place = Place(city=english(props["name"]), state=state, country=country, country_code=code,
                          latitude=round(latitude, 6), longitude=round(longitude, 6), population=0,
                          kind=_kind(props.get("osm_key"), props.get("osm_value")), source="online")
            key = (search_key(place.city), place.state, code, place.kind)
            if key in seen:  # e.g. a lake mapped twice
                continue
            seen.add(key)
            places.append(place)
        return places[:limit]


def _kind(key: str | None, value: str | None) -> str | None:
    if key == "boundary" and value == "administrative":
        return "area"
    if not value or value == "yes":
        return key
    return value.replace("_", " ")


_shared: PlaceSearch | None = None


def place_search() -> PlaceSearch:
    global _shared
    if _shared is None:
        _shared = PlaceSearch()
    return _shared
