package matchmaking

import (
	"math/rand"
	"testing"
	"time"
)

type fakeClock struct{ now time.Time }

func newClock() *fakeClock { return &fakeClock{now: time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)} }

func (c *fakeClock) Now() time.Time          { return c.now }
func (c *fakeClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

func testPool() Pool {
	return Pool{
		ID: 1, Name: "1v1 Nomod", Active: true, Ranked: true,
		LobbySize: 2, SearchRadius: 200, SearchRadiusMax: 9999, SearchRadiusExp: 30,
	}
}

func newTestQueue(pool Pool, clock *fakeClock, mutate ...func(*QueueOptions)) *Queue {
	opts := QueueOptions{
		RecentMatchupTimeout: 10 * time.Minute,
		KeepInQueueOnDecline: true,
		AllowBans:            true,
		Clock:                clock,
		Rand:                 rand.New(rand.NewSource(1)),
	}
	for _, f := range mutate {
		f(&opts)
	}
	return NewQueue(pool, opts)
}

func player(id int32, mu float64) *QueueUser {
	return &QueueUser{UserID: id, Rating: Rating{Mu: mu, Sigma: 150}}
}

func ids(users []*QueueUser) []int32 {
	out := make([]int32, len(users))
	for i, u := range users {
		out[i] = u.UserID
	}
	return out
}

func TestPlayersWithinTheSearchRadiusArePaired(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1500))
	q.Add(player(2, 1600))

	bundle := q.Update()

	if len(bundle.Formed) != 1 || len(bundle.Formed[0].Users) != 2 {
		t.Fatalf("formed = %+v, want one group of two", bundle.Formed)
	}
	for _, u := range q.Users() {
		if u.Group == nil || !u.InviteStart.Equal(clock.Now()) {
			t.Errorf("user %d was not invited: %+v", u.UserID, u)
		}
	}
	if q.Count() != 2 {
		t.Error("invited players stay in the queue until they accept")
	}
}

func TestPlayersFarApartWaitUntilTheSearchRadiusHasGrown(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1500))
	q.Add(player(2, 2300))

	if got := q.Update(); len(got.Formed) != 0 {
		t.Fatalf("800 rating points apart must not be paired at first: %+v", got.Formed)
	}

	// The radius doubles every 30 seconds: after a minute it is four times as wide.
	clock.Advance(60 * time.Second)
	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("after a minute they should be paired, formed = %d", len(got.Formed))
	}
}

func TestSearchRadiusNeverExceedsTheMaximum(t *testing.T) {
	clock := newClock()
	pool := testPool()
	pool.SearchRadiusMax = 300
	q := newTestQueue(pool, clock)
	q.SetPool(pool, 2000) // top 100 rating: no extra reach for these players
	q.Add(player(1, 1500))
	q.Add(player(2, 2100))

	clock.Advance(24 * time.Hour)

	if got := q.Update(); len(got.Formed) != 0 {
		t.Errorf("600 apart with a maximum of 300 must never pair: %+v", got.Formed)
	}
}

func TestTheBestPlayersCanAlwaysBePaired(t *testing.T) {
	clock := newClock()
	pool := testPool()
	pool.SearchRadiusMax = 300
	q := newTestQueue(pool, clock)
	q.SetPool(pool, 1000) // the 100th player is rated 1000, so these two are far above it
	q.Add(player(1, 2000))
	q.Add(player(2, 2900))

	clock.Advance(10 * time.Minute)

	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("players above the top 100 rating can be paired whatever the maximum, formed = %d", len(got.Formed))
	}
}

func TestRecentOpponentsAreNotPairedAgainUntilTheTimeoutPassed(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.MarkRecentMatchup(2, 1)

	if got := q.Update(); len(got.Formed) != 0 {
		t.Fatalf("recent opponents were paired: %+v", got.Formed)
	}

	clock.Advance(10*time.Minute + time.Second)
	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("after the timeout they may meet again, formed = %d", len(got.Formed))
	}
}

func TestRecentOpponentsAreIgnoredWhenBansAreOff(t *testing.T) {
	q := newTestQueue(testPool(), newClock(), func(o *QueueOptions) { o.AllowBans = false })
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.MarkRecentMatchup(1, 2)

	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("formed = %d, want 1", len(got.Formed))
	}
}

func TestBannedPlayersAreNotPairedUntilTheirBanEnds(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)

	banned := player(2, 1500)
	banned.BanEnd = clock.Now().Add(time.Minute)
	q.Add(player(1, 1500))
	q.Add(banned)

	if got := q.Update(); len(got.Formed) != 0 {
		t.Fatalf("a banned player was paired: %+v", got.Formed)
	}

	clock.Advance(61 * time.Second)
	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("formed = %d, want 1 once the ban ended", len(got.Formed))
	}
}

