package room

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
)

// The harness (newMatch and friends) is in rankedplay_test.go.

func TestDiscardPhaseEndsShortlyAfterBothHaveDiscarded(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.advance(20 * time.Second)

	must(t, m.room.RankedPlayDiscardCards(1, nil))
	m.wantStage(protocol.StageCardDiscard)
	if countdowns := m.stageCountdowns(); len(countdowns) != 1 || countdowns[0].TimeRemaining != protocol.Ticks(30*time.Second) {
		t.Fatalf("with one player left to discard the stage keeps its own countdown, got %+v", countdowns)
	}

	must(t, m.room.RankedPlayDiscardCards(2, nil))
	if countdowns := m.stageCountdowns(); len(countdowns) != 1 || countdowns[0].TimeRemaining != protocol.Ticks(3*time.Second) {
		t.Fatalf("once both discarded a short countdown replaces it, got %+v", countdowns)
	}

	m.advance(2 * time.Second)
	m.wantStage(protocol.StageCardDiscard)
	m.advance(time.Second)
	m.wantStage(protocol.StageFinishCardDiscard)
	m.advance(5 * time.Second)
	m.wantStage(protocol.StageCardPlay)
}

func TestDiscardPhaseEndsWhenTimeRunsOut(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.advance(20 * time.Second)

	m.advance(29 * time.Second)
	m.wantStage(protocol.StageCardDiscard)
	m.advance(time.Second)
	m.wantStage(protocol.StageFinishCardDiscard)
}

// --- playing a card ----------------------------------------------------------------------------------

func TestOnlyTheActivePlayerCanPlayACard(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	wantError(t, m.room.RankedPlayPlayCard(2, m.hand(2)[0]), "InvalidStateException", "Not the active player.")
	wantError(t, m.room.RankedPlayPlayCard(1, m.hand(2)[0]), "InvalidStateException", "Card not in the hand.")
	m.wantStage(protocol.StageCardPlay)
}

func TestPlayingACardMakesItsBeatmapTheCurrentItem(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()
	m.ev.reset()

	played := m.hand(1)[1]
	var wantBeatmap int32
	m.inspect(func(rp *rankedPlay) { wantBeatmap = rp.effects[played.ID].BeatmapID })

	must(t, m.room.RankedPlayPlayCard(1, played))
	m.wantStage(protocol.StageFinishCardPlay)

	for _, user := range []int32{1, 2} {
		for _, target := range []string{"RankedPlayCardRevealed", "RankedPlayCardPlayed", "PlaylistItemAdded", "SettingsChanged", "PlaylistItemRemoved"} {
			if m.ev.count(user, target) != 1 {
				t.Errorf("user %d received %s %d times, want once", user, target, m.ev.count(user, target))
			}
		}
	}
	if removed := m.ev.args(1, "PlaylistItemRemoved")[0][0]; removed != int64(0) {
		t.Errorf("the placeholder item (0) must be removed, got %v", removed)
	}

	snapshot := m.room.Snapshot()
	if len(snapshot.Playlist) != 1 || snapshot.Playlist[0].BeatmapID != wantBeatmap || snapshot.Settings.PlaylistItemID != snapshot.Playlist[0].ID || snapshot.Playlist[0].ID == 0 {
		t.Errorf("playlist = %+v, settings item = %d", snapshot.Playlist, snapshot.Settings.PlaylistItemID)
	}

	// The card is only spent when the round is over.
	if got := len(m.hand(1)); got != RankedPlayHandSize {
		t.Errorf("hand = %d cards, want %d", got, RankedPlayHandSize)
	}
}

func TestPlayingTheSameCardTwiceDoesNothingTheSecondTime(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	card := m.hand(1)[0]
	must(t, m.room.RankedPlayPlayCard(1, card))
	must(t, m.room.RankedPlayPlayCard(1, card)) // no longer the card play stage: ignored

	if m.ev.count(1, "RankedPlayCardPlayed") != 1 {
		t.Error("the card was played twice")
	}
}

func selectFrame(cardID string) []protocol.HandReplayFrame {
	return []protocol.HandReplayFrame{{Delay: 1, Cards: map[string]protocol.CardState{cardID: {Hovered: true, Selected: true}}}}
}

