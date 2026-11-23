package room

import (
	"context"
	"fmt"
	"math/rand"
	"sync"
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// --- fakes -------------------------------------------------------------------------------------------

// fakeClock only moves when told to, running every timer that falls due on the way.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
}

type fakeTimer struct {
	clock          *fakeClock
	at             time.Time
	f              func()
	stopped, fired bool
}

func newFakeClock() *fakeClock { return &fakeClock{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) AfterFunc(d time.Duration, f func()) Timer {
	c.mu.Lock()
	defer c.mu.Unlock()

	t := &fakeTimer{clock: c, at: c.now.Add(d), f: f}
	c.timers = append(c.timers, t)
	return t
}

// pending returns how many timers are waiting to fire.
func (c *fakeClock) pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()

	n := 0
	for _, t := range c.timers {
		if !t.stopped && !t.fired {
			n++
		}
	}
	return n
}

func (t *fakeTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()

	pending := !t.stopped && !t.fired
	t.stopped = true
	return pending
}

// Advance moves time forward, firing timers in order. Callbacks run on the calling goroutine.
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	target := c.now.Add(d)
	c.mu.Unlock()

	for {
		c.mu.Lock()
		var next *fakeTimer
		for _, t := range c.timers {
			if !t.stopped && !t.fired && !t.at.After(target) && (next == nil || t.at.Before(next.at)) {
				next = t
			}
		}
		if next == nil {
			c.now = target
			c.mu.Unlock()
			return
		}
		c.now = next.at
		next.fired = true
		c.mu.Unlock()

		next.f()
	}
}

// capture records every callback with its arguments.
type capture struct {
	mu     sync.Mutex
	events []sent
}

type sent struct {
	user   int32
	target string
	args   []any
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

// count returns how many times user received target.
func (c *capture) count(user int32, target string) int {
	c.mu.Lock()
	defer c.mu.Unlock()

	n := 0
	for _, e := range c.events {
		if e.user == user && e.target == target {
			n++
		}
	}
	return n
}

// matchEvents returns the MatchEvent unions user received, in order.
func (c *capture) matchEvents(user int32) []protocol.Union {
	c.mu.Lock()
	defer c.mu.Unlock()

	var out []protocol.Union
	for _, e := range c.events {
		if e.user == user && e.target == "MatchEvent" {
			out = append(out, e.args[0].(protocol.Union))
		}
	}
	return out
}

// args returns the arguments of every callback of that name user received.
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

type recordedMatch struct {
	pool  int32
	state protocol.RankedPlayRoomState
}

type fakeServices struct {
	recorded   []recordedMatch
	mu         sync.Mutex
	scores     RankedPlayScores
	scoreCalls int
	bans       map[int32]time.Duration
	results    []MatchResult
	ratings    map[int32]int32
}

func (f *fakeServices) Scores(context.Context, int64) (RankedPlayScores, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	f.scoreCalls++
	out := RankedPlayScores{}
	for id, score := range f.scores {
		out[id] = score
	}
	return out, nil
}

func (f *fakeServices) BanUser(userID int32, d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()

	if f.bans == nil {
		f.bans = map[int32]time.Duration{}
	}
	f.bans[userID] = d
}

func (f *fakeServices) MatchCompleted(_ context.Context, result MatchResult) (map[int32]int32, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	f.results = append(f.results, result)
	return f.ratings, nil
}

func (f *fakeServices) MatchRecorded(poolID int32, state protocol.RankedPlayRoomState) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recorded = append(f.recorded, recordedMatch{poolID, state})
}

func (f *fakeServices) recordedMatches() []recordedMatch {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]recordedMatch(nil), f.recorded...)
}

func (f *fakeServices) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.scoreCalls
}

func (f *fakeServices) banned() map[int32]time.Duration {
	f.mu.Lock()
	defer f.mu.Unlock()

	out := map[int32]time.Duration{}
	for id, d := range f.bans {
		out[id] = d
	}
	return out
}

func (f *fakeServices) reported() []MatchResult {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]MatchResult(nil), f.results...)
}

func (f *fakeServices) setRatings(ratings map[int32]int32) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ratings = ratings
}

func (f *fakeServices) setScores(scores RankedPlayScores) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.scores = scores
}

// --- harness -----------------------------------------------------------------------------------------

type match struct {
	t        *testing.T
	clock    *fakeClock
	ev       *capture
	svc      *fakeServices
	mgr      *Manager
	room     *Room
	password string
}

