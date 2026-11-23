"""add user_statistics (mode, pp) index

Rank lookups and the rankings page order and count by (mode, pp).

Revision ID: a1c4e9b27d10
Revises: 7320fdf0bb4e
Create Date: 2026-11-22 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1c4e9b27d10'
down_revision: Union[str, Sequence[str], None] = '7320fdf0bb4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_user_statistics_mode_pp', 'user_statistics', ['mode', 'pp'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_user_statistics_mode_pp', table_name='user_statistics')
