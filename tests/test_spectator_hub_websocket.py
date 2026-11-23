"""Spectator hub websocket regression tests."""

import json
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.hubs.spectator as spectator_hub
from app.api.hubs.base import SIGNALR_RECORD_SEPARATOR
from app.core.security import create_token_pair
from app.protocol.enums import SpectatedUserState
from app.protocol.models import FrameDataBundle
from app.protocol.models import FrameHeader
from app.protocol.models import SpectatorState
from app.services.hub_state import StoredPlayingState


class FakeHubStateService:
    """In-memory spectator hub state used by websocket tests."""

    def __init__(self) -> None:
        self.playing_by_user: dict[int, StoredPlayingState] = {}
        self.watching_by_user: dict[int, set[int]] = {}
        self.watchers_by_user: dict[int, set[int]] = {}
        self.presence_watchers: set[int] = set()
        self.replay_bundle_counts: dict[int, int] = {}
        self.clear_watches_calls: list[int] = []
        self.refresh_replay_ttl_calls: list[int] = []

    async def get_watchers(self, target_user_id: int) -> set[int]:
        return set(self.watchers_by_user.get(target_user_id, set()))

    async def get_presence_watchers(self) -> set[int]:
        return set(self.presence_watchers)

    async def get_watching(self, user_id: int) -> set[int]:
        return set(self.watching_by_user.get(user_id, set()))

    async def add_watcher(self, watcher_user_id: int, target_user_id: int) -> None:
        self.watching_by_user.setdefault(watcher_user_id, set()).add(target_user_id)
        self.watchers_by_user.setdefault(target_user_id, set()).add(watcher_user_id)

    async def remove_watcher(self, watcher_user_id: int, target_user_id: int) -> None:
        watching = self.watching_by_user.get(watcher_user_id)
        if watching is not None:
            watching.discard(target_user_id)
            if not watching:
                self.watching_by_user.pop(watcher_user_id, None)

        watchers = self.watchers_by_user.get(target_user_id)
        if watchers is not None:
            watchers.discard(watcher_user_id)
            if not watchers:
                self.watchers_by_user.pop(target_user_id, None)

    async def clear_user_watches(self, user_id: int) -> None:
        targets = set(self.watching_by_user.pop(user_id, set()))
        for target_user_id in targets:
            watchers = self.watchers_by_user.get(target_user_id)
            if watchers is None:
                continue
            watchers.discard(user_id)
            if not watchers:
                self.watchers_by_user.pop(target_user_id, None)

        self.clear_watches_calls.append(user_id)

    async def set_playing(
        self,
        user_id: int,
        state: SpectatorState,
        score_token: int | None = None,
    ) -> None:
        self.playing_by_user[user_id] = StoredPlayingState(
            user_id=user_id,
            state=state,
            score_token=score_token,
        )

    async def get_playing(self, user_id: int) -> StoredPlayingState | None:
        return self.playing_by_user.get(user_id)

    async def remove_playing(self, user_id: int) -> None:
        self.playing_by_user.pop(user_id, None)

    async def append_replay_frame_bundle(self, score_token: int, frame_bundle: FrameDataBundle) -> int:
        del frame_bundle
        next_count = self.replay_bundle_counts.get(score_token, 0) + 1
        self.replay_bundle_counts[score_token] = next_count
        return next_count

    async def refresh_playing_ttl(self, user_id: int) -> bool:
        return user_id in self.playing_by_user

    async def refresh_user_watch_ttl(self, watcher_user_id: int, target_user_ids: set[int]) -> None:
        del watcher_user_id, target_user_ids

    async def refresh_replay_frame_ttl(self, score_token: int) -> bool:
        self.refresh_replay_ttl_calls.append(score_token)
        return score_token in self.replay_bundle_counts


@pytest.fixture
def spectator_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, FakeHubStateService], None, None]:
    """Provide isolated TestClient with fake spectator hub state."""
    fake_hub_state = FakeHubStateService()

    async def get_fake_hub_state_service() -> FakeHubStateService:
        return fake_hub_state

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)

    async def fake_get_valid_score_token(score_token_id: int, user_id: int):
        del user_id
        return SimpleNamespace(id=score_token_id, beatmap_id=1234, ruleset_id=0)

    monkeypatch.setattr(spectator_hub, "_get_valid_score_token", fake_get_valid_score_token)

    async def fake_get_username_for_user(user_id: int) -> str:
        return f"user-{user_id}"

    monkeypatch.setattr(spectator_hub, "_get_username_for_user", fake_get_username_for_user)

    spectator_hub.connections.clear()
    spectator_hub.connections_by_user.clear()
    spectator_hub.pending_score_processed_events.clear()
    spectator_hub.score_processed_dispatch_task = None

    app = FastAPI()
    app.include_router(spectator_hub.router)

    with TestClient(app) as client:
        yield client, fake_hub_state

    spectator_hub.connections.clear()
    spectator_hub.connections_by_user.clear()
    spectator_hub.pending_score_processed_events.clear()
    spectator_hub.score_processed_dispatch_task = None


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


