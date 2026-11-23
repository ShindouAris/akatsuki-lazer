package matchmaking

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// --- fakes -------------------------------------------------------------------------------------------

type sent struct {
	user   int32
	target string
	args   []any
}

type capture struct {
	mu     sync.Mutex
	events []sent
}

func (c *capture) Send(user int32, target string, args ...any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.events = append(c.events, sent{user, target, args})
}

func (c *capture) reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.events = nil
}

// targets returns what a user received, in order.
func (c *capture) targets(user int32) []string {
	c.mu.Lock()
	defer c.mu.Unlock()

	var out []string
	for _, e := range c.events {
		if e.user == user {
			out = append(out, e.target)
		}
	}
	return out
}

func (c *capture) count(user int32, target string) int {
	n := 0
	for _, t := range c.targets(user) {
		if t == target {
			n++
		}
	}
	return n
}

func (c *capture) args(user int32, target string) [][]any {
	c.mu.Lock()
	defer c.mu.Unlock()

	var out [][]any
	for _, e := range c.events {
		if e.user == user && e.target == target {
			out = append(out, e.args)
		}
	}
	return out
}

// statuses returns the union keys of the queue statuses a user received.
func (c *capture) statuses(user int32) []int {
	var keys []int
	for _, args := range c.args(user, "MatchmakingQueueStatusChanged") {
		keys = append(keys, args[0].(protocol.Union).Key)
	}
	return keys
}

var errNotFound = errors.New("not found")

type fakeDirectory struct {
	mu      sync.Mutex
	pools   map[int32]*Pool
	cards   map[int32][]Card
	ratings map[[2]int32]Rating
	users   map[int32]UserInfo
	spread  []int32
	top     int32

	ratingsCalls int
	cardsErr     error
}

func (d *fakeDirectory) Pools(_ context.Context, t protocol.PoolType) ([]Pool, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	var out []Pool
	for id := int32(1); id <= int32(len(d.pools)); id++ {
		if p := d.pools[id]; p != nil && p.Active && p.Type == t {
			out = append(out, *p)
		}
	}
	return out, nil
}

func (d *fakeDirectory) Pool(_ context.Context, id int32) (*Pool, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if p := d.pools[id]; p != nil {
		copied := *p
		return &copied, nil
	}
	return nil, nil
}

func (d *fakeDirectory) Cards(_ context.Context, id int32) ([]Card, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.cards[id], d.cardsErr
}

func (d *fakeDirectory) UserRating(_ context.Context, pool, user int32) (Rating, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if r, ok := d.ratings[[2]int32{pool, user}]; ok {
		return r, nil
	}
	return Rating{Mu: 1500, Sigma: 150}, nil
}

func (d *fakeDirectory) Ratings(context.Context, int32) ([]int32, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.ratingsCalls++
	return d.spread, nil
}

func (d *fakeDirectory) TopRating(context.Context, int32) (int32, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.top, nil
}

func (d *fakeDirectory) User(_ context.Context, id int32) (UserInfo, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if u, ok := d.users[id]; ok {
		return u, nil
	}
	return UserInfo{}, errNotFound
}

func (d *fakeDirectory) setPool(update func(*Pool), id int32) {
	d.mu.Lock()
	defer d.mu.Unlock()
	update(d.pools[id])
}

type fakeBackend struct {
	mu      sync.Mutex
	scores  room.RankedPlayScores
	results []room.MatchResult
}

func (b *fakeBackend) Scores(context.Context, int64) (room.RankedPlayScores, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.scores, nil
}

func (b *fakeBackend) RecordResult(_ context.Context, _ int32, result room.MatchResult) (map[int32]int32, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.results = append(b.results, result)
	return map[int32]int32{}, nil
}

func (b *fakeBackend) recorded() []room.MatchResult {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]room.MatchResult(nil), b.results...)
}

// failingRooms is a Rooms that cannot create anything.
type failingRooms struct{}

func (failingRooms) CreateRankedPlay(context.Context, room.RankedPlayParams) (*room.Room, string, error) {
	return nil, "", errors.New("the database is down")
}

// --- harness -----------------------------------------------------------------------------------------

type harness struct {
	t       *testing.T
	svc     *Service
	dir     *fakeDirectory
	notes   *capture
	backend *fakeBackend
	rooms   *room.Manager
	clock   *fakeClock
	ctx     context.Context
}

