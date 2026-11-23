"""add user_rank_history

Daily global rank per user and mode, for the 90 day rank graph.

Revision ID: b7d2f5a81c34
Revises: a1c4e9b27d10
Create Date: 2026-11-22 23:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2f5a81c34'
down_revision: Union[str, Sequence[str], None] = 'a1c4e9b27d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('user_rank_history',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'mode', 'day')
    )
    op.create_index('ix_user_rank_history_day', 'user_rank_history', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_user_rank_history_day', table_name='user_rank_history')
    op.drop_table('user_rank_history')
