"""add medals

The medal catalogue and the medals users earned. The catalogue rows are inserted by the score worker the
first time a medal is awarded (see app/services/medals.py), so no data is created here.

Revision ID: e5b1d7a3c902
Revises: d4a9c6e2b718
Create Date: 2026-11-23 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b1d7a3c902'
down_revision: Union[str, Sequence[str], None] = 'd4a9c6e2b718'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('medals',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('grouping', sa.String(length=50), nullable=False),
    sa.Column('ordering', sa.Integer(), nullable=False),
    sa.Column('mode', sa.Integer(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('user_medals',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('medal_id', sa.Integer(), nullable=False),
    sa.Column('beatmap_id', sa.Integer(), nullable=True),
    sa.Column('awarded_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['beatmap_id'], ['beatmaps.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['medal_id'], ['medals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'medal_id')
    )


def downgrade() -> None:
    op.drop_table('user_medals')
    op.drop_table('medals')
