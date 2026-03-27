"""User service statistics tests."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.user_service import update_user_statistics


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
