package matchmaking

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync"
	"time"
)

// Clock tells the time. Tests substitute one they can set.
type Clock interface {
	Now() time.Time
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

// QueueUser is a player waiting in a queue, or invited to a match from it.
type QueueUser struct {
	UserID int32
	Rating Rating

	SearchStart time.Time
	InviteStart time.Time
	// BanEnd is when the player may be matched again after leaving a match or declining an invitation.
	BanEnd time.Time

	InviteAccepted bool
	Group          *QueueGroup
}

// QueueGroup is the players a match was made for.
type QueueGroup struct {
	ID    string
	Users []*QueueUser
}

// Bundle is what changed in a queue during one operation, for the caller to tell the players about.
type Bundle struct {
	Formed    []*QueueGroup // groups that were just made: invite their players
	Completed []*QueueGroup // groups whose players all accepted: create their match
	Recycled  []*QueueGroup // groups that fell apart
	Added     []*QueueUser  // players who are (again) searching
	Removed   []*QueueUser  // players who left the queue
	Declined  []*QueueUser  // removed players who had been invited and did not accept
}

func (b *Bundle) append(other Bundle) {
	b.Formed = append(b.Formed, other.Formed...)
	b.Completed = append(b.Completed, other.Completed...)
	b.Recycled = append(b.Recycled, other.Recycled...)
	b.Added = append(b.Added, other.Added...)
	b.Removed = append(b.Removed, other.Removed...)
	b.Declined = append(b.Declined, other.Declined...)
}

// QueueOptions tunes a queue. The zero value is valid.
type QueueOptions struct {
	// InviteTimeout is how long an invited player has to accept. Default 30s.
	InviteTimeout time.Duration
	// SearchTimeout is how long a player waits for an opponent. Default: for ever.
	SearchTimeout time.Duration
	// RecentMatchupTimeout is how long two players who just met are kept from meeting again.
	RecentMatchupTimeout time.Duration
	// KeepInQueueOnDecline puts the others of a group back to searching when one player declines. Duels do not.
	KeepInQueueOnDecline bool
	// AllowBans enables keeping recent opponents apart.
	AllowBans bool

	Clock Clock
	Rand  *rand.Rand
}

// DefaultInviteTimeout is how long an invited player has to accept.
const DefaultInviteTimeout = 30 * time.Second

// Queue is the waiting players of one pool, and the logic that pairs them.
type Queue struct {
	opts QueueOptions

	mu        sync.Mutex
	pool      Pool
	top100    float64
	users     map[int32]*QueueUser
	nextGroup int
	recent    map[[2]int32]time.Time
}

// NewQueue creates an empty queue for a pool.
func NewQueue(pool Pool, opts QueueOptions) *Queue {
	if opts.InviteTimeout <= 0 {
		opts.InviteTimeout = DefaultInviteTimeout
	}
	if opts.SearchTimeout <= 0 {
		opts.SearchTimeout = time.Duration(math.MaxInt64)
	}
	if opts.Clock == nil {
		opts.Clock = realClock{}
	}
	if opts.Rand == nil {
		opts.Rand = rand.New(rand.NewSource(time.Now().UnixNano()))
	}

	return &Queue{opts: opts, pool: pool, users: map[int32]*QueueUser{}, recent: map[[2]int32]time.Time{}}
}

// Pool returns the pool the queue is for.
func (q *Queue) Pool() Pool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.pool
}

// SetPool replaces the pool settings (they can be edited while the queue runs) and the top 100 rating.
func (q *Queue) SetPool(pool Pool, top100 int32) {
	q.mu.Lock()
	defer q.mu.Unlock()

	q.pool = pool
	q.top100 = float64(top100)
}

// Count returns how many players are in the queue, invited ones included.
func (q *Queue) Count() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.users)
}

// Has reports whether the player is in the queue.
func (q *Queue) Has(userID int32) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	_, ok := q.users[userID]
	return ok
}

// Users returns the players in the queue, ordered by user id.
func (q *Queue) Users() []*QueueUser {
	q.mu.Lock()
	defer q.mu.Unlock()

	users := make([]*QueueUser, 0, len(q.users))
	for _, u := range q.users {
		users = append(users, u)
	}
	sort.Slice(users, func(i, j int) bool { return users[i].UserID < users[j].UserID })
	return users
}

