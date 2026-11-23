package hub

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/matchmaking"
	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/signalr"
)

// queueDirectory is a matchmaking.Directory and Backend with one active ranked play pool (id 1) and one that is
// not (id 2). Everybody is rated 1500, except user 9 who is restricted.
type queueDirectory struct {
	mu      sync.Mutex
	results []room.MatchResult
}

func (d *queueDirectory) pools() []matchmaking.Pool {
	return []matchmaking.Pool{
		{ID: 1, Name: "1v1 Nomod", Type: protocol.PoolRankedPlay, Active: true, LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30, Ranked: true},
		{ID: 2, Name: "old", Type: protocol.PoolRankedPlay, Active: false, LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30, Ranked: true},
	}
}

func (d *queueDirectory) Pools(_ context.Context, t protocol.PoolType) ([]matchmaking.Pool, error) {
	var out []matchmaking.Pool
	for _, p := range d.pools() {
		if p.Type == t && p.Active {
			out = append(out, p)
		}
	}
	return out, nil
}

func (d *queueDirectory) Pool(_ context.Context, id int32) (*matchmaking.Pool, error) {
	for _, p := range d.pools() {
		if p.ID == id {
			return &p, nil
		}
	}
	return nil, nil
}

func (d *queueDirectory) Cards(context.Context, int32) ([]matchmaking.Card, error) {
	cards := make([]matchmaking.Card, 40)
	for i := range cards {
		cards[i] = matchmaking.Card{BeatmapID: int32(2000 + i), Checksum: fmt.Sprintf("sum-%d", i), StarRating: 3 + float64(i)/10, Rating: float64(1300 + i*10)}
	}
	return cards, nil
}

func (d *queueDirectory) UserRating(context.Context, int32, int32) (matchmaking.Rating, error) {
	return matchmaking.Rating{Mu: 1500, Sigma: 150}, nil
}

func (d *queueDirectory) Ratings(context.Context, int32) ([]int32, error) { return []int32{1500}, nil }

func (d *queueDirectory) TopRating(context.Context, int32) (int32, error) { return 2000, nil }

func (d *queueDirectory) User(_ context.Context, id int32) (matchmaking.UserInfo, error) {
	return matchmaking.UserInfo{ID: id, Username: fmt.Sprintf("user%d", id), Restricted: id == 9}, nil
}

func (d *queueDirectory) Scores(context.Context, int64) (room.RankedPlayScores, error) {
	return room.RankedPlayScores{}, nil
}

func (d *queueDirectory) RecordResult(_ context.Context, _ int32, result room.MatchResult) (map[int32]int32, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.results = append(d.results, result)
	return map[int32]int32{}, nil
}

// queueEnv is a hub with a running matchmaking queue.
func queueEnv(t *testing.T, tweak ...func(*matchmaking.Config)) *env {
	t.Helper()

	e := newEnvWith(t, room.Options{LoadTimeout: time.Hour})
	dir := &queueDirectory{}
	cfg := matchmaking.Config{
		Directory:       dir,
		Backend:         dir,
		Rooms:           e.hub.Rooms(),
		Notifier:        e.hub,
		QueueUpdateRate: 10 * time.Millisecond,
		DeckSize:        12,
	}
	for _, f := range tweak {
		f(&cfg)
	}
	svc := matchmaking.New(cfg)
	e.hub.SetMatchmaking(svc)

	ctx, cancel := context.WithCancel(context.Background())
	go svc.Run(ctx)
	t.Cleanup(func() {
		cancel()
		svc.Wait()
		e.hub.Rooms().CloseAll()
	})
	return e
}

func TestPoolsAreListedAsMatchmakingPoolArrays(t *testing.T) {
	e := queueEnv(t)
	c := e.connect(t, 1)

	result, failure := c.call("GetMatchmakingPoolsOfType", int(protocol.PoolRankedPlay))
	if failure != "" {
		t.Fatal(failure)
	}

	// MatchmakingPool: [id, ruleset id, variant, name, type]
	pools, ok := result.([]any)
	if !ok || len(pools) != 1 {
		t.Fatalf("pools = %#v, want the one active ranked play pool", result)
	}
	pool := pools[0].([]any)
	if len(pool) != 5 || toInt(pool[0]) != 1 || pool[3] != "1v1 Nomod" || toInt(pool[4]) != int64(protocol.PoolRankedPlay) {
		t.Errorf("pool = %#v", pool)
	}

	quick, failure := c.call("GetMatchmakingPoolsOfType", int(protocol.PoolQuickPlay))
	if failure != "" {
		t.Fatal(failure)
	}
	if list, _ := quick.([]any); len(list) != 0 {
		t.Errorf("quick play pools = %#v, want none (and an empty list rather than null)", quick)
	}
}