func TestCardPlayTimeoutPlaysTheCardThePlayerHadSelected(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	selected := m.hand(1)[3]
	var wantBeatmap int32
	m.inspect(func(rp *rankedPlay) { wantBeatmap = rp.effects[selected.ID].BeatmapID })

	must(t, m.room.HandleMatchRequest(1, protocol.MatchRequestCardHandReplay, &protocol.HandReplayRequest{Frames: selectFrame(selected.ID)}))
	m.advance(45 * time.Second)

	m.wantStage(protocol.StageFinishCardPlay)
	if got := m.room.CurrentItem().BeatmapID; got != wantBeatmap {
		t.Errorf("played beatmap %d, want that of the selected card (%d)", got, wantBeatmap)
	}
}

func TestCardPlayTimeoutPlaysTheFirstCardWhenNothingWasSelected(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	first := m.hand(1)[0]
	var wantBeatmap int32
	m.inspect(func(rp *rankedPlay) { wantBeatmap = rp.effects[first.ID].BeatmapID })

	m.advance(45 * time.Second)

	if got := m.room.CurrentItem().BeatmapID; got != wantBeatmap {
		t.Errorf("played beatmap %d, want that of the first card (%d)", got, wantBeatmap)
	}
}

func TestHandMovementsAreRelayedToBothPlayers(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()
	m.ev.reset()

	frames := selectFrame(m.hand(1)[0].ID)
	must(t, m.room.HandleMatchRequest(1, protocol.MatchRequestCardHandReplay, &protocol.HandReplayRequest{Frames: frames}))

	for _, user := range []int32{1, 2} {
		events := m.ev.matchEvents(user)
		if len(events) != 1 || events[0].Key != protocol.MatchEventCardHandReplay {
			t.Fatalf("user %d got %+v", user, events)
		}
		if relayed := events[0].Value.([]any); relayed[0] != int32(1) {
			t.Errorf("event names user %v, want 1", relayed[0])
		}
	}
}

func TestOpponentsHandMovementsDoNotChangeWhatIsPlayed(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	first := m.hand(1)[0]
	var wantBeatmap int32
	m.inspect(func(rp *rankedPlay) { wantBeatmap = rp.effects[first.ID].BeatmapID })

	must(t, m.room.HandleMatchRequest(2, protocol.MatchRequestCardHandReplay, &protocol.HandReplayRequest{Frames: selectFrame(m.hand(2)[4].ID)}))
	m.advance(45 * time.Second)

	if got := m.room.CurrentItem().BeatmapID; got != wantBeatmap {
		t.Errorf("played beatmap %d, want the active player's first card (%d)", got, wantBeatmap)
	}
}

func TestOtherMatchRequestsAreIgnoredAndOutsidersRefused(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()
	m.ev.reset()

	must(t, m.room.HandleMatchRequest(1, protocol.MatchRequestStartCountdown, nil))
	if len(m.ev.matchEvents(1)) != 0 {
		t.Error("a request that is not a hand replay must not be relayed")
	}

	wantError(t, m.room.HandleMatchRequest(3, protocol.MatchRequestCardHandReplay, &protocol.HandReplayRequest{}), "InvalidStateException", "User is not in the room.")
}

func TestRankedPlayOperationsNeedARankedPlayRoom(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)

	wantError(t, r.RankedPlayPlayCard(1, protocol.NewCardItem()), "InvalidStateException", "This is not a ranked play room.")
	wantError(t, r.RankedPlayDiscardCards(1, nil), "InvalidStateException", "This is not a ranked play room.")
	if r.IsRankedPlay() {
		t.Error("a head to head room is not a ranked play room")
	}
}

// --- waiting for the beatmap and for the players -----------------------------------------------------

func TestRoundWaitsForBothPlayersToHaveTheBeatmap(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()
	must(t, m.room.RankedPlayPlayCard(1, m.hand(1)[0]))

	m.have(1)
	m.wantStage(protocol.StageFinishCardPlay)

	m.have(2)
	m.wantStage(protocol.StageGameplayWarmup)
}

func TestGameplayWarmupStartsAShortCountdownOnceEveryoneIsReady(t *testing.T) {
	m := newMatch(t)
	m.toGameplayWarmup()

	m.setState(protocol.UserReady, 1)
	if countdowns := m.stageCountdowns(); len(countdowns) != 1 || countdowns[0].TimeRemaining != protocol.Ticks(2*time.Minute) {
		t.Fatalf("with one player ready the stage keeps its own countdown, got %+v", countdowns)
	}

	m.setState(protocol.UserReady, 2)
	if countdowns := m.stageCountdowns(); len(countdowns) != 1 || countdowns[0].TimeRemaining != protocol.Ticks(10*time.Second) {
		t.Fatalf("once both are ready a short countdown replaces it, got %+v", countdowns)
	}

	m.advance(10 * time.Second)
	m.wantStage(protocol.StageGameplay)
}

