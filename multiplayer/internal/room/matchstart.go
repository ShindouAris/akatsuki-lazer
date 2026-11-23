package room

import (
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// The match start timer of ordinary rooms: the host can start one ("Start in ...", StartMatchCountdownRequest),
// and a room with an auto start duration starts one by itself as soon as someone is ready. When it elapses the
// ready players are sent to gameplay. Everything here runs with the room lock held.

// handleTimerRequest handles the timer requests a client sends with SendMatchRequest in an ordinary room.
func (r *Room) handleTimerRequest(userID int32, request any) error {
	if r.user(userID) == nil {
		return invalidState("User is not in the room.")
	}

	switch req := request.(type) {
	case *protocol.StartCountdownRequest:
		if err := r.ensureHostOrReferee(userID); err != nil {
			return err
		}
		if r.state != protocol.RoomOpen {
			return invalidState("Can't start a countdown while a match is in progress.")
		}
		if req.Duration <= 0 {
			return invalidState("A countdown needs a positive duration.")
		}
		r.startMatchTimer(req.Duration)

	case *protocol.ChangeTeamRequest:
		return r.changeTeam(userID, req.TeamID)

	case *protocol.StopCountdownRequest:
		if err := r.ensureHostOrReferee(userID); err != nil {
			return err
		}
		for _, cd := range append([]*countdown(nil), r.countdowns...) {
			if cd.info.ID == req.ID && cd.info.Kind == protocol.CountdownMatchStart {
				r.stopCountdown(cd)
			}
		}
	}
	// Other request kinds (teams, slots, ...) are not supported in these rooms and are ignored.
	return nil
}

func (r *Room) startMatchTimer(d time.Duration) {
	r.startCountdown(protocol.CountdownMatchStart, 0, d, r.onMatchTimerElapsed)
}

func (r *Room) matchTimerRunning() bool {
	for _, cd := range r.countdowns {
		if cd.info.Kind == protocol.CountdownMatchStart {
			return true
		}
	}
	return false
}

// onMatchTimerElapsed starts the match for whoever is ready. Called with the room lock held.
func (r *Room) onMatchTimerElapsed() {
	if r.state != protocol.RoomOpen || r.currentItem().Expired {
		return
	}
	if !r.anyUser(func(u *protocol.RoomUser) bool { return u.State == protocol.UserReady }) {
		return
	}
	_ = r.startMatch()
}

// updateAutoStart keeps the automatic timer in line with the room: it runs while someone is ready and stops when
// nobody is. Rooms without an auto start duration, or controlled by a match controller, never have one.
func (r *Room) updateAutoStart() {
	if r.controller != nil || r.settings.AutoStartDuration <= 0 || r.state != protocol.RoomOpen {
		return
	}

	anyReady := r.anyUser(func(u *protocol.RoomUser) bool { return u.State == protocol.UserReady })
	switch {
	case anyReady && !r.matchTimerRunning():
		r.startMatchTimer(time.Duration(r.settings.AutoStartDuration) * 100)
	case !anyReady:
		r.stopCountdownsOfKind(protocol.CountdownMatchStart)
	}
}
