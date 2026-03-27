"""Multiplayer hub websocket behavior tests."""

import json
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.hubs.multiplayer as multiplayer_hub
from app.api.hubs.base import SIGNALR_RECORD_SEPARATOR
from app.core.security import create_token_pair
from app.protocol.enums import MultiplayerRoomState


@dataclass
class FakePlaylistItem:
    id: int
    owner_id: int | None = None
    beatmap_id: int = 0
    ruleset_id: int = 0
    required_mods: str = "[]"
    allowed_mods: str = "[]"
    expired: bool = False
    playlist_order: int = 0
    played_at: Any = None


@dataclass
class FakeRoom:
    id: int
    host_id: int | None
    name: str
    password: str | None
    type: str = "head_to_head"
    status: str = "idle"
    queue_mode: str = "host_only"
    max_participants: int = 16
    participant_count: int = 0
    auto_start_duration: int = 0
    auto_skip: bool = False
    current_playlist_item_id: int | None = None
    playlist_items: list[FakePlaylistItem] | None = None
    channel_id: int | None = None

    def __post_init__(self) -> None:
        if self.playlist_items is None:
            self.playlist_items = []


@dataclass
class FakeRoomStore:
    rooms: dict[int, FakeRoom]


@pytest.fixture
def multiplayer_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, FakeRoomStore], None, None]:
    """Provide test client with isolated multiplayer hub state."""
    fake_store = FakeRoomStore(
        rooms={
            10: FakeRoom(
                id=10,
                host_id=42,
                name="Test room",
                password=None,
            ),
        },
    )

    async def fake_load_room_model(room_id: int) -> FakeRoom | None:
        return fake_store.rooms.get(room_id)

    async def fake_join_room_in_db(
        user_id: int,
        room_id: int,
        password: str | None,
    ) -> tuple[FakeRoom | None, str | None]:
        room = fake_store.rooms.get(room_id)
        if room is None:
            return None, "Room not found"

        if room.status == "closed":
            return None, "Room is closed"

        if room.password and room.password != (password or ""):
            return None, "Invalid password"

        if room.participant_count >= room.max_participants:
            return None, "Room is full"

        if room.host_id is None:
            room.host_id = user_id

        room.participant_count += 1
        return room, None

    async def fake_leave_room_in_db(room_id: int) -> FakeRoom | None:
        room = fake_store.rooms.get(room_id)
        if room is None:
            return None

        room.participant_count = max(0, room.participant_count - 1)
        if room.participant_count == 0:
            room.status = "closed"

        return room

    async def fake_set_room_status_in_db(
        room_id: int,
        status: str,
        host_user_id: int,
    ) -> tuple[FakeRoom | None, str | None]:
        room = fake_store.rooms.get(room_id)
        if room is None:
            return None, "Room not found"

        if room.host_id != host_user_id:
            return None, "Only host can perform this action"

        room.status = status
        return room, None

    async def fake_update_room_settings_in_db(
        room_id: int,
        settings,
        host_user_id: int,
    ) -> tuple[FakeRoom | None, str | None]:
        room = fake_store.rooms.get(room_id)
        if room is None:
            return None, "Room not found"

        if room.host_id != host_user_id:
            return None, "Only host can update settings"

        room.name = settings.name or room.name
        room.password = settings.password or None
        room.type = room.type
        room.queue_mode = room.queue_mode
        room.auto_start_duration = int(settings.auto_start_duration.total_seconds())
        room.auto_skip = settings.auto_skip
        if settings.playlist_item_id:
            room.current_playlist_item_id = settings.playlist_item_id

        return room, None

    async def fake_create_room_in_db(
        user_id: int,
        requested_room,
    ) -> tuple[FakeRoom | None, str | None]:
        next_room_id = max(fake_store.rooms.keys(), default=0) + 1
        next_playlist_id = 1000

        playlist_items: list[FakePlaylistItem] = []
        for order, item in enumerate(sorted(requested_room.playlist, key=lambda entry: entry.playlist_order)):
            playlist_items.append(
                FakePlaylistItem(
                    id=next_playlist_id,
                    owner_id=item.owner_id or user_id,
                    beatmap_id=item.beatmap_id,
                    ruleset_id=item.ruleset_id,
                    required_mods="[]",
                    allowed_mods="[]",
                    expired=item.expired,
                    playlist_order=order,
                    played_at=item.played_at,
                ),
            )
            next_playlist_id += 1

        room = FakeRoom(
            id=next_room_id,
            host_id=user_id,
            name=requested_room.settings.name or f"User {user_id}'s room",
            password=requested_room.settings.password or None,
            type="head_to_head",
            status="idle",
            queue_mode="host_only",
            participant_count=1,
            current_playlist_item_id=playlist_items[0].id if playlist_items else None,
            playlist_items=playlist_items,
            channel_id=next_room_id,
        )
        fake_store.rooms[next_room_id] = room
        return room, None

    monkeypatch.setattr(multiplayer_hub, "_load_room_model", fake_load_room_model)
    monkeypatch.setattr(multiplayer_hub, "_join_room_in_db", fake_join_room_in_db)
    monkeypatch.setattr(multiplayer_hub, "_leave_room_in_db", fake_leave_room_in_db)
    monkeypatch.setattr(multiplayer_hub, "_set_room_status_in_db", fake_set_room_status_in_db)
    monkeypatch.setattr(multiplayer_hub, "_update_room_settings_in_db", fake_update_room_settings_in_db)
    monkeypatch.setattr(multiplayer_hub, "_create_room_in_db", fake_create_room_in_db)

    multiplayer_hub.connections.clear()
    multiplayer_hub.connections_by_user.clear()
    multiplayer_hub.room_connections.clear()
    multiplayer_hub.room_user_states.clear()

    app = FastAPI()
    app.include_router(multiplayer_hub.router)

    with TestClient(app) as client:
        yield client, fake_store

    multiplayer_hub.connections.clear()
    multiplayer_hub.connections_by_user.clear()
    multiplayer_hub.room_connections.clear()
    multiplayer_hub.room_user_states.clear()