func TestOnlyAsManyPlayersAsTheLobbyHoldsArePaired(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	for i, mu := range []float64{1500, 1510, 1520} {
		q.Add(player(int32(i+1), mu))
	}

	bundle := q.Update()

	if len(bundle.Formed) != 1 {
		t.Fatalf("formed = %d, want 1", len(bundle.Formed))
	}
	invited := 0
	for _, u := range q.Users() {
		if u.Group != nil {
			invited++
		}
	}
	if invited != 2 {
		t.Errorf("%d players invited, want 2", invited)
	}
}

func TestFourPlayersMakeTwoGroupsWithNobodyTwice(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	for i, mu := range []float64{1500, 1510, 1520, 1530} {
		q.Add(player(int32(i+1), mu))
	}

	bundle := q.Update()

	if len(bundle.Formed) != 2 {
		t.Fatalf("formed = %d, want 2", len(bundle.Formed))
	}
	seen := map[int32]bool{}
	for _, g := range bundle.Formed {
		for _, u := range g.Users {
			if seen[u.UserID] {
				t.Errorf("user %d is in two groups", u.UserID)
			}
			seen[u.UserID] = true
		}
	}
	if len(seen) != 4 {
		t.Errorf("only %d of 4 players were paired", len(seen))
	}
}

func TestInvitedPlayersAreNotPairedAgain(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()

	if got := q.Update(); len(got.Formed) != 0 {
		t.Errorf("a second group was formed from invited players: %+v", got.Formed)
	}
}

func TestGroupCompletesOnceEveryoneAccepted(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()

	first := q.MarkInvitationAccepted(1)
	if len(first.Completed) != 0 {
		t.Fatal("the group completed before the second player accepted")
	}

	second := q.MarkInvitationAccepted(2)
	if len(second.Completed) != 1 || len(second.Completed[0].Users) != 2 {
		t.Fatalf("completed = %+v", second.Completed)
	}
	if q.Count() != 0 {
		t.Error("the players of a completed group leave the queue")
	}
}

func TestAcceptingWithoutAnInvitationDoesNothing(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))

	if b := q.MarkInvitationAccepted(1); len(b.Completed) != 0 {
		t.Error("completed a group that was never formed")
	}
	if b := q.MarkInvitationAccepted(99); len(b.Completed) != 0 || len(b.Removed) != 0 {
		t.Error("an unknown player changed something")
	}
}

func TestDecliningRemovesTheDeclinerAndPutsTheOtherBackToSearching(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()

	bundle := q.MarkInvitationDeclined(1)

	if got := ids(bundle.Removed); len(got) != 1 || got[0] != 1 {
		t.Errorf("removed = %v, want [1]", got)
	}
	if got := ids(bundle.Declined); len(got) != 1 || got[0] != 1 {
		t.Errorf("declined = %v, want [1]", got)
	}
	if got := ids(bundle.Added); len(got) != 1 || got[0] != 2 {
		t.Errorf("added = %v, want player 2 searching again", got)
	}
	if len(bundle.Recycled) != 1 {
		t.Errorf("recycled = %d groups, want 1", len(bundle.Recycled))
	}

	other := q.Users()[0]
	if other.UserID != 2 || other.Group != nil || other.InviteAccepted {
		t.Errorf("the other player was not reset: %+v", other)
	}
}

func TestDecliningADuelRemovesBothPlayers(t *testing.T) {
	q := newTestQueue(testPool(), newClock(), func(o *QueueOptions) { o.KeepInQueueOnDecline = false })
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()

	bundle := q.MarkInvitationDeclined(1)

	if len(bundle.Removed) != 2 || q.Count() != 0 || len(bundle.Added) != 0 {
		t.Errorf("removed %v, queue %d, added %v", ids(bundle.Removed), q.Count(), ids(bundle.Added))
	}
}

func TestInvitationsExpireAfterThirtySeconds(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()
	q.MarkInvitationAccepted(1)

	clock.Advance(30 * time.Second)
	if b := q.Update(); len(b.Removed) != 0 {
		t.Fatal("expired exactly at the timeout; it must be after")
	}

	clock.Advance(time.Second)
	bundle := q.Update()

	// Only the player who did not accept is at fault.
	if got := ids(bundle.Declined); len(got) != 1 || got[0] != 2 {
		t.Errorf("declined = %v, want [2]", got)
	}
	if got := ids(bundle.Added); len(got) != 1 || got[0] != 1 {
		t.Errorf("the player who accepted goes back to searching, added = %v", got)
	}
}

