"""Spectator hub helper behavior tests."""

from collections.abc import Generator

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