func makeDeck(n int) []DeckCard {
	cards := make([]DeckCard, n)
	for i := range cards {
		cards[i] = DeckCard{
			Card: protocol.NewCardItem(),
			Item: protocol.PlaylistItem{
				BeatmapID:       int32(1000 + i),
				BeatmapChecksum: fmt.Sprintf("md5-%d", i),
				StarRating:      float64(i%7) + 1,
			},
		}
	}
	return cards
}

// newMatch creates a ranked play room for users 1 (rating 1500, so first to play) and 2 (rating 1600).
func newMatch(t *testing.T, mutate ...func(*RankedPlayParams)) *match {
	t.Helper()

	m := &match{t: t, clock: newFakeClock(), ev: &capture{}, svc: &fakeServices{}}
	m.mgr = NewManager(m.ev, Options{Clock: m.clock, LoadTimeout: time.Hour})

	params := RankedPlayParams{
		Name:     "duel",
		Players:  []RankedPlayPlayer{{UserID: 1, Rating: 1500}, {UserID: 2, Rating: 1600}},
		Deck:     makeDeck(20),
		Services: m.svc,
		Config:   RankedPlayConfig{Rand: rand.New(rand.NewSource(1))},
	}
	for _, f := range mutate {
		f(&params)
	}

	room, password, err := m.mgr.CreateRankedPlay(context.Background(), params)
	if err != nil {
		t.Fatal(err)
	}
	m.room, m.password = room, password
	return m
}

func (m *match) join(users ...int32) {
	m.t.Helper()
	for _, u := range users {
		if _, _, err := m.mgr.Join(m.room.ID(), u, m.password); err != nil {
			m.t.Fatalf("user %d could not join: %v", u, err)
		}
	}
}

// inspect gives a test access to the controller under the room lock.
func (m *match) inspect(f func(rp *rankedPlay)) {
	m.room.mu.Lock()
	defer m.room.mu.Unlock()
	f(m.room.controller.(*rankedPlay))
}

func (m *match) stage() protocol.RankedPlayStage {
	var s protocol.RankedPlayStage
	m.inspect(func(rp *rankedPlay) { s = rp.state.Stage })
	return s
}

func (m *match) hand(user int32) []protocol.CardItem {
	var hand []protocol.CardItem
	m.inspect(func(rp *rankedPlay) { hand = append(hand, rp.state.Users[user].Hand...) })
	return hand
}

func (m *match) life(user int32) int32 {
	var life int32
	m.inspect(func(rp *rankedPlay) { life = rp.state.Users[user].Life })
	return life
}

func (m *match) active() int32 {
	var id int32
	m.inspect(func(rp *rankedPlay) { id = *rp.state.ActiveUserID })
	return id
}

func (m *match) round() int32 {
	var round int32
	m.inspect(func(rp *rankedPlay) { round = rp.state.CurrentRound })
	return round
}

// stageCountdowns returns the running stage countdowns.
func (m *match) stageCountdowns() []protocol.Countdown {
	var out []protocol.Countdown
	m.inspect(func(rp *rankedPlay) {
		for _, cd := range rp.room.countdowns {
			if cd.info.Kind == protocol.CountdownRankedPlayStage {
				out = append(out, cd.info)
			}
		}
	})
	return out
}

func (m *match) advance(d time.Duration) { m.clock.Advance(d) }

func (m *match) wantStage(want protocol.RankedPlayStage) {
	m.t.Helper()
	if got := m.stage(); got != want {
		m.t.Fatalf("stage = %s, want %s", got, want)
	}
}

func (m *match) have(users ...int32) {
	m.t.Helper()
	for _, u := range users {
		must(m.t, m.room.ChangeBeatmapAvailability(u, protocol.BeatmapAvailability{State: protocol.DownloadLocallyAvailable}))
	}
}

func (m *match) setState(state protocol.UserState, users ...int32) {
	m.t.Helper()
	for _, u := range users {
		must(m.t, m.room.ChangeState(u, state))
	}
}

// discardNothing has both players pass their discard, which moves the match on after a short pause.
func (m *match) discardNothing() {
	m.t.Helper()
	must(m.t, m.room.RankedPlayDiscardCards(1, nil))
	must(m.t, m.room.RankedPlayDiscardCards(2, nil))
}

