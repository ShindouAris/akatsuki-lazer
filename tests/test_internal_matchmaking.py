"""Tests for the internal matchmaking API and the ranked play rating, used by the Go multiplayer service."""

import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal.matchmaking import MatchResultIn
from app.api.internal.matchmaking import MatchResultOut
from app.api.internal.matchmaking import PlayerResultIn
from app.api.internal.matchmaking import PoolBeatmapOut
from app.api.internal.matchmaking import PoolOut
from app.api.internal.matchmaking import RatingsOut
from app.api.internal.matchmaking import ScoresOut
from app.api.internal.matchmaking import TopRatingOut
from app.api.internal.matchmaking import UserOut
from app.api.internal.matchmaking import UserRatingOut
from app.api.internal.multiplayer import CreateRoomIn
from app.api.internal.multiplayer import InviteCheckOut
from app.api.internal.multiplayer import RoomStateIn
from app.core.config import get_settings
from app.core.security import get_password_hash
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.legacy import MatchmakingPool
from app.models.legacy import MatchmakingPoolBeatmap
from app.models.legacy import MatchmakingUserEloHistory
from app.models.legacy import MatchmakingUserStats
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import MultiplayerScore
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserRelation
from app.models.user import UserStatistics
from app.services.rating import Elo
from app.services.rating import card_rating
from app.services.rating import initial_rating_from_pp
from app.services.rating import parse_elo_data
from app.services.rating import rate_match
from app.services.rating import serialize_elo_data

KEY = "test-internal-key"
HEADERS = {"X-Internal-Key": KEY}


@pytest.fixture(autouse=True)
def _internal_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "internal_api_key", KEY)
    yield


async def make_user(
    db: AsyncSession, name: str, pp: float | None = None, restricted: bool = False
) -> User:
    user = User(
        username=name,
        email=f"{name}@example.test",
        password_hash=get_password_hash("x"),
        is_restricted=restricted,
    )
    db.add(user)
    await db.flush()
    if pp is not None:
        db.add(UserStatistics(user_id=user.id, mode=GameMode.OSU, pp=pp))
        await db.flush()
    return user


async def make_pool(
    db: AsyncSession, name: str = "1v1 Nomod", pool_type: str = "ranked_play", active: bool = True
) -> MatchmakingPool:
    pool = MatchmakingPool(
        ruleset_id=0,
        variant_id=0,
        name=name,
        type=pool_type,
        active=active,
        lobby_size=2,
        rating_search_radius=200,
        rating_search_radius_max=9999,
        rating_search_radius_exp=30,
    )
    db.add(pool)
    await db.flush()
    return pool


async def make_beatmap(
    db: AsyncSession, checksum: str, star_rating: float = 4.0, mode: GameMode = GameMode.OSU
) -> Beatmap:
    beatmapset = BeatmapSet(
        user_id=None, artist="a", title="t", creator="c", status=BeatmapStatus.RANKED
    )
    db.add(beatmapset)
    await db.flush()
    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        version="v",
        mode=mode,
        status=BeatmapStatus.RANKED,
        checksum=checksum,
        difficulty_rating=star_rating,
    )
    db.add(beatmap)
    await db.flush()
    return beatmap


