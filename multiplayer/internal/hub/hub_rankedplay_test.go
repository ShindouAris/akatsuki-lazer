package hub

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/signalr"
)

// rankedMatch creates a ranked play room whose first stages pass quickly, and connects users 1 and 2 to it.
func rankedMatch(t *testing.T) (e *env, players [2]*client, r *room.Room, password string) {
	t.Helper()

	e = newEnvWith(t, room.Options{LoadTimeout: time.Hour})

	deck := make([]room.DeckCard, 12)
	for i := range deck {
		deck[i] = room.DeckCard{
			Card: protocol.NewCardItem(),
			Item: protocol.PlaylistItem{BeatmapID: int32(1000 + i), BeatmapChecksum: fmt.Sprintf("md5-%d", i), StarRating: 3.5},
		}
	}

	r, password, err := e.hub.Rooms().CreateRankedPlay(context.Background(), room.RankedPlayParams{
		Players: []room.RankedPlayPlayer{{UserID: 1, Rating: 1500}, {UserID: 2, Rating: 1600}},
		Deck:    deck,
		Config: room.RankedPlayConfig{
			FirstRoundWarmup:    30 * time.Millisecond,
			CardDiscardAfterAll: 20 * time.Millisecond,
			FinishCardDiscard:   20 * time.Millisecond,
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	players[0], players[1] = e.connect(t, 1), e.connect(t, 2)
	for _, c := range players {
		if _, failure := c.call("JoinRoomWithPassword", r.ID(), password); failure != "" {
			t.Fatalf("joining the ranked play room failed: %s", failure)
		}
	}
	return e, players, r, password
}

// stageOf reads the stage out of a MatchRoomStateChanged callback: the union is [key, [stage, ...]].
func stageOf(t *testing.T, m signalr.Message) (key int, stage protocol.RankedPlayStage) {
	t.Helper()

	var union []msgpack.RawMessage
	if err := msgpack.Unmarshal(m.Args[0], &union); err != nil || len(union) != 2 {
		t.Fatalf("a match state is a union of 2 elements: %v (%d)", err, len(union))
	}
	var fields []msgpack.RawMessage
	if err := msgpack.Unmarshal(union[1], &fields); err != nil {
		t.Fatal(err)
	}

	var raw int
	if err := msgpack.Unmarshal(union[0], &key); err != nil {
		t.Fatal(err)
	}
	if err := msgpack.Unmarshal(fields[0], &raw); err != nil {
		t.Fatal(err)
	}
	return key, protocol.RankedPlayStage(raw)
}

// waitForStage reads state updates until the room reports the stage.
func waitForStage(t *testing.T, c *client, want protocol.RankedPlayStage) {
	t.Helper()

	for i := 0; i < 50; i++ {
		key, stage := stageOf(t, c.expect("MatchRoomStateChanged"))
		if key != protocol.MatchRoomStateRankedPlay {
			t.Fatalf("union key = %d, want %d (ranked play)", key, protocol.MatchRoomStateRankedPlay)
		}
		if stage == want {
			return
		}
	}
	t.Fatalf("never saw the %s stage", want)
}

func TestRankedPlayCallbacksReachTheClientsInTheirWireFormat(t *testing.T) {
	_, players, _, _ := rankedMatch(t)
	one, two := players[0], players[1]

	// Cards: the owner sees what each card is, with the card as a one element array holding its GUID.
	revealed := one.expect("RankedPlayCardRevealed")
	var card protocol.CardItem
	var item protocol.PlaylistItem
	if err := msgpack.Unmarshal(revealed.Args[0], &card); err != nil || len(card.ID) != 36 {
		t.Fatalf("revealed card %+v err=%v", card, err)
	}
	if err := msgpack.Unmarshal(revealed.Args[1], &item); err != nil || item.BeatmapID < 1000 {
		t.Fatalf("revealed item %+v err=%v", item, err)
	}

	added := two.expect("RankedPlayCardAdded")
	var owner int32
	if err := msgpack.Unmarshal(added.Args[0], &owner); err != nil || (owner != 1 && owner != 2) {
		t.Fatalf("added card owner %d err=%v", owner, err)
	}

	// The first stage's countdown is announced as a MatchEvent: [0, [[4, [id, ticks, stage]]]].
	waitForStage(t, one, protocol.StageRoundWarmup)
	var started *signalr.Message
	for i := 0; i < 20 && started == nil; i++ {
		event := one.expect("MatchEvent")
		var union []msgpack.RawMessage
		if err := msgpack.Unmarshal(event.Args[0], &union); err != nil {
			t.Fatal(err)
		}
		var key int
		_ = msgpack.Unmarshal(union[0], &key)
		if key == protocol.MatchEventCountdownStarted {
			started = &event
		}
	}
	if started == nil {
		t.Fatal("no countdown was announced")
	}
}

func TestPlayersPlayACardThroughTheWire(t *testing.T) {
	_, players, r, _ := rankedMatch(t)
	one, two := players[0], players[1]

	waitForStage(t, one, protocol.StageCardDiscard)

	// A request for cards the player does not hold is refused with the reference server's text.
	if _, failure := one.call("DiscardCards", []any{[]any{"00000000-0000-4000-8000-000000000000"}}); !strings.Contains(failure, "One or more cards were not in the hand.") {
		t.Fatalf("discarding a card that is not in the hand: %q", failure)
	}

	// Passing the discard is an empty list; once both have, the match moves on to the first turn.
	for _, c := range players {
		if _, failure := c.call("DiscardCards", []any{}); failure != "" {
			t.Fatal(failure)
		}
	}
	waitForStage(t, one, protocol.StageCardPlay)

	hand := handOf(t, r, 1)
	if _, failure := two.call("PlayCard", []any{handOf(t, r, 2)[0]}); !strings.Contains(failure, "Not the active player.") {
		t.Fatalf("the other player must not be able to play: %q", failure)
	}

	// A hand replay is relayed to the opponent, in the shape the client reads: [3, [userId, frames]].
	frame := []any{16.5, map[string]any{hand[0]: []any{true, false, true, false, 2, float32(1.25), float32(-3.5)}}}
	if _, failure := one.call("SendMatchRequest", []any{protocol.MatchRequestCardHandReplay, []any{[]any{frame}}}); failure != "" {
		t.Fatal(failure)
	}
	relayed := two.expect("MatchEvent")
	for relayedKey(t, relayed) != protocol.MatchEventCardHandReplay {
		relayed = two.expect("MatchEvent")
	}

	if _, failure := one.call("PlayCard", []any{hand[1]}); failure != "" {
		t.Fatalf("the active player could not play a card: %s", failure)
	}
	for _, c := range players {
		c.expect("RankedPlayCardPlayed")
		c.expect("PlaylistItemAdded")
	}
	waitForStage(t, two, protocol.StageFinishCardPlay)
}

func TestRankedPlayMethodsNeedARoom(t *testing.T) {
	e := newEnv(t)
	c := e.connect(t, 1)

	for _, method := range []string{"DiscardCards", "PlayCard", "SendMatchRequest"} {
		_, failure := c.call(method, []any{})
		if !strings.Contains(failure, "NotJoinedRoomException") {
			t.Errorf("%s outside a room: %q", method, failure)
		}
	}
}

func TestRankedPlayMethodsAreRefusedInOrdinaryRooms(t *testing.T) {
	e := newEnv(t)
	host := e.connect(t, 1)
	if _, failure := host.call("CreateRoom", roomPayload()); failure != "" {
		t.Fatal(failure)
	}

	if _, failure := host.call("PlayCard", []any{"00000000-0000-4000-8000-000000000000"}); !strings.Contains(failure, "This is not a ranked play room.") {
		t.Errorf("PlayCard in a head to head room: %q", failure)
	}
	// Ordinary rooms take match requests too (the start timer); kinds they do not know are ignored.
	if _, failure := host.call("SendMatchRequest", []any{protocol.MatchRequestCardHandReplay, []any{[]any{}}}); failure != "" {
		t.Errorf("SendMatchRequest in a head to head room: %q", failure)
	}
}

func TestMalformedMatchRequestsAreRefusedNotFatal(t *testing.T) {
	_, players, _, _ := rankedMatch(t)

	if _, failure := players[0].call("SendMatchRequest", "not a union"); !strings.Contains(failure, "Invalid argument 0") {
		t.Errorf("a malformed request: %q", failure)
	}
	if _, failure := players[0].call("SendMatchRequest"); !strings.Contains(failure, "Missing argument 0.") {
		t.Errorf("a missing request: %q", failure)
	}

	// The connection is still usable.
	if _, failure := players[0].call("DiscardCards", []any{}); failure != "" && !strings.Contains(failure, "InvalidState") {
		t.Errorf("the connection should survive: %q", failure)
	}
}

func relayedKey(t *testing.T, m signalr.Message) int {
	t.Helper()

	var union []msgpack.RawMessage
	if err := msgpack.Unmarshal(m.Args[0], &union); err != nil {
		t.Fatal(err)
	}
	var key int
	if err := msgpack.Unmarshal(union[0], &key); err != nil {
		t.Fatal(err)
	}
	return key
}

// handOf returns the ids of a player's cards, straight from the room.
func handOf(t *testing.T, r *room.Room, user int32) []string {
	t.Helper()

	var state []msgpack.RawMessage
	snapshot := r.Snapshot()
	if snapshot.MatchState == nil {
		t.Fatal("no match state")
	}
	if err := msgpack.Unmarshal(*snapshot.MatchState, &state); err != nil {
		t.Fatal(err)
	}

	var fields []msgpack.RawMessage
	if err := msgpack.Unmarshal(state[1], &fields); err != nil {
		t.Fatal(err)
	}

	// fields[3] maps user ids to [rating, life, hand, ...].
	dec := msgpack.NewDecoder(strings.NewReader(string(fields[3])))
	n, err := dec.DecodeMapLen()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < n; i++ {
		id, err := dec.DecodeInt32()
		if err != nil {
			t.Fatal(err)
		}
		var info []msgpack.RawMessage
		if err := dec.Decode(&info); err != nil {
			t.Fatal(err)
		}
		if id != user {
			continue
		}

		var cards []protocol.CardItem
		if err := msgpack.Unmarshal(info[2], &cards); err != nil {
			t.Fatal(err)
		}
		ids := make([]string, len(cards))
		for j, card := range cards {
			ids[j] = card.ID
		}
		return ids
	}
	t.Fatalf("user %d is not in the match state", user)
	return nil
}
