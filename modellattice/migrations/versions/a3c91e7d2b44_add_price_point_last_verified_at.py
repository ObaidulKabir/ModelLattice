"""add price_point.last_verified_at

Revision ID: a3c91e7d2b44
Revises: 4484ceea155b
Create Date: 2026-11-26 00:00:00

Freshness must reflect when a price was last CONFIRMED by a source, not
when it last CHANGED (effective_from) - an unchanged price writes no new
row, so most prices looked stale no matter how recently they were checked.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c91e7d2b44'
down_revision: Union[str, None] = '4484ceea155b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('price_point', sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True))
    # Existing rows: the only verification we can honestly attest to is the
    # moment the price was recorded.
    op.execute("UPDATE price_point SET last_verified_at = effective_from")


def downgrade() -> None:
    op.drop_column('price_point', 'last_verified_at')
