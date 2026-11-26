"""user ids start at 2

The osu! client treats a user with ``Id <= 1`` as "not a real player" (id 0 is the system user): it never loads the
local user's statistics and the avatar / name stop opening the profile. ppy's own ids start at 2 for the same reason.
Existing rows are left alone; only the sequence is moved so that the next account is never id 0 or 1.

Revision ID: f6c2e8b4d013
Revises: e5b1d7a3c902
Create Date: 2026-11-26 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f6c2e8b4d013'
down_revision: Union[str, Sequence[str], None] = 'e5b1d7a3c902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite (tests) has no sequences
    # Fresh database (the sequence has not gone past 1): restart it at 2. Otherwise keep its position and only
    # raise the floor, so existing ids are never reused.
    op.execute(
        """
        DO $$
        BEGIN
            IF (SELECT last_value FROM users_id_seq) < 2 THEN
                ALTER SEQUENCE users_id_seq MINVALUE 2 START WITH 2 RESTART WITH 2;
            ELSE
                ALTER SEQUENCE users_id_seq MINVALUE 2 START WITH 2;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER SEQUENCE users_id_seq MINVALUE 1 START WITH 1")

