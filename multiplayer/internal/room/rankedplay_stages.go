package room

import (
	"context"
	"log/slog"
	"math"
	"sort"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// rankedStage is one phase of a ranked play match. Every method runs with the room lock held.
type rankedStage interface {
	stage() protocol.RankedPlayStage
	// duration is how long the stage lasts before finish runs; noCountdown for stages that wait for events.
	duration() time.Duration
	// begin runs when the stage is entered and calls done when it is ready to be announced to the clients.
	// Most stages call it at once; the results stage first waits for the scores.
	begin(done func())
	finish()

	userJoined(user *protocol.RoomUser)
	userLeft(user protocol.RoomUser)
	userStateChanged(user *protocol.RoomUser)
	gameplayCompleted()
	discardCards(userID int32, cards []protocol.CardItem) error
	playCard(userID int32, card protocol.CardItem) error
	handReplay(userID int32, frames []protocol.HandReplayFrame)
}

// baseStage supplies the behaviour most stages share: a player leaving abandons the match.
type baseStage struct{ c *rankedPlay }

func (baseStage) duration() time.Duration { return noCountdown }

func (baseStage) begin(done func()) { done() }

func (baseStage) finish() {}

func (baseStage) userJoined(*protocol.RoomUser) {}

func (s baseStage) userLeft(user protocol.RoomUser) {
	s.c.killUser(user)
	s.c.closeMatch()
}

func (baseStage) userStateChanged(*protocol.RoomUser) {}

func (baseStage) gameplayCompleted() {}

func (baseStage) discardCards(int32, []protocol.CardItem) error { return nil }

func (baseStage) playCard(int32, protocol.CardItem) error { return nil }

func (baseStage) handReplay(int32, []protocol.HandReplayFrame) {}

// --- WaitForJoin -------------------------------------------------------------------------------------

type waitForJoinStage struct{ baseStage }

func (*waitForJoinStage) stage() protocol.RankedPlayStage { return protocol.StageWaitForJoin }

func (s *waitForJoinStage) duration() time.Duration { return s.c.cfg.WaitForJoin }

func (s *waitForJoinStage) allJoined() bool { return len(s.c.room.users) == len(s.c.state.Users) }

func (s *waitForJoinStage) finish() {
	if s.allJoined() {
		s.c.goTo(protocol.StageRoundWarmup)
	} else {
		s.c.goTo(protocol.StageEnded)
	}
}

func (s *waitForJoinStage) userJoined(user *protocol.RoomUser) {
	s.c.addCards(user.UserID, RankedPlayHandSize)

	if s.allJoined() {
		s.finish()
	}
}

// Players may leave early without losing, but the match cannot go on.
func (s *waitForJoinStage) userLeft(protocol.RoomUser) { s.c.closeMatch() }

// --- RoundWarmup -------------------------------------------------------------------------------------

type roundWarmupStage struct{ baseStage }

func (*roundWarmupStage) stage() protocol.RankedPlayStage { return protocol.StageRoundWarmup }

// Only the first round has a warmup, to give the players time to arrive.
func (s *roundWarmupStage) duration() time.Duration {
	if s.c.state.CurrentRound == 1 {
		return s.c.cfg.FirstRoundWarmup
	}
	return 0
}

func (s *roundWarmupStage) begin(done func()) {
	c, r := s.c, s.c.room

	for _, user := range append([]*protocol.RoomUser(nil), r.users...) {
		if user.State != protocol.UserIdle {
			r.changeAndBroadcastUserState(user, protocol.UserIdle)
			r.updateRoomStateIfRequired()
		}
	}

	c.state.CurrentRound++

	// Every round hits harder than the last.
	if c.state.CurrentRound > 1 {
		c.state.DamageMultiplier += 0.5
	}

	// The turn passes to the other player. The first round's active player was chosen when the room was set up.
	if c.state.CurrentRound >= 2 {
		next := c.nextActiveUser()
		c.state.ActiveUserID = &next
	}

	// From their second turn on, the active player draws a card.
	if c.state.CurrentRound >= 3 && c.state.ActiveUserID != nil {
		c.addCards(*c.state.ActiveUserID, 1)
	}

	done()
}

func (s *roundWarmupStage) finish() {
	if s.c.state.CurrentRound == 1 {
		s.c.goTo(protocol.StageCardDiscard)
	} else {
		s.c.goTo(protocol.StageCardPlay)
	}
}

func (c *rankedPlay) nextActiveUser() int32 {
	if c.state.ActiveUserID != nil {
		for i, id := range c.turnOrder {
			if id == *c.state.ActiveUserID {
				return c.turnOrder[(i+1)%len(c.turnOrder)]
			}
		}
	}
	return c.turnOrder[0]
}

// --- CardDiscard -------------------------------------------------------------------------------------

type cardDiscardStage struct {
	baseStage
	discarded map[int32]bool
}

func (*cardDiscardStage) stage() protocol.RankedPlayStage { return protocol.StageCardDiscard }

func (s *cardDiscardStage) duration() time.Duration { return s.c.cfg.CardDiscard }

func (s *cardDiscardStage) finish() { s.c.goTo(protocol.StageFinishCardDiscard) }

// discardCards replaces the given cards of a player's hand with fresh ones, once per player.
//
// Unlike the reference server, a request that names a card twice or one that is not in the hand is refused
// before it counts as the player's discard, so a bad request does not cost them their chance.
func (s *cardDiscardStage) discardCards(userID int32, cards []protocol.CardItem) error {
	if s.discarded[userID] {
		return invalidState("Cards have already been discarded.")
	}

	seen := map[string]bool{}
	for _, card := range cards {
		if seen[card.ID] || !s.c.handHolds(userID, card) {
			return invalidState("One or more cards were not in the hand.")
		}
		seen[card.ID] = true
	}

	s.discarded[userID] = true
	s.c.removeCards(userID, cards...)
	s.c.addCards(userID, len(cards))

	// Once everybody is done, wait a moment so the animations can finish.
	if len(s.discarded) == len(s.c.state.Users) {
		s.c.finishWithCountdown(s, s.c.cfg.CardDiscardAfterAll)
	}
	return nil
}

// --- FinishCardDiscard -------------------------------------------------------------------------------

type finishCardDiscardStage struct{ baseStage }

func (*finishCardDiscardStage) stage() protocol.RankedPlayStage {
	return protocol.StageFinishCardDiscard
}

func (s *finishCardDiscardStage) duration() time.Duration { return s.c.cfg.FinishCardDiscard }

func (s *finishCardDiscardStage) finish() { s.c.goTo(protocol.StageCardPlay) }

// --- CardPlay ----------------------------------------------------------------------------------------

type cardPlayStage struct {
	baseStage
	played       *protocol.CardItem
	lastSelected *protocol.CardItem
}

func (*cardPlayStage) stage() protocol.RankedPlayStage { return protocol.StageCardPlay }

func (s *cardPlayStage) duration() time.Duration { return s.c.cfg.CardPlay }

func (s *cardPlayStage) begin(done func()) {
	if active := s.c.state.ActiveUserID; active != nil && len(s.c.state.Users[*active].Hand) == 0 {
		s.c.addCards(*active, 1)
	}
	done()
}

// finish plays the card the active player chose, or else the one they last hovered, or else their first.
func (s *cardPlayStage) finish() {
	c := s.c

	if c.state.ActiveUserID == nil {
		c.closeMatch()
		return
	}
	hand := c.state.Users[*c.state.ActiveUserID].Hand

	var card protocol.CardItem
	switch {
	case s.played != nil:
		card = *s.played
	case s.lastSelected != nil:
		card = *s.lastSelected
	case len(hand) > 0:
		card = hand[0]
	default:
		slog.Error("the active player has no card to play", "room", c.room.id)
		c.closeMatch()
		return
	}

	if err := c.activateCard(card); err != nil {
		slog.Error("activating the played card failed", "room", c.room.id, "error", err)
		c.closeMatch()
		return
	}
	c.goTo(protocol.StageFinishCardPlay)
}

func (s *cardPlayStage) playCard(userID int32, card protocol.CardItem) error {
	if active := s.c.state.ActiveUserID; active == nil || userID != *active {
		return invalidState("Not the active player.")
	}
	if !s.c.handHolds(userID, card) {
		return invalidState("Card not in the hand.")
	}
	if s.played != nil {
		return nil
	}

	s.played = &card
	s.finish()
	return nil
}

// handReplay remembers the card the active player last had selected, to play it if the time runs out.
func (s *cardPlayStage) handReplay(userID int32, frames []protocol.HandReplayFrame) {
	active := s.c.state.ActiveUserID
	if active == nil || userID != *active {
		return
	}

	for i := len(frames) - 1; i >= 0; i-- {
		ids := make([]string, 0, len(frames[i].Cards))
		for id := range frames[i].Cards {
			ids = append(ids, id)
		}
		sort.Strings(ids)

		for _, id := range ids {
			if !frames[i].Cards[id].Selected {
				continue
			}

			s.lastSelected = nil
			for _, held := range s.c.state.Users[*active].Hand {
				if held.ID == id {
					card := held
					s.lastSelected = &card
				}
			}
			return
		}
	}
}

// --- FinishCardPlay and GameplayWarmup ---------------------------------------------------------------
//
// Both wait for every player to be ready, and both end the same way when somebody is not: the players who
// are ready take the round as a no-show win, the card is spent and the next round begins.

// skipRound ends a round that cannot be played: whoever is not ready is hurt if somebody else was ready.
func (c *rankedPlay) skipRound(isReady func(*protocol.RoomUser) bool) {
	r := c.room

	anyReady := r.anyUser(isReady)
	if anyReady {
		for _, user := range r.users {
			if !isReady(user) {
				c.damageNoShow(user.UserID)
			}
		}
	}

	if c.state.ActiveUserID != nil && c.lastActivated != nil {
		c.removeCards(*c.state.ActiveUserID, *c.lastActivated)
	}

	if c.hasRoundsRemaining() {
		c.goTo(protocol.StageRoundWarmup)
	} else {
		c.goTo(protocol.StageEnded)
	}
}

func (c *rankedPlay) damageNoShow(userID int32) {
	if c.state.Users[userID] != nil {
		c.damage(userID, c.cfg.NoShowDamage, c.state.DamageMultiplier, 0)
	}
}

func (c *rankedPlay) allReady(isReady func(*protocol.RoomUser) bool) bool {
	for _, user := range c.room.users {
		if !isReady(user) {
			return false
		}
	}
	return true
}

// hasBeatmap only requires a player to have the beatmap, not to have it loaded yet.
func hasBeatmap(user *protocol.RoomUser) bool {
	return user.BeatmapAvailability.State == protocol.DownloadLocallyAvailable
}

// readyToPlay requires the player to have the beatmap and to have said they are ready.
func readyToPlay(user *protocol.RoomUser) bool {
	return hasBeatmap(user) && user.State == protocol.UserReady
}

type finishCardPlayStage struct{ baseStage }

func (*finishCardPlayStage) stage() protocol.RankedPlayStage { return protocol.StageFinishCardPlay }

func (s *finishCardPlayStage) duration() time.Duration { return s.c.cfg.FinishCardPlay }

func (s *finishCardPlayStage) begin(done func()) {
	s.continueWhenAllHaveTheBeatmap()
	done()
}

func (s *finishCardPlayStage) finish() {
	if s.c.allReady(hasBeatmap) {
		s.c.goTo(protocol.StageGameplayWarmup)
		return
	}
	s.c.skipRound(hasBeatmap)
}

func (s *finishCardPlayStage) userStateChanged(*protocol.RoomUser) { s.continueWhenAllHaveTheBeatmap() }

func (s *finishCardPlayStage) continueWhenAllHaveTheBeatmap() {
	if s.c.allReady(hasBeatmap) {
		s.finish()
	}
}

type gameplayWarmupStage struct{ baseStage }

func (*gameplayWarmupStage) stage() protocol.RankedPlayStage { return protocol.StageGameplayWarmup }

func (s *gameplayWarmupStage) duration() time.Duration { return s.c.cfg.GameplayWarmup }

func (s *gameplayWarmupStage) begin(done func()) {
	s.continueWhenAllReady()
	done()
}

func (s *gameplayWarmupStage) finish() {
	if s.c.allReady(readyToPlay) {
		s.c.goTo(protocol.StageGameplay)
		return
	}
	s.c.skipRound(readyToPlay)
}

func (s *gameplayWarmupStage) userStateChanged(*protocol.RoomUser) { s.continueWhenAllReady() }

// continueWhenAllReady starts a short countdown to gameplay once everybody is ready.
func (s *gameplayWarmupStage) continueWhenAllReady() {
	if s.c.allReady(readyToPlay) {
		s.c.finishWithCountdown(s, s.c.cfg.GameplayWarmupAfterAll)
	}
}

// --- Gameplay ----------------------------------------------------------------------------------------

type gameplayStage struct{ baseStage }

func (*gameplayStage) stage() protocol.RankedPlayStage { return protocol.StageGameplay }

func (s *gameplayStage) begin(done func()) {
	s.c.startGameplay()
	done()
}

func (s *gameplayStage) finish() {
	if s.c.state.ActiveUserID != nil && s.c.lastActivated != nil {
		s.c.removeCards(*s.c.state.ActiveUserID, *s.c.lastActivated)
	}
	s.c.goTo(protocol.StageResults)
}

func (s *gameplayStage) gameplayCompleted() { s.finish() }

// A player who leaves mid-game loses all their life, but the round plays out to its end.
func (s *gameplayStage) userLeft(user protocol.RoomUser) { s.c.killUser(user) }

// --- Results -----------------------------------------------------------------------------------------

type resultsStage struct {
	baseStage
	winner *int32
}

func (*resultsStage) stage() protocol.RankedPlayStage { return protocol.StageResults }

func (s *resultsStage) duration() time.Duration { return s.c.cfg.Results }

// begin collects the scores, which can take a while to reach the database, then deals the damage. Nothing
// is announced before that, so the clients keep showing the round until they can be shown its result.
func (s *resultsStage) begin(done func()) {
	c, r := s.c, s.c.room

	itemID := r.settings.PlaylistItemID
	expected := len(c.state.Users)
	deadline := r.clock.Now().Add(c.cfg.ScoreWait)

	// poll runs without the room lock: reaching the database can be slow.
	var poll func()
	poll = func() {
		ctx, cancel := context.WithTimeout(context.Background(), r.opts.StoreTimeout)
		scores, err := c.svc.Scores(ctx, itemID)
		cancel()
		if err != nil {
			slog.Error("fetching the scores failed", "room", r.id, "item", itemID, "error", err)
		}

		if (err != nil || len(scores) < expected) && r.clock.Now().Before(deadline) {
			r.clock.AfterFunc(c.cfg.ScoreRetryInterval, poll)
			return
		}

		r.lock()
		defer r.unlock()

		// The room closed, or the match moved on (everybody left), while the scores were being fetched.
		if r.closed || c.stage != s {
			return
		}
		s.apply(scores)
		done()
	}

	go poll()
}

// apply turns the scores into damage: the player with the lower score loses life, a draw hurts nobody.
func (s *resultsStage) apply(scores RankedPlayScores) {
	c := s.c

	ids := make([]int32, 0, len(c.state.Users))
	for id := range c.state.Users {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })

	// Everybody starts the stage without damage. Players that did not play have a score of nothing.
	for _, id := range ids {
		info := c.damage(id, 0, 1, 0)
		c.state.Users[id].DamageInfo = &info
	}

	var best int64 = -1
	var winners []int32
	for _, id := range ids {
		switch score := scores[id]; {
		case score > best:
			best, winners = score, []int32{id}
		case score == best:
			winners = append(winners, id)
		}
	}

	if len(winners) == 1 {
		winner := winners[0]
		s.winner = &winner

		multiplier := c.state.DamageMultiplier + c.state.Users[winner].DamageMultiplier
		for _, id := range ids {
			if id == winner {
				continue
			}

			difference := min(best-scores[id], math.MaxInt32)
			info := c.damage(id, int32(difference), multiplier, c.cfg.BaseDamage)
			c.state.Users[id].DamageInfo = &info
		}
		c.state.Users[winner].RoundsWon++
	}

	if !c.hasRoundsRemaining() {
		c.matchCompleted()
	}
}

func (s *resultsStage) finish() {
	c := s.c

	// The winner's attacks get stronger.
	if s.winner != nil {
		c.state.Users[*s.winner].DamageMultiplier += 0.5
	}
	for _, u := range c.state.Users {
		u.DamageInfo = nil
	}

	if c.hasRoundsRemaining() {
		c.goTo(protocol.StageRoundWarmup)
	} else {
		c.goTo(protocol.StageEnded)
	}
}

// Leaving early costs nothing once the match can no longer continue; otherwise it forfeits.
func (s *resultsStage) userLeft(user protocol.RoomUser) {
	if s.c.hasRoundsRemaining() {
		s.c.killUser(user)
	}
}

// --- Ended -------------------------------------------------------------------------------------------

type endedStage struct{ baseStage }

func (*endedStage) stage() protocol.RankedPlayStage { return protocol.StageEnded }

func (s *endedStage) begin(done func()) {
	s.c.matchCompleted()
	done()
}

// The match is over: nobody needs to be killed.
func (*endedStage) userLeft(protocol.RoomUser) {}
