"""Spectator hub helper behavior tests."""

from collections.abc import Generator
import time
from time import monotonic

import pytest

import app.api.hubs.spectator as spectator_hub


class DummyWebSocket:
    """Minimal websocket stub for send_invocation tests."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture(autouse=True)
def reset_spectator_connections() -> Generator[None, None, None]:
    """Reset shared in-memory spectator connection state between tests."""
    spectator_hub.connections.clear()
    spectator_hub.connections_by_user.clear()
    yield
    spectator_hub.connections.clear()
    spectator_hub.connections_by_user.clear()


@pytest.mark.asyncio
async def test_send_to_user_fans_out_to_all_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """User-targeted events should be sent to every active connection for that user."""
    sent_websockets: list[str] = []

    async def fake_send_invocation(websocket, use_messagepack: bool, target: str, arguments: list) -> None:
        assert target == "UserScoreProcessed"
        assert arguments == [7, 101]
        assert isinstance(use_messagepack, bool)
        sent_websockets.append(websocket.name)

    monkeypatch.setattr(spectator_hub, "send_invocation", fake_send_invocation)

    user_id = 7
    spectator_hub.connections["conn-a"] = spectator_hub.SpectatorConnection(
        connection_id="conn-a",
        websocket=DummyWebSocket("a"),
        user_id=user_id,
    )
    spectator_hub.connections["conn-b"] = spectator_hub.SpectatorConnection(
        connection_id="conn-b",
        websocket=DummyWebSocket("b"),
        user_id=user_id,
    )
    spectator_hub.connections_by_user[user_id] = {"conn-a", "conn-b"}

    await spectator_hub._send_to_user(user_id, "UserScoreProcessed", [7, 101])

    assert set(sent_websockets) == {"a", "b"}


@pytest.mark.asyncio
async def test_send_to_user_prunes_stale_and_failed_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed or missing connections are removed from per-user connection tracking."""
    sent_websockets: list[str] = []

    async def fake_send_invocation(websocket, use_messagepack: bool, target: str, arguments: list) -> None:
        if websocket.name == "bad":
            raise RuntimeError("simulated send failure")
        sent_websockets.append(websocket.name)

    monkeypatch.setattr(spectator_hub, "send_invocation", fake_send_invocation)

    user_id = 42
    spectator_hub.connections["conn-good"] = spectator_hub.SpectatorConnection(
        connection_id="conn-good",
        websocket=DummyWebSocket("good"),
        user_id=user_id,
    )
    spectator_hub.connections["conn-bad"] = spectator_hub.SpectatorConnection(
        connection_id="conn-bad",
        websocket=DummyWebSocket("bad"),
        user_id=user_id,
    )
    spectator_hub.connections_by_user[user_id] = {"conn-good", "conn-bad", "missing"}

    await spectator_hub._send_to_user(user_id, "UserScoreProcessed", [42, 555])

    assert sent_websockets == ["good"]
    assert spectator_hub.connections_by_user[user_id] == {"conn-good"}


