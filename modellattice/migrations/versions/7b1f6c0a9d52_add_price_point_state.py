"""add price_point.state, make amount nullable

Revision ID: 7b1f6c0a9d52
Revises: a3c91e7d2b44
Create Date: 2026-11-26 00:00:00

A price is never just a number: OpenRouter's variable-price routers
(e.g. openrouter/auto) report -1 per token ("depends on the routed
model"), which this project stored literally as -$1,000,000 per 1M
tokens - a real sentinel masquerading as a real price, able to corrupt
any cost comparison or ranking that trusted it. `state` makes that
explicit; a non-"known" row never carries a fake amount.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b1f6c0a9d52'
down_revision: Union[str, None] = 'a3c91e7d2b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('price_point', sa.Column('state', sa.String(16), nullable=False, server_default='known'))
    op.alter_column('price_point', 'amount', existing_type=sa.Float(), nullable=True)
    # Reclassify existing sentinel rows: a negative amount was never a real
    # price - it is exactly what `state='variable'` now means explicitly.
    op.execute("UPDATE price_point SET state = 'variable', amount = NULL WHERE amount < 0")


def downgrade() -> None:
    op.execute("UPDATE price_point SET amount = -1000000 WHERE state = 'variable' AND amount IS NULL")
    op.alter_column('price_point', 'amount', existing_type=sa.Float(), nullable=False)
    op.drop_column('price_point', 'state')
