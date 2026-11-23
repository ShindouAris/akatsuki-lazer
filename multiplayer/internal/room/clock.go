package room

import "time"

// Timer is a pending call that can be cancelled.
type Timer interface {
	Stop() bool
}

// Clock is the source of time for countdowns. Production uses the real clock; tests substitute one they can
// advance by hand, so a two minute stage ends instantly.
type Clock interface {
	Now() time.Time
	AfterFunc(d time.Duration, f func()) Timer
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

func (realClock) AfterFunc(d time.Duration, f func()) Timer { return time.AfterFunc(d, f) }