func TestJoiningTheLobbyAnswersWithAnEmptyResponseAndTheLobbyStatus(t *testing.T) {
	e := queueEnv(t)
	c := e.connect(t, 1)

	result, failure := c.call("MatchmakingJoinLobbyWithParams", []any{1})
	if failure != "" {
		t.Fatal(failure)
	}
	if array, ok := result.([]any); !ok || len(array) != 0 {
		t.Errorf("response = %#v, want an empty array (a response class without members)", result)
	}

	status := c.expect("MatchmakingLobbyStatusChanged")
	var fields []msgpack.RawMessage
	if err := msgpack.Unmarshal(status.Args[0], &fields); err != nil || len(fields) < 4 {
		t.Fatalf("lobby status = %v (%d fields)", err, len(fields))
	}

	if _, failure := c.call("MatchmakingLeaveLobby"); failure != "" {
		t.Errorf("leaving the lobby: %s", failure)
	}
}

func TestMatchmakingCallsReportTheirRefusals(t *testing.T) {
	e := queueEnv(t)
	c := e.connect(t, 1)

	for _, tc := range []struct {
		name   string
		method string
		args   []any
		want   string
	}{
		{"unknown pool", "MatchmakingJoinLobbyWithParams", []any{[]any{99}}, "Pool not found: 99"},
		{"inactive pool", "MatchmakingJoinLobbyWithParams", []any{[]any{2}}, "The selected matchmaking pool is no longer active."},
		{"inactive pool queue", "MatchmakingJoinQueue", []any{2}, "The selected matchmaking pool is no longer active."},
		{"duelling oneself", "MatchmakingIssueDuel", []any{[]any{1, 1}}, "You can't duel yourself."},
		{"quick play rooms", "MatchmakingToggleSelection", []any{-1}, "MatchmakingToggleSelection is not supported."},
	} {
		if _, failure := c.call(tc.method, tc.args...); failure == "" || !strings.Contains(failure, tc.want) {
			t.Errorf("%s: failure = %q, want it to say %q", tc.name, failure, tc.want)
		}
	}

	restricted := e.connect(t, 9)
	if _, failure := restricted.call("MatchmakingJoinQueue", 1); !strings.Contains(failure, "restricted") {
		t.Errorf("a restricted player queued: failure = %q", failure)
	}
}

func TestMatchmakingIsUnavailableWithoutAQueue(t *testing.T) {
	e := newEnv(t) // no SetMatchmaking
	c := e.connect(t, 1)

	for _, method := range []string{"GetMatchmakingPoolsOfType", "MatchmakingJoinQueue", "MatchmakingLeaveQueue"} {
		if _, failure := c.call(method, 1); !strings.Contains(failure, "not available") {
			t.Errorf("%s: failure = %q, want the matchmaking is not available", method, failure)
		}
	}
}

func TestTwoQueuedPlayersAreInvitedAndSentToARankedPlayRoom(t *testing.T) {
	e := queueEnv(t)
	one, two := e.connect(t, 1), e.connect(t, 2)

	for _, c := range []*client{one, two} {
		if _, failure := c.call("MatchmakingJoinQueue", 1); failure != "" {
			t.Fatal(failure)
		}
		c.expect("MatchmakingQueueJoined")
	}

	// The invitation arrives with the opponent's pool; the obsolete parameterless one is not sent.
	for _, c := range []*client{one, two} {
		invited := c.expect("MatchmakingRoomInvitedWithParams")
		var params []msgpack.RawMessage
		if err := msgpack.Unmarshal(invited.Args[0], &params); err != nil || len(params) == 0 {
			t.Fatalf("invitation = %v (%d fields)", err, len(params))
		}
	}

	for _, c := range []*client{one, two} {
		if _, failure := c.call("MatchmakingAcceptInvitation"); failure != "" {
			t.Fatal(failure)
		}
	}

	var roomID int64
	var password string
	for _, c := range []*client{one, two} {
		ready := c.expect("MatchmakingRoomReady")
		var id int64
		var secret string
		if err := msgpack.Unmarshal(ready.Args[0], &id); err != nil {
			t.Fatal(err)
		}
		if err := msgpack.Unmarshal(ready.Args[1], &secret); err != nil || secret == "" {
			t.Fatalf("password %q err=%v", secret, err)
		}
		if roomID != 0 && (roomID != id || password != secret) {
			t.Fatalf("the players were sent to different rooms: %d/%q and %d/%q", roomID, password, id, secret)
		}
		roomID, password = id, secret
	}

	// Following the callback is what the client does: join the room with the password.
	for _, c := range []*client{one, two} {
		// The snapshot is not inspected here: it holds the ranked play state, whose int keyed union the generic
		// decoder of these tests cannot read (the wire format of the state is pinned by the protocol golden tests).
		if _, failure := c.call("JoinRoomWithPassword", roomID, password); failure != "" {
			t.Fatalf("joining the matchmaking room: %s", failure)
		}
	}

	r, ok := e.hub.Rooms().Get(roomID)
	if !ok || !r.IsRankedPlay() {
		t.Fatalf("room %d is not a ranked play room", roomID)
	}
}