# --- authentication ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/internal/matchmaking/pools",
        "/internal/matchmaking/users/1",
        "/internal/matchmaking/items/1/scores",
    ],
)
async def test_requires_the_internal_key(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-Internal-Key": "wrong"})).status_code == 401


# --- pools ------------------------------------------------------------------------------------------------


async def test_lists_active_pools_of_a_type(client: AsyncClient, db_session: AsyncSession) -> None:
    ranked = await make_pool(db_session, "1v1 RX")
    await make_pool(db_session, "quick", pool_type="quick_play")
    await make_pool(db_session, "retired", active=False)

    response = await client.get(
        "/internal/matchmaking/pools", params={"type": "ranked_play"}, headers=HEADERS
    )

    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [ranked.id]
    pool = response.json()[0]
    assert pool["name"] == "1v1 RX"
    assert (pool["lobby_size"], pool["rating_search_radius"], pool["rating_search_radius_exp"]) == (
        2,
        200,
        30,
    )


async def test_pools_can_include_inactive_ones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_pool(db_session, "a")
    await make_pool(db_session, "b", active=False)

    response = await client.get(
        "/internal/matchmaking/pools", params={"active_only": "false"}, headers=HEADERS
    )

    assert {p["name"] for p in response.json()} == {"a", "b"}


async def test_unknown_pool_is_not_found(client: AsyncClient) -> None:
    for path in (
        "/internal/matchmaking/pools/99",
        "/internal/matchmaking/pools/99/beatmaps",
        "/internal/matchmaking/pools/99/ratings",
    ):
        assert (await client.get(path, headers=HEADERS)).status_code == 404


async def test_only_ranked_cards_with_a_rating_are_dealt(client: AsyncClient, db_session: AsyncSession) -> None:
    pool = await make_pool(db_session)
    ranked = await make_beatmap(db_session, "ranked", star_rating=4.0)
    unranked = {
        status: await make_beatmap(db_session, status.name, star_rating=4.0)
        for status in (BeatmapStatus.GRAVEYARD, BeatmapStatus.WIP, BeatmapStatus.PENDING, BeatmapStatus.LOVED)
    }
    approved = await make_beatmap(db_session, "approved", star_rating=4.0)
    unrated = await make_beatmap(db_session, "unrated", star_rating=0.0)
    approved.status = BeatmapStatus.APPROVED
    for status, beatmap in unranked.items():
        beatmap.status = status
    db_session.add_all(
        MatchmakingPoolBeatmap(pool_id=pool.id, beatmap_id=b.id, mods="", rating=1000)
        for b in (ranked, approved, unrated, *unranked.values())
    )
    await db_session.flush()

    response = await client.get(f"/internal/matchmaking/pools/{pool.id}/beatmaps", headers=HEADERS)

    assert sorted(c["checksum"] for c in response.json()) == ["approved", "ranked"]


async def test_pool_beatmaps_carry_their_mods_and_ratings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    other_pool = await make_pool(db_session, "other")
    easy = await make_beatmap(db_session, "easy", star_rating=2.0)
    hard = await make_beatmap(db_session, "hard", star_rating=6.0)

    db_session.add_all(
        [
            MatchmakingPoolBeatmap(
                pool_id=pool.id, beatmap_id=easy.id, mods="", rating=1100, selection_count=3
            ),
            MatchmakingPoolBeatmap(
                pool_id=pool.id, beatmap_id=hard.id, mods='[{"acronym":"RX"}]', rating=None
            ),
            MatchmakingPoolBeatmap(pool_id=other_pool.id, beatmap_id=easy.id, mods="", rating=1),
        ],
    )
    await db_session.flush()

    response = await client.get(f"/internal/matchmaking/pools/{pool.id}/beatmaps", headers=HEADERS)

    assert response.status_code == 200
    cards = response.json()
    assert [c["checksum"] for c in cards] == ["easy", "hard"], "only this pool's cards"
    assert cards[0] == {
        "beatmap_id": easy.id,
        "checksum": "easy",
        "mode": 0,
        "star_rating": 2.0,
        "mods": "",
        "rating": 1100,
        "selection_count": 3,
    }
    assert cards[1]["mods"] == '[{"acronym":"RX"}]'
    assert cards[1]["rating"] == card_rating(6.0), (
        "a card without a rating gets the one its difficulty implies"
    )


# --- players ----------------------------------------------------------------------------------------------


async def test_new_player_starts_at_a_rating_estimated_from_their_pp(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    user = await make_user(db_session, "alice", pp=3000)

    response = await client.get(
        f"/internal/matchmaking/pools/{pool.id}/users/{user.id}", headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json()["mu"] == pytest.approx(-4000 + 600 * math.log(3000 + 4000))
    assert response.json()["sigma"] == 150

    stored = await db_session.get(MatchmakingUserStats, (user.id, pool.id))
    assert stored is not None, "the stats are created on first use"


def test_estimate_of_a_player_without_pp_is_what_zero_pp_gives() -> None:
    # -4000 + 600 * ln(4000); negative pp (which should not exist) is treated as none.
    assert initial_rating_from_pp(0).mu == pytest.approx(976.43, abs=0.01)
    assert initial_rating_from_pp(-50) == initial_rating_from_pp(0)
    assert initial_rating_from_pp(0).mu > 600


async def test_known_player_keeps_their_rating(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    user = await make_user(db_session, "bob", pp=100)
    db_session.add(
        MatchmakingUserStats(
            user_id=user.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(1777, 88))
        )
    )
    await db_session.flush()

    response = await client.get(
        f"/internal/matchmaking/pools/{pool.id}/users/{user.id}", headers=HEADERS
    )

    assert response.json() == {"user_id": user.id, "mu": 1777.0, "sigma": 88.0, "rank": None}


async def test_unknown_player_is_not_found(client: AsyncClient, db_session: AsyncSession) -> None:
    pool = await make_pool(db_session)

    assert (
        await client.get(f"/internal/matchmaking/pools/{pool.id}/users/12345", headers=HEADERS)
    ).status_code == 404
    assert (
        await client.get("/internal/matchmaking/users/12345", headers=HEADERS)
    ).status_code == 404


async def test_user_info_for_the_queue(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session, "carol", restricted=True)

    response = await client.get(f"/internal/matchmaking/users/{user.id}", headers=HEADERS)

    assert response.json() == {"id": user.id, "username": "carol", "is_restricted": True}


async def test_rating_distribution_and_top_rating(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)

    empty = await client.get(f"/internal/matchmaking/pools/{pool.id}/top-rating", headers=HEADERS)
    assert empty.json() == {"rating": 0}

    for index, mu in enumerate([1200, 1800, 1500]):
        user = await make_user(db_session, f"p{index}")
        db_session.add(
            MatchmakingUserStats(
                user_id=user.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(mu))
            )
        )
    await db_session.flush()

    ratings = await client.get(f"/internal/matchmaking/pools/{pool.id}/ratings", headers=HEADERS)
    assert sorted(ratings.json()["ratings"]) == [1200, 1500, 1800]

    # With fewer than 100 players the "100th" is the lowest of them.
    top = await client.get(f"/internal/matchmaking/pools/{pool.id}/top-rating", headers=HEADERS)
    assert top.json() == {"rating": 1200}


# --- scores -----------------------------------------------------------------------------------------------


async def test_item_scores_are_the_best_score_of_each_player(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    beatmap = await make_beatmap(db_session, "b")
    room = MultiplayerRoom(name="r")
    db_session.add(room)
    await db_session.flush()

    from app.models.multiplayer import MultiplayerPlaylistItem

    item = MultiplayerPlaylistItem(room_id=room.id, beatmap_id=beatmap.id)
    other = MultiplayerPlaylistItem(room_id=room.id, beatmap_id=beatmap.id)
    db_session.add_all([item, other])
    await db_session.flush()

    db_session.add_all(
        [
            MultiplayerScore(
                room_id=room.id, playlist_item_id=item.id, user_id=one.id, total_score=500_000
            ),
            MultiplayerScore(
                room_id=room.id, playlist_item_id=item.id, user_id=one.id, total_score=700_000
            ),
            MultiplayerScore(
                room_id=room.id, playlist_item_id=item.id, user_id=two.id, total_score=300_000
            ),
            MultiplayerScore(
                room_id=room.id, playlist_item_id=other.id, user_id=two.id, total_score=999_999
            ),
        ],
    )
    await db_session.flush()

    response = await client.get(f"/internal/matchmaking/items/{item.id}/scores", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"scores": {str(one.id): 700_000, str(two.id): 300_000}}


async def test_item_without_scores_has_none(client: AsyncClient) -> None:
    response = await client.get("/internal/matchmaking/items/424242/scores", headers=HEADERS)

    assert response.json() == {"scores": {}}


# --- recording a match ------------------------------------------------------------------------------------


async def two_rated_players(
    db: AsyncSession, pool: MatchmakingPool, mu_one: float = 1500, mu_two: float = 1600
) -> tuple[User, User]:
    one, two = await make_user(db, "one"), await make_user(db, "two")
    db.add_all(
        [
            MatchmakingUserStats(
                user_id=one.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(mu_one))
            ),
            MatchmakingUserStats(
                user_id=two.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(mu_two))
            ),
        ],
    )
    await db.flush()
    return one, two


def result_body(
    room_id: int,
    one: User,
    two: User,
    *,
    winner: User | None,
    lives: tuple[int, int],
    **overrides: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "room_id": room_id,
        "ranked": True,
        "played": True,
        "winner": winner.id if winner else None,
        "players": [{"user_id": one.id, "life": lives[0]}, {"user_id": two.id, "life": lives[1]}],
    }
    body.update(overrides)
    return body


async def test_ranked_win_moves_ratings_and_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results",
        json=result_body(7, one, two, winner=one, lives=(600_000, 0)),
        headers=HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "rated"
    assert body["ratings"][str(one.id)] > 1500 > 1400, "the winner gains rating"
    assert body["ratings"][str(two.id)] < 1600, "the loser loses rating"

    history = (
        (
            await db_session.execute(
                select(MatchmakingUserEloHistory).order_by(MatchmakingUserEloHistory.user_id)
            )
        )
        .scalars()
        .all()
    )
    assert [(h.user_id, h.opponent_id, h.result, h.elo_before) for h in history] == [
        (one.id, two.id, "win", 1500),
        (two.id, one.id, "loss", 1600),
    ]
    assert [h.elo_after for h in history] == [
        body["ratings"][str(one.id)],
        body["ratings"][str(two.id)],
    ]
    assert all(h.room_id == 7 and h.pool_id == pool.id for h in history)

    winner_stats = await db_session.get(
        MatchmakingUserStats, (one.id, pool.id), populate_existing=True
    )
    assert winner_stats is not None and winner_stats.first_placements == 1
    assert round(parse_elo_data(winner_stats.elo_data).mu) == body["ratings"][str(one.id)]
    assert json.loads(winner_stats.elo_data)["contests"] == 1


async def test_a_draw_is_recorded_as_one_and_helps_the_lower_rated_player(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results",
        json=result_body(8, one, two, winner=None, lives=(400_000, 400_000)),
        headers=HEADERS,
    )

    ratings = response.json()["ratings"]
    assert ratings[str(one.id)] > 1500 and ratings[str(two.id)] < 1600

    results = (await db_session.execute(select(MatchmakingUserEloHistory.result))).scalars().all()
    assert results == ["draw", "draw"]
    assert (await db_session.get(MatchmakingUserStats, (one.id, pool.id))).first_placements == 0


async def test_recording_the_same_match_twice_changes_nothing_the_second_time(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)
    body = result_body(9, one, two, winner=two, lives=(0, 300_000))

    first = (
        await client.post(
            f"/internal/matchmaking/pools/{pool.id}/results", json=body, headers=HEADERS
        )
    ).json()
    second = (
        await client.post(
            f"/internal/matchmaking/pools/{pool.id}/results", json=body, headers=HEADERS
        )
    ).json()

    assert second["result"] == "already_recorded"
    assert second["ratings"] == first["ratings"]
    assert len((await db_session.execute(select(MatchmakingUserEloHistory))).scalars().all()) == 2


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [({"ranked": False}, "unranked"), ({"played": False}, "not_played")],
)
async def test_unranked_and_unplayed_matches_do_not_change_ratings(
    client: AsyncClient,
    db_session: AsyncSession,
    overrides: dict[str, object],
    expected: str,
) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results",
        json=result_body(10, one, two, winner=one, lives=(1, 0), **overrides),
        headers=HEADERS,
    )

    assert response.json() == {"ratings": {}, "result": expected}
    assert (await db_session.execute(select(MatchmakingUserEloHistory))).scalars().all() == []
    assert (
        round(
            parse_elo_data(
                (await db_session.get(MatchmakingUserStats, (one.id, pool.id))).elo_data
            ).mu
        )
        == 1500
    )


