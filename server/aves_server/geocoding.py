"""Offline geocoding over the GeoNames cities dataset bundled with reverse_geocode.

Nothing is sent over the network; the dataset ships inside the package.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import reverse_geocode
from scipy.spatial import cKDTree


@dataclass(frozen=True)
class Place:
    city: str
    state: str | None
    country: str
    country_code: str
    latitude: float
    longitude: float
    population: int
    distance_km: float | None = None

    @property
    def label(self) -> str:
        return ", ".join(part for part in (self.city, self.state, self.country) if part)

    def to_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


class Geocoder:
    _instance: "Geocoder | None" = None
    _lock = threading.Lock()

    def __init__(self):
        data_dir = Path(reverse_geocode.__file__).parent
        with gzip.open(data_dir / "geocode.gz") as handle:
            self._locations: list[dict] = json.loads(handle.read())
        with (data_dir / "countries.csv").open(encoding="utf-8") as handle:
            self._countries = {code: name for code, name in csv.reader(handle)}
        self._search_keys = [location["city"].casefold() for location in self._locations]
        self._trees: dict[int, tuple[cKDTree, list[int]]] = {}

    @classmethod
    def shared(cls) -> "Geocoder":
        with cls._lock:
            if cls._instance is None:
                cls._instance = Geocoder()
            return cls._instance

    def reverse(self, latitude: float, longitude: float, min_population: int = 0) -> Place | None:
        tree, indices = self._tree(min_population)
        if not indices:
            return None
        _distance, position = tree.query(self._unit_vector(latitude, longitude), k=1)
        location = self._locations[indices[int(position)]]
        return self._place(location, _haversine_km(latitude, longitude, location["latitude"], location["longitude"]))

    def search(self, query: str, limit: int = 10) -> list[Place]:
        """Finds places by name. "Ooty, India" narrows by state or country."""
        name, _, qualifier = query.partition(",")
        name, qualifier = name.strip().casefold(), qualifier.strip().casefold()
        if len(name) < 2:
            return []
        ranked = []
        for index, key in enumerate(self._search_keys):
            if name not in key:
                continue
            location = self._locations[index]
            if qualifier and not self._matches_qualifier(location, qualifier):
                continue
            rank = 0 if key == name else 1 if key.startswith(name) else 2
            ranked.append((rank, -location["population"], index))
        ranked.sort()
        return [self._place(self._locations[index]) for _rank, _population, index in ranked[:limit]]

    def _matches_qualifier(self, location: dict, qualifier: str) -> bool:
        country = self._countries.get(location["country_code"], "")
        return any(qualifier in value.casefold() for value in (country, location.get("state", ""), location["country_code"]))

    def _tree(self, min_population: int) -> tuple[cKDTree, list[int]]:
        with self._lock:
            cached = self._trees.get(min_population)
            if cached is None:
                indices = [i for i, loc in enumerate(self._locations) if loc["population"] >= min_population]
                points = [self._unit_vector(self._locations[i]["latitude"], self._locations[i]["longitude"]) for i in indices]
                cached = (cKDTree(points), indices) if indices else (None, [])
                self._trees[min_population] = cached
            return cached

    @staticmethod
    def _unit_vector(latitude: float, longitude: float) -> tuple[float, float, float]:
        # 3D points keep nearest-neighbour search correct across the antimeridian and near the poles.
        lat, lon = math.radians(latitude), math.radians(longitude)
        return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))

    def _place(self, location: dict, distance_km: float | None = None) -> Place:
        return Place(
            city=location["city"],
            state=location.get("state"),
            country=self._countries.get(location["country_code"], location["country_code"]),
            country_code=location["country_code"],
            latitude=location["latitude"],
            longitude=location["longitude"],
            population=location["population"],
            distance_km=round(distance_km, 1) if distance_km is not None else None,
        )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))
