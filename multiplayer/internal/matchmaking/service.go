package matchmaking

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"math/rand"
	"sort"
	"sync"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// Timings of the service. The zero value of every field selects the reference server's.
const (
	defaultQueueUpdateRate = time.Second
	defaultLobbyUpdateRate = 5 * time.Second
	defaultPoolRefreshRate = time.Minute
	// How long a player who declined an invitation of a ranked queue cannot be matched.
	declineBan = time.Minute

	defaultRecentMatchupTimeout = 10 * time.Minute
	duelSearchTimeout           = 5 * time.Minute
	duelCooldown                = 30 * time.Second
	defaultCreateTimeout        = 30 * time.Second

	ratingBucketSize    = 25
	ratingRefreshPeriod = 5 * time.Minute
	lobbyQueueSample    = 50
	recentMatchesKept   = 50
)

// Config configures a Service.
type Config struct {
	Directory Directory
	Rooms     Rooms
	Backend   Backend
	Notifier  Notifier

	Clock Clock
	Rand  *rand.Rand

	QueueUpdateRate      time.Duration
	LobbyUpdateRate      time.Duration
	PoolRefreshRate      time.Duration
	RecentMatchupTimeout time.Duration
	// DisableBans turns off keeping players out of the queue (for abandoning or declining) and keeping recent
	// opponents apart.
	DisableBans   bool
	CreateTimeout time.Duration
	DeckSize      int

	// Match configures the matches that are created (stage timings).
	Match room.RankedPlayConfig
}

// Service is the matchmaking queue. All its methods are safe for concurrent use.
type Service struct {
	cfg  Config
	bans *Bans

	mu         sync.Mutex
	queues     map[int32]*Queue
	duels      map[string]*Queue
	lobbies    map[int32]*lobby
	recentDuel map[int32]time.Time
	refreshAt  time.Time

	matches sync.WaitGroup
}

// lobby is the players watching a pool's queue (the screen before queueing).
type lobby struct {
	users map[int32]bool

	distribution   []protocol.RatingBucket
	distributionAt time.Time

	// matches finished since the last broadcast, and the most recent ones for those who just arrived
	fresh  []msgpack.RawMessage
	recent []msgpack.RawMessage
}

// New creates a service.
func New(cfg Config) *Service {
	if cfg.Clock == nil {
		cfg.Clock = realClock{}
	}
	if cfg.Rand == nil {
		cfg.Rand = rand.New(rand.NewSource(time.Now().UnixNano()))
	}
	if cfg.QueueUpdateRate <= 0 {
		cfg.QueueUpdateRate = defaultQueueUpdateRate
	}
	if cfg.LobbyUpdateRate <= 0 {
		cfg.LobbyUpdateRate = defaultLobbyUpdateRate
	}
	if cfg.PoolRefreshRate <= 0 {
		cfg.PoolRefreshRate = defaultPoolRefreshRate
	}
	if cfg.RecentMatchupTimeout <= 0 {
		cfg.RecentMatchupTimeout = defaultRecentMatchupTimeout
	}
	if cfg.CreateTimeout <= 0 {
		cfg.CreateTimeout = defaultCreateTimeout
	}
	if cfg.DeckSize <= 0 {
		cfg.DeckSize = DeckSize
	}

	return &Service{
		cfg:        cfg,
		bans:       NewBans(cfg.Clock),
		queues:     map[int32]*Queue{},
		duels:      map[string]*Queue{},
		lobbies:    map[int32]*lobby{},
		recentDuel: map[int32]time.Time{},
	}
}

// Bans returns the list of players kept out of the queue.
func (s *Service) Bans() *Bans { return s.bans }

// Wait blocks until the matches being created have been created. For tests and for shutting down.
func (s *Service) Wait() { s.matches.Wait() }

func (s *Service) queueOptions(keepInQueue bool, searchTimeout time.Duration) QueueOptions {
	return QueueOptions{
		SearchTimeout:        searchTimeout,
		RecentMatchupTimeout: s.cfg.RecentMatchupTimeout,
		KeepInQueueOnDecline: keepInQueue,
		AllowBans:            !s.cfg.DisableBans,
		Clock:                s.cfg.Clock,
		Rand:                 s.cfg.Rand,
	}
}

// --- running -----------------------------------------------------------------------------------------