func TestPlayerWhoIsNotReadyTakesDamageAndTheRoundIsSkipped(t *testing.T) {
	m := newMatch(t)
	played := m.toGameplayWarmup()
	m.ev.reset()

	m.setState(protocol.UserReady, 1)
	m.advance(2 * time.Minute)

	// 100,000 scaled by the room's multiplier of the first round (0.5).
	if got := m.life(2); got != 950_000 {
		t.Errorf("absent player's life = %d, want 950000", got)
	}
	if got := m.life(1); got != 1_000_000 {
		t.Errorf("ready player's life = %d, want it untouched", got)
	}

	// The spent card leaves the hand, and the next round starts with the other player's turn.
	for _, card := range m.hand(1) {
		if card.ID == played.ID {
			t.Error("the played card is still in the hand")
		}
	}
	m.wantStage(protocol.StageCardPlay)
	if m.round() != 2 || m.active() != 2 {
		t.Errorf("round %d, active player %d, want round 2 and player 2", m.round(), m.active())
	}
}

func TestNobodyHavingTheBeatmapSkipsTheRoundWithoutDamage(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()
	must(t, m.room.RankedPlayPlayCard(1, m.hand(1)[0]))

	m.advance(2 * time.Minute)

	if m.life(1) != 1_000_000 || m.life(2) != 1_000_000 {
		t.Errorf("lives %d, %d, want nobody hurt", m.life(1), m.life(2))
	}
	m.wantStage(protocol.StageCardPlay)
	if m.round() != 2 {
		t.Errorf("round = %d, want 2", m.round())
	}
}

// --- gameplay and results ----------------------------------------------------------------------------

func TestGameplayStartsLoadingForBothPlayers(t *testing.T) {
	m := newMatch(t)
	m.toGameplay()

	if state := m.room.Snapshot().State; state != protocol.RoomWaitingForLoad {
		t.Errorf("room state = %d, want WaitingForLoad", state)
	}
	if userState(m.room, 1) != protocol.UserWaitingForLoad || userState(m.room, 2) != protocol.UserWaitingForLoad {
		t.Error("both players must be asked to load the beatmap")
	}
}

func TestLowerScoreLosesLifeScaledByTheDamageMultipliers(t *testing.T) {
	m := newMatch(t)
	m.svc.setScores(RankedPlayScores{1: 900_000, 2: 700_000})
	played := m.toGameplay()

	m.playRound()
	m.waitForResults()

	var loser, winner protocol.RankedPlayUserInfo
	var winnerDamage protocol.DamageInfo
	m.inspect(func(rp *rankedPlay) {
		loser, winner = *rp.state.Users[2], *rp.state.Users[1]
		winnerDamage = *winner.DamageInfo
	})

	// 200,000 points of difference, times the room's 0.5 plus the winner's own 0.5, plus 50,000 for the round.
	want := protocol.DamageInfo{Damage: 250_000, RawDamage: 250_000, OldLife: 1_000_000, NewLife: 750_000, DirectDamage: 200_000, Multiplier: 1, BonusDamage: 50_000}
	if loser.DamageInfo == nil || *loser.DamageInfo != want {
		t.Errorf("loser's damage = %+v, want %+v", loser.DamageInfo, want)
	}
	if loser.Life != 750_000 || winner.Life != 1_000_000 || winner.RoundsWon != 1 {
		t.Errorf("loser life %d, winner life %d, rounds won %d", loser.Life, winner.Life, winner.RoundsWon)
	}
	if winnerDamage.Damage != 0 || winnerDamage.NewLife != 1_000_000 {
		t.Errorf("the winner takes no damage, got %+v", winnerDamage)
	}

	m.wantStage(protocol.StageResults)
	if countdowns := m.stageCountdowns(); len(countdowns) != 1 || countdowns[0].Stage != protocol.StageResults || countdowns[0].TimeRemaining != protocol.Ticks(15*time.Second) {
		t.Errorf("countdowns = %+v", countdowns)
	}

	m.advance(15 * time.Second)

	// The next round: the turn passes, every damage multiplier grew, the damage numbers are cleared.
	m.inspect(func(rp *rankedPlay) {
		if rp.state.CurrentRound != 2 || *rp.state.ActiveUserID != 2 {
			t.Errorf("round %d, active %d, want round 2 and player 2", rp.state.CurrentRound, *rp.state.ActiveUserID)
		}
		if rp.state.DamageMultiplier != 1.0 || rp.state.Users[1].DamageMultiplier != 1.0 || rp.state.Users[2].DamageMultiplier != 0.5 {
			t.Errorf("multipliers: room %v, winner %v, loser %v", rp.state.DamageMultiplier, rp.state.Users[1].DamageMultiplier, rp.state.Users[2].DamageMultiplier)
		}
		if rp.state.Users[1].DamageInfo != nil || rp.state.Users[2].DamageInfo != nil {
			t.Error("damage info must be cleared when the next round starts")
		}
	})
	for _, card := range m.hand(1) {
		if card.ID == played.ID {
			t.Error("the played card is still in the hand")
		}
	}
}

