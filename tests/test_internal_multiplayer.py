"""Tests for the internal persistence API used by the Go multiplayer service."""

import json
from pathlib import Path
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal.multiplayer import CreateRoomIn
from app.api.internal.multiplayer import ItemIn
from app.api.internal.multiplayer import RoomStateIn
from app.core.config import get_settings
from app.core.security import create_token_pair
from app.core.security import get_password_hash
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.multiplayer import MultiplayerPlaylistItem
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import MultiplayerScore
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User

KEY = "test-internal-key"
HEADERS = {"X-Internal-Key": KEY}


@pytest.fixture(autouse=True)
def _internal_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "internal_api_key", KEY)
    yield


async def make_beatmap(db: AsyncSession, checksum: str = "abc123", mode: GameMode = GameMode.OSU) -> Beatmap:
    beatmapset = BeatmapSet(user_id=None, artist="a", title="t", creator="c", status=BeatmapStatus.RANKED)
    db.add(beatmapset)
    await db.flush()
    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        version="v",
        mode=mode,
        status=BeatmapStatus.RANKED,
        checksum=checksum,
        difficulty_rating=5.5,
    )
    db.add(beatmap)
    await db.flush()
    return beatmap


def room_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "host_id": None,
        "name": "room",
        "password": None,
        "type": "head_to_head",
        "queue_mode": "host_only",
        "status": "idle",
        "participant_count": 1,
        "max_participants": None,
        "auto_skip": False,
        "auto_start_seconds": 0,
        "current_playlist_item_id": None,
    }
    state.update(overrides)
    return state


def item(beatmap_id: int, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"owner_id": None, "beatmap_id": beatmap_id, "ruleset_id": 0, "playlist_order": 0}
    data.update(overrides)
    return data