@pytest.mark.asyncio
async def test_broadcast_to_watchers_includes_presence_watchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presence watchers should also receive spectator broadcasts in compatibility mode."""

    class FakeHubState:
        async def get_watchers(self, target_user_id: int) -> set[int]:
            assert target_user_id == 100
            return {200}

        async def get_presence_watchers(self) -> set[int]:
            return {300, 100}

    sent_websockets: list[str] = []

    async def get_fake_hub_state_service() -> FakeHubState:
        return FakeHubState()

    async def fake_send_invocation(websocket, use_messagepack: bool, target: str, arguments: list) -> None:
        del use_messagepack, target, arguments
        sent_websockets.append(websocket.name)

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)
    monkeypatch.setattr(spectator_hub, "send_invocation", fake_send_invocation)

    spectator_hub.connections["conn-explicit"] = spectator_hub.SpectatorConnection(
        connection_id="conn-explicit",
        websocket=DummyWebSocket("explicit"),
        user_id=200,
    )
    spectator_hub.connections["conn-presence"] = spectator_hub.SpectatorConnection(
        connection_id="conn-presence",
        websocket=DummyWebSocket("presence"),
        user_id=300,
    )

    spectator_hub.connections_by_user[200] = {"conn-explicit"}
    spectator_hub.connections_by_user[300] = {"conn-presence"}

    await spectator_hub._broadcast_to_watchers(100, "UserSentFrames", [100, []])

    assert set(sent_websockets) == {"explicit", "presence"}


@pytest.mark.asyncio
async def test_broadcast_to_watchers_deduplicates_explicit_and_presence_watchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user appearing in both watcher sets should still receive one send."""

    class FakeHubState:
        async def get_watchers(self, target_user_id: int) -> set[int]:
            assert target_user_id == 111
            return {222}

        async def get_presence_watchers(self) -> set[int]:
            return {222}

    send_count = 0

    async def get_fake_hub_state_service() -> FakeHubState:
        return FakeHubState()

    async def fake_send_invocation(websocket, use_messagepack: bool, target: str, arguments: list) -> None:
        del websocket, use_messagepack, target, arguments
        nonlocal send_count
        send_count += 1

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)
    monkeypatch.setattr(spectator_hub, "send_invocation", fake_send_invocation)

    spectator_hub.connections["conn-a"] = spectator_hub.SpectatorConnection(
        connection_id="conn-a",
        websocket=DummyWebSocket("a"),
        user_id=222,
    )
    spectator_hub.connections_by_user[222] = {"conn-a"}

    await spectator_hub._broadcast_to_watchers(111, "UserSentFrames", [111, []])

    assert send_count == 1


@pytest.mark.asyncio
async def test_send_user_score_processed_queues_event_in_hub_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Score processed notifications should enqueue in hub state and start dispatcher."""

    class FakeHubState:
        def __init__(self) -> None:
            self.last_upsert: tuple[int, int, float, float] | None = None

        async def upsert_pending_score_processed_event(
            self,
            user_id: int,
            score_id: int,
            next_attempt_at: float,
            expires_at: float,
        ) -> int:
            self.last_upsert = (user_id, score_id, next_attempt_at, expires_at)
            return 1

    fake_hub_state = FakeHubState()

    async def get_fake_hub_state_service() -> FakeHubState:
        return fake_hub_state

    dispatcher_started = False

    def fake_ensure_dispatch_task() -> None:
        nonlocal dispatcher_started
        dispatcher_started = True

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)
    monkeypatch.setattr(spectator_hub, "_ensure_score_processed_dispatch_task", fake_ensure_dispatch_task)

    queued_count = await spectator_hub.send_user_score_processed(user_id=11, score_id=222)

    assert queued_count == 1
    assert fake_hub_state.last_upsert is not None
    assert fake_hub_state.last_upsert[0] == 11
    assert fake_hub_state.last_upsert[1] == 222
    assert fake_hub_state.last_upsert[2] > spectator_hub.LEGACY_TIMESTAMP_EPOCH_FLOOR
    assert fake_hub_state.last_upsert[3] > fake_hub_state.last_upsert[2]
    assert dispatcher_started


@pytest.mark.asyncio
async def test_dispatch_pending_score_processed_events_retries_until_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher should retry pending score notifications until delivery succeeds."""

    class FakeHubState:
        def __init__(self) -> None:
            now = monotonic()
            self.events: dict[int, dict[int, dict[str, float | int]]] = {
                33: {
                    444: {
                        "score_id": 444,
                        "next_attempt_at": now - 0.1,
                        "expires_at": now + 2.0,
                        "attempts": 0,
                    },
                },
            }
            self.saved_events: list[dict[str, float | int]] = []

        async def list_pending_score_processed_users(self) -> set[int]:
            return set(self.events.keys())

        async def get_pending_score_processed_events(self, user_id: int) -> dict[int, dict[str, float | int]]:
            return {score_id: dict(payload) for score_id, payload in self.events.get(user_id, {}).items()}

        async def clear_pending_score_processed_user(self, user_id: int) -> None:
            self.events.pop(user_id, None)

        async def save_pending_score_processed_event(self, user_id: int, event: dict[str, float | int]) -> None:
            self.saved_events.append(dict(event))
            self.events.setdefault(user_id, {})[int(event["score_id"])] = dict(event)

        async def remove_pending_score_processed_event(self, user_id: int, score_id: int) -> None:
            user_events = self.events.get(user_id)
            if user_events is None:
                return

            user_events.pop(score_id, None)
            if not user_events:
                self.events.pop(user_id, None)

    fake_hub_state = FakeHubState()

    async def get_fake_hub_state_service() -> FakeHubState:
        return fake_hub_state

    send_attempts = 0

    async def fake_send_to_user(user_id: int, target: str, arguments: list) -> int:
        nonlocal send_attempts
        assert user_id == 33
        assert target == "UserScoreProcessed"
        assert arguments == [33, 444]
        send_attempts += 1
        return 1 if send_attempts >= 2 else 0

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)
    monkeypatch.setattr(spectator_hub, "_send_to_user", fake_send_to_user)
    monkeypatch.setattr(spectator_hub, "SCORE_PROCESSED_RETRY_INTERVAL_SECONDS", 0.01)

    await spectator_hub._dispatch_pending_score_processed_events()

    assert send_attempts >= 2
    assert fake_hub_state.saved_events
    assert any(
        float(saved_event["next_attempt_at"]) > spectator_hub.LEGACY_TIMESTAMP_EPOCH_FLOOR
        for saved_event in fake_hub_state.saved_events
    )
    assert fake_hub_state.events == {}


