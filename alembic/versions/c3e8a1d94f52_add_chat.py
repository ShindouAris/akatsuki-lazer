"""add chat

Chat of multiplayer rooms: who may read a channel, and its messages.

Revision ID: c3e8a1d94f52
Revises: b7d2f5a81c34
Create Date: 2026-11-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e8a1d94f52'
down_revision: Union[str, Sequence[str], None] = 'b7d2f5a81c34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('chat_channel_members',
    sa.Column('channel_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('last_read_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('channel_id', 'user_id')
    )
    op.create_table('chat_messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('channel_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_id', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('is_action', sa.Boolean(), nullable=False),
    sa.Column('uuid', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chat_messages_channel_id', 'chat_messages', ['channel_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_chat_messages_channel_id', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_table('chat_channel_members')