// Add puts a player in the queue. A player who is already in it is left alone.
func (q *Queue) Add(user *QueueUser) Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	var bundle Bundle
	if _, ok := q.users[user.UserID]; ok {
		return bundle
	}

	user.SearchStart = q.opts.Clock.Now()
	q.users[user.UserID] = user
	bundle.Added = append(bundle.Added, user)
	return bundle
}

// Remove takes a player out of the queue. For somebody who was invited that counts as declining.
func (q *Queue) Remove(userID int32) Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	user, ok := q.users[userID]
	if !ok {
		return Bundle{}
	}
	return q.removeFromQueue([]*QueueUser{user}, true)
}

// Clear empties the queue.
func (q *Queue) Clear() Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	users := make([]*QueueUser, 0, len(q.users))
	for _, u := range q.users {
		users = append(users, u)
	}
	if len(users) == 0 {
		return Bundle{}
	}
	return q.removeFromQueue(users, false)
}

// MarkInvitationAccepted records that a player accepted. Once every player of their group has, the group is
// complete and leaves the queue.
func (q *Queue) MarkInvitationAccepted(userID int32) Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	var bundle Bundle
	user, ok := q.users[userID]
	if !ok || user.Group == nil {
		return bundle
	}

	user.InviteAccepted = true
	for _, u := range user.Group.Users {
		if !u.InviteAccepted {
			return bundle
		}
	}

	for _, u := range user.Group.Users {
		delete(q.users, u.UserID)
	}
	bundle.Completed = append(bundle.Completed, user.Group)
	return bundle
}

// MarkInvitationDeclined removes a player who turned the invitation down.
func (q *Queue) MarkInvitationDeclined(userID int32) Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	user, ok := q.users[userID]
	if !ok {
		return Bundle{}
	}
	return q.removeFromQueue([]*QueueUser{user}, true)
}

// MarkRecentMatchup keeps two players who just played each other from being paired again for a while.
func (q *Queue) MarkRecentMatchup(a, b int32) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.recent[matchup(a, b)] = q.opts.Clock.Now().Add(q.opts.RecentMatchupTimeout)
}

func matchup(a, b int32) [2]int32 {
	if a > b {
		a, b = b, a
	}
	return [2]int32{a, b}
}

// isRecentMatchup is called with the lock held.
func (q *Queue) isRecentMatchup(a, b int32) bool {
	if !q.opts.AllowBans {
		return false
	}
	expires, ok := q.recent[matchup(a, b)]
	return ok && !expires.Before(q.opts.Clock.Now())
}

// Update pairs waiting players into groups and expires invitations and searches that took too long.
func (q *Queue) Update() Bundle {
	q.mu.Lock()
	defer q.mu.Unlock()

	var bundle Bundle
	now := q.opts.Clock.Now()

	for _, users := range q.matchUsers() {
		group := &QueueGroup{ID: fmt.Sprintf("matchmaking-%d", q.nextGroup), Users: users}
		q.nextGroup++

		for _, u := range users {
			u.InviteStart = now
			u.Group = group
		}
		bundle.Formed = append(bundle.Formed, group)
	}

	var inviteTimeouts, searchTimeouts []*QueueUser
	for _, u := range q.sorted() {
		switch {
		case u.Group != nil && !u.InviteAccepted && now.Sub(u.InviteStart) > q.opts.InviteTimeout:
			inviteTimeouts = append(inviteTimeouts, u)
		case u.Group == nil && now.Sub(u.SearchStart) > q.opts.SearchTimeout:
			searchTimeouts = append(searchTimeouts, u)
		}
	}
	bundle.append(q.removeFromQueue(inviteTimeouts, true))
	bundle.append(q.removeFromQueue(searchTimeouts, false))

	return bundle
}

// sorted returns the players in a stable order (the lock must be held).
func (q *Queue) sorted() []*QueueUser {
	users := make([]*QueueUser, 0, len(q.users))
	for _, u := range q.users {
		users = append(users, u)
	}
	sort.Slice(users, func(i, j int) bool { return users[i].UserID < users[j].UserID })
	return users
}

