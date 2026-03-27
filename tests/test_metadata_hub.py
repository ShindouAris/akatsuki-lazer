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

    async def set_presence(
        self,
        user_id: int,
        activity: UserActivity | None,
        status: UserStatus,
    ) -> None:
        self.presence[user_id] = StoredPresence(user_id=user_id, activity=activity, status=status)

    async def remove_presence(self, user_id: int) -> None:
        self.presence.pop(user_id, None)

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

    metadata_hub.connections.clear()
    metadata_hub.connections_by_user.clear()
    metadata_hub.presence_watching_connections.clear()

    app = FastAPI()
    app.include_router(metadata_hub.router)

    with TestClient(app) as client:
        yield client, fake_hub_state

    metadata_hub.connections.clear()
    metadata_hub.connections_by_user.clear()
    metadata_hub.presence_watching_connections.clear()


def _token_headers(user_id: int) -> dict[str, str]:
    token = create_token_pair(user_id=user_id, scopes=["*"]).access_token
    return {"Authorization": f"Bearer {token}"}


def _signalr_handshake(websocket) -> None:
    websocket.send_text('{"protocol":"json","version":1}' + SIGNALR_RECORD_SEPARATOR)
    assert websocket.receive_text() == "{}" + SIGNALR_RECORD_SEPARATOR


def _send_invocation(websocket, target: str, arguments: list) -> None:
    payload = {
        "type": 1,
        "target": target,
        "arguments": arguments,
    }
    websocket.send_text(json.dumps(payload) + SIGNALR_RECORD_SEPARATOR)


def _read_invocation(websocket) -> dict[str, Any]:
    raw = websocket.receive_text()
    messages = [part for part in raw.split(SIGNALR_RECORD_SEPARATOR) if part]
    assert messages
    return json.loads(messages[0])


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