func newHarness(t *testing.T, mutate ...func(*Config)) *harness {
	t.Helper()

	dir := &fakeDirectory{
		pools: map[int32]*Pool{
			1: {ID: 1, Name: "1v1 Nomod", Type: protocol.PoolRankedPlay, Active: true, LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30},
			2: {ID: 2, Name: "1v1 RX", Type: protocol.PoolRankedPlay, Active: true, LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30},
			3: {ID: 3, Name: "quick", Type: protocol.PoolQuickPlay, Active: true, LobbySize: 8, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30},
			4: {ID: 4, Name: "retired", Type: protocol.PoolRankedPlay, Active: false, LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30},
		},
		cards:   map[int32][]Card{},
		ratings: map[[2]int32]Rating{},
		users: map[int32]UserInfo{
			1: {ID: 1, Username: "alice"},
			2: {ID: 2, Username: "bob"},
			3: {ID: 3, Username: "carol"},
			4: {ID: 4, Username: "dave"},
			5: {ID: 5, Username: "eve", Restricted: true},
		},
	}
	for pool := int32(1); pool <= 2; pool++ {
		for i := 0; i < 12; i++ {
			dir.cards[pool] = append(dir.cards[pool], Card{BeatmapID: int32(100*pool) + int32(i), Checksum: fmt.Sprintf("md5-%d-%d", pool, i), StarRating: 4, Rating: 1400 + float64(i)*10})
		}
	}

	h := &harness{t: t, dir: dir, notes: &capture{}, backend: &fakeBackend{}, clock: newClock(), ctx: context.Background()}
	h.rooms = room.NewManager(&capture{}, room.Options{})
	t.Cleanup(h.rooms.CloseAll)

	cfg := Config{
		Directory: dir,
		Rooms:     h.rooms,
		Backend:   h.backend,
		Notifier:  h.notes,
		Clock:     h.clock,
		Rand:      rand.New(rand.NewSource(1)),
		Match:     room.RankedPlayConfig{Rand: rand.New(rand.NewSource(2))},
	}
	for _, f := range mutate {
		f(&cfg)
	}
	h.svc = New(cfg)
	return h
}

func (h *harness) join(pool int32, users ...int32) {
	h.t.Helper()
	for _, u := range users {
		if err := h.svc.JoinQueue(h.ctx, u, pool); err != nil {
			h.t.Fatalf("user %d could not queue: %v", u, err)
		}
	}
}

// meet queues two players, has them invited and has both accept, and returns the room they were sent to.
func (h *harness) meet(pool, a, b int32) (roomID int64, password string) {
	h.t.Helper()

	h.join(pool, a, b)
	h.svc.Tick()
	h.svc.AcceptInvitation(a)
	h.svc.AcceptInvitation(b)
	h.svc.Wait()

	ready := h.notes.args(a, "MatchmakingRoomReady")
	if len(ready) != 1 {
		h.t.Fatalf("user %d was sent to %d rooms, want 1 (events: %v)", a, len(ready), h.notes.targets(a))
	}
	return ready[0][0].(int64), ready[0][1].(string)
}

func (h *harness) wantError(err error, text string) {
	h.t.Helper()

	var roomErr *room.Error
	if !errors.As(err, &roomErr) || roomErr.Type != "InvalidStateException" || roomErr.Message != text {
		h.t.Fatalf("error = %v, want InvalidStateException: %s", err, text)
	}
}

func eq(t *testing.T, got, want []string) {
	t.Helper()
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("got  %v\nwant %v", got, want)
	}
}

// --- listing and the queue -----------------------------------------------------------------------------

func TestPoolsOfATypeAreListedInTheClientsFormat(t *testing.T) {
	h := newHarness(t)

	ranked, err := h.svc.Pools(h.ctx, protocol.PoolRankedPlay)
	if err != nil {
		t.Fatal(err)
	}
	if len(ranked) != 2 || ranked[0].Name != "1v1 Nomod" || ranked[1].Name != "1v1 RX" || ranked[0].Type != protocol.PoolRankedPlay {
		t.Errorf("ranked pools = %+v, want the two active ranked play pools", ranked)
	}

	quick, _ := h.svc.Pools(h.ctx, protocol.PoolQuickPlay)
	if len(quick) != 1 || quick[0].ID != 3 {
		t.Errorf("quick play pools = %+v", quick)
	}
}

