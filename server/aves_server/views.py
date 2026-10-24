"""Read-only SQL views for other applications that query the database directly."""

from __future__ import annotations

from sqlalchemy import Connection, text


VIEWS = {
    # One row per published file per confirmed species (files without a species appear once with NULLs).
    "v_published_photos": """
        SELECT mf.id AS file_id,
               mf.final_filename AS filename,
               mf.original_filename,
               mf.file_type,
               mf.published_relpath,
               mf.sha256,
               mf.size_bytes,
               mf.camera_make,
               mf.camera_model,
               mf.lens_model,
               mf.focal_length,
               mf.f_number,
               mf.exposure_time,
               mf.iso,
               mf.width,
               mf.height,
               mf.published_at,
               s.id AS shot_id,
               s.captured_at,
               se.shoot_date,
               COALESCE(s.gps_lat, se.location_lat) AS latitude,
               COALESCE(s.gps_lon, se.location_lon) AS longitude,
               s.location_source,
               s.country,
               s.state,
               s.city,
               d.id AS detection_id,
               d.species_id,
               sp.scientific_name,
               sp.common_name,
               sp.family,
               sp.order_name,
               d.confidence AS model_confidence,
               d.source AS identification_source,
               d.box
        FROM media_files mf
        JOIN shots s ON s.id = mf.shot_id
        JOIN sessions se ON se.id = s.session_id
        LEFT JOIN detections d ON d.shot_id = s.id AND d.status = 'confirmed'
        LEFT JOIN species sp ON sp.species_id = d.species_id
        WHERE mf.status = 'published'
    """,
    # Species seen in the published library, with first and last sighting.
    "v_species_sightings": """
        SELECT sp.species_id,
               sp.scientific_name,
               sp.common_name,
               sp.family,
               COUNT(DISTINCT s.id) AS shots,
               MIN(s.captured_at) AS first_seen,
               MAX(s.captured_at) AS last_seen
        FROM detections d
        JOIN shots s ON s.id = d.shot_id
        JOIN species sp ON sp.species_id = d.species_id
        WHERE d.status = 'confirmed' AND s.status = 'published'
        GROUP BY sp.species_id, sp.scientific_name, sp.common_name, sp.family
    """,
}


def create_views(connection: Connection) -> None:
    for name, body in VIEWS.items():
        if connection.dialect.name == "postgresql":
            connection.execute(text(f"CREATE OR REPLACE VIEW {name} AS {body}"))
        else:
            connection.execute(text(f"DROP VIEW IF EXISTS {name}"))
            connection.execute(text(f"CREATE VIEW {name} AS {body}"))