def _token_headers(user_id: int) -> dict[str, str]:
    token = create_token_pair(user_id=user_id, scopes=["*"]).access_token
    return {"Authorization": f"Bearer {token}"}


def _signalr_handshake(websocket) -> None:
    websocket.send_text('{"protocol":"json","version":1}' + SIGNALR_RECORD_SEPARATOR)
    assert websocket.receive_text() == "{}" + SIGNALR_RECORD_SEPARATOR


def _send_invocation(websocket, target: str, arguments: list, invocation_id: str | None = None) -> None:
    payload = {
        "type": 1,
        "target": target,
        "arguments": arguments,
    }
    if invocation_id is not None:
        payload["invocationId"] = invocation_id

    websocket.send_text(json.dumps(payload) + SIGNALR_RECORD_SEPARATOR)


def _read_message(websocket) -> dict[str, Any]:
    raw = websocket.receive_text()
    messages = [part for part in raw.split(SIGNALR_RECORD_SEPARATOR) if part]
    assert messages
    return json.loads(messages[0])


def _read_until(websocket, predicate, max_messages: int = 10) -> dict[str, Any]:
    for _ in range(max_messages):
        message = _read_message(websocket)
        if predicate(message):
            return message
    raise AssertionError("Expected websocket message was not received")


def _wait_for(predicate, timeout_seconds: float = 0.5) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_multiplayer_websocket_rejects_unauthorized(
    multiplayer_test_client: tuple[TestClient, FakeRoomStore],
) -> None:
    """Multiplayer websocket should reject clients without a token."""
    client, _ = multiplayer_test_client

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/multiplayer"):
            pass

    assert exc_info.value.code == 4401


