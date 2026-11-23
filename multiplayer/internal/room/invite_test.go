package room

import (
	"context"
	"testing"
)

func TestInvitingSendsTheRoomAndItsPasswordToTheInvitedUser(t *testing.T) {
	_, r, rec := lobby(t, Options{})

	must(t, r.Invite(1, 7))

	if got := rec.take(); len(got) != 1 || !contains(got, "7:Invited(1,1,)") {
		t.Fatalf("events = %v, want one Invited for user 7", got)
	}
}

func TestInviteArgumentsAreInviterRoomAndPassword(t *testing.T) {
	var sent []any
	m := NewManager(eventsFunc(func(user int32, target string, args ...any) {
		if target == "Invited" {
			sent = append([]any{user}, args...)
		}
	}), Options{})
	req := request(newItem(100))
	req.Settings.Password = "pw"
	r, _, err := m.Create(context.Background(), 1, req)
	must(t, err)

	must(t, r.Invite(1, 7))

	if len(sent) != 4 || sent[0] != int32(7) || sent[1] != int32(1) || sent[2] != r.ID() || sent[3] != "pw" {
		t.Fatalf("sent = %#v, want [7 1 %d pw]", sent, r.ID())
	}
}

type eventsFunc func(user int32, target string, args ...any)

func (f eventsFunc) Send(user int32, target string, args ...any) { f(user, target, args...) }

func TestOnlyPeopleInTheRoomInvite(t *testing.T) {
	_, r, rec := lobby(t, Options{})

	if err := r.Invite(99, 7); err != ErrNotJoined {
		t.Errorf("err = %v, want not joined", err)
	}
	if got := rec.take(); len(got) != 0 {
		t.Errorf("events = %v, want none", got)
	}
}

func TestBannedUsersAreNotInvited(t *testing.T) {
	_, r, rec := lobby(t, Options{})
	r.banned[2] = struct{}{} // what a ban leaves behind

	wantError(t, r.Invite(1, 2), "InvalidStateException", "Player is not eligible to join this room.")
	if got := rec.take(); len(got) != 0 {
		t.Errorf("events = %v, want none", got)
	}
}

func TestRankedPlayRoomsCannotInvite(t *testing.T) {
	m := newMatch(t)
	m.join(1, 2)

	wantError(t, m.room.Invite(1, 7), "InvalidStateException", "Can't invite players to matchmaking rooms.")
}

func TestInviteRefusals(t *testing.T) {
	for _, tc := range []struct {
		name  string
		check InviteCheck
		typ   string
		text  string
	}{
		{"restricted", InviteCheck{Restricted: true}, "InvalidStateException", "Can't invite a restricted user to a room."},
		{"blocked", InviteCheck{Blocked: true}, "UserBlockedException", UserBlockedMessage},
		{"closed to pms", InviteCheck{BlocksPMs: true}, "UserBlocksPMsException", UserBlocksPMsMessage},
	} {
		wantError(t, CheckInvite(tc.check), tc.typ, tc.text)
	}
	if err := CheckInvite(InviteCheck{}); err != nil {
		t.Errorf("a clean check was refused: %v", err)
	}
	if UserBlockedMessage != "Cannot perform action due to user being blocked." ||
		UserBlocksPMsMessage != "Cannot perform action because user has disabled non-friend communications." {
		t.Error("the messages must be exactly the ones the client matches on")
	}
}
