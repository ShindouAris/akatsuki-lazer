"""The extra profile fields (tab counts, play history, per-mode statistics)."""

import pytest

from app.api.v2.users import _user_to_response
from app.api.v2.users import with_profile_extras
from app.models.beatmap import BeatmapStatus
from app.models.user import GameMode
from app.services.variant import Variant
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user
from tests.test_score_stats import session_maker  # noqa: F401


@pytest.mark.asyncio
async def test_profile_carries_counts_history_and_all_rulesets(session_maker) -> None:  # noqa: F811
    async with session_maker() as db:
        user = await make_user(db, "extras")
        one = await make_beatmap(db, BeatmapStatus.RANKED)
        two = await make_beatmap(db, BeatmapStatus.RANKED)
        await make_score(db, user, one, 1000, 50.0)
        await make_score(db, user, one, 2000, 60.0)
        await make_score(db, user, two, 500, 10.0, passed=False)
        await make_score(db, user, two, 900, 30.0, variant=1)  # another board: not in the vanilla best count
        await db.commit()
        await db.refresh(user, ["statistics"])

        response = await with_profile_extras(_user_to_response(user), user, GameMode.OSU, db, Variant.VANILLA)

    # the 2000-point play is first on `one`; `two` has no passed vanilla play, so one first place in total
    assert response.scores_first_count == 1
    assert response.scores_best_count == 2
    assert response.scores_recent_count == 4
    assert response.beatmap_playcounts_count == 2
    assert [point.count for point in response.monthly_playcounts] == [4]
    assert set(response.statistics_rulesets) == {"osu", "taiko", "fruits", "mania"}