// toCardPlay joins both players and plays the match up to the first turn.
func (m *match) toCardPlay() {
	m.t.Helper()
	m.join(1, 2)
	m.advance(20 * time.Second) // round warmup
	m.wantStage(protocol.StageCardDiscard)
	m.discardNothing()
	m.advance(3 * time.Second)
	m.wantStage(protocol.StageFinishCardDiscard)
	m.advance(5 * time.Second)
	m.wantStage(protocol.StageCardPlay)
}

// toGameplayWarmup has the active player play their first card and both players get the beatmap.
func (m *match) toGameplayWarmup() protocol.CardItem {
	m.t.Helper()
	m.toCardPlay()

	card := m.hand(m.active())[0]
	must(m.t, m.room.RankedPlayPlayCard(m.active(), card))
	m.wantStage(protocol.StageFinishCardPlay)

	m.have(1, 2)
	m.wantStage(protocol.StageGameplayWarmup)
	return card
}

// toGameplay readies both players so gameplay begins.
func (m *match) toGameplay() protocol.CardItem {
	m.t.Helper()
	card := m.toGameplayWarmup()

	m.setState(protocol.UserReady, 1, 2)
	m.advance(10 * time.Second)
	m.wantStage(protocol.StageGameplay)
	return card
}

// playRound runs the loaded gameplay to its end for both players.
func (m *match) playRound() {
	m.t.Helper()
	m.setState(protocol.UserLoaded, 1, 2)
	m.setState(protocol.UserReadyForGameplay, 1, 2)
	m.setState(protocol.UserFinishedPlay, 1, 2)
}

// waitForResults waits until the damage of the results stage has been dealt.
func (m *match) waitForResults() {
	m.t.Helper()
	waitFor(m.t, func() bool {
		var done bool
		m.inspect(func(rp *rankedPlay) { done = rp.state.Users[1].DamageInfo != nil })
		return done
	})
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			t.Fatal("timed out waiting for the room")
		}
		time.Sleep(time.Millisecond)
	}
}

// --- joining -----------------------------------------------------------------------------------------

func TestOnlyThePlayersOfTheMatchCanJoin(t *testing.T) {
	m := newMatch(t)

	_, _, err := m.mgr.Join(m.room.ID(), 3, m.password)
	wantError(t, err, "InvalidStateException", "Not eligible to join this room.")

	_, _, err = m.mgr.Join(m.room.ID(), 1, "wrong")
	wantError(t, err, "InvalidPasswordException", "Invalid password")

	m.join(1)
}

func TestClientsCannotCreateRankedPlayRooms(t *testing.T) {
	mgr, _ := newManager(Options{})

	req := request(newItem(1))
	req.Settings.MatchType = protocol.MatchRankedPlay
	_, _, err := mgr.Create(context.Background(), 1, req)

	wantError(t, err, "InvalidStateException", "This match type is not supported yet.")
}

func TestRankedPlayRoomNeedsTwoPlayersAndACard(t *testing.T) {
	mgr, _ := newManager(Options{})

	_, _, err := mgr.CreateRankedPlay(context.Background(), RankedPlayParams{Players: []RankedPlayPlayer{{UserID: 1}}, Deck: makeDeck(3)})
	wantError(t, err, "InvalidStateException", "A ranked play match needs exactly two players.")

	_, _, err = mgr.CreateRankedPlay(context.Background(), RankedPlayParams{Players: []RankedPlayPlayer{{UserID: 1}, {UserID: 2}}})
	wantError(t, err, "InvalidStateException", "A ranked play match needs cards to play.")
}

func TestPlayersAreDealtAHandAsTheyJoin(t *testing.T) {
	m := newMatch(t)
	m.wantStage(protocol.StageWaitForJoin)

	m.join(1)
	if got := len(m.hand(1)); got != RankedPlayHandSize {
		t.Fatalf("hand = %d cards, want %d", got, RankedPlayHandSize)
	}
	m.wantStage(protocol.StageWaitForJoin)

	m.join(2)
	if got := len(m.hand(2)); got != RankedPlayHandSize {
		t.Fatalf("hand = %d cards, want %d", got, RankedPlayHandSize)
	}

	// Both players see every card being added, but only its owner is told what it is.
	if m.ev.count(2, "RankedPlayCardAdded") != 5 || m.ev.count(1, "RankedPlayCardAdded") != 10 {
		t.Errorf("added: user1=%d user2=%d", m.ev.count(1, "RankedPlayCardAdded"), m.ev.count(2, "RankedPlayCardAdded"))
	}
	if m.ev.count(1, "RankedPlayCardRevealed") != 5 || m.ev.count(2, "RankedPlayCardRevealed") != 5 {
		t.Errorf("revealed: user1=%d user2=%d", m.ev.count(1, "RankedPlayCardRevealed"), m.ev.count(2, "RankedPlayCardRevealed"))
	}

	// What a player is shown of their own card is the beatmap it stands for.
	revealed := m.ev.args(1, "RankedPlayCardRevealed")[0]
	card, item := revealed[0].(protocol.CardItem), revealed[1].(protocol.PlaylistItem)
	if card.ID != m.hand(1)[0].ID || item.BeatmapID < 1000 {
		t.Errorf("revealed %+v / %+v", card, item)
	}
}