def _read_until(websocket, predicate, max_messages: int = 12) -> dict[str, Any]:
    for _ in range(max_messages):
        message = _read_message(websocket)
        if predicate(message):
            return message

    raise AssertionError("Expected websocket message was not received")


def test_begin_play_session_rejects_invalid_score_token(
    spectator_test_client: tuple[TestClient, FakeHubStateService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BeginPlaySession should reject invalid/used score tokens."""
    client, fake_hub_state = spectator_test_client

    async def reject_score_token(score_token_id: int, user_id: int):
        del score_token_id, user_id
        return None

    monkeypatch.setattr(spectator_hub, "_get_valid_score_token", reject_score_token)

    state = SpectatorState(
        beatmap_id=1234,
        ruleset_id=0,
        state=SpectatedUserState.PLAYING,
    ).to_msgpack()

    with client.websocket_connect("/spectator", headers=_token_headers(10)) as ws:
        _signalr_handshake(ws)
        _send_invocation(ws, "BeginPlaySession", [999, state], invocation_id="1")

        completion = _read_message(ws)
        assert completion["type"] == 3
        assert completion["invocationId"] == "1"
        assert fake_hub_state.playing_by_user == {}


def test_a_watcher_sees_what_a_current_client_plays_through_the_v2_methods(
    spectator_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Current clients call BeginPlaySessionV2/SendFrameDataV2/EndPlaySessionV2: the live leaderboard of a room
    is built from what the watchers receive, so these must reach them like the original methods."""
    client, fake_hub_state = spectator_test_client
    state = SpectatorState(beatmap_id=1234, ruleset_id=0, state=SpectatedUserState.PLAYING).to_msgpack()
    frames = FrameDataBundle(header=FrameHeader(total_score=600000, accuracy=0.9, combo=12, max_combo=12)).to_msgpack()
    frames[0][6] = None  # a client's own timestamp; the server ignores it, and this test talks JSON

    with client.websocket_connect("/spectator", headers=_token_headers(21)) as watcher_ws:
        _signalr_handshake(watcher_ws)
        _send_invocation(watcher_ws, "StartWatchingUser", [22], invocation_id="watch")

        with client.websocket_connect("/spectator", headers=_token_headers(22)) as player_ws:
            _signalr_handshake(player_ws)
            _send_invocation(player_ws, "BeginPlaySessionV2", [777, state], invocation_id="begin")
            began = _read_until(
                watcher_ws, lambda msg: msg.get("type") == 1 and msg.get("target") == "UserBeganPlaying",
            )
            assert began["arguments"][0] == 22 and began["arguments"][1][0] == 1234

            _send_invocation(player_ws, "SendFrameDataV2", [777, frames])
            sent = _read_until(
                watcher_ws, lambda msg: msg.get("type") == 1 and msg.get("target") == "UserSentFrames",
            )
            assert sent["arguments"][0] == 22
            assert sent["arguments"][1][0][0] == 600000  # the score the leaderboard shows
            assert fake_hub_state.replay_bundle_counts == {777: 1}

            _send_invocation(player_ws, "EndPlaySessionV2", [777, int(SpectatedUserState.PASSED)], invocation_id="end")
            finished = _read_until(
                watcher_ws, lambda msg: msg.get("type") == 1 and msg.get("target") == "UserFinishedPlaying",
            )
            # The state is the one the session began with, with the final state the client reported.
            assert finished["arguments"][0] == 22
            assert finished["arguments"][1][0] == 1234
            assert finished["arguments"][1][3] == int(SpectatedUserState.PASSED)


def test_frame_header_writes_received_time_the_way_the_client_reads_a_datetimeoffset() -> None:
    """The client reads Key 6 as DateTimeOffset: [msgpack timestamp, offset minutes]. An ISO string made it reject
    every UserSentFrames, so the live leaderboard never moved (verified against the real client's deserializer)."""
    import msgpack

    header = FrameHeader(total_score=1296).to_msgpack()

    assert isinstance(header[6], list) and len(header[6]) == 2 and header[6][1] == 0
    assert isinstance(header[6][0], msgpack.Timestamp)
    packed = msgpack.packb(header)
    assert bytes.fromhex("92d7ff") in packed  # array(2) of a timestamp ext (type -1, 4 or 8 bytes)


class _ListRedis:
    """The few list commands the replay buffer uses."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> None:
        del key, start, end

    async def expire(self, key: str, seconds) -> None:
        del key, seconds

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        del start, end
        return list(self.lists.get(key, []))


@pytest.mark.asyncio
async def test_replay_buffer_stores_the_bundles_the_hub_now_produces() -> None:
    """The buffered bundles are JSON in Redis; the msgpack timestamp of the header must not break that."""
    from app.protocol.models import LegacyReplayFrame
    from app.services.hub_state import HubStateService

    service = HubStateService()
    service._redis = _ListRedis()  # noqa: SLF001
    bundle = FrameDataBundle(
        header=FrameHeader(total_score=1296, combo=3),
        frames=[LegacyReplayFrame(time=120.5, mouse_x=10.0, mouse_y=20.0)],
    )

    assert await service.append_replay_frame_bundle(14, bundle) == 1

    stored = await service.get_replay_frame_bundles(14)
    assert len(stored) == 1 and stored[0].header.total_score == 1296 and stored[0].frames[0].time == 120.5


def test_reconnect_restores_watch_and_sends_playing_snapshot(
    spectator_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Reconnect should restore watched users and emit UserBeganPlaying snapshots."""
    client, fake_hub_state = spectator_test_client

    fake_hub_state.watching_by_user[101] = {202}
    fake_hub_state.watchers_by_user[202] = {101}
    fake_hub_state.playing_by_user[202] = StoredPlayingState(
        user_id=202,
        state=SpectatorState(
            beatmap_id=4321,
            ruleset_id=0,
            state=SpectatedUserState.PLAYING,
        ),
        score_token=555,
    )

    with client.websocket_connect("/spectator", headers=_token_headers(101)) as ws:
        _signalr_handshake(ws)

        invocation = _read_until(
            ws,
            lambda msg: msg.get("type") == 1 and msg.get("target") == "UserBeganPlaying",
        )

        assert invocation["arguments"][0] == 202
        assert invocation["arguments"][1][0] == 4321


def test_start_watching_user_notifies_target_with_resolved_username(
    spectator_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """StartWatchingUser should notify target with real watcher username payload."""
    client, _ = spectator_test_client

    with client.websocket_connect("/spectator", headers=_token_headers(303)) as target_ws:
        _signalr_handshake(target_ws)

        with client.websocket_connect("/spectator", headers=_token_headers(404)) as watcher_ws:
            _signalr_handshake(watcher_ws)
            _send_invocation(watcher_ws, "StartWatchingUser", [303], invocation_id="watch-1")

            started = _read_until(
                target_ws,
                lambda msg: msg.get("type") == 1 and msg.get("target") == "UserStartedWatching",
            )
            assert started["arguments"][0][0] == [404, "user-404"]

            completion = _read_until(
                watcher_ws,
                lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "watch-1",
            )
            assert completion["type"] == 3


def test_disconnect_last_connection_notifies_targets(
    spectator_test_client: tuple[TestClient, FakeHubStateService],
) -> None:
    """Cleanup should notify watched targets when the user's last connection disconnects."""
    client, fake_hub_state = spectator_test_client

    with client.websocket_connect("/spectator", headers=_token_headers(505)) as target_ws:
        _signalr_handshake(target_ws)

        with client.websocket_connect("/spectator", headers=_token_headers(606)) as watcher_ws:
            _signalr_handshake(watcher_ws)
            _send_invocation(watcher_ws, "StartWatchingUser", [505], invocation_id="watch-2")

            _read_until(
                target_ws,
                lambda msg: msg.get("type") == 1 and msg.get("target") == "UserStartedWatching",
            )
            _read_until(
                watcher_ws,
                lambda msg: msg.get("type") == 3 and msg.get("invocationId") == "watch-2",
            )

        ended = _read_until(
            target_ws,
            lambda msg: msg.get("type") == 1 and msg.get("target") == "UserEndedWatching",
        )

        assert ended["arguments"][0] == 606
        assert 606 in fake_hub_state.clear_watches_calls
