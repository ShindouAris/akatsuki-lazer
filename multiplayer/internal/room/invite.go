package room

import (
	"context"

	"akatsuki/multiplayer/internal/protocol"
)

// Messages the client recognises (UserBlockedException.MESSAGE and UserBlocksPMsException.MESSAGE).
const (
	UserBlockedMessage   = "Cannot perform action due to user being blocked."
	UserBlocksPMsMessage = "Cannot perform action because user has disabled non-friend communications."
)

// InviteCheck is what stands between a user and inviting another.
type InviteCheck struct {
	// Restricted: the invited user is restricted.
	Restricted bool
	// Blocked: either of the two has blocked the other.
	Blocked bool
	// BlocksPMs: the invited user takes no messages from non-friends and the inviter is not their friend.
	BlocksPMs bool
}

// Relations answers whether a user may invite another (the Python API's users and relations).
type Relations interface {
	InviteCheck(ctx context.Context, from, to int32) (InviteCheck, error)
}

// CheckInvite turns the answer of Relations into the refusal the reference server gives, or nil.
func CheckInvite(c InviteCheck) error {
	switch {
	case c.Restricted:
		return invalidState("Can't invite a restricted user to a room.")
	case c.Blocked:
		return &Error{Type: "UserBlockedException", Message: UserBlockedMessage}
	case c.BlocksPMs:
		return &Error{Type: "UserBlocksPMsException", Message: UserBlocksPMsMessage}
	}
	return nil
}

// Invite tells a user (with the room's password) that `by` invites them. Rooms made by matchmaking cannot invite.
func (r *Room) Invite(by, target int32) error {
	r.lock()
	defer r.unlock()

	if r.user(by) == nil {
		return ErrNotJoined
	}
	if r.settings.MatchType == protocol.MatchRankedPlay || r.controller != nil {
		return invalidState("Can't invite players to matchmaking rooms.")
	}
	if r.closed {
		return invalidState("Player is not eligible to join this room.")
	}
	if _, banned := r.banned[target]; banned {
		return invalidState("Player is not eligible to join this room.")
	}

	r.ev.Send(target, "Invited", by, r.id, r.settings.Password)
	return nil
}