// sees reports whether the client is sent the callback within the time. Waiting out the time closes the connection,
// so it is for the last thing a test does with a client.
func (c *client) sees(target string, within time.Duration) bool {
	c.t.Helper()
	for _, m := range c.pending {
		if m.Target == target {
			return true
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), within)
	defer cancel()
	for {
		elements, err := c.tryRead(ctx)
		if err != nil {
			return false
		}
		for _, element := range elements {
			if element.callback != nil && element.callback.Target == target {
				return true
			}
		}
	}
}

func queueUp(t *testing.T, c *client, pool int) {
	t.Helper()
	if _, failure := c.call("MatchmakingJoinQueue", pool); failure != "" {
		t.Fatal(failure)
	}
	c.expect("MatchmakingQueueJoined")
}

func TestLeavingTheQueueTellsThePlayer(t *testing.T) {
	e := queueEnv(t)
	one := e.connect(t, 1)

	queueUp(t, one, 1)
	if _, failure := one.call("MatchmakingLeaveQueue"); failure != "" {
		t.Fatal(failure)
	}
	one.expect("MatchmakingQueueLeft")
}

func TestAPlayerWhoDisconnectsIsNotPairedAnyMore(t *testing.T) {
	e := queueEnv(t)

	gone := e.connect(t, 3)
	queueUp(t, gone, 1)
	_ = gone.ws.Close(websocket.StatusNormalClosure, "bye")
	time.Sleep(100 * time.Millisecond)

	other := e.connect(t, 4)
	queueUp(t, other, 1)

	if other.sees("MatchmakingRoomInvitedWithParams", 300*time.Millisecond) {
		t.Fatal("paired with a player who had disconnected")
	}
}

func TestAPlayerWhoReconnectsLeavesTheQueueOfTheirOldConnection(t *testing.T) {
	e := queueEnv(t)

	first := e.connect(t, 3)
	queueUp(t, first, 1)
	e.connect(t, 3) // replaces the first connection, which was queued

	other := e.connect(t, 4)
	queueUp(t, other, 1)

	if other.sees("MatchmakingRoomInvitedWithParams", 300*time.Millisecond) {
		t.Fatal("paired with the old connection of a player who reconnected")
	}
}

func TestDecliningAnInvitationRemovesOnlyThePlayerWhoDeclined(t *testing.T) {
	e := queueEnv(t)
	one, two := e.connect(t, 1), e.connect(t, 2)
	queueUp(t, one, 1)
	queueUp(t, two, 1)
	one.expect("MatchmakingRoomInvitedWithParams")
	two.expect("MatchmakingRoomInvitedWithParams")

	if _, failure := two.call("MatchmakingDeclineInvitation"); failure != "" {
		t.Fatal(failure)
	}
	two.expect("MatchmakingQueueLeft")

	if _, failure := one.call("MatchmakingAcceptInvitation"); failure != "" {
		t.Fatal(failure)
	}
	if one.sees("MatchmakingRoomReady", 300*time.Millisecond) {
		t.Fatal("a room was made although the opponent declined")
	}
}

func TestTheLobbyKeepsItsWatchersUpToDateUntilTheyLeave(t *testing.T) {
	e := queueEnv(t, func(c *matchmaking.Config) { c.LobbyUpdateRate = 20 * time.Millisecond })
	c := e.connect(t, 1)

	if _, failure := c.call("MatchmakingJoinLobbyWithParams", []any{1}); failure != "" {
		t.Fatal(failure)
	}
	c.expect("MatchmakingLobbyStatusChanged") // on joining
	c.expect("MatchmakingLobbyStatusChanged") // from the periodic update

	if _, failure := c.call("MatchmakingLeaveLobby"); failure != "" {
		t.Fatal(failure)
	}
	c.pending = nil // whatever was sent before the answer
	if c.sees("MatchmakingLobbyStatusChanged", 300*time.Millisecond) {
		t.Fatal("the lobby still updates a player who left it")
	}
}

