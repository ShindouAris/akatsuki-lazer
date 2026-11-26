"""Tests for the Duel pool seeder."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.models.legacy import MatchmakingPool
from app.models.legacy import MatchmakingPoolBeatmap
from app.seeders.matchmaking_seeder import DUEL_MODES
from app.seeders.matchmaking_seeder import MatchmakingSeeder
from app.seeders.matchmaking_seeder import rating_for
from app.seeders.matchmaking_seeder import star_factor


def _beatmap(beatmap_id: int, mode: str, star_rating: float, status: str = "ranked") -> dict:
    return {
        "id": beatmap_id,
        "version": f"diff {beatmap_id}",
        "mode": mode,
        "status": status,
        "checksum": f"{beatmap_id:032x}",
        "difficulty_rating": star_rating,
        "total_length": 100,
        "hit_length": 90,
        "bpm": 180.0,
        "cs": 4.0,
        "ar": 9.0,
        "accuracy": 8.0,
        "drain": 5.0,
    }


SETS = {
    1: {
        "id": 1,
        "user_id": 999,
        "artist": "artist",
        "title": "title",
        "creator": "mapper",
        "status": "ranked",
        "beatmaps": [
            _beatmap(11, "osu", 2.5),
            _beatmap(12, "osu", 5.0),
            _beatmap(13, "taiko", 4.0),
        ],
    },
    2: {
        "id": 2,
        "user_id": 999,
        "artist": "artist 2",
        "title": "title 2",
        "creator": "mapper",
        "status": "ranked",
        "beatmaps": [_beatmap(21, "osu", 6.0)],
    },
    3: {
        "id": 3,
        "user_id": 999,
        "artist": "artist 3",
        "title": "title 3",
        "creator": "mapper",
        "status": "ranked",
        "beatmaps": [_beatmap(31, "osu", 0.0), _beatmap(32, "osu", 3.0)],
    },
    4: {
        "id": 4,
        "user_id": 999,
        "artist": "artist 4",
        "title": "title 4",
        "creator": "mapper",
        "status": "ranked",
        "beatmaps": [
            _beatmap(41, "osu", 3.0, "graveyard"),
            _beatmap(42, "osu", 3.1, "pending"),
            _beatmap(43, "osu", 3.2, "wip"),
            _beatmap(44, "osu", 3.3, "loved"),
            _beatmap(45, "osu", 3.4, "approved"),
            _beatmap(46, "osu", 3.5, "ranked"),
        ],
    },
}


class FakeMirror:
    """Stands in for the metadata mirror and remembers what was asked."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(request.url.path)
        set_id = int(request.url.path.rsplit("/", 1)[1])
        if set_id in SETS:
            return httpx.Response(200, json=SETS[set_id])
        return httpx.Response(404)


@pytest.fixture
def mirror() -> FakeMirror:
    return FakeMirror()


@pytest.fixture
def make_seeder(tmp_path: Path, mirror: FakeMirror) -> Callable[..., MatchmakingSeeder]:
    def make(*lines: str) -> MatchmakingSeeder:
        sets_file = tmp_path / "sets.txt"
        sets_file.write_text("\n".join(lines), encoding="utf-8")
        return MatchmakingSeeder(
            sets_file=sets_file,
            metadata_url="https://mirror.test/api",
            http_client=mirror.client(),
            fetch_delay=0,
        )

    return make


async def _pools(session: AsyncSession) -> dict[str, MatchmakingPool]:
    result = await session.execute(select(MatchmakingPool))
    return {pool.name: pool for pool in result.scalars().all()}


async def _cards(session: AsyncSession, pool_id: int) -> dict[int, MatchmakingPoolBeatmap]:
    result = await session.execute(
        select(MatchmakingPoolBeatmap).where(MatchmakingPoolBeatmap.pool_id == pool_id),
    )
    return {card.beatmap_id: card for card in result.scalars().all()}


async def _card_count(session: AsyncSession, pool_id: int) -> int:
    # Counts rows: _cards() is keyed by beatmap id, so it would hide duplicated cards.
    result = await session.execute(
        select(func.count())
        .select_from(MatchmakingPoolBeatmap)
        .where(MatchmakingPoolBeatmap.pool_id == pool_id),
    )
    return result.scalar_one()