func TestJoiningTheQueueTellsThePlayerTheyAreSearching(t *testing.T) {
	h := newHarness(t)

	h.join(1, 1)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
	if got := h.notes.statuses(1); len(got) != 1 || got[0] != 0 {
		t.Errorf("statuses = %v, want [0] (searching)", got)
	}
}

func TestQueueRefusals(t *testing.T) {
	h := newHarness(t)

	h.wantError(h.svc.JoinQueue(h.ctx, 5, 1), "Can't queue when restricted.")
	h.wantError(h.svc.JoinQueue(h.ctx, 1, 99), "Pool not found: 99")
	h.wantError(h.svc.JoinQueue(h.ctx, 1, 4), "The selected matchmaking pool is no longer active.")
	h.wantError(h.svc.JoinQueue(h.ctx, 1, 3), "Only ranked play pools can be queued for.")

	if err := h.svc.JoinQueue(h.ctx, 99, 1); !errors.Is(err, errNotFound) {
		t.Errorf("an unknown player: %v", err)
	}
	if len(h.notes.targets(1)) != 0 {
		t.Error("a refused player must not be told anything")
	}
}

func TestLeavingTheQueueTellsThePlayer(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1)
	h.notes.reset()

	h.svc.LeaveQueue(1)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})
	h.svc.LeaveQueue(1) // no longer queued: nothing more
	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})
}

func TestJoiningAnotherQueueLeavesTheFirst(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1)
	h.notes.reset()

	h.join(2, 1)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft", "MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
	if h.svc.queues[1].Has(1) || !h.svc.queues[2].Has(1) {
		t.Error("the player must only be in the second queue")
	}
}

// --- invitations and rooms -----------------------------------------------------------------------------

func TestPairedPlayersAreInvitedWithTheKindOfPool(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1, 2)
	h.notes.reset()

	h.svc.Tick()

	for _, user := range []int32{1, 2} {
		eq(t, h.notes.targets(user), []string{"MatchmakingRoomInvitedWithParams", "MatchmakingQueueStatusChanged"})

		invitation := h.notes.args(user, "MatchmakingRoomInvitedWithParams")[0][0].(protocol.RoomInvitationParams)
		if invitation.Type != protocol.PoolRankedPlay {
			t.Errorf("user %d invited to %v", user, invitation.Type)
		}
		if got := h.notes.statuses(user); got[len(got)-1] != 1 {
			t.Errorf("statuses = %v, want the last to be 1 (match found)", got)
		}
	}
}

func TestAcceptingShowsJoiningAtOnceAndBothAcceptingCreatesTheRoom(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1, 2)
	h.svc.Tick()
	h.notes.reset()

	h.svc.AcceptInvitation(1)
	h.svc.Wait()

	if got := h.notes.statuses(1); len(got) != 1 || got[0] != 2 {
		t.Fatalf("statuses = %v, want [2] (joining match)", got)
	}
	if h.notes.count(1, "MatchmakingRoomReady") != 0 {
		t.Fatal("the room was created before the second player accepted")
	}

	h.svc.AcceptInvitation(2)
	h.svc.Wait()

	for _, user := range []int32{1, 2} {
		ready := h.notes.args(user, "MatchmakingRoomReady")
		if len(ready) != 1 {
			t.Fatalf("user %d was told %d times that the room is ready", user, len(ready))
		}
		roomID, password := ready[0][0].(int64), ready[0][1].(string)

		r, ok := h.rooms.Get(roomID)
		if !ok || !r.IsRankedPlay() {
			t.Fatalf("room %d is not a live ranked play room", roomID)
		}
		if _, _, err := h.rooms.Join(roomID, user, password); err != nil {
			t.Errorf("user %d could not join with the password they were given: %v", user, err)
		}
	}
}

func TestTheRoomIsNamedAfterThePoolAndThePlayers(t *testing.T) {
	h := newHarness(t)
	roomID, _ := h.meet(1, 1, 2)

	r, _ := h.rooms.Get(roomID)
	if got := r.Snapshot().Settings.Name; got != "osu! (1v1 Nomod): alice vs bob" {
		t.Errorf("room name = %q", got)
	}
}

