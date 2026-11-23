package matchmaking

import (
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

func (h *harness) issueDuel(from, to, pool int32) string {
	h.t.Helper()

	h.notes.reset()
	if err := h.svc.IssueDuel(h.ctx, from, protocol.IssueDuelRequest{UserID: to, PoolID: pool}); err != nil {
		h.t.Fatalf("duel from %d to %d: %v", from, to, err)
	}

	issued := h.notes.args(to, "MatchmakingDuelIssued")
	if len(issued) != 1 {
		h.t.Fatalf("the challenged player got %d invitations, want 1 (%v)", len(issued), h.notes.targets(to))
	}
	return issued[0][0].(protocol.DuelIssuedParams).ID
}

func TestIssuingADuelQueuesTheChallengerAndInvitesTheOther(t *testing.T) {
	h := newHarness(t)

	if err := h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 2, PoolID: 1}); err != nil {
		t.Fatal(err)
	}

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
	eq(t, h.notes.targets(2), []string{"MatchmakingDuelIssued"})

	issued := h.notes.args(2, "MatchmakingDuelIssued")[0][0].(protocol.DuelIssuedParams)
	if len(issued.ID) != 36 || issued.UserID != 1 || issued.Pool.ID != 1 || issued.Pool.Name != "1v1 Nomod" || issued.Pool.Type != protocol.PoolRankedPlay {
		t.Errorf("invitation = %+v", issued)
	}
}

func TestAnAcceptedDuelIsAnUnrankedMatchThatPunishesNobody(t *testing.T) {
	h := newHarness(t)
	id := h.issueDuel(1, 2, 1)

	if err := h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: id}); err != nil {
		t.Fatal(err)
	}
	eq(t, h.notes.targets(2), []string{"MatchmakingDuelIssued", "MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})

	h.svc.Tick()
	h.svc.AcceptInvitation(1)
	h.svc.AcceptInvitation(2)
	h.svc.Wait()

	ready := h.notes.args(1, "MatchmakingRoomReady")
	if len(ready) != 1 {
		t.Fatalf("the challenger was sent to %d rooms", len(ready))
	}
	roomID, password := ready[0][0].(int64), ready[0][1].(string)
	for _, u := range []int32{1, 2} {
		if _, _, err := h.rooms.Join(roomID, u, password); err != nil {
			t.Fatal(err)
		}
	}

	r, _ := h.rooms.Get(roomID)
	if err := h.rooms.Leave(r, 2, 2); err != nil {
		t.Fatal(err)
	}

	results := h.backend.recorded()
	if len(results) != 1 || results[0].Ranked || results[0].PoolID != 1 {
		t.Fatalf("results = %+v, want one unranked result of pool 1", results)
	}
	if !h.svc.Bans().EndOf(2).IsZero() {
		t.Error("leaving a duel must not ban the player")
	}
}

func TestDuelsAreLimitedToOneEveryThirtySeconds(t *testing.T) {
	h := newHarness(t)
	h.issueDuel(1, 2, 1)

	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 3, PoolID: 1}), "You are requesting too many duels. Slow down.")

	h.clock.Advance(29 * time.Second)
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 3, PoolID: 1}), "You are requesting too many duels. Slow down.")

	h.clock.Advance(2 * time.Second)
	h.issueDuel(1, 3, 1)

	// The limit is per player.
	h.issueDuel(2, 4, 1)
}

func TestDuelRefusals(t *testing.T) {
	h := newHarness(t)

	h.wantError(h.svc.IssueDuel(h.ctx, 5, protocol.IssueDuelRequest{UserID: 2, PoolID: 1}), "Can't duel when restricted.")
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 1, PoolID: 1}), "You can't duel yourself.")
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 99, PoolID: 1}), "The player you want to duel could not be found.")
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 2, PoolID: 99}), "Pool not found: 99")
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 2, PoolID: 3}), "Only ranked play pools can be queued for.")
	h.wantError(h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 2, PoolID: 4}), "The selected matchmaking pool is no longer active.")

	if len(h.notes.targets(1))+len(h.notes.targets(2)) != 0 {
		t.Error("a refused duel must not tell anybody anything")
	}
}

