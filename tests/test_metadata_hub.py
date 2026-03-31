"""Metadata hub websocket behavior tests."""

import json
import time
from collections.abc import Generator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.hubs.metadata as metadata_hub
from app.api.hubs.base import SIGNALR_RECORD_SEPARATOR
from app.core.security import create_token_pair
from app.protocol.enums import UserStatus
from app.protocol.models import UserActivity
from app.services.hub_state import StoredPresence


class FakeHubStateService:
    """In-memory hub state implementation for websocket tests."""

    def __init__(self) -> None:
        self.presence: dict[int, StoredPresence] = {}
        self.presence_watchers: set[int] = set()
        self.friend_map: dict[int, set[int]] = {}
        self._beatmap_updates: list[tuple[int, list[int]]] = []
        self._queue_id = 0

    async def set_presence(
        self,
        user_id: int,
        activity: UserActivity | None,
        status: UserStatus,
    ) -> None:
        self.presence[user_id] = StoredPresence(user_id=user_id, activity=activity, status=status)

    async def remove_presence(self, user_id: int) -> None:
        self.presence.pop(user_id, None)

    async def get_presence(self, user_id: int) -> StoredPresence | None:
        return self.presence.get(user_id)

    async def get_all_online_users(self) -> list[StoredPresence]:
        return list(self.presence.values())

    async def refresh_presence_ttl(self, user_id: int) -> bool:
        return user_id in self.presence

    async def add_presence_watcher(self, user_id: int) -> None:
        self.presence_watchers.add(user_id)

    async def remove_presence_watcher(self, user_id: int) -> None:
        self.presence_watchers.discard(user_id)

    async def get_presence_watchers(self) -> set[int]:
        return set(self.presence_watchers)

    async def append_beatmap_updates(self, beatmap_set_ids: list[int]) -> int:
        self._queue_id += 1
        self._beatmap_updates.append((self._queue_id, list(beatmap_set_ids)))
        return self._queue_id

    async def get_beatmap_updates_since(self, queue_id: int, limit: int = 500) -> tuple[list[int], int]:
        del limit
        parsed_queue_id = max(0, int(queue_id))
        beatmap_set_ids: list[int] = []
        latest_queue_id = parsed_queue_id

        for update_queue_id, update_ids in self._beatmap_updates:
            if update_queue_id <= parsed_queue_id:
                continue

            latest_queue_id = max(latest_queue_id, update_queue_id)
            for beatmap_set_id in update_ids:
                if beatmap_set_id not in beatmap_set_ids:
                    beatmap_set_ids.append(beatmap_set_id)

        return beatmap_set_ids, latest_queue_id


@pytest.fixture
def metadata_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, FakeHubStateService], None, None]:
    """Provide test client with isolated metadata hub state."""
    fake_hub_state = FakeHubStateService()

    async def get_fake_hub_state_service() -> FakeHubStateService:
        return fake_hub_state

    monkeypatch.setattr(
        metadata_hub,
        "get_hub_state_service",
        get_fake_hub_state_service,
    )

    async def fake_get_friend_ids_for_user(user_id: int) -> set[int]:
        return set(fake_hub_state.friend_map.get(user_id, set()))

    monkeypatch.setattr(
        metadata_hub,
        "_get_friend_ids_for_user",
        fake_get_friend_ids_for_user,
    )

    async def fake_get_active_daily_challenge_info():
        return None

    monkeypatch.setattr(
        metadata_hub,
        "_get_active_daily_challenge_info",
        fake_get_active_daily_challenge_info,
    )

    async def fake_build_playlist_stats_for_room(room_id: int):
        del room_id
        return []

    monkeypatch.setattr(
        metadata_hub,
        "_build_playlist_stats_for_room",
        fake_build_playlist_stats_for_room,
    )

    metadata_hub.connections.clear()
    metadata_hub.connections_by_user.clear()
    metadata_hub.presence_watching_connections.clear()
    metadata_hub.friend_presence_watching_connections.clear()
    metadata_hub.room_watching_connections.clear()

    app = FastAPI()
    app.include_router(metadata_hub.router)

    with TestClient(app) as client:
        yield client, fake_hub_state

    metadata_hub.connections.clear()
    metadata_hub.connections_by_user.clear()
    metadata_hub.presence_watching_connections.clear()
    metadata_hub.friend_presence_watching_connections.clear()
    metadata_hub.room_watching_connections.clear()


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


def _read_invocation(websocket) -> dict[str, Any]:
    raw = websocket.receive_text()
    messages = [part for part in raw.split(SIGNALR_RECORD_SEPARATOR) if part]
    assert messages
    return json.loads(messages[0])


def _read_until(websocket, predicate, max_messages: int = 10) -> dict[str, Any]:
    for _ in range(max_messages):
        invocation = _read_invocation(websocket)
        if predicate(invocation):
            return invocation
    raise AssertionError("Expected websocket message was not received")