// recordingRooms remembers what rooms were asked for, and creates them for real.
type recordingRooms struct {
	inner  Rooms
	mu     sync.Mutex
	params []room.RankedPlayParams
}

func (r *recordingRooms) CreateRankedPlay(ctx context.Context, params room.RankedPlayParams) (*room.Room, string, error) {
	r.mu.Lock()
	r.params = append(r.params, params)
	r.mu.Unlock()
	return r.inner.CreateRankedPlay(ctx, params)
}

func (r *recordingRooms) last(t *testing.T) room.RankedPlayParams {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()

	if len(r.params) == 0 {
		t.Fatal("no room was created")
	}
	return r.params[len(r.params)-1]
}

func TestTheRoomIsCreatedForThePlayersWithTheDeckOfThePool(t *testing.T) {
	var rooms *recordingRooms
	h := newHarness(t, func(c *Config) {
		rooms = &recordingRooms{inner: c.Rooms}
		c.Rooms = rooms
	})
	h.dir.ratings[[2]int32{1, 1}] = Rating{Mu: 1480.4, Sigma: 100}
	h.dir.ratings[[2]int32{1, 2}] = Rating{Mu: 1520.6, Sigma: 100}
	h.dir.cards[1] = []Card{
		{BeatmapID: 7, Checksum: "seven", Mode: 0, StarRating: 3, Rating: 1500, Mods: `[{"acronym":"RX"}]`},
		{BeatmapID: 8, Checksum: "eight", Mode: 0, StarRating: 5, Rating: 1500, Mods: `[{"acronym":"RX"}]`},
	}

	h.meet(1, 1, 2)

	params := rooms.last(t)
	if !params.Ranked || params.PoolID != 1 || params.Services == nil {
		t.Errorf("ranked=%v pool=%d services=%v", params.Ranked, params.PoolID, params.Services)
	}
	if len(params.Players) != 2 || params.Players[0].UserID != 1 || params.Players[0].Rating != 1480 || params.Players[1].Rating != 1521 {
		t.Errorf("players = %+v", params.Players)
	}

	if len(params.Deck) != 2 {
		t.Fatalf("deck = %d cards, want the pool's 2", len(params.Deck))
	}
	beatmaps := map[int32]bool{}
	for _, card := range params.Deck {
		beatmaps[card.Item.BeatmapID] = true
		if len(card.Item.RequiredMods) != 1 || card.Item.RequiredMods[0].Acronym != "RX" {
			t.Errorf("card %+v must require Relax", card.Item)
		}
		if len(card.Card.ID) != 36 {
			t.Errorf("card id %q is not a GUID", card.Card.ID)
		}
	}
	if !beatmaps[7] || !beatmaps[8] {
		t.Errorf("deck beatmaps = %v", beatmaps)
	}
}

func TestFailingToCreateTheRoomTellsThePlayers(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.Rooms = failingRooms{} })
	h.join(1, 1, 2)
	h.svc.Tick()
	h.notes.reset()

	h.svc.AcceptInvitation(1)
	h.svc.AcceptInvitation(2)
	h.svc.Wait()

	for _, user := range []int32{1, 2} {
		if h.notes.count(user, "MatchmakingQueueLeft") != 1 || h.notes.count(user, "MatchmakingRoomReady") != 0 {
			t.Errorf("user %d got %v", user, h.notes.targets(user))
		}
	}
}

func TestAPoolWithoutCardsCannotMakeAMatch(t *testing.T) {
	h := newHarness(t)
	h.dir.cards[1] = nil
	h.join(1, 1, 2)
	h.svc.Tick()
	h.notes.reset()

	h.svc.AcceptInvitation(1)
	h.svc.AcceptInvitation(2)
	h.svc.Wait()

	if h.notes.count(1, "MatchmakingRoomReady") != 0 || h.notes.count(1, "MatchmakingQueueLeft") != 1 {
		t.Errorf("user 1 got %v", h.notes.targets(1))
	}
}