func TestAcceptingADuelThatIsGoneIsNotAnError(t *testing.T) {
	h := newHarness(t)

	if err := h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: protocol.NewGUID()}); err != nil {
		t.Errorf("accepting a duel that does not exist: %v", err)
	}
	if len(h.notes.targets(2)) != 0 {
		t.Error("nothing should happen")
	}
}

func TestDuelsIgnoreRatingsAndQueueBans(t *testing.T) {
	h := newHarness(t)
	h.dir.ratings[[2]int32{1, 1}] = Rating{Mu: 600, Sigma: 150}
	h.dir.ratings[[2]int32{1, 2}] = Rating{Mu: 2900, Sigma: 150}
	h.svc.Bans().Ban(1, time.Hour) // both the challenger and the challenged player are banned from the queue
	h.svc.Bans().Ban(2, time.Hour)

	id := h.issueDuel(1, 2, 1)
	if err := h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: id}); err != nil {
		t.Fatal(err)
	}
	h.notes.reset()

	h.svc.Tick()

	for _, u := range []int32{1, 2} {
		if h.notes.count(u, "MatchmakingRoomInvitedWithParams") != 1 {
			t.Errorf("user %d was not invited: %v", u, h.notes.targets(u))
		}
	}
}

func TestDecliningADuelRemovesBothPlayersAndBansNobody(t *testing.T) {
	h := newHarness(t)
	id := h.issueDuel(1, 2, 1)
	_ = h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: id})
	h.svc.Tick()
	h.notes.reset()

	h.svc.DeclineInvitation(2)

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})
	eq(t, h.notes.targets(2), []string{"MatchmakingQueueLeft"})
	if !h.svc.Bans().EndOf(2).IsZero() {
		t.Error("declining a duel must not ban the player")
	}
}

func TestADuelNobodyAcceptsIsDroppedAfterFiveMinutes(t *testing.T) {
	h := newHarness(t)
	h.issueDuel(1, 2, 1)
	h.notes.reset()

	h.clock.Advance(5*time.Minute + time.Second)
	h.svc.Tick()

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft"})

	h.svc.Tick()
	if len(h.svc.duels) != 0 {
		t.Errorf("%d duels are still kept", len(h.svc.duels))
	}
}

func TestIssuingADuelLeavesTheQueue(t *testing.T) {
	h := newHarness(t)
	h.join(1, 1)
	h.notes.reset()

	if err := h.svc.IssueDuel(h.ctx, 1, protocol.IssueDuelRequest{UserID: 2, PoolID: 2}); err != nil {
		t.Fatal(err)
	}

	eq(t, h.notes.targets(1), []string{"MatchmakingQueueLeft", "MatchmakingQueueJoined", "MatchmakingQueueStatusChanged"})
	if h.svc.queues[1].Has(1) {
		t.Error("the challenger is still in the ranked queue")
	}
}

func TestAcceptingADuelLeavesTheQueueAndOtherDuels(t *testing.T) {
	h := newHarness(t)
	h.join(1, 2)

	first := h.issueDuel(3, 2, 1)
	h.clock.Advance(time.Minute)
	second := h.issueDuel(4, 2, 1)

	h.notes.reset()
	if err := h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: first}); err != nil {
		t.Fatal(err)
	}
	if h.svc.queues[1].Has(2) {
		t.Error("the player is still in the ranked queue")
	}

	// Accepting the other duel moves them there.
	if err := h.svc.AcceptDuel(h.ctx, 2, protocol.AcceptDuelRequest{ID: second}); err != nil {
		t.Fatal(err)
	}
	if h.svc.duels[first].Has(2) || !h.svc.duels[second].Has(2) {
		t.Error("the player must only be in the duel they accepted last")
	}
}