async def test_new_players_are_created_when_their_first_match_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, two = (
        await make_user(db_session, "n1", pp=1000),
        await make_user(db_session, "n2", pp=1000),
    )

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results",
        json=result_body(11, one, two, winner=one, lives=(1, 0)),
        headers=HEADERS,
    )

    assert response.json()["result"] == "rated"
    assert await db_session.get(MatchmakingUserStats, (one.id, pool.id)) is not None
    assert await db_session.get(MatchmakingUserStats, (two.id, pool.id)) is not None


async def test_result_validation(client: AsyncClient, db_session: AsyncSession) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)
    stranger = await make_user(db_session, "stranger")

    wrong_winner = result_body(12, one, two, winner=stranger, lives=(1, 0))
    assert (
        await client.post(
            f"/internal/matchmaking/pools/{pool.id}/results", json=wrong_winner, headers=HEADERS
        )
    ).status_code == 422

    assert (
        await client.post(
            "/internal/matchmaking/pools/99/results",
            json=result_body(12, one, two, winner=one, lives=(1, 0)),
            headers=HEADERS,
        )
    ).status_code == 404


# --- the rating maths -------------------------------------------------------------------------------------


def test_the_player_with_more_life_gains_and_the_other_loses() -> None:
    after = rate_match({1: Elo(1500), 2: Elo(1500)}, {1: 800_000, 2: 100_000})

    assert after[1].mu > 1500 > after[2].mu
    assert after[1].sigma < 150 and after[2].sigma < 150, "playing makes the system more certain"


