package room

import (
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// Rules that the round-by-round tests in rankedplay_flow_test.go cannot tell apart, because their numbers are
// round ones.

func TestDamageIsRoundedUp(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	var info protocol.DamageInfo
	m.inspect(func(rp *rankedPlay) { info = rp.damage(2, 333_333, 1.5, 10_000) })

	// 333,333 * 1.5 = 499,999.5, which counts as 500,000; the round's bonus damage is not scaled.
	if info.Damage != 510_000 || info.RawDamage != 343_333 || info.NewLife != 490_000 || info.OldLife != 1_000_000 {
		t.Errorf("damage = %+v", info)
	}
}

func TestNobodyIsKilledFromFullLifeInOneHit(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	var info protocol.DamageInfo
	m.inspect(func(rp *rankedPlay) { info = rp.damage(2, 5_000_000, 1, 0) })
	if info.NewLife != 1 {
		t.Errorf("life after a huge hit from full life = %d, want 1", info.NewLife)
	}

	// Already hurt, the same hit kills.
	m.inspect(func(rp *rankedPlay) { info = rp.damage(2, 5_000_000, 1, 0) })
	if info.NewLife != 0 {
		t.Errorf("life after a second huge hit = %d, want 0", info.NewLife)
	}
}

func TestActivePlayerDrawsACardFromTheirSecondTurnOn(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	// Round 1: player 1 plays, nobody downloads the beatmap, the round is skipped and the played card is spent.
	must(t, m.room.RankedPlayPlayCard(1, m.hand(1)[0]))
	m.advance(2 * time.Minute)
	if got := len(m.hand(1)); got != RankedPlayHandSize-1 {
		t.Fatalf("hand after the first round = %d, want %d", got, RankedPlayHandSize-1)
	}

	// Round 2 is player 2's first turn: no card is drawn.
	m.wantStage(protocol.StageCardPlay)
	if got := len(m.hand(2)); got != RankedPlayHandSize {
		t.Fatalf("player 2's hand in round 2 = %d, want %d", got, RankedPlayHandSize)
	}

	must(t, m.room.RankedPlayPlayCard(2, m.hand(2)[0]))
	m.advance(2 * time.Minute)

	// Round 3 is player 1's second turn: a card is drawn to make up for the spent one.
	m.wantStage(protocol.StageCardPlay)
	if m.round() != 3 || m.active() != 1 {
		t.Fatalf("round %d, active player %d, want round 3 and player 1", m.round(), m.active())
	}
	if got := len(m.hand(1)); got != RankedPlayHandSize {
		t.Errorf("player 1's hand in round 3 = %d, want %d", got, RankedPlayHandSize)
	}
	if got := len(m.hand(2)); got != RankedPlayHandSize-1 {
		t.Errorf("player 2's hand in round 3 = %d, want %d", got, RankedPlayHandSize-1)
	}
}

// A timer that was already on its way when its countdown was stopped must not run the countdown's action.
func TestStoppedCountdownDoesNothingWhenItsTimerStillFires(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	called := false
	m.room.lock()
	cd := m.room.startCountdown(protocol.CountdownMatchStart, 0, time.Minute, func() { called = true })
	m.room.stopCountdown(cd)
	m.room.unlock()

	m.room.countdownElapsed(cd)

	if called {
		t.Error("the action of a stopped countdown ran")
	}
}

func TestCountdownRunsItsActionWhenItElapses(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	called := false
	m.room.lock()
	m.room.startCountdown(protocol.CountdownMatchStart, 0, time.Minute, func() { called = true })
	m.room.unlock()

	m.advance(59 * time.Second)
	if called {
		t.Fatal("ran too early")
	}
	m.advance(time.Second)
	if !called {
		t.Error("the action did not run")
	}
}

func TestFinishedMatchIsRecordedWithItsPoolAndFinalRatings(t *testing.T) {
	m := newMatch(t, func(p *RankedPlayParams) {
		p.Ranked = true
		p.PoolID = 7
	})
	m.svc.setScores(RankedPlayScores{1: 900_000, 2: 700_000})
	m.svc.setRatings(map[int32]int32{1: 1512, 2: 1588})
	m.toGameplay()
	m.inspect(func(rp *rankedPlay) { rp.state.Users[2].Life = 100_000 })

	m.playRound()
	m.waitForResults()

	results := m.svc.reported()
	recorded := m.svc.recordedMatches()
	if len(results) != 1 || results[0].PoolID != 7 {
		t.Fatalf("results = %+v, want one result carrying pool 7", results)
	}
	if len(recorded) != 1 || recorded[0].pool != 7 {
		t.Fatalf("recorded = %+v, want one match of pool 7", recorded)
	}

	state := recorded[0].state
	if state.Users[1].RatingAfter != 1512 || state.Users[2].RatingAfter != 1588 || state.WinningUserID == nil || *state.WinningUserID != 1 {
		t.Errorf("recorded state lacks the final ratings or the winner: %+v", state)
	}

	// The record is a copy: the room going on does not change it.
	m.inspect(func(rp *rankedPlay) { rp.state.Users[1].Life = 12345 })
	if state.Users[1].Life == 12345 {
		t.Error("the recorded state shares memory with the room")
	}
}

func TestAMatchThatNeverStartedIsStillRecordedOnce(t *testing.T) {
	m := newMatch(t)
	m.join(1)
	m.advance(60 * time.Second)

	if got := len(m.svc.recordedMatches()); got != 1 {
		t.Errorf("recorded %d matches, want 1", got)
	}
}
