package matchmaking

import (
	"sync"
	"time"
)

// Bans keeps players who abandoned a match or declined an invitation out of the queue for a while.
type Bans struct {
	mu    sync.Mutex
	ends  map[int32]time.Time
	clock Clock
}

// NewBans creates an empty list. A nil clock means the real one.
func NewBans(clock Clock) *Bans {
	if clock == nil {
		clock = realClock{}
	}
	return &Bans{ends: map[int32]time.Time{}, clock: clock}
}

// Ban keeps the player out of the queue for d, or longer if they already were banned for longer.
func (b *Bans) Ban(userID int32, d time.Duration) {
	b.mu.Lock()
	defer b.mu.Unlock()

	end := b.clock.Now().Add(d)
	if existing, ok := b.ends[userID]; !ok || end.After(existing) {
		b.ends[userID] = end
	}
}

// EndOf returns when the player's ban ends; a time in the past when they have none.
func (b *Bans) EndOf(userID int32) time.Time {
	b.mu.Lock()
	defer b.mu.Unlock()

	end, ok := b.ends[userID]
	if !ok {
		return time.Time{}
	}
	if !end.After(b.clock.Now()) {
		delete(b.ends, userID)
		return time.Time{}
	}
	return end
}