def test_multiplayer_join_ready_start_flow(
    multiplayer_test_client: tuple[TestClient, FakeRoomStore],
) -> None:
    """Host can join room, ready up, and start match with realtime events."""
    client, fake_store = multiplayer_test_client

    with client.websocket_connect("/multiplayer", headers=_token_headers(42)) as ws:
        _signalr_handshake(ws)

        _send_invocation(ws, "JoinRoom", [10], invocation_id="1")
        join_completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "1",
        )
        assert join_completion["result"][0] == 10
        assert fake_store.rooms[10].participant_count == 1

        _send_invocation(ws, "ReadyUp", [], invocation_id="2")
        state_change = _read_until(ws, lambda msg: msg.get("target") == "UserStateChanged")
        assert state_change["arguments"] == [42, 1]
        _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "2",
        )

        _send_invocation(ws, "StartMatch", [], invocation_id="3")
        match_started = _read_until(ws, lambda msg: msg.get("target") == "MatchStarted")
        assert match_started["arguments"] == [10]

        start_completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "3",
        )
        assert start_completion["result"][0] == 10
        assert start_completion["result"][1] == MultiplayerRoomState.PLAYING
        assert fake_store.rooms[10].status == "playing"


def test_multiplayer_create_room_returns_protocol_room(
    multiplayer_test_client: tuple[TestClient, FakeRoomStore],
) -> None:
    """CreateRoom should return a created MultiplayerRoom payload with valid IDs and host/user fields."""
    client, _ = multiplayer_test_client

    room_payload = [
        0,
        0,
        ["chisadin_2's awesome room", 0, "", 1, 0, 0, False],
        [],
        None,
        None,
        [[0, 0, 5542525, "a7882ec3260c99c93d243bbdb3defd3e", 0, [], [], False, 0, None, 4.489249268165526, True]],
        [],
        0,
    ]

    with client.websocket_connect("/multiplayer", headers=_token_headers(42)) as ws:
        _signalr_handshake(ws)
        _send_invocation(ws, "CreateRoom", [room_payload], invocation_id="1")

        completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "1",
        )
        room = completion["result"]

        assert room[0] > 0
        assert room[8] > 0
        assert room[4] is not None
        assert room[4][0] == 42
        assert len(room[3]) == 1
        assert room[3][0][0] == 42
        assert len(room[6]) == 1
        assert room[6][0][0] > 0
        assert room[2][1] == room[6][0][0]


def test_multiplayer_non_host_cannot_start_match(
    multiplayer_test_client: tuple[TestClient, FakeRoomStore],
) -> None:
    """Only host user can start a match."""
    client, fake_store = multiplayer_test_client
    fake_store.rooms[10].host_id = 7

    with client.websocket_connect("/multiplayer", headers=_token_headers(42)) as ws:
        _signalr_handshake(ws)

        _send_invocation(ws, "JoinRoom", [10], invocation_id="1")
        _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "1",
        )

        _send_invocation(ws, "StartMatch", [], invocation_id="2")
        completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "2",
        )

        assert completion["result"]["success"] is False
        assert "host" in completion["result"]["error"].lower()
        assert fake_store.rooms[10].status == "idle"


def test_multiplayer_disconnect_cleans_up_room_participant(
    multiplayer_test_client: tuple[TestClient, FakeRoomStore],
) -> None:
    """Disconnecting the last connection should leave room and decrement participant count."""
    client, fake_store = multiplayer_test_client

    with client.websocket_connect("/multiplayer", headers=_token_headers(42)) as ws:
        _signalr_handshake(ws)
        _send_invocation(ws, "JoinRoom", [10], invocation_id="1")
        _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "1",
        )
        assert fake_store.rooms[10].participant_count == 1

    assert _wait_for(lambda: fake_store.rooms[10].participant_count == 0)
    assert fake_store.rooms[10].status == "closed"
