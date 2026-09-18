"""Lossless thumbnail interning on the private worker pipe, scoped to one scan."""
from __future__ import annotations


class CollectionStatusEncoder:
    def __init__(self):
        self._ids: dict[str, int] = {}

    def encode(self, status: dict) -> dict:
        additions = []

        def intern(value):
            if not isinstance(value, str):
                return value
            if value not in self._ids:
                self._ids[value] = len(self._ids)
                additions.append(value)
            return self._ids[value]

        payload = _map_thumbnails(status, intern)
        payload["_collectionTransport"] = 1
        payload["_thumbnailValues"] = additions
        return payload


class CollectionStatusDecoder:
    def __init__(self):
        self._values: list[str] = []

    def decode(self, payload: dict) -> dict:
        if "_collectionTransport" not in payload:
            return payload  # Accept full snapshots from older workers.
        if payload["_collectionTransport"] != 1:
            raise ValueError("Unknown collection transport version")
        additions = payload.get("_thumbnailValues")
        if not isinstance(additions, list) or not all(isinstance(value, str) for value in additions):
            raise ValueError("Invalid collection thumbnail table")
        previous_count = len(self._values)
        self._values.extend(additions)

        def resolve(value):
            if value is None:
                return None
            if type(value) is not int or not 0 <= value < len(self._values):
                raise ValueError("Invalid collection thumbnail reference")
            return self._values[value]

        try:
            status = _map_thumbnails(payload, resolve)
        except Exception:
            del self._values[previous_count:]
            raise
        del status["_collectionTransport"]
        del status["_thumbnailValues"]
        return status


def _map_thumbnails(status, transform):
    """Copy only the containers we edit; never alter a published snapshot."""
    result = dict(status)
    if isinstance(status.get("species"), list):
        result["species"] = []
        for species in status["species"]:
            item = dict(species)
            if "thumbnailDataUrl" in item:
                item["thumbnailDataUrl"] = transform(item["thumbnailDataUrl"])
            if isinstance(item.get("occurrences"), list):
                item["occurrences"] = []
                for occurrence in species["occurrences"]:
                    copy = dict(occurrence)
                    if "thumbnailDataUrl" in copy:
                        copy["thumbnailDataUrl"] = transform(copy["thumbnailDataUrl"])
                    item["occurrences"].append(copy)
            result["species"].append(item)
    return result
