"""Camera clock corrections, and file changes visible to external apps.

- shots.captured_at_offset_days: days added to the camera's date when its clock was wrong
  (captured_at holds the corrected time; the camera's own value is captured_at minus this).
- media_files.updated_at, now part of v_published_photos.changed_at, so corrections such as lens names
  read again reach apps that sync incrementally.
- v_published_photos gains capture_offset_days (appended, as the view contract requires).

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

from aves_server.migrations.helpers import replace_view

revision = "0004"
down_revision = "0003"
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
               {latest}(mf.published_at, mf.updated_at, s.updated_at, se.updated_at, COALESCE(d.updated_at, s.updated_at)) AS changed_at,
               s.captured_at_offset_days AS capture_offset_days
"""
FROM = """        FROM media_files mf
        JOIN shots s ON s.id = mf.shot_id
        JOIN sessions se ON se.id = s.session_id
        LEFT JOIN detections d ON d.shot_id = s.id AND d.status = 'confirmed'
        LEFT JOIN species sp ON sp.species_id = d.species_id
        WHERE mf.status = 'published'
"""
PREVIOUS_SELECT = """
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


def _latest() -> str:
    return "GREATEST" if op.get_bind().dialect.name == "postgresql" else "MAX"


def upgrade() -> None:
    # Plain ADD COLUMN: batch mode would rebuild the tables on SQLite and break the views that use them.
    op.add_column("shots", sa.Column("captured_at_offset_days", sa.Integer(), nullable=False, server_default="0"))
    # SQLite can't add a column whose default is a function; existing rows there get a fixed time instead.
    now = sa.func.now() if op.get_bind().dialect.name == "postgresql" else sa.text("'1970-01-01 00:00:00'")
    op.add_column("media_files", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now))
    replace_view("v_published_photos", SELECT.format(latest=_latest()) + FROM)


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    op.execute("DROP VIEW IF EXISTS v_published_photos")
    if postgres:
        op.drop_column("media_files", "updated_at")
        op.drop_column("shots", "captured_at_offset_days")
    else:
        sightings = __import__("importlib").import_module("aves_server.migrations.versions.0001_baseline").BASELINE_VIEWS["v_species_sightings"]
        op.execute("DROP VIEW IF EXISTS v_species_sightings")
        with op.batch_alter_table("media_files") as batch:
            batch.drop_column("updated_at")
        with op.batch_alter_table("shots") as batch:
            batch.drop_column("captured_at_offset_days")
        replace_view("v_species_sightings", sightings)
    replace_view("v_published_photos", PREVIOUS_SELECT.format(latest=_latest()) + FROM)
    if postgres:
        op.execute("GRANT SELECT ON v_published_photos TO featherbase_reader")