def test_equal_life_pulls_the_ratings_together() -> None:
    after = rate_match({1: Elo(1500), 2: Elo(1600)}, {1: 500_000, 2: 500_000})

    assert after[1].mu > 1500 and after[2].mu < 1600


def test_an_upset_is_worth_more_than_the_expected_result() -> None:
    expected = rate_match({1: Elo(1700), 2: Elo(1300)}, {1: 1, 2: 0})
    upset = rate_match({1: Elo(1700), 2: Elo(1300)}, {1: 0, 2: 1})

    assert upset[2].mu - 1300 > expected[1].mu - 1700


def test_elo_data_round_trips_and_reads_the_reference_servers_spelling() -> None:
    assert parse_elo_data(serialize_elo_data(Elo(1234.5, 99.5))) == Elo(1234.5, 99.5)
    assert parse_elo_data('{"mu": 1400, "sig": 80}') == Elo(1400, 80)
    assert parse_elo_data('{"mu": 1400, "sigma": 80}') == Elo(1400, 80)
    assert parse_elo_data("not json") == Elo()
    assert parse_elo_data(None) == Elo()
    assert parse_elo_data("[]") == Elo()


def test_card_rating_matches_the_reference_servers_formula() -> None:
    assert card_rating(4.18904) == 1277
    assert card_rating(0) == 800