func TestMatchBeginsOnceBothPlayersHaveJoined(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	m.wantStage(protocol.StageRoundWarmup)
	if got := m.round(); got != 1 {
		t.Errorf("round = %d, want 1", got)
	}

	// The player with the lower rating goes first.
	if got := m.active(); got != 1 {
		t.Errorf("active player = %d, want 1", got)
	}

	countdowns := m.stageCountdowns()
	if len(countdowns) != 1 || countdowns[0].Stage != protocol.StageRoundWarmup || countdowns[0].TimeRemaining != protocol.Ticks(20*time.Second) {
		t.Errorf("countdowns = %+v", countdowns)
	}
}

func TestStageCountdownsAreAnnouncedAndEndedBeforeTheNextOne(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.ev.reset()

	m.advance(20 * time.Second)

	events := m.ev.matchEvents(1)
	if len(events) != 2 {
		t.Fatalf("match events = %d, want a stop and a start: %+v", len(events), events)
	}
	if events[0].Key != protocol.MatchEventCountdownStopped || events[1].Key != protocol.MatchEventCountdownStarted {
		t.Errorf("event kinds = %d, %d", events[0].Key, events[1].Key)
	}

	started := events[1].Value.([]any)[0].(protocol.Countdown)
	if started.Stage != protocol.StageCardDiscard || started.Kind != protocol.CountdownRankedPlayStage {
		t.Errorf("started %+v", started)
	}
}

func TestJoiningTooSlowlyEndsTheMatch(t *testing.T) {
	m := newMatch(t)
	m.join(1)

	m.advance(60 * time.Second)

	m.wantStage(protocol.StageEnded)
	if results := m.svc.reported(); len(results) != 1 || results[0].Played {
		t.Errorf("results = %+v, want one result of a match that was never played", results)
	}
}

func TestLeavingBeforeTheMatchBeganEndsItWithoutAPenalty(t *testing.T) {
	m := newMatch(t, func(p *RankedPlayParams) { p.Ranked = true })
	m.join(1)

	must(t, m.mgr.Leave(m.room, 1, 1))

	m.wantStage(protocol.StageEnded)
	if bans := m.svc.banned(); len(bans) != 0 {
		t.Errorf("bans = %v, want none", bans)
	}
}

// --- discarding --------------------------------------------------------------------------------------

func TestDiscardingReplacesTheCardsOnce(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.advance(20 * time.Second)

	before := m.hand(1)
	discarded := []protocol.CardItem{before[0], before[2]}
	must(t, m.room.RankedPlayDiscardCards(1, discarded))

	after := m.hand(1)
	if len(after) != RankedPlayHandSize {
		t.Fatalf("hand = %d cards, want %d", len(after), RankedPlayHandSize)
	}
	for _, card := range after {
		if card.ID == discarded[0].ID || card.ID == discarded[1].ID {
			t.Errorf("card %s is still in the hand", card.ID)
		}
	}

	wantError(t, m.room.RankedPlayDiscardCards(1, nil), "InvalidStateException", "Cards have already been discarded.")
}

func TestDiscardingCardsThatAreNotInTheHandIsRefusedAndDoesNotUseUpTheDiscard(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.advance(20 * time.Second)

	opponent := m.hand(2)[0]
	wantError(t, m.room.RankedPlayDiscardCards(1, []protocol.CardItem{opponent}), "InvalidStateException", "One or more cards were not in the hand.")

	own := m.hand(1)[0]
	wantError(t, m.room.RankedPlayDiscardCards(1, []protocol.CardItem{own, own}), "InvalidStateException", "One or more cards were not in the hand.")

	must(t, m.room.RankedPlayDiscardCards(1, []protocol.CardItem{own}))
}