def _wait_for(predicate, timeout_seconds: float = 0.5) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_metadata_websocket_rejects_unauthorized(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Metadata websocket should reject clients without a token."""
    client, _ = metadata_test_client

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/metadata"):
            pass

    assert exc_info.value.code == 4401


def test_metadata_presence_not_removed_until_last_connection_closes(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Presence should stay online while at least one connection remains."""
    client, fake_hub_state = metadata_test_client
    headers = _token_headers(user_id=42)

    with client.websocket_connect("/metadata", headers=headers) as ws_primary:
        _signalr_handshake(ws_primary)

        with client.websocket_connect("/metadata", headers=headers) as ws_secondary:
            _signalr_handshake(ws_secondary)
            assert 42 in fake_hub_state.presence

        # Primary connection is still active, so presence must remain online.
        assert 42 in fake_hub_state.presence

    assert 42 not in fake_hub_state.presence


def test_metadata_watcher_state_is_reference_counted_per_connection(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Ending watch on one connection must not unsubscribe the user's other watcher connection."""
    client, fake_hub_state = metadata_test_client
    watcher_headers = _token_headers(user_id=100)
    target_headers = _token_headers(user_id=200)

    with client.websocket_connect("/metadata", headers=watcher_headers) as ws_watch_1:
        _signalr_handshake(ws_watch_1)

        with client.websocket_connect("/metadata", headers=watcher_headers) as ws_watch_2:
            _signalr_handshake(ws_watch_2)

            _send_invocation(ws_watch_1, "BeginWatchingUserPresence", [])
            _send_invocation(ws_watch_2, "BeginWatchingUserPresence", [])
            assert _wait_for(lambda: 100 in fake_hub_state.presence_watchers)

            _send_invocation(ws_watch_1, "EndWatchingUserPresence", [])
            assert 100 in fake_hub_state.presence_watchers

            with client.websocket_connect("/metadata", headers=target_headers) as ws_target:
                _signalr_handshake(ws_target)

                invocation = _read_invocation(ws_watch_2)
                assert invocation["target"] == "UserPresenceUpdated"
                assert invocation["arguments"][0] == 200

            _send_invocation(ws_watch_2, "EndWatchingUserPresence", [])
            assert _wait_for(lambda: 100 not in fake_hub_state.presence_watchers)


def test_metadata_refresh_friends_subscribes_and_pushes_online_friend(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """RefreshFriends should subscribe connection to friend presence updates and emit online friend snapshot."""
    client, fake_hub_state = metadata_test_client
    watcher_headers = _token_headers(user_id=100)

    fake_hub_state.presence[200] = StoredPresence(
        user_id=200,
        activity=None,
        status=UserStatus.ONLINE,
    )

    with client.websocket_connect("/metadata", headers=watcher_headers) as ws_watcher:
        _signalr_handshake(ws_watcher)

        fake_hub_state.friend_map[100] = {200}
        _send_invocation(ws_watcher, "RefreshFriends", [])

        invocation = _read_until(
            ws_watcher,
            lambda msg: msg.get("target") == "FriendPresenceUpdated" and msg.get("arguments", [None])[0] == 200,
        )
        assert invocation["arguments"][1] is not None
        watcher_ids = metadata_hub.friend_presence_watching_connections.get(200)
        assert watcher_ids is not None
        assert len(watcher_ids) == 1


def test_metadata_friend_presence_update_sent_to_subscribed_connections(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """FriendPresenceUpdated should be sent when a tracked friend connects."""
    client, fake_hub_state = metadata_test_client
    fake_hub_state.friend_map[100] = {200}

    with client.websocket_connect("/metadata", headers=_token_headers(user_id=100)) as ws_watcher:
        _signalr_handshake(ws_watcher)

        with client.websocket_connect("/metadata", headers=_token_headers(user_id=200)) as ws_target:
            _signalr_handshake(ws_target)

            invocation = _read_until(
                ws_watcher,
                lambda msg: msg.get("target") == "FriendPresenceUpdated" and msg.get("arguments", [None])[0] == 200,
            )
            assert invocation["arguments"][1] is not None


def test_metadata_offline_status_removes_presence_and_notifies_caller(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """UpdateStatus(offline) should remove redis presence and return a null self presence payload."""
    client, fake_hub_state = metadata_test_client

    with client.websocket_connect("/metadata", headers=_token_headers(user_id=42)) as ws:
        _signalr_handshake(ws)
        assert 42 in fake_hub_state.presence

        _send_invocation(ws, "UpdateStatus", [int(UserStatus.OFFLINE)])
        invocation = _read_until(
            ws,
            lambda msg: msg.get("target") == "UserPresenceUpdated" and msg.get("arguments", [None])[0] == 42,
        )

        assert invocation["arguments"][1] is None
        assert 42 not in fake_hub_state.presence


def test_metadata_get_changes_since_returns_queued_updates(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """GetChangesSince should return queued beatmap updates with latest queue id."""
    client, fake_hub_state = metadata_test_client

    fake_hub_state._beatmap_updates = [
        (1, [11, 12]),
        (2, [12, 13]),
    ]
    fake_hub_state._queue_id = 2

    with client.websocket_connect("/metadata", headers=_token_headers(user_id=42)) as ws:
        _signalr_handshake(ws)
        _send_invocation(ws, "GetChangesSince", [0], invocation_id="42")

        completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "42",
        )

        assert completion["result"] == [[11, 12, 13], 2]


def test_metadata_begin_watching_multiplayer_room_returns_stats_completion(
    metadata_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """BeginWatchingMultiplayerRoom should register watcher and return room stats completion."""
    client, _ = metadata_test_client

    with client.websocket_connect("/metadata", headers=_token_headers(user_id=55)) as ws:
        _signalr_handshake(ws)
        _send_invocation(ws, "BeginWatchingMultiplayerRoom", [9001], invocation_id="9001")

        completion = _read_until(
            ws,
            lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "9001",
        )

        assert completion["result"] == []
        watcher_ids = metadata_hub.room_watching_connections.get(9001)
        assert watcher_ids is not None
        assert len(watcher_ids) == 1
