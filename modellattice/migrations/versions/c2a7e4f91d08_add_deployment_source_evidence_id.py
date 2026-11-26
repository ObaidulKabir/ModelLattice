"""add deployment.source_evidence_id

Revision ID: c2a7e4f91d08
Revises: 7b1f6c0a9d52
Create Date: 2026-11-26 00:00:00

Deployment is the UMIM D12 table (region/data residency/serving config)
but, unlike PricePoint, had no way to trace a row back to the evidence it
came from. No backfill needed - the table has zero real rows anywhere in
this catalog today.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2a7e4f91d08'
down_revision: Union[str, None] = '7b1f6c0a9d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('deployment', sa.Column('source_evidence_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_deployment_source_evidence_id', 'deployment', 'evidence_source', ['source_evidence_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_deployment_source_evidence_id', 'deployment', type_='foreignkey')
    op.drop_column('deployment', 'source_evidence_id')