func TestADuelFromChallengeToRoom(t *testing.T) {
	e := queueEnv(t)
	one, two := e.connect(t, 1), e.connect(t, 2)

	result, failure := one.call("MatchmakingIssueDuel", []any{2, 1})
	if failure != "" {
		t.Fatal(failure)
	}
	if array, ok := result.([]any); !ok || len(array) != 0 {
		t.Errorf("issuing a duel answers %#v, want an empty array", result)
	}
	one.expect("MatchmakingQueueJoined")

	// MatchmakingDuelIssuedParams: [duel id, challenger, pool]
	issued := two.expect("MatchmakingDuelIssued")
	var fields []msgpack.RawMessage
	if err := msgpack.Unmarshal(issued.Args[0], &fields); err != nil || len(fields) != 3 {
		t.Fatalf("invitation = %v (%d fields)", err, len(fields))
	}
	var id string
	var challenger int32
	if err := msgpack.Unmarshal(fields[0], &id); err != nil || len(id) != 36 {
		t.Fatalf("duel id %q err=%v", id, err)
	}
	if err := msgpack.Unmarshal(fields[1], &challenger); err != nil || challenger != 1 {
		t.Fatalf("challenger %d err=%v", challenger, err)
	}

	result, failure = two.call("MatchmakingAcceptDuel", []any{id})
	if failure != "" {
		t.Fatal(failure)
	}
	if array, ok := result.([]any); !ok || len(array) != 0 {
		t.Errorf("accepting a duel answers %#v, want an empty array", result)
	}

	for _, c := range []*client{one, two} {
		c.expect("MatchmakingRoomInvitedWithParams")
		if _, failure := c.call("MatchmakingAcceptInvitation"); failure != "" {
			t.Fatal(failure)
		}
	}

	var ready signalr.Message
	for _, c := range []*client{one, two} {
		ready = c.expect("MatchmakingRoomReady")
	}
	var roomID int64
	if err := msgpack.Unmarshal(ready.Args[0], &roomID); err != nil {
		t.Fatal(err)
	}
	if r, ok := e.hub.Rooms().Get(roomID); !ok || !r.IsRankedPlay() {
		t.Fatalf("room %d is not a ranked play room", roomID)
	}
}

// relationsFake answers the invitation checks of InvitePlayer.
type relationsFake struct {
	checks map[int32]room.InviteCheck
	err    map[int32]error
}

func (r relationsFake) InviteCheck(_ context.Context, _, to int32) (room.InviteCheck, error) {
	return r.checks[to], r.err[to]
}

func TestInvitingAPlayerSendsThemTheRoom(t *testing.T) {
	e := newEnv(t)
	e.hub.SetRelations(relationsFake{checks: map[int32]room.InviteCheck{
		8: {Blocked: true}, 9: {Restricted: true}, 10: {BlocksPMs: true},
	}, err: map[int32]error{11: context.DeadlineExceeded}})
	host, guest := e.connect(t, 1), e.connect(t, 7)
	if _, failure := host.call("CreateRoom", roomPayload()); failure != "" {
		t.Fatal(failure)
	}

	if _, failure := host.call("InvitePlayer", 7); failure != "" {
		t.Fatalf("invite: %s", failure)
	}
	invited := guest.expect("Invited")
	var by int32
	var roomID int64
	if err := msgpack.Unmarshal(invited.Args[0], &by); err != nil || by != 1 {
		t.Fatalf("invited by %d err=%v", by, err)
	}
	if err := msgpack.Unmarshal(invited.Args[1], &roomID); err != nil || roomID == 0 {
		t.Fatalf("room %d err=%v", roomID, err)
	}

	for target, want := range map[int]string{
		8:  room.UserBlockedMessage,
		9:  "Can't invite a restricted user to a room.",
		10: room.UserBlocksPMsMessage,
		11: "Player not found.",
	} {
		if _, failure := host.call("InvitePlayer", target); !strings.Contains(failure, want) {
			t.Errorf("inviting %d: failure = %q, want %q", target, failure, want)
		}
	}
}

func TestInvitingNeedsARoom(t *testing.T) {
	e := newEnv(t)
	c := e.connect(t, 1)

	if _, failure := c.call("InvitePlayer", 7); failure == "" {
		t.Error("inviting outside a room worked")
	}
}

func TestMatchmakingRoomsCannotInvite(t *testing.T) {
	_, players, _, _ := rankedMatch(t)

	if _, failure := players[0].call("InvitePlayer", 7); !strings.Contains(failure, "Can't invite players to matchmaking rooms.") {
		t.Errorf("failure = %q", failure)
	}
}
