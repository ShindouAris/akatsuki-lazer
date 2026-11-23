"""Ranked play rooms through the internal multiplayer API: they start without a playlist item."""

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.multiplayer import MultiplayerRoom

KEY = "test-internal-key"
HEADERS = {"X-Internal-Key": KEY}


@pytest.fixture(autouse=True)
def _internal_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "internal_api_key", KEY)
    yield


def state(room_type: str) -> dict[str, object]:
    return {
        "host_id": None,
        "name": "Ranked play: a vs b",
        "password": "secret",
        "type": room_type,
        "queue_mode": "host_only",
        "status": "idle",
        "participant_count": 0,
        "max_participants": None,
        "auto_skip": False,
        "auto_start_seconds": 0,
        "current_playlist_item_id": None,
    }


async def test_a_ranked_play_room_can_be_created_without_items(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/internal/multiplayer/rooms",
        json={"state": state("ranked_play"), "items": []},
        headers=HEADERS,
    )

    assert response.status_code == 200
    created = response.json()
    assert created["item_ids"] == []
    assert created["channel_id"] == created["room_id"]

    room = await db_session.get(MultiplayerRoom, created["room_id"])
    assert room is not None and room.type == "ranked_play" and room.current_playlist_item_id is None


async def test_the_items_field_may_be_left_out_for_ranked_play(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/multiplayer/rooms", json={"state": state("ranked_play")}, headers=HEADERS
    )

    assert response.status_code == 200


@pytest.mark.parametrize("room_type", ["head_to_head", "team_versus", "playlists"])
async def test_other_rooms_still_need_an_item(client: AsyncClient, room_type: str) -> None:
    response = await client.post(
        "/internal/multiplayer/rooms",
        json={"state": state(room_type), "items": []},
        headers=HEADERS,
    )

    assert response.status_code == 422


async def test_items_can_be_added_to_a_ranked_play_room(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    created = (
        await client.post(
            "/internal/multiplayer/rooms",
            json={"state": state("ranked_play"), "items": []},
            headers=HEADERS,
        )
    ).json()

    from app.models.beatmap import Beatmap
    from app.models.beatmap import BeatmapSet
    from app.models.beatmap import BeatmapStatus
    from app.models.user import GameMode

    beatmapset = BeatmapSet(
        user_id=None, artist="a", title="t", creator="c", status=BeatmapStatus.RANKED
    )
    db_session.add(beatmapset)
    await db_session.flush()
    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        version="v",
        mode=GameMode.OSU,
        status=BeatmapStatus.RANKED,
        checksum="c",
    )
    db_session.add(beatmap)
    await db_session.flush()

    item = {
        "owner_id": None,
        "beatmap_id": beatmap.id,
        "ruleset_id": 0,
        "required_mods": [{"acronym": "RX", "settings": {}}],
        "allowed_mods": [],
    }
    response = await client.post(
        f"/internal/multiplayer/rooms/{created['room_id']}/items", json=item, headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json()["id"] > 0


async def test_stale_ranked_play_rooms_are_closed_at_startup(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    created = (
        await client.post(
            "/internal/multiplayer/rooms",
            json={"state": state("ranked_play"), "items": []},
            headers=HEADERS,
        )
    ).json()

    response = await client.post("/internal/multiplayer/rooms/close-stale", headers=HEADERS)

    assert response.json()["closed"] >= 1
    room = (
        await db_session.execute(
            select(MultiplayerRoom)
            .where(MultiplayerRoom.id == created["room_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert room.status == "closed"