def test_seeder_cli_imports_every_model() -> None:
    # A fresh interpreter, so that app.main (which the other tests import and which pulls in every model) is
    # not there to hide a missing import: `python -m app.seeders` must configure the mappers on its own.
    code = "import app.seeders.__main__; from sqlalchemy.orm import configure_mappers; configure_mappers()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr[-800:]


def test_rating_formula_matches_the_spectator_server() -> None:
    # osu-server-spectator's debug map: 4.18904 stars is rated 1277.
    assert rating_for(4.18904) == 1277
    assert rating_for(0) == 800


def test_mods_are_stored_in_the_form_the_server_serialises_them_to() -> None:
    modes = dict(DUEL_MODES)
    assert modes["1v1 HD"] == '[{"acronym":"HD"}]'
    assert modes["1v1 RX HD"] == '[{"acronym":"RX"},{"acronym":"HD"}]'
    assert len(modes) == len(DUEL_MODES)  # names are unique


def test_dt_and_nc_cards_are_rated_by_the_stars_they_play_at() -> None:
    from app.seeders.matchmaking_seeder import star_factor

    assert star_factor("") == 1.0
    assert star_factor(dict(DUEL_MODES)["1v1 RX HD"]) == 1.0
    assert star_factor(dict(DUEL_MODES)["1v1 AP DT"]) == star_factor(dict(DUEL_MODES)["1v1 RX NC"]) == 1.4


async def test_creates_one_ranked_play_pool_per_mode(db_session: AsyncSession, make_seeder) -> None:
    await make_seeder("1").run(db_session)

    pools = await _pools(db_session)
    assert set(pools) == {name for name, _ in DUEL_MODES}
    for pool in pools.values():
        assert pool.type == "ranked_play"
        assert pool.ruleset_id == 0
        assert pool.variant_id == 0
        assert pool.active is True
        assert pool.lobby_size == 2
        assert pool.rating_search_radius_exp > 0  # used as a divisor


async def test_every_pool_gets_every_osu_map_with_its_own_mods(
    db_session: AsyncSession, make_seeder
) -> None:
    await make_seeder("1", "2").run(db_session)

    pools = await _pools(db_session)
    expected_mods = dict(DUEL_MODES)
    for name, pool in pools.items():
        cards = await _cards(db_session, pool.id)
        # 13 is a taiko map: Relax/Autopilot only exist for osu!standard, so it is never a card.
        assert set(cards) == {11, 12, 21}
        assert {card.mods for card in cards.values()} == {expected_mods[name]}
        factor = star_factor(expected_mods[name])
        assert cards[11].rating == rating_for(2.5 * factor)
        assert cards[21].rating == rating_for(6.0 * factor)
        assert all(card.selection_count == 0 for card in cards.values())


async def test_a_map_without_a_star_rating_is_never_a_card(db_session: AsyncSession, make_seeder) -> None:
    await make_seeder("3").run(db_session)

    for pool in (await _pools(db_session)).values():
        assert set(await _cards(db_session, pool.id)) == {32}


async def test_only_ranked_maps_are_cards(db_session: AsyncSession, make_seeder) -> None:
    await make_seeder("4").run(db_session)

    for pool in (await _pools(db_session)).values():
        # Ranked, approved and loved are cards; graveyard, pending and wip are not.
        assert set(await _cards(db_session, pool.id)) == {44, 45, 46}


async def test_cards_come_from_every_ranked_map_in_the_database_not_only_the_listed_sets(
    db_session: AsyncSession, make_seeder
) -> None:
    await make_seeder("1").run(db_session)
    # A ranked map that arrived some other way (played, searched for...) is a card too on the next run.
    await make_seeder("2").run(db_session)
    await make_seeder().run(db_session)  # no sets listed at all

    for pool in (await _pools(db_session)).values():
        assert set(await _cards(db_session, pool.id)) == {11, 12, 21}