# Produced by OpenSkillSharp 1.1.0 (the library osu-server-spectator rates ranked play with), configured as it
# configures it: mu 1500, sigma 150, beta 0, tau 15, gamma 1, every player a team of one, life as the score.
REFERENCE_RATINGS = [
    (
        "a wins",
        [(1500, 150), (1600, 150)],
        [900_000, 0],
        [(1565.5733, 141.5453), (1534.4267, 141.5453)],
    ),
    (
        "b wins",
        [(1500, 150), (1600, 150)],
        [0, 900_000],
        [(1458.9782, 141.5453), (1641.0218, 141.5453)],
    ),
    (
        "draw",
        [(1500, 150), (1600, 150)],
        [500_000, 500_000],
        [(1512.2757, 141.5453), (1587.7243, 141.5453)],
    ),
    ("upset", [(1700, 150), (1300, 150)], [0, 1], [(1607.5628, 146.3431), (1392.4372, 146.3431)]),
    (
        "expected",
        [(1700, 150), (1300, 150)],
        [1, 0],
        [(1714.1578, 146.3431), (1285.8422, 146.3431)],
    ),
    (
        "unequal sigma",
        [(1500, 100), (1450, 200)],
        [300_000, 700_000],
        [(1474.7154, 98.5560), (1549.4694, 179.7371)],
    ),
]


