"""add score variants

Relax and Autopilot get their own boards: `scores.variant` and `user_statistics.variant`
(0 vanilla, 1 relax, 2 autopilot). Existing scores are classified from their mods; run
`python -m app.services.score_worker rebuild` afterwards to recompute the statistics per board.

Revision ID: d4a9c6e2b718
Revises: c3e8a1d94f52
Create Date: 2026-11-23 10:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a9c6e2b718'
down_revision: Union[str, Sequence[str], None] = 'c3e8a1d94f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RELAX_RULESETS = {0, 1, 2}
_AUTOPILOT_RULESETS = {0}


def _variant(data: str | None, ruleset_id: int) -> int:
    try:
        mods = (json.loads(data or "{}") or {}).get("mods", [])
    except (TypeError, ValueError, AttributeError):
        return 0
    acronyms = {str(m.get("acronym", "")).upper() for m in mods if isinstance(m, dict)}
    if "RX" in acronyms and ruleset_id in _RELAX_RULESETS:
        return 1
    if "AP" in acronyms and ruleset_id in _AUTOPILOT_RULESETS:
        return 2
    return 0


def upgrade() -> None:
    op.add_column('scores', sa.Column('variant', sa.Integer(), server_default='0', nullable=False))
    op.create_index('ix_scores_variant', 'scores', ['variant'], unique=False)
    op.add_column('user_statistics', sa.Column('variant', sa.Integer(), server_default='0', nullable=False))
    op.create_index(
        'ix_user_statistics_variant_mode_pp', 'user_statistics', ['variant', 'mode', 'pp'], unique=False,
    )

    bind = op.get_bind()
    scores = sa.table(
        'scores',
        sa.column('id', sa.Integer),
        sa.column('ruleset_id', sa.Integer),
        sa.column('data', sa.Text),
        sa.column('variant', sa.Integer),
    )
    rows = bind.execute(
        sa.select(scores.c.id, scores.c.ruleset_id, scores.c.data).where(
            scores.c.data.like('%"RX"%') | scores.c.data.like('%"AP"%'),
        ),
    ).all()
    for score_id, ruleset_id, data in rows:
        variant = _variant(data, ruleset_id)
        if variant:
            bind.execute(scores.update().where(scores.c.id == score_id).values(variant=variant))


def downgrade() -> None:
    op.drop_index('ix_user_statistics_variant_mode_pp', table_name='user_statistics')
    op.drop_column('user_statistics', 'variant')
    op.drop_index('ix_scores_variant', table_name='scores')
    op.drop_column('scores', 'variant')
