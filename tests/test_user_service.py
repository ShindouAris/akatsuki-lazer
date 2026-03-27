"""User service statistics tests."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.user_service import calculate_profile_hit_accuracy
from app.services.user_service import refresh_user_hit_accuracy
from app.services.user_service import refresh_user_pp_and_ranks
from app.services.user_service import update_user_statistics


async def _create_ranked_beatmap(
    db_session: AsyncSession,
    owner_user_id: int,
    version: str,
    mode: GameMode = GameMode.OSU,
) -> Beatmap:
    beatmapset = BeatmapSet(
        user_id=owner_user_id,
        artist="artist",
        title="title",
        creator="creator",
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmapset)
    await db_session.flush()

    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        user_id=owner_user_id,
        version=version,
        mode=mode,
        status=BeatmapStatus.RANKED,
    )
    db_session.add(beatmap)
    await db_session.flush()
    return beatmap


@pytest.mark.asyncio
async def test_update_user_statistics_updates_ranked_score_and_grade_a(
    db_session: AsyncSession,
) -> None:
    """Passed ranked score increments ranked score and grade counter."""
    user = User(
        username="stats_ranked_user",
        email="stats_ranked_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    score = Score(
        user_id=user.id,
        beatmap_id=1,
        ruleset_id=int(GameMode.OSU),
        data="{}",
        total_score=654321,
        accuracy=98.2,
        pp=150.0,
        max_combo=777,
        rank="A",
        passed=True,
        ranked=True,
    )

    await update_user_statistics(db_session, stats, score)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.play_count == 1
    assert stats.total_score == 654321
    assert stats.ranked_score == 654321
    assert stats.grade_a == 1
    assert stats.maximum_combo == 777


@pytest.mark.asyncio
async def test_update_user_statistics_does_not_track_grade_b_and_below(
    db_session: AsyncSession,
) -> None:
    """B rank does not increment stored grade counters, but score stats still update."""
    user = User(
        username="stats_b_user",
        email="stats_b_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    score = Score(
        user_id=user.id,
        beatmap_id=1,
        ruleset_id=int(GameMode.OSU),
        data="{}",
        total_score=321654,
        accuracy=95.5,
        pp=120.0,
        max_combo=555,
        rank="B",
        passed=True,
        ranked=True,
    )

    await update_user_statistics(db_session, stats, score)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.play_count == 1
    assert stats.total_score == 321654
    assert stats.ranked_score == 321654
    assert stats.grade_ss == 0
    assert stats.grade_ssh == 0
    assert stats.grade_s == 0
    assert stats.grade_sh == 0
    assert stats.grade_a == 0


@pytest.mark.asyncio
async def test_update_user_statistics_failed_score_does_not_increment_ranked_or_grades(
    db_session: AsyncSession,
) -> None:
    """Failed/unranked score still increments play count but not ranked stats."""
    user = User(
        username="stats_failed_user",
        email="stats_failed_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    score = Score(
        user_id=user.id,
        beatmap_id=1,
        ruleset_id=int(GameMode.OSU),
        data="{}",
        total_score=222222,
        accuracy=90.0,
        pp=None,
        max_combo=300,
        rank="F",
        passed=False,
        ranked=False,
    )

    await update_user_statistics(db_session, stats, score)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.play_count == 1
    assert stats.total_score == 222222
    assert stats.ranked_score == 0
    assert stats.grade_ss == 0
    assert stats.grade_ssh == 0
    assert stats.grade_s == 0
    assert stats.grade_sh == 0
    assert stats.grade_a == 0


@pytest.mark.asyncio
async def test_update_user_statistics_increments_grade_ssh_for_xh_rank(
    db_session: AsyncSession,
) -> None:
    """XH rank maps to SSH grade counter for passed ranked scores."""
    user = User(
        username="stats_xh_user",
        email="stats_xh_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    score = Score(
        user_id=user.id,
        beatmap_id=1,
        ruleset_id=int(GameMode.OSU),
        data="{}",
        total_score=999999,
        accuracy=100.0,
        pp=250.0,
        max_combo=1000,
        rank="XH",
        passed=True,
        ranked=True,
    )

    await update_user_statistics(db_session, stats, score)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.grade_ssh == 1
    assert stats.ranked_score == 999999


@pytest.mark.asyncio
async def test_calculate_profile_hit_accuracy_averages_best_per_passed_ranked_beatmap(
    db_session: AsyncSession,
) -> None:
    """Profile accuracy averages best ranked pass per beatmap and excludes invalid scores."""
    user = User(
        username="accuracy_user",
        email="accuracy_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    beatmap_one = await _create_ranked_beatmap(db_session, user.id, "Map One")
    beatmap_two = await _create_ranked_beatmap(db_session, user.id, "Map Two")
    beatmap_three = await _create_ranked_beatmap(db_session, user.id, "Map Three")
    beatmap_four = await _create_ranked_beatmap(db_session, user.id, "Map Four")

    db_session.add_all(
        [
            Score(
                user_id=user.id,
                beatmap_id=beatmap_one.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=100,
                accuracy=90.0,
                pp=100.0,
                max_combo=100,
                rank="A",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_one.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=200,
                accuracy=95.0,
                pp=120.0,
                max_combo=200,
                rank="S",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_two.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=300,
                accuracy=80.0,
                pp=130.0,
                max_combo=300,
                rank="B",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_three.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=400,
                accuracy=60.0,
                pp=90.0,
                max_combo=150,
                rank="F",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_four.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=500,
                accuracy=70.0,
                pp=80.0,
                max_combo=150,
                rank="A",
                passed=False,
                ranked=True,
            ),
        ],
    )
    await db_session.commit()

    value = await calculate_profile_hit_accuracy(db_session, user.id, GameMode.OSU)

    assert value == 87.5


@pytest.mark.asyncio
async def test_calculate_profile_hit_accuracy_normalizes_fraction_scale(
    db_session: AsyncSession,
) -> None:
    """Stored score accuracy in 0-1 scale is normalized to profile percent scale."""
    user = User(
        username="accuracy_fraction_user",
        email="accuracy_fraction_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    beatmap_one = await _create_ranked_beatmap(db_session, user.id, "Fraction One")
    beatmap_two = await _create_ranked_beatmap(db_session, user.id, "Fraction Two")

    db_session.add_all(
        [
            Score(
                user_id=user.id,
                beatmap_id=beatmap_one.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=100,
                accuracy=0.96,
                pp=100.0,
                max_combo=100,
                rank="A",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_two.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=200,
                accuracy=0.84,
                pp=120.0,
                max_combo=200,
                rank="S",
                passed=True,
                ranked=True,
            ),
        ],
    )
    await db_session.commit()

    value = await calculate_profile_hit_accuracy(db_session, user.id, GameMode.OSU)

    assert value == 90.0


@pytest.mark.asyncio
async def test_refresh_user_pp_and_ranks_updates_hit_accuracy(
    db_session: AsyncSession,
) -> None:
    """Refreshing rankings also recalculates profile hit accuracy."""
    user = User(
        username="accuracy_refresh_user",
        email="accuracy_refresh_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    beatmap_one = await _create_ranked_beatmap(db_session, user.id, "Refresh One")
    beatmap_two = await _create_ranked_beatmap(db_session, user.id, "Refresh Two")

    db_session.add_all(
        [
            Score(
                user_id=user.id,
                beatmap_id=beatmap_one.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=1000,
                accuracy=96.0,
                pp=180.0,
                max_combo=400,
                rank="S",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_two.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=900,
                accuracy=84.0,
                pp=160.0,
                max_combo=350,
                rank="A",
                passed=True,
                ranked=True,
            ),
        ],
    )
    await db_session.commit()

    await refresh_user_pp_and_ranks(db_session, user.id, GameMode.OSU)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.hit_accuracy == 90.0
    assert stats.accuracy == 90.0


@pytest.mark.asyncio
async def test_refresh_user_hit_accuracy_updates_only_from_allowed_scores(
    db_session: AsyncSession,
) -> None:
    """Hit accuracy refresh includes only passed/ranked/non-F plays."""
    user = User(
        username="accuracy_only_user",
        email="accuracy_only_user@example.com",
        password_hash=get_password_hash("testpassword"),
        country_acronym="US",
    )
    db_session.add(user)
    await db_session.flush()

    stats = UserStatistics(user_id=user.id, mode=GameMode.OSU)
    db_session.add(stats)
    await db_session.flush()

    beatmap_one = await _create_ranked_beatmap(db_session, user.id, "Allowed One")
    beatmap_two = await _create_ranked_beatmap(db_session, user.id, "Allowed Two")
    beatmap_three = await _create_ranked_beatmap(db_session, user.id, "Denied F")

    db_session.add_all(
        [
            Score(
                user_id=user.id,
                beatmap_id=beatmap_one.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=101,
                accuracy=91.0,
                pp=80.0,
                max_combo=200,
                rank="A",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_two.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=102,
                accuracy=89.0,
                pp=81.0,
                max_combo=210,
                rank="S",
                passed=True,
                ranked=True,
            ),
            Score(
                user_id=user.id,
                beatmap_id=beatmap_three.id,
                ruleset_id=int(GameMode.OSU),
                data="{}",
                total_score=99,
                accuracy=10.0,
                pp=1.0,
                max_combo=10,
                rank="F",
                passed=True,
                ranked=True,
            ),
        ],
    )
    await db_session.commit()

    await refresh_user_hit_accuracy(db_session, user.id, GameMode.OSU)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.hit_accuracy == 90.0
    assert stats.accuracy == 90.0
