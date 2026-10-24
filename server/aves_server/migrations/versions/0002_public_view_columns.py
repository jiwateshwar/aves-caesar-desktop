"""Extend v_published_photos for external apps (e.g. the public catalogue in its own stack).

New columns, appended at the end so existing readers keep working (PostgreSQL requires that for
CREATE OR REPLACE VIEW):
- session_title: the title you gave the shooting day
- species_names: common names in 70+ languages, as JSON keyed by language code
- in_library: false once the file has disappeared from the published folder
- changed_at: latest change to the file, shot, day or identification, for incremental sync

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from aves_server.migrations.helpers import replace_view

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SELECT = """
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
               d.box,
               se.title AS session_title,
               sp.names AS species_names,
               EXISTS (SELECT 1 FROM library_files lf WHERE lf.relpath = mf.published_relpath) AS in_library,
               {latest}(mf.published_at, s.updated_at, se.updated_at, COALESCE(d.updated_at, s.updated_at)) AS changed_at
"""
FROM = """        FROM media_files mf
        JOIN shots s ON s.id = mf.shot_id
        JOIN sessions se ON se.id = s.session_id
        LEFT JOIN detections d ON d.shot_id = s.id AND d.status = 'confirmed'
        LEFT JOIN species sp ON sp.species_id = d.species_id
        WHERE mf.status = 'published'
"""
PREVIOUS = """
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
"""


def upgrade() -> None:
    # GREATEST ignores NULLs on PostgreSQL; SQLite's multi-argument MAX is its equivalent here (no NULL inputs).
    latest = "GREATEST" if op.get_bind().dialect.name == "postgresql" else "MAX"
    replace_view("v_published_photos", SELECT.format(latest=latest) + FROM)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP VIEW IF EXISTS v_published_photos")  # PostgreSQL can't drop columns with OR REPLACE
    replace_view("v_published_photos", PREVIOUS)