func TestSearchesExpireAfterTheSearchTimeout(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock, func(o *QueueOptions) { o.SearchTimeout = 5 * time.Minute })
	q.Add(player(1, 1500))

	clock.Advance(5 * time.Minute)
	if b := q.Update(); len(b.Removed) != 0 {
		t.Fatal("expired exactly at the timeout; it must be after")
	}

	clock.Advance(time.Second)
	bundle := q.Update()
	if len(bundle.Removed) != 1 || len(bundle.Declined) != 0 {
		t.Errorf("removed %v declined %v: a search that timed out is not a decline", ids(bundle.Removed), ids(bundle.Declined))
	}
}

func TestLeavingDuringAnInvitationCountsAsDeclining(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))
	q.Add(player(2, 1500))
	q.Update()

	if got := q.Remove(1); len(got.Declined) != 1 {
		t.Errorf("declined = %v, want the leaver", ids(got.Declined))
	}

	solo := newTestQueue(testPool(), newClock())
	solo.Add(player(3, 1500))
	if got := solo.Remove(3); len(got.Declined) != 0 || len(got.Removed) != 1 {
		t.Errorf("leaving the queue while searching is not a decline: %+v", got)
	}
}

func TestAddingAPlayerTwiceKeepsTheirSearchTime(t *testing.T) {
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1500))
	clock.Advance(time.Minute)

	if got := q.Add(player(1, 1500)); len(got.Added) != 0 {
		t.Error("a player already in the queue was added again")
	}
	if !q.Users()[0].SearchStart.Equal(clock.Now().Add(-time.Minute)) {
		t.Error("their search time was reset")
	}
}

func TestClearReportsEveryone(t *testing.T) {
	q := newTestQueue(testPool(), newClock())
	q.Add(player(1, 1500))
	q.Add(player(2, 2500))

	bundle := q.Clear()

	if len(bundle.Removed) != 2 || q.Count() != 0 {
		t.Errorf("removed %v, queue %d", ids(bundle.Removed), q.Count())
	}
	if again := q.Clear(); len(again.Removed) != 0 {
		t.Error("clearing an empty queue reported players")
	}
}

func TestPoolSettingsCanBeEditedWhileTheQueueRuns(t *testing.T) {
	clock := newClock()
	pool := testPool()
	q := newTestQueue(pool, clock)
	q.Add(player(1, 1500))
	q.Add(player(2, 1900))

	if got := q.Update(); len(got.Formed) != 0 {
		t.Fatal("400 apart with a radius of 200 must not pair")
	}

	pool.SearchRadius = 1000
	q.SetPool(pool, 0)
	if got := q.Update(); len(got.Formed) != 1 {
		t.Error("the wider radius was not picked up")
	}
}

func TestPlayersExactlyAtTheSearchRadiusArePaired(t *testing.T) {
	// The maximum caps the radius at 300 whatever the players' ratings, so both of them search exactly that far.
	pool := testPool()
	pool.SearchRadius = 10_000
	pool.SearchRadiusMax = 300

	paired := func(a, b float64) bool {
		q := newTestQueue(pool, newClock())
		q.SetPool(pool, 5000) // no top 100 bonus
		q.Add(player(1, a))
		q.Add(player(2, b))
		return len(q.Update().Formed) == 1
	}

	if !paired(1500, 1800) {
		t.Error("300 apart with a radius of exactly 300 must pair")
	}
	if paired(1500, 1801) {
		t.Error("301 apart with a radius of 300 must not pair")
	}
}

func TestPlayersAwayFromTheAverageSearchWider(t *testing.T) {
	// The same 300 rating points apart: too far near the average, fine well above it, where there are few players.
	near := newTestQueue(testPool(), newClock())
	near.Add(player(1, 1500))
	near.Add(player(2, 1800))
	if got := near.Update(); len(got.Formed) != 0 {
		t.Errorf("300 apart near the average must not pair, formed = %d", len(got.Formed))
	}

	far := newTestQueue(testPool(), newClock())
	far.Add(player(3, 2300))
	far.Add(player(4, 2600))
	if got := far.Update(); len(got.Formed) != 1 {
		t.Errorf("300 apart far above the average must pair, formed = %d", len(got.Formed))
	}
}

func TestTheSearchRadiusDoublesEveryExpPeriod(t *testing.T) {
	// Equally far from the average, so that both players have the same rating bonus (about 1.12): their radius
	// is 223 to begin with and doubles every 30 seconds.
	clock := newClock()
	q := newTestQueue(testPool(), clock)
	q.Add(player(1, 1250))
	q.Add(player(2, 1750)) // 500 apart

	clock.Advance(30 * time.Second) // radius 447
	if got := q.Update(); len(got.Formed) != 0 {
		t.Fatalf("after one period the radius is 447, which is not enough for 500: formed = %d", len(got.Formed))
	}

	clock.Advance(15 * time.Second) // radius 223 * 2^1.5 = 632
	if got := q.Update(); len(got.Formed) != 1 {
		t.Errorf("after 45 seconds the radius is 632, formed = %d", len(got.Formed))
	}
}
