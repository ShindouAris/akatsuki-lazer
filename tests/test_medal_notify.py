import json

import pytest

from app.services.chat import connections
from app.services.medals import CATALOGUE
from app.services.medals import notify_medals


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_medal_unlock_is_pushed_to_connected_client():
    socket = FakeSocket()
    conn = connections.register(777, socket)  # never sent chat.start
    other = FakeSocket()
    other_conn = connections.register(778, other)
    try:
        rule = CATALOGUE[0]
        await notify_medals(777, [rule.id, 999999])
    finally:
        connections.unregister(777, conn)
        connections.unregister(778, other_conn)

    assert other.sent == []
    assert len(socket.sent) == 1
    message = socket.sent[0]
    assert message["event"] == "new"
    assert message["data"]["name"] == "user_achievement_unlock"
    details = message["data"]["details"]
    assert details["achievement_id"] == rule.id
    assert details["title"] == rule.name
    assert details["slug"] == rule.icon_slug == "osu-skill-pass-1"
    assert details["user_id"] == 777