// removeFromQueue takes players out. markAsDeclined counts those who had been invited as having declined. The
// lock must be held.
func (q *Queue) removeFromQueue(users []*QueueUser, markAsDeclined bool) Bundle {
	var bundle Bundle

	groups := map[*QueueGroup]bool{}
	var order []*QueueGroup

	for _, u := range users {
		delete(q.users, u.UserID)
		if u.Group != nil && markAsDeclined {
			bundle.Declined = append(bundle.Declined, u)
		}
		bundle.Removed = append(bundle.Removed, u)

		if u.Group != nil && !groups[u.Group] {
			groups[u.Group] = true
			order = append(order, u.Group)
		}
	}

	for _, group := range order {
		bundle.Recycled = append(bundle.Recycled, group)

		for _, u := range group.Users {
			if _, stillQueued := q.users[u.UserID]; !stillQueued {
				continue
			}

			if q.opts.KeepInQueueOnDecline {
				u.Group = nil
				u.InviteAccepted = false
				bundle.Added = append(bundle.Added, u)
			} else {
				delete(q.users, u.UserID)
				bundle.Removed = append(bundle.Removed, u)
			}
		}
	}

	return bundle
}

// matchUsers forms groups out of the players who are free to be matched. The lock must be held.
func (q *Queue) matchUsers() [][]*QueueUser {
	now := q.opts.Clock.Now()

	var available []*QueueUser
	for _, u := range q.sorted() {
		if u.Group == nil && u.BanEnd.Before(now) {
			available = append(available, u)
		}
	}
	sort.SliceStable(available, func(i, j int) bool { return available[i].Rating.Mu < available[j].Rating.Mu })

	var groups [][]*QueueUser
	for i := 0; len(available) >= q.pool.LobbySize && i < len(available); {
		matches := q.findMatchesFor(available, i)
		if len(matches) < q.pool.LobbySize {
			i++
			continue
		}

		groups = append(groups, matches)

		// The matched players are no longer available; the pivot was one of them, so index i is somebody new.
		remaining := available[:0:0]
		for _, u := range available {
			matched := false
			for _, m := range matches {
				if u == m {
					matched = true
					break
				}
			}
			if !matched {
				remaining = append(remaining, u)
			}
		}
		available = remaining
	}
	return groups
}

// findMatchesFor looks for opponents for the player at pivotIndex within their search radius.
func (q *Queue) findMatchesFor(users []*QueueUser, pivotIndex int) []*QueueUser {
	pivot := users[pivotIndex]

	// Players far from the average have fewer opponents around them, so they search wider.
	ratingBonus := math.Exp(math.Pow((pivot.Rating.Mu-1500)/750, 2))

	// The longer somebody has waited, the wider they search.
	waited := q.opts.Clock.Now().Sub(pivot.SearchStart).Seconds()
	timeBonus := 1.0
	if q.pool.SearchRadiusExp > 0 {
		timeBonus = math.Pow(2, waited/q.pool.SearchRadiusExp)
	}

	// The best players can always be matched against each other.
	top100Bonus := 1.0
	if q.pool.SearchRadiusMax > 0 {
		top100Bonus = math.Max(1, (pivot.Rating.Mu-q.top100)/q.pool.SearchRadiusMax)
	}

	radius := math.Min(q.pool.SearchRadiusMax*top100Bonus, q.pool.SearchRadius*ratingBonus*timeBonus)

	var candidates []*QueueUser
	for _, u := range users {
		if u == pivot || q.isRecentMatchup(pivot.UserID, u.UserID) || math.Abs(pivot.Rating.Mu-u.Rating.Mu) > radius {
			continue
		}
		candidates = append(candidates, u)
	}
	q.opts.Rand.Shuffle(len(candidates), func(i, j int) { candidates[i], candidates[j] = candidates[j], candidates[i] })

	matches := []*QueueUser{pivot}
	for _, c := range candidates {
		if len(matches) >= q.pool.LobbySize {
			break
		}
		matches = append(matches, c)
	}
	return matches
}