async def test_unranked_cards_left_by_earlier_runs_are_removed(db_session: AsyncSession, make_seeder) -> None:
    await make_seeder("4").run(db_session)
    pool = next(iter((await _pools(db_session)).values()))
    for beatmap_id in (41, 42):  # a graveyard and another unranked map, as an older seeder would have added them
        db_session.add(
            MatchmakingPoolBeatmap(
                pool_id=pool.id, beatmap_id=beatmap_id, mods="", rating=1000, selection_count=0
            )
        )
    await db_session.flush()

    await make_seeder("4").run(db_session)

    assert set(await _cards(db_session, pool.id)) == {44, 45, 46}


async def test_beatmaps_are_cached_with_their_real_checksums(
    db_session: AsyncSession, make_seeder
) -> None:
    await make_seeder("1").run(db_session)

    beatmap = await db_session.get(Beatmap, 11)
    assert beatmap is not None
    assert beatmap.checksum == f"{11:032x}"
    assert beatmap.beatmapset_id == 1


async def test_running_twice_changes_nothing_and_keeps_learned_ratings(
    db_session: AsyncSession,
    make_seeder,
    mirror: FakeMirror,
) -> None:
    await make_seeder("1").run(db_session)
    await db_session.commit()

    pools = await _pools(db_session)
    card = (await _cards(db_session, pools["1v1 RX HD"].id))[11]
    card.rating = 1  # as if the server had learned a rating for it
    card.selection_count = 7
    await db_session.commit()
    requests_after_first_run = len(mirror.requested)

    await make_seeder("1").run(db_session)
    await db_session.commit()

    assert len(await _pools(db_session)) == len(DUEL_MODES)
    for pool in (await _pools(db_session)).values():
        assert await _card_count(db_session, pool.id) == 2
    refreshed = (await _cards(db_session, pools["1v1 RX HD"].id))[11]
    assert (refreshed.rating, refreshed.selection_count) == (1, 7)
    assert len(mirror.requested) == requests_after_first_run, (
        "cached sets must not be fetched again"
    )


async def test_adding_a_set_later_only_adds_its_cards(
    db_session: AsyncSession, make_seeder
) -> None:
    await make_seeder("1").run(db_session)
    await make_seeder("1", "2").run(db_session)

    for pool in (await _pools(db_session)).values():
        assert set(await _cards(db_session, pool.id)) == {11, 12, 21}
        assert await _card_count(db_session, pool.id) == 3


async def test_pools_that_are_no_longer_modes_leave_the_queue_but_keep_their_data(
    db_session: AsyncSession,
    make_seeder,
) -> None:
    old = MatchmakingPool(
        ruleset_id=0, variant_id=0, name="1v1 Nomod", type="ranked_play", active=True,
        lobby_size=2, rating_search_radius=200, rating_search_radius_max=9999, rating_search_radius_exp=30,
    )
    custom = MatchmakingPool(
        ruleset_id=0, variant_id=0, name="Friday cup", type="ranked_play", active=True,
        lobby_size=2, rating_search_radius=200, rating_search_radius_max=9999, rating_search_radius_exp=30,
    )
    db_session.add_all([old, custom])
    await db_session.flush()

    await make_seeder("1").run(db_session)

    pools = await _pools(db_session)
    assert pools["1v1 Nomod"].active is False  # retired mode: out of the queue, row kept
    assert pools["Friday cup"].active is True  # not a "1v1 ..." pool: left alone
    assert all(pools[name].active for name, _ in DUEL_MODES)

async def test_unknown_sets_are_skipped_without_failing_the_rest(
    db_session: AsyncSession,
    make_seeder,
    mirror: FakeMirror,
) -> None:
    await make_seeder("424242", "2").run(db_session)

    assert "/api/beatmapset/424242" in mirror.requested
    for pool in (await _pools(db_session)).values():
        assert set(await _cards(db_session, pool.id)) == {21}


async def test_empty_set_list_seeds_nothing(db_session: AsyncSession, make_seeder) -> None:
    await make_seeder("# only a comment", "").run(db_session)

    assert await _pools(db_session) == {}