func TestEqualScoresHurtNobody(t *testing.T) {
	m := newMatch(t)
	m.svc.setScores(RankedPlayScores{1: 800_000, 2: 800_000})
	m.toGameplay()

	m.playRound()
	m.waitForResults()

	m.inspect(func(rp *rankedPlay) {
		if rp.state.Users[1].Life != 1_000_000 || rp.state.Users[2].Life != 1_000_000 {
			t.Errorf("lives %d, %d, want nobody hurt", rp.state.Users[1].Life, rp.state.Users[2].Life)
		}
		if rp.state.Users[1].RoundsWon != 0 || rp.state.Users[2].RoundsWon != 0 {
			t.Error("a draw wins no round")
		}
	})

	m.advance(15 * time.Second)
	m.inspect(func(rp *rankedPlay) {
		if rp.state.Users[1].DamageMultiplier != 0.5 || rp.state.Users[2].DamageMultiplier != 0.5 {
			t.Error("nobody wins, so nobody's multiplier grows")
		}
	})
}

func TestResultsWaitForLateScoresAndTreatMissingOnesAsZero(t *testing.T) {
	m := newMatch(t)
	m.toGameplay()
	m.playRound()

	// No score has arrived: the stage keeps polling instead of announcing anything.
	waitFor(t, func() bool { return m.clock.pending() > 0 })
	m.advance(3 * time.Second)
	if m.svc.calls() < 3 {
		t.Fatalf("scores were asked for %d times, want them polled", m.svc.calls())
	}

	// Only one player's score ever shows up; after the time limit the other counts as nothing.
	m.svc.setScores(RankedPlayScores{1: 500_000})
	m.advance(10 * time.Second)
	m.waitForResults()

	// 500,000 of difference at the first round's multipliers (0.5 + 0.5), plus 50,000.
	if got := m.life(2); got != 450_000 {
		t.Errorf("life of the player without a score = %d, want 450000", got)
	}
}

func TestScoresThatArriveInTimeAreUsedImmediately(t *testing.T) {
	m := newMatch(t)
	m.toGameplay()
	m.playRound()

	waitFor(t, func() bool { return m.clock.pending() > 0 })
	m.svc.setScores(RankedPlayScores{1: 600_000, 2: 400_000})
	m.advance(time.Second) // the next poll finds both scores

	m.waitForResults()
	if got := m.life(2); got != 750_000 {
		t.Errorf("life = %d, want 750000", got)
	}
}

func TestMatchEndsWhenAPlayerRunsOutOfLife(t *testing.T) {
	m := newMatch(t, func(p *RankedPlayParams) { p.Ranked = true })
	m.svc.setScores(RankedPlayScores{1: 900_000, 2: 700_000})
	m.svc.setRatings(map[int32]int32{1: 1512, 2: 1588})
	m.toGameplay()
	m.inspect(func(rp *rankedPlay) { rp.state.Users[2].Life = 100_000 })

	m.playRound()
	m.waitForResults()

	if got := m.life(2); got != 0 {
		t.Fatalf("life = %d, want 0", got)
	}
	m.inspect(func(rp *rankedPlay) {
		if rp.state.WinningUserID == nil || *rp.state.WinningUserID != 1 {
			t.Errorf("winner = %v, want player 1", rp.state.WinningUserID)
		}
		if rp.state.Users[1].RatingAfter != 1512 || rp.state.Users[2].RatingAfter != 1588 {
			t.Errorf("ratings after = %d, %d", rp.state.Users[1].RatingAfter, rp.state.Users[2].RatingAfter)
		}
	})

	m.advance(15 * time.Second)
	m.wantStage(protocol.StageEnded)

	results := m.svc.reported()
	if len(results) != 1 {
		t.Fatalf("the match was reported %d times, want once", len(results))
	}
	if r := results[0]; !r.Ranked || !r.Played || r.Winner == nil || *r.Winner != 1 || len(r.Players) != 2 {
		t.Errorf("result = %+v", r)
	}
}

// --- joining, leaving and the room itself ------------------------------------------------------------