func TestDecliningRemovesTheDeclinerBansThemAndRequeuesTheOther(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1, 2)
	h.svc.Tick()
	h.notes.reset()

	h.svc.DeclineInvitation(2)

	eq(t, h.notes.targets(2), []string{"MatchmakingQueueLeft"})
	eq(t, h.notes.targets(1), []string{"MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
	if got := h.notes.statuses(1); len(got) != 1 || got[0] != 0 {
		t.Errorf("player 1's statuses = %v, want searching again", got)
	}

	if end := h.svc.Bans().EndOf(2); !end.Equal(h.clock.Now().Add(time.Minute)) {
		t.Errorf("ban ends %v, want a minute from now", end)
	}

	// The ban keeps them out of matches, not out of the queue.
	h.join(1, 2)
	h.svc.Tick()
	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 0 {
		t.Fatal("a banned player was matched")
	}
	h.clock.Advance(61 * time.Second)
	h.svc.Tick()
	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 1 {
		t.Error("the player was not matched once the ban ended")
	}
}

func TestInvitationsThatAreNotAnsweredExpire(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1, 2)
	h.svc.Tick()
	h.svc.AcceptInvitation(1)
	h.notes.reset()

	h.clock.Advance(31 * time.Second)
	h.svc.Tick()

	eq(t, h.notes.targets(2), []string{"MatchmakingQueueLeft"})
	if h.svc.Bans().EndOf(2).IsZero() || !h.svc.Bans().EndOf(1).IsZero() {
		t.Error("only the player who did not answer is banned")
	}
}

func TestDisconnectingLeavesTheQueue(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1, 2)
	h.svc.Tick()
	h.notes.reset()

	h.svc.Disconnected(1)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})
	eq(t, h.notes.targets(2), []string{"MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
}

func TestRefreshingAPoolThatWasSwitchedOffEmptiesItsQueue(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1)
	h.notes.reset()

	h.dir.setPool(func(p *Pool) { p.Active = false }, 1)
	h.svc.RefreshPools(h.ctx)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})
}

func TestRefreshingAPoolPicksUpItsNewSettings(t *testing.T) {
	h := newHarness(t)
	h.dir.ratings[[2]int32{1, 1}] = Rating{Mu: 1500, Sigma: 150}
	h.dir.ratings[[2]int32{1, 2}] = Rating{Mu: 1900, Sigma: 150}
	h.join(1, 1, 2)
	h.svc.Tick()
	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 0 {
		t.Fatal("400 apart with a radius of 200 must not pair")
	}

	h.dir.setPool(func(p *Pool) { p.SearchRadius = 1000 }, 1)
	h.svc.RefreshPools(h.ctx)
	h.svc.Tick()

	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 1 {
		t.Error("the wider radius was not picked up")
	}
	if !h.svc.queues[1].Pool().Ranked {
		t.Error("a refreshed pool must stay ranked")
	}
}

// --- finished matches ----------------------------------------------------------------------------------

func TestResultsOfAFinishedMatchGoToTheBackendWithTheirPool(t *testing.T) {
	h := newHarness(t)
	roomID, password := h.meet(1, 1, 2)
	for _, u := range []int32{1, 2} {
		if _, _, err := h.rooms.Join(roomID, u, password); err != nil {
			t.Fatal(err)
		}
	}

	// Player 2 abandons the match.
	r, _ := h.rooms.Get(roomID)
	if err := h.rooms.Leave(r, 2, 2); err != nil {
		t.Fatal(err)
	}

	results := h.backend.recorded()
	if len(results) != 1 {
		t.Fatalf("%d results recorded, want 1", len(results))
	}
	if got := results[0]; got.PoolID != 1 || !got.Ranked || !got.Played || got.Winner == nil || *got.Winner != 1 || got.RoomID != roomID {
		t.Errorf("result = %+v", got)
	}

	// Abandoning a ranked match keeps the player out of the queue.
	if h.svc.Bans().EndOf(2).IsZero() {
		t.Error("the player who left was not banned")
	}
}

func TestRecentOpponentsAreNotPairedAgainRightAfterTheirMatch(t *testing.T) {
	// Longer than the ten minute ban of the player who leaves, so that only the rematch rule is at work.
	h := newHarness(t, func(c *Config) { c.RecentMatchupTimeout = 30 * time.Minute })
	roomID, password := h.meet(1, 1, 2)
	for _, u := range []int32{1, 2} {
		if _, _, err := h.rooms.Join(roomID, u, password); err != nil {
			t.Fatal(err)
		}
	}
	r, _ := h.rooms.Get(roomID)
	_ = h.rooms.Leave(r, 2, 2)

	// Both are free to queue again, but not for each other.
	h.clock.Advance(11 * time.Minute) // the ban of the one who left is over
	h.notes.reset()
	h.join(1, 1, 2)
	h.svc.Tick()
	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 0 {
		t.Fatal("the two players who just met were paired again")
	}

	// In time they may meet again.
	h.clock.Advance(20 * time.Minute)
	h.svc.Tick()
	if h.notes.count(1, "MatchmakingRoomInvitedWithParams") != 1 {
		t.Error("the players were never allowed to meet again")
	}
}

