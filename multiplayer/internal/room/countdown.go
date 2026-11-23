package room

import (
	"log/slog"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
)

// A countdown is a timer clients can see: it is announced with a MatchEvent when it starts and again when it
// ends or is cancelled, and late joiners receive the running ones in the room snapshot. Everything here runs with
// the room lock held, except the timer callback, which takes it.
type countdown struct {
	info   protocol.Countdown
	endsAt time.Time
	timer  Timer
	onDone func()
}

// startCountdown announces a countdown and runs onDone, under the room lock, when it elapses. Countdowns of
// one kind are exclusive: starting one cancels the previous.
func (r *Room) startCountdown(kind protocol.CountdownKind, stage protocol.RankedPlayStage, d time.Duration, onDone func()) *countdown {
	r.stopCountdownsOfKind(kind)

	r.nextCountdownID++
	r.countdownSeq++

	cd := &countdown{
		info: protocol.Countdown{
			Kind:          kind,
			ID:            r.nextCountdownID,
			TimeRemaining: protocol.Ticks(d),
			Stage:         stage,
		},
		endsAt: r.clock.Now().Add(d),
		onDone: onDone,
	}
	r.countdowns = append(r.countdowns, cd)
	r.broadcast("MatchEvent", protocol.CountdownStartedEvent(cd.info))
	cd.timer = r.clock.AfterFunc(d, func() { r.countdownElapsed(cd) })
	return cd
}

// countdownElapsed is the timer callback.
func (r *Room) countdownElapsed(cd *countdown) {
	r.lock()
	defer r.unlock()

	// A countdown that was stopped (or whose room closed) while its timer was already firing must do nothing.
	if r.closed || !r.hasCountdown(cd) {
		return
	}

	r.stopCountdown(cd)
	if cd.onDone != nil {
		cd.onDone()
	}
}

// stopCountdown cancels a running countdown and announces that it is over.
func (r *Room) stopCountdown(cd *countdown) {
	if !r.hasCountdown(cd) {
		return
	}

	cd.timer.Stop()
	for i, other := range r.countdowns {
		if other == cd {
			r.countdowns = append(r.countdowns[:i], r.countdowns[i+1:]...)
			break
		}
	}
	r.broadcast("MatchEvent", protocol.CountdownStoppedEvent(cd.info.ID))
}

func (r *Room) stopCountdownsOfKind(kind protocol.CountdownKind) {
	for _, cd := range append([]*countdown(nil), r.countdowns...) {
		if cd.info.Kind == kind {
			r.stopCountdown(cd)
		}
	}
}

// stopAllCountdowns cancels every timer without announcing it; used when the room is discarded.
func (r *Room) stopAllCountdowns() {
	for _, cd := range r.countdowns {
		cd.timer.Stop()
	}
	r.countdowns = nil
}

func (r *Room) hasCountdown(cd *countdown) bool {
	for _, other := range r.countdowns {
		if other == cd {
			return true
		}
	}
	return false
}

// activeCountdowns encodes the running countdowns for a room snapshot, with the time that is actually left.
func (r *Room) activeCountdowns() []msgpack.RawMessage {
	raw := make([]msgpack.RawMessage, 0, len(r.countdowns))
	now := r.clock.Now()

	for _, cd := range r.countdowns {
		info := cd.info
		if left := cd.endsAt.Sub(now); left > 0 {
			info.TimeRemaining = protocol.Ticks(left)
		} else {
			info.TimeRemaining = 0
		}

		data, err := msgpack.Marshal(info)
		if err != nil {
			slog.Error("encoding a countdown failed", "room", r.id, "error", err)
			continue
		}
		raw = append(raw, data)
	}
	return raw
}