async def create_room(
    client: AsyncClient,
    beatmap_id: int,
    host_id: int | None = None,
    **state: object,
) -> dict:
    response = await client.post(
        "/internal/multiplayer/rooms",
        headers=HEADERS,
        json={"state": room_state(host_id=host_id, **state), "items": [item(beatmap_id), item(beatmap_id, playlist_order=1)]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def make_user(db: AsyncSession, name: str) -> User:
    user = User(username=name, email=f"{name}@example.com", password_hash=get_password_hash("password"))
    db.add(user)
    await db.flush()
    return user


# --- authentication ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_the_internal_key(client: AsyncClient) -> None:
    path = "/internal/multiplayer/beatmaps/1"

    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-Internal-Key": "wrong"})).status_code == 401
    # a normal user token is not an internal credential
    token = create_token_pair(1, ["*"]).access_token
    assert (await client.get(path, headers={"Authorization": f"Bearer {token}"})).status_code == 401


@pytest.mark.asyncio
async def test_is_disabled_when_no_key_is_configured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "internal_api_key", "")

    response = await client.get("/internal/multiplayer/beatmaps/1", headers={"X-Internal-Key": ""})

    assert response.status_code == 503


# --- rooms --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_room_returns_database_ids(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    host = await make_user(db_session, "host")
    await db_session.commit()

    created = await create_room(client, beatmap.id, host_id=host.id, name="My Room", password="pw")

    assert created["room_id"] > 0
    assert created["channel_id"] == created["room_id"]
    assert len(created["item_ids"]) == 2 and created["item_ids"][0] != created["item_ids"][1]

    room = (await db_session.execute(select(MultiplayerRoom).where(MultiplayerRoom.id == created["room_id"]))).scalar_one()
    assert (room.name, room.password, room.host_id, room.type) == ("My Room", "pw", host.id, "head_to_head")
    assert room.max_participants == 16 and room.channel_id == room.id and room.ends_at is None

    items = (
        await db_session.execute(
            select(MultiplayerPlaylistItem)
            .where(MultiplayerPlaylistItem.room_id == room.id)
            .order_by(MultiplayerPlaylistItem.playlist_order),
        )
    ).scalars().all()
    assert [i.id for i in items] == created["item_ids"]
    assert [i.playlist_order for i in items] == [0, 1]


@pytest.mark.asyncio
async def test_create_room_validates_the_payload(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    await db_session.commit()
    path = "/internal/multiplayer/rooms"

    no_items = await client.post(path, headers=HEADERS, json={"state": room_state(), "items": []})
    bad_type = await client.post(path, headers=HEADERS, json={"state": room_state(type="nonsense"), "items": [item(beatmap.id)]})
    bad_ruleset = await client.post(path, headers=HEADERS, json={"state": room_state(), "items": [item(beatmap.id, ruleset_id=9)]})

    assert no_items.status_code == bad_type.status_code == bad_ruleset.status_code == 422
    assert (await db_session.execute(select(MultiplayerRoom))).first() is None


@pytest.mark.asyncio
async def test_put_room_state_is_idempotent_and_last_write_wins(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    user = await make_user(db_session, "u")
    await db_session.commit()
    created = await create_room(client, beatmap.id)
    path = f"/internal/multiplayer/rooms/{created['room_id']}"
    current = created["item_ids"][1]

    first = room_state(host_id=user.id, status="playing", participant_count=3, name="renamed", current_playlist_item_id=current,
                       queue_mode="all_players_round_robin", auto_skip=True, auto_start_seconds=15, max_participants=8)
    for _ in range(2):  # retries must be harmless
        assert (await client.put(path, headers=HEADERS, json=first)).status_code == 204
    assert (await client.put(path, headers=HEADERS, json=room_state(participant_count=2))).status_code == 204

    room = (await db_session.execute(select(MultiplayerRoom).where(MultiplayerRoom.id == created["room_id"]))).scalar_one()
    await db_session.refresh(room)
    assert (room.status, room.participant_count, room.name) == ("idle", 2, "room")
    assert room.host_id is None and room.current_playlist_item_id is None


@pytest.mark.asyncio
async def test_closing_a_room_sets_the_end_time_once(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    await db_session.commit()
    created = await create_room(client, beatmap.id)
    path = f"/internal/multiplayer/rooms/{created['room_id']}"

    await client.put(path, headers=HEADERS, json=room_state(status="closed", participant_count=0))
    room = (await db_session.execute(select(MultiplayerRoom).where(MultiplayerRoom.id == created["room_id"]))).scalar_one()
    await db_session.refresh(room)
    first_end = room.ends_at
    await client.put(path, headers=HEADERS, json=room_state(status="closed", participant_count=0))
    await db_session.refresh(room)

    assert first_end is not None and room.ends_at == first_end and room.status == "closed"


@pytest.mark.asyncio
async def test_unknown_room_is_404(client: AsyncClient) -> None:
    assert (await client.put("/internal/multiplayer/rooms/999", headers=HEADERS, json=room_state())).status_code == 404
    assert (await client.post("/internal/multiplayer/rooms/999/items", headers=HEADERS, json=item(1))).status_code == 404


# --- items --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_lifecycle(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    await db_session.commit()
    created = await create_room(client, beatmap.id)
    base = f"/internal/multiplayer/rooms/{created['room_id']}/items"

    added = await client.post(
        base,
        headers=HEADERS,
        json=item(beatmap.id, playlist_order=65535, required_mods=[{"acronym": "DT", "settings": {"speed_change": 1.3}}]),
    )
    assert added.status_code == 200
    item_id = added.json()["id"]

    updated = item(beatmap.id, playlist_order=2, expired=True, allowed_mods=[{"acronym": "HD", "settings": {}}],
                   played_at="2026-01-02T03:04:05Z")
    for _ in range(2):
        assert (await client.put(f"{base}/{item_id}", headers=HEADERS, json=updated)).status_code == 204

    row = (await db_session.execute(select(MultiplayerPlaylistItem).where(MultiplayerPlaylistItem.id == item_id))).scalar_one()
    await db_session.refresh(row)
    assert (row.playlist_order, row.expired) == (2, True)
    assert json.loads(row.allowed_mods) == [{"acronym": "HD", "settings": {}}]
    assert json.loads(row.required_mods) == []  # replaced, not merged
    assert row.played_at is not None

    assert (await client.delete(f"{base}/{item_id}", headers=HEADERS)).status_code == 204
    assert (await client.delete(f"{base}/{item_id}", headers=HEADERS)).status_code == 204  # idempotent
    assert (await client.put(f"{base}/{item_id}", headers=HEADERS, json=updated)).status_code == 404


@pytest.mark.asyncio
async def test_items_cannot_be_touched_through_another_room(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    await db_session.commit()
    first = await create_room(client, beatmap.id)
    second = await create_room(client, beatmap.id)
    foreign = first["item_ids"][0]

    put = await client.put(f"/internal/multiplayer/rooms/{second['room_id']}/items/{foreign}", headers=HEADERS, json=item(beatmap.id))
    delete = await client.delete(f"/internal/multiplayer/rooms/{second['room_id']}/items/{foreign}", headers=HEADERS)

    assert put.status_code == 404 and delete.status_code == 204
    still_there = await db_session.execute(select(MultiplayerPlaylistItem).where(MultiplayerPlaylistItem.id == foreign))
    assert still_there.scalar_one_or_none() is not None


# --- stale rooms ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_stale_only_closes_realtime_rooms(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    await db_session.commit()
    live = await create_room(client, beatmap.id, participant_count=4)
    team = await create_room(client, beatmap.id, type="team_versus")
    already_closed = await create_room(client, beatmap.id, status="closed")
    playlist = await create_room(client, beatmap.id, type="playlists")
    daily = await create_room(client, beatmap.id)
    daily_row = (await db_session.execute(select(MultiplayerRoom).where(MultiplayerRoom.id == daily["room_id"]))).scalar_one()
    daily_row.category = "daily_challenge"
    await db_session.commit()

    result = await client.post("/internal/multiplayer/rooms/close-stale", headers=HEADERS)

    assert result.json() == {"closed": 2}
    statuses = {}
    for name, created in {"live": live, "team": team, "closed": already_closed, "playlist": playlist, "daily": daily}.items():
        row = (await db_session.execute(select(MultiplayerRoom).where(MultiplayerRoom.id == created["room_id"]))).scalar_one()
        await db_session.refresh(row)
        statuses[name] = (row.status, row.participant_count)
    assert statuses["live"] == ("closed", 0) and statuses["team"] == ("closed", 0)
    assert statuses["playlist"][0] == "idle" and statuses["daily"][0] == "idle"


# --- beatmaps -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beatmap_lookup(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session, checksum="deadbeef", mode=GameMode.MANIA)
    await db_session.commit()

    found = await client.get(f"/internal/multiplayer/beatmaps/{beatmap.id}", headers=HEADERS)

    assert found.status_code == 200
    assert found.json() == {"id": beatmap.id, "checksum": "deadbeef", "mode": 3, "star_rating": 5.5}


# --- the reason this API exists: scores submitted in a room link up --------------------------------------


@pytest.mark.asyncio
async def test_a_score_set_in_a_go_room_links_to_its_playlist_item(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    beatmap = await make_beatmap(db_session, checksum="abc123")
    player = await make_user(db_session, "player")
    await db_session.commit()
    created = await create_room(client, beatmap.id, host_id=player.id)
    item_id = created["item_ids"][0]

    headers = {"Authorization": f"Bearer {create_token_pair(player.id, ['*']).access_token}"}
    token = await client.post(
        f"/api/v2/beatmaps/{beatmap.id}/solo/scores",
        data={"version_hash": "x", "beatmap_hash": "abc123", "ruleset_id": 0, "playlistItemId": item_id},
        headers=headers,
    )
    assert token.status_code == 200, token.text

    submitted = await client.put(
        f"/api/v2/beatmaps/{beatmap.id}/solo/scores/{token.json()['id']}",
        headers=headers,
        json={
            "accuracy": 98.0, "max_combo": 100, "mods": [], "passed": True, "rank": "A",
            "statistics": {"great": 100}, "maximum_statistics": {"great": 100}, "total_score": 900000,
            "total_score_without_mods": 900000, "ruleset_id": 0, "pp": None, "pauses": [],
        },
    )
    assert submitted.status_code == 200, submitted.text

    linked = (await db_session.execute(select(MultiplayerScore).where(MultiplayerScore.playlist_item_id == item_id))).scalar_one()
    assert linked.room_id == created["room_id"] and linked.user_id == player.id and linked.total_score == 900000

    # An item the Go service never persisted cannot be used for a token at all.
    missing = await client.post(
        f"/api/v2/beatmaps/{beatmap.id}/solo/scores",
        data={"version_hash": "x", "beatmap_hash": "abc123", "ruleset_id": 0, "playlistItemId": 424242},
        headers=headers,
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_a_score_is_played_and_submitted_through_the_room_routes_the_client_uses(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    beatmap = await make_beatmap(db_session, checksum="abc123")
    player = await make_user(db_session, "roomplayer")
    await db_session.commit()
    created = await create_room(client, beatmap.id, host_id=player.id)
    room_id, item_id = created["room_id"], created["item_ids"][0]
    headers = {"Authorization": f"Bearer {create_token_pair(player.id, ['*']).access_token}"}
    base = f"/api/v2/rooms/{room_id}/playlist/{item_id}/scores"

    token = await client.post(
        base,
        data={"version_hash": "x", "beatmap_id": beatmap.id, "beatmap_hash": "abc123", "ruleset_id": 0},
        headers=headers,
    )
    assert token.status_code == 200, token.text

    submitted = await client.put(
        f"{base}/{token.json()['id']}",
        headers=headers,
        json={
            "accuracy": 98.0, "max_combo": 100, "mods": [], "passed": True, "rank": "A",
            "statistics": {"great": 100}, "maximum_statistics": {"great": 100}, "total_score": 900000,
            "total_score_without_mods": 900000, "ruleset_id": 0, "pp": None, "pauses": [],
        },
    )
    assert submitted.status_code == 200, submitted.text
    linked = (await db_session.execute(select(MultiplayerScore).where(MultiplayerScore.playlist_item_id == item_id))).scalar_one()
    assert linked.room_id == room_id and linked.user_id == player.id

    listed = await client.get(base, headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    assert body["scores"][0]["total_score"] == 900000 and body["scores"][0]["user"]["id"] == player.id
    assert body["scores"][0]["position"] == 1 and body["scores"][0]["rank"] == "A"
    assert body["scores"][0]["statistics"] == {"great": 100}
    assert body["user_score"]["total_score"] == 900000
    # Somebody who has not played sees the scores but has none of their own.
    spectator = await make_user(db_session, "roomwatcher")
    await db_session.commit()
    other = {"Authorization": f"Bearer {create_token_pair(spectator.id, ['*']).access_token}"}
    assert (await client.get(base, headers=other)).json()["user_score"] is None
    # An item that belongs to another room is not found.
    wrong_room = await client.post(
        f"/api/v2/rooms/{room_id + 1000}/playlist/{item_id}/scores",
        data={"version_hash": "x", "beatmap_id": beatmap.id, "beatmap_hash": "abc123", "ruleset_id": 0},
        headers=headers,
    )
    assert wrong_room.status_code == 404


@pytest.mark.asyncio
async def test_the_realtime_lobby_lists_rooms_the_multiplayer_service_made(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The lobby asks for category=realtime; rooms made by the Go service have no stored category, so a plain
    category filter hid every one of them from other players."""
    beatmap = await make_beatmap(db_session)
    viewer = await make_user(db_session, "lobbyviewer")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token_pair(viewer.id, ['*']).access_token}"}
    open_room = await create_room(client, beatmap.id, type="head_to_head")
    team_room = await create_room(client, beatmap.id, type="team_versus")
    ranked = await create_room(client, beatmap.id, type="ranked_play")
    closed = await create_room(client, beatmap.id, type="head_to_head", status="closed")

    listed = await client.get("/api/v2/rooms?category=realtime", headers=headers)

    assert listed.status_code == 200, listed.text
    ids = {room["id"] for room in listed.json()}
    assert ids == {open_room["room_id"], team_room["room_id"]}, "matchmaking and closed rooms are not listed"
    assert closed["room_id"] not in ids and ranked["room_id"] not in ids
    # The client parses category into a non-nullable enum, so null would break the whole list.
    assert all(room["category"] == "normal" for room in listed.json())


@pytest.mark.asyncio
async def test_a_beatmap_leaderboard_lists_each_player_once_with_their_best_score(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    beatmap = await make_beatmap(db_session)
    ana = await make_user(db_session, "leaderana")
    ben = await make_user(db_session, "leaderben")
    data = json.dumps({"mods": [], "statistics": {"great": 10}, "maximum_statistics": {"great": 10}})

    def score(user: User, total: int, passed: bool = True) -> Score:
        return Score(
            user_id=user.id, beatmap_id=beatmap.id, ruleset_id=int(GameMode.OSU), data=data, total_score=total,
            accuracy=99.0, pp=1.0, max_combo=10, rank="A", passed=passed, ranked=True,
        )

    db_session.add_all([
        score(ana, 900_000), score(ana, 1_200_000), score(ana, 1_000_000),
        score(ben, 1_100_000), score(ben, 500_000),
        score(ben, 9_999_999, passed=False),
    ])
    await db_session.commit()

    response = await client.get(f"/api/v2/beatmaps/{beatmap.id}/scores")

    assert response.status_code == 200, response.text
    board = [(row["user_id"], row["total_score"]) for row in response.json()["scores"]]
    assert board == [(ana.id, 1_200_000), (ben.id, 1_100_000)], "one row per player, best first"


@pytest.mark.asyncio
async def test_beatmap_responses_carry_cover_urls_for_the_client(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    player = await make_user(db_session, "coverviewer")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token_pair(player.id, ['*']).access_token}"}

    beatmap.total_length, beatmap.hit_length, beatmap.bpm = 200, 170, 140.0
    await db_session.commit()

    found = await client.get(f"/api/v2/beatmaps/?ids[]={beatmap.id}", headers=headers)

    # The card's "Length" is the drain time (hit_length); without it the client shows 00:00.
    shown = found.json()["beatmaps"][0]
    assert (shown["total_length"], shown["hit_length"], shown["bpm"]) == (200, 170, 140.0)
    covers = shown["beatmapset"]["covers"]
    assert covers["card"] == f"https://assets.ppy.sh/beatmaps/{beatmap.beatmapset_id}/covers/card.jpg"
    assert covers["list@2x"].endswith("/list@2x.jpg") and covers["cover"].endswith("/cover.jpg")


# --- contract shared with the Go client (multiplayer/contract/*.json) -------------------------------------------


CONTRACT_DIR = Path(__file__).resolve().parent.parent / "multiplayer" / "contract"


def load_contract(name: str) -> dict:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("room_state.json", RoomStateIn),
        ("room_state_unset.json", RoomStateIn),
        ("item_played.json", ItemIn),
    ],
)
def test_contract_samples_match_the_models(name: str, model: type) -> None:
    """The Go tests assert they produce exactly these documents; here the Python models must accept them
    and describe exactly the same set of fields, so a field added on either side fails a test."""
    sample = load_contract(name)

    assert set(sample) == set(model.model_fields), f"{name} and {model.__name__} disagree on the field set"
    model.model_validate(sample)


def test_create_room_contract_matches_the_models() -> None:
    sample = load_contract("create_room.json")

    assert set(sample) == set(CreateRoomIn.model_fields)
    parsed = CreateRoomIn.model_validate(sample)
    assert set(sample["state"]) == set(RoomStateIn.model_fields)
    assert all(set(item) == set(ItemIn.model_fields) for item in sample["items"])
    assert parsed.items[0].required_mods == [{"acronym": "DT", "settings": {"speed_change": 1.3}}]


@pytest.mark.asyncio
async def test_contract_create_room_is_accepted_by_the_endpoint(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session)
    host = await make_user(db_session, "host")
    await db_session.commit()

    sample = load_contract("create_room.json")
    sample["state"]["host_id"] = host.id
    sample["items"][0]["owner_id"] = host.id
    sample["items"][0]["beatmap_id"] = beatmap.id

    response = await client.post("/internal/multiplayer/rooms", headers=HEADERS, json=sample)

    assert response.status_code == 200, response.text
    assert len(response.json()["item_ids"]) == 1