func TestMatchesOfThePoolAppearInItsLobby(t *testing.T) {
	h := newHarness(t)
	h.dir.spread = []int32{1510, 1520, 1530, 1790}
	if err := h.svc.JoinLobby(h.ctx, 3, 1); err != nil {
		t.Fatal(err)
	}

	roomID, password := h.meet(1, 1, 2)
	for _, u := range []int32{1, 2} {
		if _, _, err := h.rooms.Join(roomID, u, password); err != nil {
			t.Fatal(err)
		}
	}
	r, _ := h.rooms.Get(roomID)
	_ = h.rooms.Leave(r, 2, 2)

	h.notes.reset()
	h.svc.UpdateLobbies(h.ctx)

	updates := h.notes.args(3, "MatchmakingLobbyStatusChanged")
	if len(updates) != 1 {
		t.Fatalf("lobby watcher got %d updates, want 1", len(updates))
	}
	status := updates[0][0].(protocol.LobbyStatus)
	if len(status.RecentMatches) != 1 {
		t.Fatalf("recent matches = %d, want the one that just finished", len(status.RecentMatches))
	}

	// It is a MatchRoomState union: [2, [stage, ...]].
	var union []msgpack.RawMessage
	if err := msgpack.Unmarshal(status.RecentMatches[0], &union); err != nil || len(union) != 2 {
		t.Fatalf("recent match is not a union: %v", err)
	}
	var key int
	_ = msgpack.Unmarshal(union[0], &key)
	if key != protocol.MatchRoomStateRankedPlay {
		t.Errorf("union key = %d, want ranked play", key)
	}

	// The next update does not repeat it.
	h.notes.reset()
	h.svc.UpdateLobbies(h.ctx)
	again := h.notes.args(3, "MatchmakingLobbyStatusChanged")[0][0].(protocol.LobbyStatus)
	if len(again.RecentMatches) != 0 {
		t.Error("a match was announced twice")
	}
}

// --- lobby ---------------------------------------------------------------------------------------------

func TestJoiningTheLobbyShowsTheQueueTheDistributionAndYourRating(t *testing.T) {
	h := newHarness(t)
	h.dir.spread = []int32{1510, 1524, 1525, 1790}
	h.dir.ratings[[2]int32{1, 3}] = Rating{Mu: 1612.4, Sigma: 99.5}
	h.join(1, 1, 2)

	if err := h.svc.JoinLobby(h.ctx, 3, 1); err != nil {
		t.Fatal(err)
	}

	updates := h.notes.args(3, "MatchmakingLobbyStatusChanged")
	if len(updates) != 1 {
		t.Fatalf("got %d updates, want 1", len(updates))
	}
	status := updates[0][0].(protocol.LobbyStatus)

	if len(status.UsersInQueue) != 2 {
		t.Errorf("players in queue = %v, want the 2 waiting", status.UsersInQueue)
	}
	want := []protocol.RatingBucket{{Rating: 1500, Count: 2}, {Rating: 1525, Count: 1}, {Rating: 1775, Count: 1}}
	if fmt.Sprint(status.RatingDistribution) != fmt.Sprint(want) {
		t.Errorf("distribution = %v, want %v", status.RatingDistribution, want)
	}
	if status.UserRating == nil || *status.UserRating != 1612 || status.UserSigma == nil || *status.UserSigma != 99.5 {
		t.Errorf("own rating = %v sigma %v", status.UserRating, status.UserSigma)
	}
	if status.UserRank != nil {
		t.Errorf("rank = %v, want none", *status.UserRank)
	}
}

