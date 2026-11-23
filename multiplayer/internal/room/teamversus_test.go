package room

import (
	"context"
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

func teamRoom(t *testing.T, players ...int32) (*Manager, *Room, *recorder) {
	t.Helper()
	m, rec := newManager(Options{LoadTimeout: time.Hour})
	req := request(newItem(100))
	req.Settings.MatchType = protocol.MatchTeamVersus
	r, _, err := m.Create(context.Background(), 1, req)
	must(t, err)
	for _, p := range players {
		_, _, err := m.Join(r.ID(), p, "")
		must(t, err)
	}
	rec.take()
	return m, r, rec
}

func teamOfUser(r *Room, id int32) int {
	r.lock()
	defer r.unlock()
	return teamOf(r.user(id))
}

func TestTeamRoomsAreAllowed(t *testing.T) {
	_, r, _ := teamRoom(t)
	if got := r.Snapshot().Settings.MatchType; got != protocol.MatchTeamVersus {
		t.Fatalf("match type = %v, want team versus", got)
	}
	if r.Snapshot().MatchState == nil {
		t.Fatal("a team room's snapshot must carry the team list")
	}
}

func TestPlayersAreSplitBetweenTheTeams(t *testing.T) {
	_, r, _ := teamRoom(t, 2, 3, 4)
	counts := map[int]int{}
	for _, id := range []int32{1, 2, 3, 4} {
		counts[teamOfUser(r, id)]++
	}
	if counts[0] != 2 || counts[1] != 2 {
		t.Fatalf("team sizes = %v, want 2 and 2", counts)
	}
}

func TestPlayersChangeTheirOwnTeam(t *testing.T) {
	_, r, rec := teamRoom(t, 2)
	other := 1 - teamOfUser(r, 2)

	must(t, r.HandleMatchRequest(2, protocol.MatchRequestChangeTeam, &protocol.ChangeTeamRequest{TeamID: int32(other)}))

	if got := teamOfUser(r, 2); got != other {
		t.Fatalf("team = %d, want %d", got, other)
	}
	if !containsPrefix(to(rec.take(), 1), "MatchUserStateChanged") {
		t.Fatal("the room should be told about the team change")
	}
	wantError(t, r.HandleMatchRequest(2, protocol.MatchRequestChangeTeam, &protocol.ChangeTeamRequest{TeamID: 5}), "InvalidStateException", "invalid team")
}

func TestTeamsCannotBeChangedInAHeadToHeadRoom(t *testing.T) {
	_, r, _ := lobby(t, Options{LoadTimeout: time.Hour}, 2)
	wantError(t, r.HandleMatchRequest(2, protocol.MatchRequestChangeTeam, &protocol.ChangeTeamRequest{TeamID: 0}), "InvalidStateException", "not a team match")
}

func TestSwitchingTheMatchTypeCreatesAndRemovesTeams(t *testing.T) {
	_, r, rec := lobby(t, Options{LoadTimeout: time.Hour}, 2, 3)

	settings := r.Snapshot().Settings
	settings.MatchType = protocol.MatchTeamVersus
	must(t, r.ChangeSettings(1, settings))
	if teamOfUser(r, 1) < 0 || teamOfUser(r, 2) < 0 || teamOfUser(r, 3) < 0 {
		t.Fatal("everyone should be on a team after switching to team versus")
	}
	if !containsPrefix(to(rec.take(), 2), "MatchRoomStateChanged") {
		t.Fatal("the room state should be announced")
	}

	settings.MatchType = protocol.MatchHeadToHead
	must(t, r.ChangeSettings(1, settings))
	if teamOfUser(r, 2) != -1 || r.Snapshot().MatchState != nil {
		t.Fatal("teams should be gone after switching back")
	}
}
