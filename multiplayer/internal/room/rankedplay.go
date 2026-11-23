package room

import (
	"context"
	"log/slog"
	"math"
	"math/rand"
	"sort"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// This file and rankedplay_stages.go are a port of osu-server-spectator's RankedPlayMatchController and its
// stages. What clients can observe (callbacks, their order, durations, damage numbers, error texts) follows that
// server, because the osu! client is written against it. Where this port deliberately differs, it says so.

// Rules of a ranked play match.
const (
	// RankedPlayHandSize is how many cards every player holds.
	RankedPlayHandSize = 5

	// RankedPlayBanDuration is how long a ranked player who abandons a match stays out of the queue.
	RankedPlayBanDuration = 10 * time.Minute

	noCountdown = time.Duration(math.MaxInt64)
)

// RankedPlayScores maps a user id to the total score of their play of the current playlist item.
type RankedPlayScores map[int32]int64

// MatchResult is the final standing of a ranked play match.
type MatchResult struct {
	RoomID int64
	// PoolID is the matchmaking pool the match was made in; 0 for a room that came from none.
	PoolID int32
	// Ranked matches change the players' ratings; unranked ones (duels) do not.
	Ranked bool
	// Played is false when the match was abandoned before the first round.
	Played  bool
	Winner  *int32 // nil for a draw
	Players []PlayerResult
}

// PlayerResult is one player's part of a MatchResult.
type PlayerResult struct {
	UserID int32
	Rating int32 // before the match
	Life   int32
}

// RankedPlayServices is everything a ranked play match needs from outside the room.
type RankedPlayServices interface {
	// Scores returns the total score of every user that has a score on the playlist item. It is called without
	// the room lock held, since it usually reaches the database.
	Scores(ctx context.Context, itemID int64) (RankedPlayScores, error)

	// BanUser keeps a ranked player who abandoned the match out of the queue for a while.
	BanUser(userID int32, d time.Duration)

	// MatchCompleted receives the final standing once. For ranked matches it returns every player's rating
	// afterwards; a nil map leaves the ratings as they were. It runs with the room lock held and is bounded by
	// the room's store timeout.
	MatchCompleted(ctx context.Context, result MatchResult) (map[int32]int32, error)

	// MatchRecorded is handed the match state, ratings included, once the match is over. It must not block: it
	// feeds the "recent matches" shown in the queue.
	MatchRecorded(poolID int32, state protocol.RankedPlayRoomState)
}

// NoServices is a RankedPlayServices that does nothing: nobody has a score and ratings never change.
type NoServices struct{}

// Scores implements RankedPlayServices.
func (NoServices) Scores(context.Context, int64) (RankedPlayScores, error) {
	return RankedPlayScores{}, nil
}

// BanUser implements RankedPlayServices.
func (NoServices) BanUser(int32, time.Duration) {}

// MatchRecorded implements RankedPlayServices.
func (NoServices) MatchRecorded(int32, protocol.RankedPlayRoomState) {}

// MatchCompleted implements RankedPlayServices.
func (NoServices) MatchCompleted(context.Context, MatchResult) (map[int32]int32, error) {
	return nil, nil
}

// RankedPlayConfig holds the timings of a match. Zero values select the reference server's.
type RankedPlayConfig struct {
	WaitForJoin            time.Duration // players have this long to join before the match is cancelled
	FirstRoundWarmup       time.Duration
	CardDiscard            time.Duration
	CardDiscardAfterAll    time.Duration // grace period once every player has discarded
	FinishCardDiscard      time.Duration
	CardPlay               time.Duration
	FinishCardPlay         time.Duration // players have this long to download the beatmap
	GameplayWarmup         time.Duration // players have this long to get ready
	GameplayWarmupAfterAll time.Duration // grace period once every player is ready
	Results                time.Duration
	ScoreWait              time.Duration // how long to wait for every score to reach the database
	ScoreRetryInterval     time.Duration
	BaseDamage             int32 // damage the loser of a round takes before multipliers
	NoShowDamage           int32 // damage taken by a player who is not ready when the other is
	Rand                   *rand.Rand
}

func (c RankedPlayConfig) withDefaults() RankedPlayConfig {
	def := func(v *time.Duration, d time.Duration) {
		if *v == 0 {
			*v = d
		}
	}

	def(&c.WaitForJoin, 60*time.Second)
	def(&c.FirstRoundWarmup, 20*time.Second)
	def(&c.CardDiscard, 30*time.Second)
	def(&c.CardDiscardAfterAll, 3*time.Second)
	def(&c.FinishCardDiscard, 5*time.Second)
	def(&c.CardPlay, 45*time.Second)
	def(&c.FinishCardPlay, 2*time.Minute)
	def(&c.GameplayWarmup, 2*time.Minute)
	def(&c.GameplayWarmupAfterAll, 10*time.Second)
	def(&c.Results, 15*time.Second)
	def(&c.ScoreWait, 10*time.Second)
	def(&c.ScoreRetryInterval, time.Second)

	if c.BaseDamage == 0 {
		c.BaseDamage = 50_000
	}
	if c.NoShowDamage == 0 {
		c.NoShowDamage = 100_000
	}
	if c.Rand == nil {
		c.Rand = rand.New(rand.NewSource(time.Now().UnixNano()))
	}
	return c
}

// DeckCard is a card of the deck and the playlist item playing it puts on the room.
type DeckCard struct {
	Card protocol.CardItem
	Item protocol.PlaylistItem
}

// RankedPlayPlayer is a participant of a match.
type RankedPlayPlayer struct {
	UserID int32
	Rating int32
}

// RankedPlayParams describes a ranked play room.
type RankedPlayParams struct {
	Name    string
	Players []RankedPlayPlayer
	// Deck is the draw pile, first card on top. The caller shuffles it.
	Deck []DeckCard
	// Ranked matches change ratings and punish players who abandon them.
	Ranked bool
	// PoolID is the matchmaking pool the match was made in; it is handed back with the result.
	PoolID   int32
	Services RankedPlayServices
	Config   RankedPlayConfig
}

// rankedPlay is the match controller of a ranked play room.
type rankedPlay struct {
	room *Room
	svc  RankedPlayServices
	cfg  RankedPlayConfig

	state  *protocol.RankedPlayRoomState
	stage  rankedStage
	ranked bool
	poolID int32

	turnOrder     []int32
	deck          []protocol.CardItem
	effects       map[string]protocol.PlaylistItem // card id -> the item playing it creates
	lastActivated *protocol.CardItem

	ratingsUpdated bool
}

func newRankedPlay(r *Room, p RankedPlayParams) *rankedPlay {
	c := &rankedPlay{
		room:    r,
		svc:     p.Services,
		cfg:     p.Config.withDefaults(),
		state:   protocol.NewRankedPlayRoomState(),
		ranked:  p.Ranked,
		poolID:  p.PoolID,
		effects: map[string]protocol.PlaylistItem{},
	}
	if c.svc == nil {
		c.svc = NoServices{}
	}

	var starRating float64
	for _, card := range p.Deck {
		item := card.Item
		item.Normalise()
		c.effects[card.Card.ID] = item
		c.deck = append(c.deck, card.Card)
		starRating += item.StarRating
	}
	if len(p.Deck) > 0 {
		c.state.StarRating = starRating / float64(len(p.Deck))
	}

	players := append([]RankedPlayPlayer(nil), p.Players...)
	for _, player := range players {
		c.state.Users[player.UserID] = protocol.NewRankedPlayUserInfo(player.Rating)
	}

	// The player with the lowest rating goes first; equal ratings are settled at random.
	tiebreak := map[int32]float64{}
	for _, player := range players {
		tiebreak[player.UserID] = c.cfg.Rand.Float64()
	}
	sort.SliceStable(players, func(i, j int) bool {
		if players[i].Rating != players[j].Rating {
			return players[i].Rating < players[j].Rating
		}
		return tiebreak[players[i].UserID] < tiebreak[players[j].UserID]
	})
	for _, player := range players {
		c.turnOrder = append(c.turnOrder, player.UserID)
	}
	if len(c.turnOrder) > 0 {
		first := c.turnOrder[0]
		c.state.ActiveUserID = &first
	}

	return c
}

// --- MatchController ---------------------------------------------------------------------------------

func (c *rankedPlay) initialise() {
	c.broadcastState()
	c.goTo(protocol.StageWaitForJoin)
}

func (c *rankedPlay) userCanJoin(userID int32) bool {
	_, ok := c.state.Users[userID]
	return ok
}

func (c *rankedPlay) userJoined(user *protocol.RoomUser) { c.stage.userJoined(user) }

func (c *rankedPlay) userLeft(user protocol.RoomUser) { c.stage.userLeft(user) }

func (c *rankedPlay) userStateChanged(user *protocol.RoomUser) { c.stage.userStateChanged(user) }

func (c *rankedPlay) matchState() protocol.Union { return c.state.Union() }

// gameplayCompleted marks the played item as played, then tells the stage.
func (c *rankedPlay) gameplayCompleted() {
	r := c.room

	for _, p := range r.playlist {
		if p.ID != r.settings.PlaylistItemID {
			continue
		}

		p.Expired = true
		r.played++
		r.playedSeq[p.ID] = r.played
		r.playedAt[p.ID] = r.clock.Now().UTC()
		r.handlePlaylistItemChanged(p, true)
		break
	}

	c.stage.gameplayCompleted()
}

// userRequest handles a MatchUserRequest. Only the hand replay of the player's cards does anything here.
func (c *rankedPlay) userRequest(userID int32, key int, request any) {
	if key != protocol.MatchRequestCardHandReplay {
		return
	}

	replay, ok := request.(*protocol.HandReplayRequest)
	if !ok {
		return
	}

	c.stage.handReplay(userID, replay.Frames)
	c.room.broadcast("MatchEvent", protocol.HandReplayEvent(userID, replay.Frames))
}

// --- stage machine -----------------------------------------------------------------------------------

func (c *rankedPlay) broadcastState() {
	c.room.broadcast("MatchRoomStateChanged", c.state.Union())
}

// goTo leaves the current stage for another.
func (c *rankedPlay) goTo(next protocol.RankedPlayStage) {
	var s rankedStage
	base := baseStage{c: c}

	switch next {
	case protocol.StageWaitForJoin:
		s = &waitForJoinStage{baseStage: base}
	case protocol.StageRoundWarmup:
		s = &roundWarmupStage{baseStage: base}
	case protocol.StageCardDiscard:
		s = &cardDiscardStage{baseStage: base, discarded: map[int32]bool{}}
	case protocol.StageFinishCardDiscard:
		s = &finishCardDiscardStage{baseStage: base}
	case protocol.StageCardPlay:
		s = &cardPlayStage{baseStage: base}
	case protocol.StageFinishCardPlay:
		s = &finishCardPlayStage{baseStage: base}
	case protocol.StageGameplayWarmup:
		s = &gameplayWarmupStage{baseStage: base}
	case protocol.StageGameplay:
		s = &gameplayStage{baseStage: base}
	case protocol.StageResults:
		s = &resultsStage{baseStage: base}
	case protocol.StageEnded:
		s = &endedStage{baseStage: base}
	default:
		slog.Error("unknown ranked play stage", "room", c.room.id, "stage", next)
		return
	}

	c.enter(s)
}

// enter makes s the current stage: its begin runs, then clients are told and its countdown starts.
//
// Unlike the reference server, nothing is started for a stage that has already been left by the time begin
// returns (a begin that moves on at once, such as everybody already being ready). The reference server would
// start the abandoned stage's countdown anyway, replacing the current stage's.
func (c *rankedPlay) enter(s rankedStage) {
	c.stage = s
	c.state.Stage = s.stage()

	seq := c.room.countdownSeq
	s.begin(func() {
		if c.stage != s || c.room.closed {
			return
		}

		c.broadcastState()

		// begin may already have started the countdown it wants (everybody ready: a short one).
		if c.room.countdownSeq != seq {
			return
		}
		c.finishWithCountdown(s, s.duration())
	})
}

// finishWithCountdown replaces the stage countdown by one that ends s after d (immediately when d is zero, never
// when d is noCountdown).
func (c *rankedPlay) finishWithCountdown(s rankedStage, d time.Duration) {
	c.room.stopCountdownsOfKind(protocol.CountdownRankedPlayStage)

	switch {
	case d == noCountdown:
	case d <= 0:
		s.finish()
	default:
		c.room.startCountdown(protocol.CountdownRankedPlayStage, s.stage(), d, func() {
			if c.stage == s {
				s.finish()
			}
		})
	}
}

// --- cards, damage, completion -----------------------------------------------------------------------

func (c *rankedPlay) deckCount() int { return len(c.deck) }

// hasRoundsRemaining reports whether another round can be played: two players alive and cards left to play.
func (c *rankedPlay) hasRoundsRemaining() bool {
	alive, cards := 0, c.deckCount()
	for _, u := range c.state.Users {
		if u.Life > 0 {
			alive++
		}
		cards += len(u.Hand)
	}
	return alive > 1 && cards > 0
}

// addCards deals up to count cards from the deck. The owner sees what each card is; the opponent only sees that
// a card was added.
func (c *rankedPlay) addCards(userID int32, count int) {
	user := c.state.Users[userID]
	if user == nil {
		return
	}

	if count > len(c.deck) {
		count = len(c.deck)
	}
	dealt := c.deck[:count]
	c.deck = c.deck[count:]

	for _, card := range dealt {
		user.Hand = append(user.Hand, card)
		c.room.broadcast("RankedPlayCardAdded", userID, card)
		c.room.ev.Send(userID, "RankedPlayCardRevealed", card, c.effects[card.ID])
	}

	c.broadcastState()
}

// removeCards takes cards out of a hand.
func (c *rankedPlay) removeCards(userID int32, cards ...protocol.CardItem) {
	user := c.state.Users[userID]
	if user == nil {
		return
	}

	for _, card := range cards {
		for i, held := range user.Hand {
			if held.ID == card.ID {
				user.Hand = append(user.Hand[:i], user.Hand[i+1:]...)
				break
			}
		}
		c.room.broadcast("RankedPlayCardRemoved", userID, card)
	}

	c.broadcastState()
}

func (c *rankedPlay) handHolds(userID int32, card protocol.CardItem) bool {
	user := c.state.Users[userID]
	if user == nil {
		return false
	}

	for _, held := range user.Hand {
		if held.ID == card.ID {
			return true
		}
	}
	return false
}

// activateCard reveals the played card to everybody and makes its beatmap the room's current playlist item.
func (c *rankedPlay) activateCard(card protocol.CardItem) error {
	effect, ok := c.effects[card.ID]
	if !ok {
		return invalidState("Unknown card.")
	}

	r := c.room
	r.broadcast("RankedPlayCardRevealed", card, effect)
	r.broadcast("RankedPlayCardPlayed", card)

	ctx, cancel := r.storeContext(context.Background())
	defer cancel()

	added, err := r.addServerItem(ctx, effect)
	if err != nil {
		return err
	}

	previous := r.settings.PlaylistItemID
	r.settings.PlaylistItemID = added.ID
	r.handleSettingsChanged(true)

	// The room starts out with an empty placeholder item, since the client wants a current item at all times.
	if previous == 0 {
		r.removePlaceholderItem()
	}

	c.lastActivated = &card
	return nil
}

// damage hurts a player and describes what happened. A player cannot be killed from full life in one hit.
func (c *rankedPlay) damage(userID int32, direct int32, multiplier float64, bonus int32) protocol.DamageInfo {
	user := c.state.Users[userID]
	total := int32(math.Ceil(float64(direct)*multiplier)) + bonus

	floor := int32(0)
	if user.Life == protocol.StartingLife {
		floor = 1
	}

	info := protocol.DamageInfo{
		Damage:       total,
		RawDamage:    direct + bonus,
		OldLife:      user.Life,
		NewLife:      max(floor, user.Life-total),
		DirectDamage: direct,
		Multiplier:   multiplier,
		BonusDamage:  bonus,
	}
	user.Life = info.NewLife
	return info
}

// killUser takes all life from a player who left, and ends the match if it cannot go on.
func (c *rankedPlay) killUser(user protocol.RoomUser) {
	if c.ranked {
		c.svc.BanUser(user.UserID, RankedPlayBanDuration)
	}

	if info := c.state.Users[user.UserID]; info != nil {
		info.Life = 0
	}
	c.broadcastState()

	if !c.hasRoundsRemaining() {
		c.matchCompleted()
	}
}

func (c *rankedPlay) closeMatch() { c.goTo(protocol.StageEnded) }

// matchCompleted decides the winner and, for ranked matches, the new ratings. It runs once.
func (c *rankedPlay) matchCompleted() {
	if c.ratingsUpdated {
		return
	}
	c.ratingsUpdated = true

	result := MatchResult{RoomID: c.room.id, PoolID: c.poolID, Ranked: c.ranked, Played: c.state.CurrentRound != 0}

	if result.Played {
		var best int32 = -1
		var winners []int32
		for id, u := range c.state.Users {
			switch {
			case u.Life > best:
				best, winners = u.Life, []int32{id}
			case u.Life == best:
				winners = append(winners, id)
			}
		}

		// Equal life is a draw.
		if len(winners) == 1 {
			winner := winners[0]
			c.state.WinningUserID = &winner
			result.Winner = &winner
		}
	}

	ids := make([]int32, 0, len(c.state.Users))
	for id := range c.state.Users {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	for _, id := range ids {
		u := c.state.Users[id]
		result.Players = append(result.Players, PlayerResult{UserID: id, Rating: u.Rating, Life: u.Life})
	}

	ctx, cancel := c.room.storeContext(context.Background())
	defer cancel()

	after, err := c.svc.MatchCompleted(ctx, result)
	if err != nil {
		slog.Error("recording the ranked play match failed", "room", c.room.id, "error", err)
	}
	for id, rating := range after {
		if u := c.state.Users[id]; u != nil {
			u.RatingAfter = rating
		}
	}

	c.svc.MatchRecorded(c.poolID, c.state.Clone())
}

// startGameplay starts loading the played beatmap for the players who are ready.
func (c *rankedPlay) startGameplay() {
	r := c.room

	if r.state != protocol.RoomOpen || r.currentItem().Expired {
		slog.Error("cannot start ranked play gameplay", "room", r.id, "roomState", r.state)
		return
	}

	// With nobody ready there is no match to play; the round is simply over.
	if !r.anyUser(func(u *protocol.RoomUser) bool { return u.State == protocol.UserReady }) {
		c.gameplayCompleted()
		return
	}

	if err := r.startMatch(); err != nil {
		slog.Error("starting ranked play gameplay failed", "room", r.id, "error", err)
	}
}
