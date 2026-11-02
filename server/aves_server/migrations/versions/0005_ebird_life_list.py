"""The photographer's eBird life list (latest upload only), for marking new species in review.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 00:39:33.839316
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('life_list_imports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=300), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('species_count', sa.Integer(), nullable=False),
    sa.Column('matched_count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('life_list_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('import_id', sa.Integer(), nullable=False),
    sa.Column('species_id', sa.String(length=16), nullable=True),
    sa.Column('common_name', sa.String(length=200), nullable=False),
    sa.Column('scientific_name', sa.String(length=200), nullable=False),
    sa.Column('first_seen', sa.Date(), nullable=True),
    sa.Column('location', sa.String(length=300), nullable=True),
    sa.Column('region', sa.String(length=40), nullable=True),
    sa.Column('matched_by', sa.String(length=16), nullable=True),
    sa.ForeignKeyConstraint(['import_id'], ['life_list_imports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['species_id'], ['species.species_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('life_list_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_list_entries_import_id'), ['import_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_life_list_entries_species_id'), ['species_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('life_list_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_list_entries_species_id'))
        batch_op.drop_index(batch_op.f('ix_life_list_entries_import_id'))

    op.drop_table('life_list_entries')
    op.drop_table('life_list_imports')