// Run drives the queue until ctx ends: queues are updated every second, lobbies every few seconds and the pools
// are re-read from the database every minute.
func (s *Service) Run(ctx context.Context) {
	queueTicker := time.NewTicker(s.cfg.QueueUpdateRate)
	lobbyTicker := time.NewTicker(s.cfg.LobbyUpdateRate)
	defer queueTicker.Stop()
	defer lobbyTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-queueTicker.C:
			s.Tick()
			if s.refreshDue() {
				s.RefreshPools(ctx)
			}
		case <-lobbyTicker.C:
			s.UpdateLobbies(ctx)
		}
	}
}

func (s *Service) refreshDue() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.cfg.Clock.Now().Before(s.refreshAt)
}

// Tick pairs the waiting players of every queue and expires invitations. Matches that become ready are created in
// the background.
func (s *Service) Tick() {
	s.mu.Lock()
	var ready []readyMatch

	for _, q := range s.queues {
		ready = append(ready, s.process(q, q.Update())...)
	}
	for id, q := range s.duels {
		if q.Count() == 0 {
			delete(s.duels, id)
			continue
		}
		ready = append(ready, s.process(q, q.Update())...)
	}
	s.mu.Unlock()

	s.startMatches(ready)
}

// RefreshPools re-reads every queue's pool: a pool that was switched off or deleted empties its queue, and the
// others pick up edited settings and the current top 100 rating.
func (s *Service) RefreshPools(ctx context.Context) {
	s.mu.Lock()
	s.refreshAt = s.cfg.Clock.Now().Add(s.cfg.PoolRefreshRate)
	queues := make(map[int32]*Queue, len(s.queues))
	for id, q := range s.queues {
		queues[id] = q
	}
	s.mu.Unlock()

	type refreshed struct {
		pool *Pool
		top  int32
		err  error
	}
	results := make(map[int32]refreshed, len(queues))
	for id := range queues {
		pool, err := s.cfg.Directory.Pool(ctx, id)
		var top int32
		if err == nil && pool != nil && pool.Active {
			top, err = s.cfg.Directory.TopRating(ctx, id)
		}
		results[id] = refreshed{pool, top, err}
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	for id, q := range queues {
		r := results[id]
		switch {
		case r.err != nil:
			slog.Warn("could not refresh a matchmaking pool", "pool", id, "error", r.err)
		case r.pool == nil || !r.pool.Active:
			s.process(q, q.Clear())
		default:
			pool := *r.pool
			pool.Ranked = true
			q.SetPool(pool, r.top)
		}
	}
}

// --- queue -------------------------------------------------------------------------------------------

type readyMatch struct {
	pool  Pool
	group *QueueGroup
}

// process tells the players what happened to them and returns the matches that are ready to be created. The
// service lock must be held.
func (s *Service) process(q *Queue, b Bundle) []readyMatch {
	pool := q.Pool()

	if pool.Ranked && !s.cfg.DisableBans {
		for _, u := range b.Declined {
			s.bans.Ban(u.UserID, declineBan)
		}
	}

	for _, u := range b.Removed {
		s.cfg.Notifier.Send(u.UserID, "MatchmakingQueueLeft")
	}
	for _, u := range b.Added {
		s.cfg.Notifier.Send(u.UserID, "MatchmakingQueueJoined")
		s.cfg.Notifier.Send(u.UserID, "MatchmakingQueueStatusChanged", protocol.QueueSearching())
	}

	// Unlike the reference server, the obsolete MatchmakingRoomInvited (for clients from before ranked play)
	// is not sent as well: every client that can play ranked play understands the version with parameters.
	for _, g := range b.Formed {
		for _, u := range g.Users {
			s.cfg.Notifier.Send(u.UserID, "MatchmakingRoomInvitedWithParams", protocol.RoomInvitationParams{Type: pool.Type})
			s.cfg.Notifier.Send(u.UserID, "MatchmakingQueueStatusChanged", protocol.QueueMatchFound())
		}
	}

	var ready []readyMatch
	for _, g := range b.Completed {
		ready = append(ready, readyMatch{pool: pool, group: g})
	}
	return ready
}

// removeFromAll takes the player out of every queue they are in. The service lock must be held.
func (s *Service) removeFromAll(userID int32) []readyMatch {
	var ready []readyMatch
	for _, q := range s.queues {
		ready = append(ready, s.process(q, q.Remove(userID))...)
	}
	for _, q := range s.duels {
		ready = append(ready, s.process(q, q.Remove(userID))...)
	}
	return ready
}

// queuePool looks the pool up and checks that it can be queued for.
func (s *Service) queuePool(ctx context.Context, poolID int32) (Pool, error) {
	pool, err := s.cfg.Directory.Pool(ctx, poolID)
	if err != nil {
		return Pool{}, err
	}
	if pool == nil {
		return Pool{}, invalid("Pool not found: %d", poolID)
	}
	if !pool.Active {
		return Pool{}, invalid("The selected matchmaking pool is no longer active.")
	}
	if pool.Type != protocol.PoolRankedPlay {
		return Pool{}, invalid("Only ranked play pools can be queued for.")
	}

	result := *pool
	result.Ranked = true
	return result, nil
}

func (s *Service) newQueueUser(ctx context.Context, poolID, userID int32) (*QueueUser, error) {
	rating, err := s.cfg.Directory.UserRating(ctx, poolID, userID)
	if err != nil {
		return nil, err
	}

	user := &QueueUser{UserID: userID, Rating: rating}
	if !s.cfg.DisableBans {
		user.BanEnd = s.bans.EndOf(userID)
	}
	return user, nil
}

// JoinQueue puts a player in the queue of a pool, taking them out of any other.
func (s *Service) JoinQueue(ctx context.Context, userID, poolID int32) error {
	info, err := s.cfg.Directory.User(ctx, userID)
	if err != nil {
		return err
	}
	if info.Restricted {
		return invalid("Can't queue when restricted.")
	}

	pool, err := s.queuePool(ctx, poolID)
	if err != nil {
		return err
	}
	user, err := s.newQueueUser(ctx, poolID, userID)
	if err != nil {
		return err
	}

	s.mu.Lock()
	ready := s.removeFromAll(userID)

	q, ok := s.queues[pool.ID]
	if !ok {
		q = NewQueue(pool, s.queueOptions(true, 0))
		s.queues[pool.ID] = q
		s.refreshAt = time.Time{} // the first refresh also fetches the top 100 rating
	}
	ready = append(ready, s.process(q, q.Add(user))...)
	s.mu.Unlock()

	s.startMatches(ready)
	return nil
}

// LeaveQueue takes a player out of every queue.
func (s *Service) LeaveQueue(userID int32) {
	s.mu.Lock()
	ready := s.removeFromAll(userID)
	s.mu.Unlock()

	s.startMatches(ready)
}

// AcceptInvitation records that an invited player accepted. When both have, the match is created.
func (s *Service) AcceptInvitation(userID int32) {
	s.mu.Lock()

	// The player learns at once that their answer was received.
	s.cfg.Notifier.Send(userID, "MatchmakingQueueStatusChanged", protocol.QueueJoiningMatch())

	var ready []readyMatch
	for _, q := range s.queues {
		ready = append(ready, s.process(q, q.MarkInvitationAccepted(userID))...)
	}
	for _, q := range s.duels {
		ready = append(ready, s.process(q, q.MarkInvitationAccepted(userID))...)
	}
	s.mu.Unlock()

	s.startMatches(ready)
}

// DeclineInvitation removes a player who turned an invitation down.
func (s *Service) DeclineInvitation(userID int32) {
	s.mu.Lock()
	for _, q := range s.queues {
		s.process(q, q.MarkInvitationDeclined(userID))
	}
	for _, q := range s.duels {
		s.process(q, q.MarkInvitationDeclined(userID))
	}
	s.mu.Unlock()
}

// Disconnected removes a player who went offline from every queue and lobby.
func (s *Service) Disconnected(userID int32) {
	s.mu.Lock()
	ready := s.removeFromAll(userID)
	for _, l := range s.lobbies {
		delete(l.users, userID)
	}
	s.mu.Unlock()

	s.startMatches(ready)
}

// --- duels -------------------------------------------------------------------------------------------

// IssueDuel challenges another player to an unranked match on the maps of a pool. The challenger waits in a queue
// of their own until the challenged player accepts.
func (s *Service) IssueDuel(ctx context.Context, userID int32, request protocol.IssueDuelRequest) error {
	info, err := s.cfg.Directory.User(ctx, userID)
	if err != nil {
		return err
	}
	if info.Restricted {
		return invalid("Can't duel when restricted.")
	}
	if request.UserID == userID {
		return invalid("You can't duel yourself.")
	}
	if _, err := s.cfg.Directory.User(ctx, request.UserID); err != nil {
		return invalid("The player you want to duel could not be found.")
	}

	pool, err := s.queuePool(ctx, request.PoolID)
	if err != nil {
		return err
	}
	user, err := s.newQueueUser(ctx, pool.ID, userID)
	if err != nil {
		return err
	}
	user.BanEnd = time.Time{} // duels ignore queue bans

	now := s.cfg.Clock.Now()

	s.mu.Lock()
	if now.Before(s.recentDuel[userID]) {
		s.mu.Unlock()
		return invalid("You are requesting too many duels. Slow down.")
	}
	s.recentDuel[userID] = now.Add(duelCooldown)

	ready := s.removeFromAll(userID)

	// A duel is a match of two, between whoever is invited, unranked, and with no rating search.
	duel := pool
	duel.LobbySize = 2
	duel.SearchRadius = math.MaxFloat32
	duel.SearchRadiusMax = math.MaxFloat32
	duel.Ranked = false

	q := NewQueue(duel, s.queueOptions(false, duelSearchTimeout))

	id := protocol.NewGUID()
	s.duels[id] = q

	// The challenger joins before the challenged player is told, so that the invitation is never for nothing.
	ready = append(ready, s.process(q, q.Add(user))...)
	s.cfg.Notifier.Send(request.UserID, "MatchmakingDuelIssued", protocol.DuelIssuedParams{ID: id, UserID: userID, Pool: pool.Protocol()})
	s.mu.Unlock()

	s.startMatches(ready)
	return nil
}

// AcceptDuel joins the duel a player was challenged to. A duel that no longer exists (the challenger gave up)
// is not an error.
func (s *Service) AcceptDuel(ctx context.Context, userID int32, request protocol.AcceptDuelRequest) error {
	s.mu.Lock()
	q, ok := s.duels[request.ID]
	s.mu.Unlock()
	if !ok {
		return nil
	}

	user, err := s.newQueueUser(ctx, q.Pool().ID, userID)
	if err != nil {
		return err
	}
	user.BanEnd = time.Time{}

	s.mu.Lock()
	defer s.mu.Unlock()

	if current, ok := s.duels[request.ID]; !ok || current != q {
		return nil
	}

	// Leave every queue and every other duel.
	for _, other := range s.queues {
		s.process(other, other.Remove(userID))
	}
	for id, other := range s.duels {
		if id != request.ID {
			s.process(other, other.Remove(userID))
		}
	}

	s.process(q, q.Add(user))
	return nil
}

// --- lobby -------------------------------------------------------------------------------------------

// Pools returns the pools of a type that can be queued for.
func (s *Service) Pools(ctx context.Context, poolType protocol.PoolType) ([]protocol.Pool, error) {
	pools, err := s.cfg.Directory.Pools(ctx, poolType)
	if err != nil {
		return nil, err
	}

	out := make([]protocol.Pool, 0, len(pools))
	for _, p := range pools {
		out = append(out, p.Protocol())
	}
	return out, nil
}

// JoinLobby makes a player watch a pool's queue, and sends them its current state.
func (s *Service) JoinLobby(ctx context.Context, userID, poolID int32) error {
	pool, err := s.cfg.Directory.Pool(ctx, poolID)
	if err != nil {
		return err
	}
	if pool == nil {
		return invalid("Pool not found: %d", poolID)
	}
	if !pool.Active {
		return invalid("The selected matchmaking pool is no longer active.")
	}

	if err := s.refreshDistribution(ctx, poolID); err != nil {
		slog.Warn("could not fetch the rating distribution", "pool", poolID, "error", err)
	}

	rating, err := s.cfg.Directory.UserRating(ctx, poolID, userID)
	if err != nil {
		return err
	}

	s.mu.Lock()
	for _, l := range s.lobbies {
		delete(l.users, userID)
	}
	l := s.lobbyFor(poolID)
	l.users[userID] = true

	status := s.lobbyStatus(poolID, l.recent)
	mu, sigma := int32(math.Round(rating.Mu)), rating.Sigma
	status.UserRating, status.UserSigma = &mu, &sigma
	status.UserRank = rating.Rank
	s.mu.Unlock()

	s.cfg.Notifier.Send(userID, "MatchmakingLobbyStatusChanged", status)
	return nil
}

// LeaveLobby stops a player watching any pool.
func (s *Service) LeaveLobby(userID int32) {
	s.mu.Lock()
	defer s.mu.Unlock()

	for _, l := range s.lobbies {
		delete(l.users, userID)
	}
}

// UpdateLobbies sends every lobby's watchers the state of their pool's queue and the matches finished since.
func (s *Service) UpdateLobbies(ctx context.Context) {
	s.mu.Lock()
	var pools []int32
	for id, l := range s.lobbies {
		if len(l.users) > 0 {
			pools = append(pools, id)
		}
	}
	s.mu.Unlock()

	for _, id := range pools {
		if err := s.refreshDistribution(ctx, id); err != nil {
			slog.Warn("could not fetch the rating distribution", "pool", id, "error", err)
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	for _, id := range pools {
		l := s.lobbies[id]
		if l == nil {
			continue
		}

		status := s.lobbyStatus(id, l.fresh)
		l.fresh = nil

		users := make([]int32, 0, len(l.users))
		for u := range l.users {
			users = append(users, u)
		}
		sort.Slice(users, func(i, j int) bool { return users[i] < users[j] })
		for _, u := range users {
			s.cfg.Notifier.Send(u, "MatchmakingLobbyStatusChanged", status)
		}
	}
}

func (s *Service) lobbyFor(poolID int32) *lobby {
	l, ok := s.lobbies[poolID]
	if !ok {
		l = &lobby{users: map[int32]bool{}}
		s.lobbies[poolID] = l
	}
	return l
}

// lobbyStatus describes a pool's queue: who is in it (a sample), the rating distribution and the given matches.
// The service lock must be held.
func (s *Service) lobbyStatus(poolID int32, matches []msgpack.RawMessage) protocol.LobbyStatus {
	status := protocol.LobbyStatus{RecentMatches: append([]msgpack.RawMessage{}, matches...)}

	if l := s.lobbies[poolID]; l != nil {
		status.RatingDistribution = append([]protocol.RatingBucket{}, l.distribution...)
	}

	if q := s.queues[poolID]; q != nil {
		users := q.Users()
		s.cfg.Rand.Shuffle(len(users), func(i, j int) { users[i], users[j] = users[j], users[i] })
		for _, u := range users[:min(len(users), lobbyQueueSample)] {
			status.UsersInQueue = append(status.UsersInQueue, u.UserID)
		}
	}
	return status
}

// refreshDistribution re-reads the pool's ratings and groups them in buckets, at most every few minutes.
func (s *Service) refreshDistribution(ctx context.Context, poolID int32) error {
	s.mu.Lock()
	l := s.lobbyFor(poolID)
	fresh := s.cfg.Clock.Now().Sub(l.distributionAt) < ratingRefreshPeriod && !l.distributionAt.IsZero()
	s.mu.Unlock()
	if fresh {
		return nil
	}

	ratings, err := s.cfg.Directory.Ratings(ctx, poolID)
	if err != nil {
		return err
	}

	counts := map[int32]int32{}
	for _, rating := range ratings {
		counts[int32(math.Floor(float64(rating)/ratingBucketSize))*ratingBucketSize]++
	}
	buckets := make([]protocol.RatingBucket, 0, len(counts))
	for rating, count := range counts {
		buckets = append(buckets, protocol.RatingBucket{Rating: rating, Count: count})
	}
	sort.Slice(buckets, func(i, j int) bool { return buckets[i].Rating < buckets[j].Rating })

	s.mu.Lock()
	defer s.mu.Unlock()
	l = s.lobbyFor(poolID)
	l.distribution, l.distributionAt = buckets, s.cfg.Clock.Now()
	return nil
}

// recordMatch is told about every finished match of a pool: the lobby shows it, and the two players are kept
// apart for a while.
func (s *Service) recordMatch(poolID int32, state protocol.RankedPlayRoomState) {
	raw, err := msgpack.Marshal((&state).Union())
	if err != nil {
		slog.Error("encoding a finished match failed", "pool", poolID, "error", err)
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	l := s.lobbyFor(poolID)
	l.fresh = append(l.fresh, raw)
	l.recent = append(l.recent, raw)
	if len(l.recent) > recentMatchesKept {
		l.recent = l.recent[len(l.recent)-recentMatchesKept:]
	}

	if q := s.queues[poolID]; q != nil {
		ids := make([]int32, 0, len(state.Users))
		for id := range state.Users {
			ids = append(ids, id)
		}
		for i := range ids {
			for j := i + 1; j < len(ids); j++ {
				q.MarkRecentMatchup(ids[i], ids[j])
			}
		}
	}
}

// --- creating matches --------------------------------------------------------------------------------

func (s *Service) startMatches(ready []readyMatch) {
	for _, m := range ready {
		s.matches.Add(1)
		go func() {
			defer s.matches.Done()
			s.createMatch(m.pool, m.group)
		}()
	}
}

// createMatch makes the room two players who accepted an invitation play in, and tells them to join it.
func (s *Service) createMatch(pool Pool, group *QueueGroup) {
	ctx, cancel := context.WithTimeout(context.Background(), s.cfg.CreateTimeout)
	defer cancel()

	if err := s.tryCreateMatch(ctx, pool, group); err != nil {
		slog.Error("could not create the match", "pool", pool.ID, "error", err)

		// The players already left the queue; tell them it did not work out.
		for _, u := range group.Users {
			s.cfg.Notifier.Send(u.UserID, "MatchmakingQueueLeft")
		}
	}
}

func (s *Service) tryCreateMatch(ctx context.Context, pool Pool, group *QueueGroup) error {
	cards, err := s.cfg.Directory.Cards(ctx, pool.ID)
	if err != nil {
		return fmt.Errorf("loading the cards of pool %d: %w", pool.ID, err)
	}

	players := make([]room.RankedPlayPlayer, 0, len(group.Users))
	ratings := make([]float64, 0, len(group.Users))
	names := make([]string, 0, len(group.Users))
	for _, u := range group.Users {
		players = append(players, room.RankedPlayPlayer{UserID: u.UserID, Rating: int32(math.Round(u.Rating.Mu))})
		ratings = append(ratings, u.Rating.Mu)

		info, err := s.cfg.Directory.User(ctx, u.UserID)
		if err != nil {
			return fmt.Errorf("loading user %d: %w", u.UserID, err)
		}
		names = append(names, info.Username)
	}

	picked := SelectCards(cards, s.cfg.DeckSize, ratings, s.cfg.Rand)
	s.cfg.Rand.Shuffle(len(picked), func(i, j int) { picked[i], picked[j] = picked[j], picked[i] })

	deck := make([]room.DeckCard, 0, len(picked))
	for _, card := range picked {
		item, err := card.PlaylistItem()
		if err != nil {
			slog.Error("skipping a card with unreadable mods", "pool", pool.ID, "beatmap", card.BeatmapID, "error", err)
			continue
		}
		deck = append(deck, room.DeckCard{Card: protocol.NewCardItem(), Item: item})
	}

	name := pool.DisplayName()
	if len(names) == 2 {
		name = fmt.Sprintf("%s: %s vs %s", pool.DisplayName(), names[0], names[1])
	}

	created, password, err := s.cfg.Rooms.CreateRankedPlay(ctx, room.RankedPlayParams{
		Name:     name,
		Players:  players,
		Deck:     deck,
		Ranked:   pool.Ranked,
		PoolID:   pool.ID,
		Services: matchServices{s},
		Config:   s.cfg.Match,
	})
	if err != nil {
		return fmt.Errorf("creating the room: %w", err)
	}

	for _, u := range group.Users {
		s.cfg.Notifier.Send(u.UserID, "MatchmakingRoomReady", created.ID(), password)
	}
	return nil
}

// matchServices is what a match needs from the service: scores and results from the backend, bans, and the lobby.
type matchServices struct{ s *Service }

func (m matchServices) Scores(ctx context.Context, itemID int64) (room.RankedPlayScores, error) {
	return m.s.cfg.Backend.Scores(ctx, itemID)
}

func (m matchServices) BanUser(userID int32, d time.Duration) {
	if !m.s.cfg.DisableBans {
		m.s.bans.Ban(userID, d)
	}
}

func (m matchServices) MatchCompleted(ctx context.Context, result room.MatchResult) (map[int32]int32, error) {
	return m.s.cfg.Backend.RecordResult(ctx, result.PoolID, result)
}

func (m matchServices) MatchRecorded(poolID int32, state protocol.RankedPlayRoomState) {
	m.s.recordMatch(poolID, state)
}