func TestTheLobbyShowsTheRankOfARankedPlayer(t *testing.T) {
	h := newHarness(t)
	rank := int32(42)
	h.dir.ratings[[2]int32{1, 3}] = Rating{Mu: 1612, Sigma: 99.5, Rank: &rank}

	if err := h.svc.JoinLobby(h.ctx, 3, 1); err != nil {
		t.Fatal(err)
	}

	status := h.notes.args(3, "MatchmakingLobbyStatusChanged")[0][0].(protocol.LobbyStatus)
	if status.UserRank == nil || *status.UserRank != 42 {
		t.Errorf("rank = %v, want 42", status.UserRank)
	}
}

func TestTheRatingDistributionIsNotRefetchedWithinFiveMinutes(t *testing.T) {
	h := newHarness(t)
	h.dir.spread = []int32{1500}

	_ = h.svc.JoinLobby(h.ctx, 3, 1)
	_ = h.svc.JoinLobby(h.ctx, 4, 1)
	if h.dir.ratingsCalls != 1 {
		t.Errorf("ratings fetched %d times, want once", h.dir.ratingsCalls)
	}

	h.clock.Advance(4 * time.Minute)
	_ = h.svc.JoinLobby(h.ctx, 3, 1)
	if h.dir.ratingsCalls != 1 {
		t.Errorf("ratings fetched %d times after four minutes, want it still cached", h.dir.ratingsCalls)
	}

	h.clock.Advance(2 * time.Minute)
	_ = h.svc.JoinLobby(h.ctx, 3, 1)
	if h.dir.ratingsCalls != 2 {
		t.Errorf("ratings fetched %d times, want again after five minutes", h.dir.ratingsCalls)
	}
}

func TestLobbyRefusals(t *testing.T) {
	h := newHarness(t)

	h.wantError(h.svc.JoinLobby(h.ctx, 1, 99), "Pool not found: 99")
	h.wantError(h.svc.JoinLobby(h.ctx, 1, 4), "The selected matchmaking pool is no longer active.")
}

func TestLeavingTheLobbyStopsTheUpdates(t *testing.T) {
	h := newHarness(t)
	_ = h.svc.JoinLobby(h.ctx, 3, 1)
	h.notes.reset()

	h.svc.UpdateLobbies(h.ctx)
	if h.notes.count(3, "MatchmakingLobbyStatusChanged") != 1 {
		t.Fatal("a watcher must get updates")
	}

	h.svc.LeaveLobby(3)
	h.notes.reset()
	h.svc.UpdateLobbies(h.ctx)
	if h.notes.count(3, "MatchmakingLobbyStatusChanged") != 0 {
		t.Error("somebody who left the lobby still got updates")
	}
}

func TestWatchingOnePoolAtATime(t *testing.T) {
	h := newHarness(t)
	_ = h.svc.JoinLobby(h.ctx, 3, 1)
	_ = h.svc.JoinLobby(h.ctx, 3, 2)
	h.notes.reset()

	h.svc.UpdateLobbies(h.ctx)

	if h.notes.count(3, "MatchmakingLobbyStatusChanged") != 1 {
		t.Errorf("a player watching one pool got %d updates", h.notes.count(3, "MatchmakingLobbyStatusChanged"))
	}
}

func TestDisconnectingLeavesTheLobby(t *testing.T) {
	h := newHarness(t)
	_ = h.svc.JoinLobby(h.ctx, 3, 1)

	h.svc.Disconnected(3)
	h.notes.reset()
	h.svc.UpdateLobbies(h.ctx)

	if h.notes.count(3, "MatchmakingLobbyStatusChanged") != 0 {
		t.Error("a disconnected player still gets lobby updates")
	}
}

func TestOnlyASampleOfTheQueueIsShown(t *testing.T) {
	h := newHarness(t)
	h.dir.users = map[int32]UserInfo{}
	for id := int32(1); id <= 70; id++ {
		h.dir.users[id] = UserInfo{ID: id, Username: fmt.Sprint(id)}
	}
	h.dir.setPool(func(p *Pool) { p.SearchRadius = 0 }, 1) // nobody gets paired, so they all wait
	for id := int32(1); id <= 70; id++ {
		h.join(1, id)
	}

	_ = h.svc.JoinLobby(h.ctx, 1000, 1)

	status := h.notes.args(1000, "MatchmakingLobbyStatusChanged")[0][0].(protocol.LobbyStatus)
	if len(status.UsersInQueue) != 50 {
		t.Errorf("%d players shown, want a sample of 50", len(status.UsersInQueue))
	}
}