@pytest.mark.parametrize(
    ("name", "before", "lives", "expected"),
    REFERENCE_RATINGS,
    ids=[r[0] for r in REFERENCE_RATINGS],
)
def test_ratings_match_the_reference_servers_library(
    name: str,
    before: list[tuple[float, float]],
    lives: list[int],
    expected: list[tuple[float, float]],
) -> None:
    after = rate_match({i: Elo(*rating) for i, rating in enumerate(before)}, dict(enumerate(lives)))

    for index, (mu, sigma) in enumerate(expected):
        assert after[index].mu == pytest.approx(mu, abs=1e-3), name
        assert after[index].sigma == pytest.approx(sigma, abs=1e-3), name


# --- contract shared with the Go client (multiplayer/contract/*.json) ---------------------------------

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "multiplayer" / "contract"


def load_contract(name: str) -> dict:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("matchmaking_pool.json", PoolOut),
        ("matchmaking_card.json", PoolBeatmapOut),
        ("matchmaking_user_rating.json", UserRatingOut),
        ("matchmaking_user.json", UserOut),
        ("matchmaking_scores.json", ScoresOut),
        ("matchmaking_ratings.json", RatingsOut),
        ("matchmaking_top_rating.json", TopRatingOut),
        ("matchmaking_result_request.json", MatchResultIn),
        ("matchmaking_result_response.json", MatchResultOut),
    ],
)
def test_contract_samples_match_the_models(name: str, model: type) -> None:
    """The Go tests serve and expect exactly these documents; here the Python models must accept them and
    describe exactly the same set of fields, so a field added on either side fails a test."""
    sample = load_contract(name)

    assert set(sample) == set(model.model_fields), (
        f"{name} and {model.__name__} disagree on the field set"
    )
    model.model_validate(sample)


def test_contract_player_results_match_the_model() -> None:
    sample = load_contract("matchmaking_result_request.json")

    assert all(set(p) == set(PlayerResultIn.model_fields) for p in sample["players"])


@pytest.mark.asyncio
async def test_contract_ranked_play_room_is_accepted_by_the_endpoint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    sample = load_contract("create_ranked_play_room.json")

    assert set(sample) == set(CreateRoomIn.model_fields)
    assert set(sample["state"]) == set(RoomStateIn.model_fields)

    response = await client.post("/internal/multiplayer/rooms", headers=HEADERS, json=sample)

    assert response.status_code == 200, response.text
    assert response.json()["item_ids"] == []


