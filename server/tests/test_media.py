from __future__ import annotations

from datetime import datetime

from aves_server.media import parse_exif_datetime, parse_metadata


def test_metadata_prefers_signed_composite_gps_and_strips_nul_padding():
    meta = parse_metadata({
        "SourceFile": "/dump/a.NEF",
        "EXIF:DateTimeOriginal": "2026:03:01 06:15:00",
        "EXIF:GPSLatitude": 33.9, "EXIF:GPSLatitudeRef": "S",
        "Composite:GPSLatitude": -33.9, "Composite:GPSLongitude": 18.4,
        "EXIF:UserComment": "\x00\x00\x00\x00",
        "MakerNotes:Nested": {"Note": "abc\x00"},
    })
    assert (meta.latitude, meta.longitude) == (-33.9, 18.4)
    assert meta.captured_at == datetime(2026, 3, 1, 6, 15)
    assert meta.exif["EXIF:UserComment"] == "" and meta.exif["MakerNotes:Nested"] == {"Note": "abc"}
    assert "SourceFile" not in meta.exif


def test_unset_gps_and_camera_clock_are_treated_as_missing():
    meta = parse_metadata({"EXIF:DateTimeOriginal": "0000:00:00 00:00:00", "Composite:GPSLatitude": 0.0, "Composite:GPSLongitude": 0.0})
    assert meta.captured_at is None and meta.latitude is None


def test_datetime_formats():
    assert parse_exif_datetime("2026:10:18 07:12:33.45+05:30") == datetime(2026, 10, 18, 7, 12, 33)
    assert parse_exif_datetime("2026-10-18T07:12:33") == datetime(2026, 10, 18, 7, 12, 33)
    assert parse_exif_datetime("2026:10:18") == datetime(2026, 10, 18)
    assert parse_exif_datetime("garbage") is None
