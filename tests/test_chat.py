"""Chat of multiplayer rooms: membership follows the room, messages are stored and delivered to members only."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2 import notifications
from app.api.v2.chat import router as chat_router  # noqa: F401  (imported to make sure the router loads)
from app.core.config import get_settings
from app.core.security import create_token_pair
from app.core.security import get_password_hash
from app.models.chat import ChatChannelMember
from app.models.chat import ChatMessage
from app.models.user import User
from app.services import chat as chat_service

KEY = "test-internal-key"
INTERNAL = {"X-Internal-Key": KEY}


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "internal_api_key", KEY)
    monkeypatch.setattr(chat_service, "MEMBERSHIP_WAIT_SECONDS", 0.0)  # nobody is expected to arrive late
    chat_service.connections._by_user.clear()
    yield
    chat_service.connections._by_user.clear()


async def make_user(db: AsyncSession, name: str) -> User:
    user = User(username=name, email=f"{name}@example.test", password_hash=get_password_hash("x"))
    db.add(user)
    await db.flush()
    return user


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token_pair(user_id=user.id, scopes=['*']).access_token}"}


def state(participants: list[int], **overrides: object) -> dict[str, Any]:
    body: dict[str, Any] = {
        "host_id": None,
        "name": "Ranked play: a vs b",
        "password": "secret",
        "type": "ranked_play",
        "queue_mode": "host_only",
        "status": "idle",
        "participant_count": len(participants),
        "participant_ids": participants,
        "max_participants": None,
        "auto_skip": False,
        "auto_start_seconds": 0,
        "current_playlist_item_id": None,
    }
    body.update(overrides)
    return body


async def make_room(client: AsyncClient, participants: list[int]) -> int:
    response = await client.post("/internal/multiplayer/rooms", headers=INTERNAL, json={"state": state(participants)})
    assert response.status_code == 200, response.text
    return response.json()["channel_id"]


async def put_state(client: AsyncClient, room_id: int, participants: list[int], **overrides: object) -> None:
    response = await client.put(
        f"/internal/multiplayer/rooms/{room_id}", headers=INTERNAL, json=state(participants, **overrides),
    )
    assert response.status_code == 204, response.text


async def members(db: AsyncSession, channel_id: int) -> list[int]:
    rows = await db.execute(
        select(ChatChannelMember.user_id).where(ChatChannelMember.channel_id == channel_id).order_by(ChatChannelMember.user_id),
    )
    return list(rows.scalars().all())


async def send(client: AsyncClient, user: User, channel_id: int, text: str, **fields: object) -> Any:
    return await client.post(
        f"/api/v2/chat/channels/{channel_id}/messages",
        headers=auth(user),
        data={"message": text, "is_action": "false", "uuid": "local-1", **fields},
    )


# --- membership -------------------------------------------------------------------------------------------------


async def test_the_channel_is_open_to_the_users_in_the_room(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two, three = [await make_user(db_session, n) for n in ("one", "two", "three")]
    channel = await make_room(client, [one.id, two.id])
    assert await members(db_session, channel) == [one.id, two.id]

    await put_state(client, channel, [two.id, three.id])  # one left and three arrived: the count is unchanged
    assert await members(db_session, channel) == [two.id, three.id]


async def test_a_closed_room_has_no_members_and_no_chat(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])

    await put_state(client, channel, [one.id, two.id], status="closed")

    assert await members(db_session, channel) == []
    assert (await send(client, one, channel, "hello")).status_code == 404


async def test_users_who_do_not_exist_are_not_made_members(client: AsyncClient, db_session: AsyncSession) -> None:
    one = await make_user(db_session, "one")
    channel = await make_room(client, [one.id, 99_999])

    assert await members(db_session, channel) == [one.id]


async def test_a_room_that_reports_nobody_has_an_empty_channel(client: AsyncClient, db_session: AsyncSession) -> None:
    channel = await make_room(client, [])

    assert await members(db_session, channel) == []


# --- reading and writing ----------------------------------------------------------------------------------------


async def test_a_member_posts_and_reads_messages(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])

    posted = await send(client, one, channel, "  hi there  ", is_action="true", uuid="abc")

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["content"] == "hi there"
    assert (body["channel_id"], body["sender_id"], body["is_action"], body["uuid"]) == (channel, one.id, True, "abc")
    assert body["message_id"] > 0 and body["timestamp"]
    assert body["sender"]["username"] == "one"

    await send(client, two, channel, "second")
    listed = await client.get(f"/api/v2/chat/channels/{channel}/messages", headers=auth(two))
    assert [m["content"] for m in listed.json()] == ["hi there", "second"], "oldest first"
    assert listed.json()[1]["sender"]["username"] == "two"


async def test_history_can_be_paged(client: AsyncClient, db_session: AsyncSession) -> None:
    one = await make_user(db_session, "one")
    channel = await make_room(client, [one.id])
    ids = [(await send(client, one, channel, f"m{i}")).json()["message_id"] for i in range(5)]

    async def read(**params: object) -> list[int]:
        response = await client.get(f"/api/v2/chat/channels/{channel}/messages", headers=auth(one), params=params)
        assert response.status_code == 200, response.text
        return [m["message_id"] for m in response.json()]

    assert await read(limit=2) == ids[-2:], "the latest messages, oldest first"
    assert await read(since=ids[1]) == ids[2:]
    assert await read(since=ids[1], limit=2) == ids[2:4]
    assert await read(until=ids[3], limit=2) == ids[1:3]


async def test_outsiders_cannot_read_write_or_see_the_channel(client: AsyncClient, db_session: AsyncSession) -> None:
    one, outsider = await make_user(db_session, "one"), await make_user(db_session, "outsider")
    channel = await make_room(client, [one.id])
    await send(client, one, channel, "secret")

    for response in (
        await send(client, outsider, channel, "let me in"),
        await client.get(f"/api/v2/chat/channels/{channel}/messages", headers=auth(outsider)),
        await client.get(f"/api/v2/chat/channels/{channel}", headers=auth(outsider)),
        await client.put(f"/api/v2/chat/channels/{channel}/users/{outsider.id}", headers=auth(outsider)),
        await client.put(f"/api/v2/chat/channels/{channel}/mark-as-read/1", headers=auth(outsider)),
    ):
        assert response.status_code == 404, response.text
    assert (await db_session.execute(select(ChatMessage))).scalars().all()[0].content == "secret"
    assert len((await db_session.execute(select(ChatMessage))).scalars().all()) == 1


async def test_unknown_channels_and_missing_tokens(client: AsyncClient, db_session: AsyncSession) -> None:
    one = await make_user(db_session, "one")

    assert (await send(client, one, 424242, "x")).status_code == 404
    unauthenticated = await client.get("/api/v2/chat/channels")
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize(("text", "why"), [("   ", "blank"), ("x" * 451, "too long")])
async def test_bad_messages_are_refused(client: AsyncClient, db_session: AsyncSession, text: str, why: str) -> None:
    one = await make_user(db_session, "one")
    channel = await make_room(client, [one.id])

    assert (await send(client, one, channel, text)).status_code == 422, why
    assert (await send(client, one, channel, "x" * 450)).status_code == 200, "the limit itself is allowed"


async def test_nobody_joins_somebody_elses_place(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])

    joined = await client.put(f"/api/v2/chat/channels/{channel}/users/{two.id}", headers=auth(one))
    left = await client.delete(f"/api/v2/chat/channels/{channel}/users/{two.id}", headers=auth(one))
    mine = await client.put(f"/api/v2/chat/channels/{channel}/users/{one.id}", headers=auth(one))

    assert (joined.status_code, left.status_code) == (403, 403)
    assert mine.status_code == 200 and mine.json()["channel_id"] == channel
    assert (await client.delete(f"/api/v2/chat/channels/{channel}/users/{one.id}", headers=auth(one))).status_code == 204
    assert await members(db_session, channel) == [one.id, two.id], "leaving is the room's business"


async def test_private_messages_are_not_supported(client: AsyncClient, db_session: AsyncSession) -> None:
    one = await make_user(db_session, "one")

    response = await client.post("/api/v2/chat/channels", headers=auth(one), data={"type": "PM", "target_id": "2"})

    assert response.status_code == 422


# --- channels, presence, updates --------------------------------------------------------------------------------


async def test_channels_describe_the_room_for_the_client(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])
    posted = (await send(client, one, channel, "hello")).json()

    channels = (await client.get("/api/v2/chat/channels", headers=auth(two))).json()

    assert len(channels) == 1
    described = channels[0]
    assert described["channel_id"] == channel
    assert described["name"] == f"#lazermp_{channel}"  # the room id, which is also the channel id
    assert described["type"] == "MULTIPLAYER"
    assert described["users"] == [one.id, two.id]
    assert described["last_message_id"] == posted["message_id"]
    assert described["message_length_limit"] == 450

    single = (await client.get(f"/api/v2/chat/channels/{channel}", headers=auth(two))).json()
    assert single["channel"]["channel_id"] == channel
    assert [u["username"] for u in single["users"]] == ["one", "two"]


async def test_a_user_only_sees_their_own_channels(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two, three = [await make_user(db_session, n) for n in ("one", "two", "three")]
    first = await make_room(client, [one.id, two.id])
    second = await make_room(client, [three.id])

    assert [c["channel_id"] for c in (await client.get("/api/v2/chat/channels", headers=auth(one))).json()] == [first]
    assert [c["channel_id"] for c in (await client.get("/api/v2/chat/presence", headers=auth(three))).json()] == [second]

    await put_state(client, first, [one.id], status="closed")
    assert (await client.get("/api/v2/chat/channels", headers=auth(one))).json() == []


async def test_updates_carry_presence_and_the_messages_after_since(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])
    other = await make_room(client, [two.id])
    first = (await send(client, one, channel, "first")).json()["message_id"]
    await send(client, one, channel, "second")
    await send(client, two, other, "elsewhere")

    fresh = (await client.get("/api/v2/chat/updates", headers=auth(one), params={"since": 0})).json()
    assert fresh["messages"] == [], "a fresh start reads history by opening the channel"
    assert [c["channel_id"] for c in fresh["presence"]] == [channel]
    assert fresh["silences"] == []

    later = (await client.get("/api/v2/chat/updates", headers=auth(two), params={"since": first})).json()
    assert [m["content"] for m in later["messages"]] == ["second", "elsewhere"]
    assert {c["channel_id"] for c in later["presence"]} == {channel, other}


async def test_reading_only_moves_forward(client: AsyncClient, db_session: AsyncSession) -> None:
    one = await make_user(db_session, "one")
    channel = await make_room(client, [one.id])
    ids = [(await send(client, one, channel, f"m{i}")).json()["message_id"] for i in range(3)]

    async def last_read() -> int | None:
        return (await client.get("/api/v2/chat/channels", headers=auth(one))).json()[0]["last_read_id"]

    assert await last_read() is None
    for message_id, expected in [(ids[1], ids[1]), (ids[0], ids[1]), (ids[2], ids[2])]:
        response = await client.put(f"/api/v2/chat/channels/{channel}/mark-as-read/{message_id}", headers=auth(one))
        assert response.status_code == 204
        assert await last_read() == expected


# --- waiting for a room's participants to arrive ---------------------------------------------------------------


async def test_a_user_who_just_joined_the_room_is_waited_for(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([False, False, True])
    calls = 0

    async def late(*_: object) -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    monkeypatch.setattr(chat_service, "is_member", late)
    monkeypatch.setattr(chat_service, "MEMBERSHIP_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(chat_service, "MEMBERSHIP_POLL_SECONDS", 0.01)

    assert await chat_service.wait_for_membership(db_session, 1, 1) is True
    assert calls == 3


async def test_the_wait_ends(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "MEMBERSHIP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(chat_service, "MEMBERSHIP_POLL_SECONDS", 0.01)

    assert await chat_service.wait_for_membership(db_session, 1, 1) is False


# --- delivery to connected clients ------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("gone")
        self.sent.append(json.loads(text))


async def test_messages_reach_the_members_who_started_chat(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two, outsider = [await make_user(db_session, n) for n in ("one", "two", "outsider")]
    channel = await make_room(client, [one.id, two.id])
    connections = chat_service.connections

    sockets = {name: FakeSocket() for name in ("one", "two", "two_not_started", "outsider")}
    connections.start_chat(connections.register(one.id, sockets["one"]))  # type: ignore[arg-type]
    connections.start_chat(connections.register(two.id, sockets["two"]))  # type: ignore[arg-type]
    connections.register(two.id, sockets["two_not_started"])  # type: ignore[arg-type]
    connections.start_chat(connections.register(outsider.id, sockets["outsider"]))  # type: ignore[arg-type]

    await send(client, one, channel, "hello all", uuid="u-1")

    for name in ("one", "two"):
        assert len(sockets[name].sent) == 1, name
        event = sockets[name].sent[0]
        assert event["event"] == "chat.message.new"
        message = event["data"]["messages"][0]
        assert (message["content"], message["uuid"], message["sender_id"]) == ("hello all", "u-1", one.id)
        assert [u["id"] for u in event["data"]["users"]] == [one.id], "the client looks senders up in `users`"
    assert sockets["two_not_started"].sent == []
    assert sockets["outsider"].sent == []


async def test_a_dead_connection_does_not_stop_the_others(client: AsyncClient, db_session: AsyncSession) -> None:
    one, two = await make_user(db_session, "one"), await make_user(db_session, "two")
    channel = await make_room(client, [one.id, two.id])
    connections = chat_service.connections
    dead, alive = FakeSocket(fail=True), FakeSocket()
    connections.start_chat(connections.register(one.id, dead))  # type: ignore[arg-type]
    connections.start_chat(connections.register(two.id, alive))  # type: ignore[arg-type]

    response = await send(client, two, channel, "still delivered")

    assert response.status_code == 200
    assert len(alive.sent) == 1
    assert connections.listening(one.id) == 0, "the dead connection is forgotten"


def test_connections_are_counted_per_user() -> None:
    connections = chat_service.ChatConnections()
    first = connections.register(1, FakeSocket())  # type: ignore[arg-type]
    second = connections.register(1, FakeSocket())  # type: ignore[arg-type]
    connections.start_chat(first)
    assert connections.listening(1) == 1

    connections.unregister(1, first)
    connections.unregister(1, second)
    assert connections.listening(1) == 0
    assert 1 not in connections._by_user


# --- the notifications websocket -------------------------------------------------------------------------------


@pytest.fixture
def socket_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(notifications.router, prefix="/api/v2")
    with TestClient(app) as client:
        yield client


def test_the_websocket_starts_chat_for_the_user_of_its_token(socket_client: TestClient) -> None:
    token = create_token_pair(user_id=7, scopes=["*"]).access_token

    with socket_client.websocket_connect(
        "/api/v2/notifications/websocket", headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        assert chat_service.connections.listening(7) == 0, "connected, but not listening before chat.start"
        ws.send_text(json.dumps({"event": "chat.start"}))
        assert ws.receive_json()["event"] == "chat.start"
        assert chat_service.connections.listening(7) == 1

    assert chat_service.connections.listening(7) == 0, "closing the socket removes it"


def test_the_token_may_come_in_the_query(socket_client: TestClient) -> None:
    token = create_token_pair(user_id=8, scopes=["*"]).access_token

    with socket_client.websocket_connect(f"/api/v2/notifications/websocket?access_token={token}") as ws:
        ws.send_text(json.dumps({"event": "chat.start"}))
        ws.receive_json()
        assert chat_service.connections.listening(8) == 1


def test_a_socket_without_a_valid_token_still_works_but_gets_no_chat(socket_client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer nonsense"}):
        with socket_client.websocket_connect("/api/v2/notifications/websocket", headers=headers) as ws:
            ws.send_text(json.dumps({"event": "chat.start"}))
            assert ws.receive_json()["event"] == "chat.start"
        assert chat_service.connections._by_user == {}
