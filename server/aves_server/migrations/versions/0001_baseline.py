"""Baseline: the schema as first deployed (created with create_all before migrations existed).

Databases from that time have these tables but no alembic_version; Database.migrate() stamps them
with this revision instead of running it.

Revision ID: 0001
Revises: 
Create Date: 2026-10-24 22:14:00.134565
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from aves_server.migrations.helpers import drop_view, replace_view

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

BASELINE_VIEWS = {
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



def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.Enum('queued', 'running', 'done', 'failed', name='jobstatus', native_enum=False, length=32), nullable=False),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_status'), ['status'], unique=False)

    op.create_table('sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shoot_date', sa.Date(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('location_lat', sa.Float(), nullable=True),
    sa.Column('location_lon', sa.Float(), nullable=True),
    sa.Column('location_label', sa.String(length=300), nullable=True),
    sa.Column('country', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=120), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('analyse_without_location', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('shoot_date')
    )
    op.create_table('settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('species',
    sa.Column('species_id', sa.String(length=16), nullable=False),
    sa.Column('scientific_name', sa.String(length=200), nullable=False),
    sa.Column('common_name', sa.String(length=200), nullable=False),
    sa.Column('family', sa.String(length=200), nullable=True),
    sa.Column('order_name', sa.String(length=200), nullable=True),
    sa.Column('names', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('species_id')
    )
    with op.batch_alter_table('species', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_species_common_name'), ['common_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_species_scientific_name'), ['scientific_name'], unique=False)

    op.create_table('worker_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('activity', sa.String(length=300), nullable=True),
    sa.Column('providers', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('model_error', sa.Text(), nullable=True),
    sa.Column('last_scan', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('last_scan_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('shots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('dump_folder', sa.String(length=1000), nullable=False),
    sa.Column('group_key', sa.String(length=1200), nullable=False),
    sa.Column('captured_at', sa.DateTime(), nullable=True),
    sa.Column('captured_at_source', sa.String(length=16), nullable=False),
    sa.Column('exif_datetime', sa.String(length=40), nullable=True),
    sa.Column('gps_lat', sa.Float(), nullable=True),
    sa.Column('gps_lon', sa.Float(), nullable=True),
    sa.Column('location_source', sa.String(length=16), nullable=True),
    sa.Column('country', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=120), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('status', sa.Enum('awaiting_location', 'pending', 'review', 'confirmed', 'published', 'ignored', 'error', name='shotstatus', native_enum=False, length=32), nullable=False),
    sa.Column('analysed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('analysed_with_location', sa.Boolean(), nullable=True),
    sa.Column('analysis_provider', sa.String(length=64), nullable=True),
    sa.Column('analysis_error', sa.Text(), nullable=True),
    sa.Column('analysis_attempts', sa.Integer(), nullable=False),
    sa.Column('image_width', sa.Integer(), nullable=True),
    sa.Column('image_height', sa.Integer(), nullable=True),
    sa.Column('min_confidence', sa.Float(), nullable=True),
    sa.Column('bird_count', sa.Integer(), nullable=False),
    sa.Column('preview_ready', sa.Boolean(), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_key')
    )
    with op.batch_alter_table('shots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shots_dump_folder'), ['dump_folder'], unique=False)
        batch_op.create_index(batch_op.f('ix_shots_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shots_status'), ['status'], unique=False)

    op.create_table('detections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shot_id', sa.Integer(), nullable=False),
    sa.Column('box', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('box_confidence', sa.Float(), nullable=True),
    sa.Column('predictions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('species_id', sa.String(length=16), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('source', sa.Enum('model', 'manual', name='detectionsource', native_enum=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('pending', 'confirmed', 'rejected', name='detectionstatus', native_enum=False, length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['shot_id'], ['shots.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['species_id'], ['species.species_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('detections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_detections_shot_id'), ['shot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_detections_species_id'), ['species_id'], unique=False)

    op.create_table('media_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shot_id', sa.Integer(), nullable=True),
    sa.Column('original_filename', sa.String(length=500), nullable=False),
    sa.Column('final_filename', sa.String(length=600), nullable=False),
    sa.Column('filename_key', sa.String(length=600), nullable=False),
    sa.Column('extension', sa.String(length=16), nullable=False),
    sa.Column('file_type', sa.String(length=16), nullable=False),
    sa.Column('is_raw', sa.Boolean(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('mtime', sa.Float(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('in_dump', 'published', 'duplicate', 'missing', name='filestatus', native_enum=False, length=32), nullable=False),
    sa.Column('duplicate_of_id', sa.Integer(), nullable=True),
    sa.Column('dump_relpath', sa.String(length=1500), nullable=True),
    sa.Column('published_relpath', sa.String(length=1500), nullable=True),
    sa.Column('camera_make', sa.String(length=120), nullable=True),
    sa.Column('camera_model', sa.String(length=120), nullable=True),
    sa.Column('lens_model', sa.String(length=200), nullable=True),
    sa.Column('focal_length', sa.Float(), nullable=True),
    sa.Column('f_number', sa.Float(), nullable=True),
    sa.Column('exposure_time', sa.Float(), nullable=True),
    sa.Column('iso', sa.Integer(), nullable=True),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('exif', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['duplicate_of_id'], ['media_files.id'], ),
    sa.ForeignKeyConstraint(['shot_id'], ['shots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('media_files', schema=None) as batch_op:
        batch_op.create_index('ix_media_files_dump_relpath_status', ['dump_relpath', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_files_filename_key'), ['filename_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_files_sha256'), ['sha256'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_files_shot_id'), ['shot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_files_status'), ['status'], unique=False)

    op.create_table('library_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('relpath', sa.String(length=1500), nullable=False),
    sa.Column('filename_key', sa.String(length=600), nullable=False),
    sa.Column('media_file_id', sa.Integer(), nullable=True),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['media_file_id'], ['media_files.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('relpath')
    )
    with op.batch_alter_table('library_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_library_files_filename_key'), ['filename_key'], unique=False)
    for name, body in BASELINE_VIEWS.items():
        replace_view(name, body)


def downgrade() -> None:
    for name in BASELINE_VIEWS:
        drop_view(name)
    with op.batch_alter_table('library_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_library_files_filename_key'))

    op.drop_table('library_files')
    with op.batch_alter_table('media_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_media_files_status'))
        batch_op.drop_index(batch_op.f('ix_media_files_shot_id'))
        batch_op.drop_index(batch_op.f('ix_media_files_sha256'))
        batch_op.drop_index(batch_op.f('ix_media_files_filename_key'))
        batch_op.drop_index('ix_media_files_dump_relpath_status')

    op.drop_table('media_files')
    with op.batch_alter_table('detections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_detections_species_id'))
        batch_op.drop_index(batch_op.f('ix_detections_shot_id'))

    op.drop_table('detections')
    with op.batch_alter_table('shots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shots_status'))
        batch_op.drop_index(batch_op.f('ix_shots_session_id'))
        batch_op.drop_index(batch_op.f('ix_shots_dump_folder'))

    op.drop_table('shots')
    op.drop_table('worker_state')
    with op.batch_alter_table('species', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_species_scientific_name'))
        batch_op.drop_index(batch_op.f('ix_species_common_name'))

    op.drop_table('species')
    op.drop_table('settings')
    op.drop_table('sessions')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_jobs_kind'))

    op.drop_table('jobs')
