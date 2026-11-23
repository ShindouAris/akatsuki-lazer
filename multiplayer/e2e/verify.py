"""Reads the database the end-to-end run wrote and checks it. Usage: python multiplayer/e2e/verify.py ROOM_ID FIRST_ITEM_ID"""

import json
import os
import sqlite3
import sys

room_id, first_item = int(sys.argv[1]), int(sys.argv[2])
db = sqlite3.connect(os.environ["E2E_DB"])
db.row_factory = sqlite3.Row

problems: list[str] = []


def expect(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FAIL ") + message)
    if not condition:
        problems.append(message)


room = db.execute("select * from multiplayer_rooms where id = ?", (room_id,)).fetchone()
expect(room is not None, f"room {room_id} exists")
if room is not None:
    expect(room["name"] == "e2e room", f"name is stored ({room['name']!r})")
    expect(room["status"] == "closed", f"room closed after everybody left (status={room['status']})")
    expect(room["participant_count"] == 0, f"participant_count is 0 (={room['participant_count']})")
    expect(room["type"] == "head_to_head" and room["queue_mode"] == "host_only", "type / queue mode stored")
    expect(room["channel_id"] == room_id, "channel id = room id")
    expect(room["ends_at"] is not None, "ends_at set when closed")

items = db.execute(
    "select * from multiplayer_playlist_items where room_id = ? order by id", (room_id,),
).fetchall()
expect(len(items) == 3, f"3 items: the original, the requeued copy and the host's addition (found {len(items)})")
if len(items) == 3:
    first, requeued, added = items
    expect(first["id"] == first_item, "the first item id is the one the client was told")
    expect(first["expired"] == 1 and first["played_at"] is not None, "played item is expired with a play time")
    expect(requeued["expired"] == 0 and requeued["beatmap_id"] == 1, "requeued copy exists and is not expired")
    expect(added["beatmap_id"] == 1 and added["owner_id"] == 1, "host's added item stored with its owner")
    expect(json.loads(first["required_mods"]) == [], "mods stored as JSON")
    expect(all(i["beatmap_id"] == 1 for i in items), "no item for the unknown beatmap was stored")
    orders = sorted(i["playlist_order"] for i in items if not i["expired"])
    expect(orders == list(range(len(orders))), f"unplayed items are numbered 0..n (got {orders})")

stale = db.execute("select status, participant_count from multiplayer_rooms where id = 900").fetchone()
expect(stale is not None and stale["status"] == "closed" and stale["participant_count"] == 0,
       "the room a previous run left open was closed at startup")

print("\nALL GOOD" if not problems else f"\n{len(problems)} PROBLEM(S)")
sys.exit(1 if problems else 0)