@pytest.mark.asyncio
async def test_contract_result_is_accepted_and_answered_in_the_contract_shape(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    pool = await make_pool(db_session)
    one, two = await two_rated_players(db_session, pool)
    await db_session.commit()

    body = load_contract("matchmaking_result_request.json")
    body["room_id"] = 4242
    body["winner"] = one.id
    body["players"] = [{"user_id": one.id, "life": 600_000}, {"user_id": two.id, "life": 0}]

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results", headers=HEADERS, json=body
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == set(load_contract("matchmaking_result_response.json"))
    assert response.json()["result"] == "rated"


async def test_a_match_of_one_player_is_not_a_match(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, _ = await two_rated_players(db_session, pool)
    body = {
        "room_id": 13,
        "ranked": True,
        "played": True,
        "winner": None,
        "players": [{"user_id": one.id, "life": 5}],
    }

    response = await client.post(
        f"/internal/matchmaking/pools/{pool.id}/results", json=body, headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"ratings": {}, "result": "not_played"}
    assert (await db_session.execute(select(MatchmakingUserEloHistory))).scalars().all() == []


async def test_the_same_room_can_be_recorded_in_another_pool(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A match is recorded once per pool: the guard against recording twice must not span pools."""
    first_pool = await make_pool(db_session, "first")
    second_pool = await make_pool(db_session, "second")
    one, two = await two_rated_players(db_session, first_pool)
    db_session.add_all(
        [
            MatchmakingUserStats(
                user_id=one.id, pool_id=second_pool.id, elo_data=serialize_elo_data(Elo(1500))
            ),
            MatchmakingUserStats(
                user_id=two.id, pool_id=second_pool.id, elo_data=serialize_elo_data(Elo(1600))
            ),
        ],
    )
    await db_session.flush()
    body = result_body(14, one, two, winner=one, lives=(1, 0))

    first = await client.post(
        f"/internal/matchmaking/pools/{first_pool.id}/results", json=body, headers=HEADERS
    )
    second = await client.post(
        f"/internal/matchmaking/pools/{second_pool.id}/results", json=body, headers=HEADERS
    )

    assert first.json()["result"] == "rated"
    assert second.json()["result"] == "rated"


async def test_a_new_players_rating_comes_from_the_pp_of_the_pools_own_mode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)  # osu!
    user = await make_user(db_session, "taikoplayer")
    db_session.add(UserStatistics(user_id=user.id, mode=GameMode.TAIKO, pp=5000))
    await db_session.flush()

    response = await client.get(
        f"/internal/matchmaking/pools/{pool.id}/users/{user.id}", headers=HEADERS
    )

    assert response.json()["mu"] == pytest.approx(initial_rating_from_pp(0).mu)


async def test_the_starting_rating_is_kept_through_later_matches(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    one, two = (
        await make_user(db_session, "s1", pp=2000),
        await make_user(db_session, "s2", pp=2000),
    )
    start = initial_rating_from_pp(2000).mu

    for room_id in (15, 16):
        response = await client.post(
            f"/internal/matchmaking/pools/{pool.id}/results",
            json=result_body(room_id, one, two, winner=one, lives=(1, 0)),
            headers=HEADERS,
        )
        assert response.json()["result"] == "rated"

    stats = await db_session.get(MatchmakingUserStats, (one.id, pool.id), populate_existing=True)
    assert stats is not None
    assert parse_elo_data(stats.elo_data).mu > start + 10, "the rating moved on"
    assert json.loads(stats.elo_data)["initial_mu"] == pytest.approx(start, abs=1e-3), (
        "but the start is remembered"
    )
    assert json.loads(stats.elo_data)["contests"] == 2


async def test_rank_is_none_until_a_ranked_match_was_finished(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    user = await make_user(db_session, "fresh", pp=1000)

    response = await client.get(
        f"/internal/matchmaking/pools/{pool.id}/users/{user.id}", headers=HEADERS
    )

    assert response.json()["rank"] is None


async def test_rank_counts_the_players_rated_higher(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pool = await make_pool(db_session)
    users = [await make_user(db_session, f"p{i}") for i in range(4)]
    for user, mu, contests in zip(users, (1800, 1700, 1700, 1900), (3, 5, 2, 0), strict=True):
        db_session.add(
            MatchmakingUserStats(
                user_id=user.id,
                pool_id=pool.id,
                elo_data=serialize_elo_data(Elo(mu), contests=contests),
            ),
        )
    await db_session.flush()

    async def rank(user: User) -> int | None:
        response = await client.get(
            f"/internal/matchmaking/pools/{pool.id}/users/{user.id}", headers=HEADERS
        )
        return response.json()["rank"]

    # The 1900 player has no finished match, so is not ranked and does not push the others down.
    assert [await rank(u) for u in users] == [1, 2, 2, None]


async def test_rank_is_per_pool(client: AsyncClient, db_session: AsyncSession) -> None:
    first, second = await make_pool(db_session, "a"), await make_pool(db_session, "b")
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    for pool, user, mu in ((first, one, 1500), (first, two, 1600), (second, one, 1500)):
        db_session.add(
            MatchmakingUserStats(
                user_id=user.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(mu), contests=1)
            ),
        )
    await db_session.flush()

    response = await client.get(
        f"/internal/matchmaking/pools/{second.id}/users/{one.id}", headers=HEADERS
    )

    assert response.json()["rank"] == 1


# --- invitation checks -------------------------------------------------------------------------------------


async def invite_check(client: AsyncClient, from_user: User, to_user: User) -> dict:
    response = await client.get(
        f"/internal/multiplayer/invites/{from_user.id}/{to_user.id}", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_plain_invitation_is_allowed(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")

    assert await invite_check(client, one, two) == {
        "restricted": False,
        "blocked": False,
        "blocks_pms": False,
    }


async def test_a_restricted_user_cannot_be_invited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one, two = (
        await make_user(db_session, "one"),
        await make_user(db_session, "two", restricted=True),
    )

    assert (await invite_check(client, one, two))["restricted"] is True
    assert (await invite_check(client, two, one))["restricted"] is False, (
        "it is the invited user who counts"
    )


@pytest.mark.parametrize("blocker", ["inviter", "invited"])
async def test_a_block_in_either_direction_stops_the_invitation(
    client: AsyncClient,
    db_session: AsyncSession,
    blocker: str,
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    owner, target = (one, two) if blocker == "inviter" else (two, one)
    db_session.add(UserRelation(user_id=owner.id, target_id=target.id, foe=True))
    await db_session.flush()

    assert (await invite_check(client, one, two))["blocked"] is True


async def test_a_friendship_alone_is_not_a_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    db_session.add(UserRelation(user_id=one.id, target_id=two.id, friend=True))
    await db_session.flush()

    assert (await invite_check(client, one, two))["blocked"] is False


async def test_players_closed_to_messages_only_take_invitations_from_friends(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    two.allow_pm_from_non_friends = False
    await db_session.flush()

    assert (await invite_check(client, one, two))["blocks_pms"] is True

    db_session.add(
        UserRelation(user_id=two.id, target_id=one.id, friend=True)
    )  # two befriended one
    await db_session.flush()
    assert (await invite_check(client, one, two))["blocks_pms"] is False


async def test_players_open_to_everyone_take_invitations_from_anyone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")

    assert (await invite_check(client, one, two))["blocks_pms"] is False


async def test_the_befriending_has_to_come_from_the_invited_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    two.allow_pm_from_non_friends = False
    db_session.add(
        UserRelation(user_id=one.id, target_id=two.id, friend=True)
    )  # one befriended two, not the reverse
    await db_session.flush()

    assert (await invite_check(client, one, two))["blocks_pms"] is True


async def test_invitation_checks_of_unknown_users(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    one = await make_user(db_session, "one")

    assert (
        await client.get(f"/internal/multiplayer/invites/{one.id}/9999", headers=HEADERS)
    ).status_code == 404
    assert (
        await client.get(f"/internal/multiplayer/invites/9999/{one.id}", headers=HEADERS)
    ).status_code == 404
    assert (await client.get(f"/internal/multiplayer/invites/{one.id}/{one.id}")).status_code == 401


def test_the_invite_contract_matches_the_model() -> None:
    sample = load_contract("invite_check.json")

    assert set(sample) == set(InviteCheckOut.model_fields)
    InviteCheckOut.model_validate(sample)


async def test_losing_the_race_to_create_stats_returns_the_winners_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client asks about several pools at once: a second request must not fail on the unique key."""
    from app.api.internal import matchmaking as api

    pool = await make_pool(db_session)
    user = await make_user(db_session, "racer", pp=1000)
    winner = MatchmakingUserStats(
        user_id=user.id, pool_id=pool.id, elo_data=serialize_elo_data(Elo(mu=1234.0))
    )
    db_session.add(winner)
    await db_session.flush()

    real_get = db_session.get
    lookups = {"stats": 0}

    async def get(entity, ident, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if entity is MatchmakingUserStats:
            lookups["stats"] += 1
            if lookups["stats"] == 1:
                return None  # what the loser saw before the winner committed
        return await real_get(entity, ident, *args, **kwargs)

    monkeypatch.setattr(db_session, "get", get)

    stats = await api._stats(db_session, pool, user.id)

    assert stats is winner
    assert parse_elo_data(stats.elo_data).mu == pytest.approx(1234.0)