func TestAbandoningARankedMatchBansTheLeaverAndAwardsTheWin(t *testing.T) {
	m := newMatch(t, func(p *RankedPlayParams) { p.Ranked = true })
	m.toCardPlay()

	must(t, m.mgr.Leave(m.room, 2, 2))

	m.wantStage(protocol.StageEnded)
	if got := m.svc.banned()[2]; got != 10*time.Minute {
		t.Errorf("player 2 is banned for %v, want 10 minutes", got)
	}
	if results := m.svc.reported(); len(results) != 1 || results[0].Winner == nil || *results[0].Winner != 1 {
		t.Errorf("results = %+v, want player 1 to win", results)
	}
}

func TestAbandoningAnUnrankedMatchIsNotPunished(t *testing.T) {
	m := newMatch(t)
	m.toCardPlay()

	must(t, m.mgr.Leave(m.room, 2, 2))

	if bans := m.svc.banned(); len(bans) != 0 {
		t.Errorf("bans = %v, want none for a duel", bans)
	}
}

func TestLeavingDuringGameplayForfeitsButLetsTheRoundFinish(t *testing.T) {
	m := newMatch(t)
	m.toGameplay()

	must(t, m.mgr.Leave(m.room, 2, 2))

	m.wantStage(protocol.StageGameplay)
	if got := m.life(2); got != 0 {
		t.Errorf("life = %d, want 0", got)
	}
	if results := m.svc.reported(); len(results) != 1 || results[0].Winner == nil || *results[0].Winner != 1 {
		t.Errorf("results = %+v, want the match decided in player 1's favour", results)
	}
}

func TestSnapshotShowsTheMatchStateAndRunningCountdowns(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)
	m.advance(5 * time.Second)

	snapshot := m.room.Snapshot()
	if snapshot.Settings.MatchType != protocol.MatchRankedPlay || snapshot.Settings.PlaylistItemID != 0 || len(snapshot.Playlist) != 1 {
		t.Errorf("settings %+v, playlist %+v", snapshot.Settings, snapshot.Playlist)
	}

	if snapshot.MatchState == nil {
		t.Fatal("the snapshot carries no match state")
	}
	// Only the union key is read: the state itself holds maps keyed by user id, which plain Go decoding rejects.
	var state []msgpack.RawMessage
	must(t, msgpack.Unmarshal(*snapshot.MatchState, &state))
	var key int
	must(t, msgpack.Unmarshal(state[0], &key))
	if key != protocol.MatchRoomStateRankedPlay {
		t.Errorf("match state union key = %d, want %d (ranked play)", key, protocol.MatchRoomStateRankedPlay)
	}

	if len(snapshot.ActiveCountdowns) != 1 {
		t.Fatalf("active countdowns = %d, want 1", len(snapshot.ActiveCountdowns))
	}
	var countdown []any
	must(t, msgpack.Unmarshal(snapshot.ActiveCountdowns[0], &countdown))
	fields := countdown[1].([]any)
	if fmt.Sprint(countdown[0]) != "4" || fmt.Sprint(fields[1]) != fmt.Sprint(protocol.Ticks(15*time.Second)) {
		t.Errorf("countdown = %v, want a ranked play stage countdown with 15 seconds left", countdown)
	}
}

func TestClosingTheRoomCancelsItsCountdowns(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	m.mgr.CloseAll()
	m.advance(time.Hour)

	m.wantStage(protocol.StageRoundWarmup)
}

func TestRoomIsPersistedWithItsRealItem(t *testing.T) {
	store := NewRecordingStore()
	m := newMatch(t)
	m.mgr = NewManager(m.ev, Options{Clock: m.clock, LoadTimeout: time.Hour, Store: store})

	room, password, err := m.mgr.CreateRankedPlay(context.Background(), RankedPlayParams{
		Players:  []RankedPlayPlayer{{UserID: 1, Rating: 1500}, {UserID: 2, Rating: 1600}},
		Deck:     makeDeck(10),
		Services: m.svc,
	})
	must(t, err)
	m.room, m.password = room, password

	m.toCardPlay()
	must(t, m.room.RankedPlayPlayCard(1, m.hand(1)[0]))

	item := m.room.CurrentItem()
	if _, ok := store.Item(room.ID(), item.ID); !ok {
		t.Errorf("the played item %d was not persisted", item.ID)
	}
	if _, ok := store.Item(room.ID(), 0); ok {
		t.Error("the in-memory placeholder item must never be persisted")
	}
	if got := store.Room(room.ID()); got.CurrentItemID != item.ID || got.MatchType != protocol.MatchRankedPlay {
		t.Errorf("persisted room = %+v", got)
	}
}