@pytest.mark.asyncio
async def test_dispatch_pending_score_processed_events_removes_expired_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired pending score notifications should be removed without send attempts."""

    class FakeHubState:
        def __init__(self) -> None:
            now = time.time()
            self.events: dict[int, dict[int, dict[str, float | int]]] = {
                55: {
                    999: {
                        "score_id": 999,
                        "next_attempt_at": now - 1.0,
                        "expires_at": now - 0.1,
                        "attempts": 2,
                    },
                },
            }
            self.removed: list[tuple[int, int]] = []

        async def list_pending_score_processed_users(self) -> set[int]:
            return set(self.events.keys())

        async def get_pending_score_processed_events(self, user_id: int) -> dict[int, dict[str, float | int]]:
            return {score_id: dict(payload) for score_id, payload in self.events.get(user_id, {}).items()}

        async def clear_pending_score_processed_user(self, user_id: int) -> None:
            self.events.pop(user_id, None)

        async def save_pending_score_processed_event(self, user_id: int, event: dict[str, float | int]) -> None:
            self.events.setdefault(user_id, {})[int(event["score_id"])] = dict(event)

        async def remove_pending_score_processed_event(self, user_id: int, score_id: int) -> None:
            self.removed.append((user_id, score_id))
            user_events = self.events.get(user_id)
            if user_events is None:
                return

            user_events.pop(score_id, None)
            if not user_events:
                self.events.pop(user_id, None)

    fake_hub_state = FakeHubState()

    async def get_fake_hub_state_service() -> FakeHubState:
        return fake_hub_state

    send_attempts = 0

    async def fake_send_to_user(user_id: int, target: str, arguments: list) -> int:
        del user_id, target, arguments
        nonlocal send_attempts
        send_attempts += 1
        return 0

    monkeypatch.setattr(spectator_hub, "get_hub_state_service", get_fake_hub_state_service)
    monkeypatch.setattr(spectator_hub, "_send_to_user", fake_send_to_user)

    await spectator_hub._dispatch_pending_score_processed_events()

    assert send_attempts == 0
    assert fake_hub_state.removed == [(55, 999)]
    assert fake_hub_state.events == {}


def test_resume_score_processed_dispatcher_starts_dispatch_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup helper should trigger dispatcher scheduling."""
    started = False

    def fake_ensure_dispatch() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(spectator_hub, "_ensure_score_processed_dispatch_task", fake_ensure_dispatch)

    spectator_hub.resume_score_processed_dispatcher()

    assert started
