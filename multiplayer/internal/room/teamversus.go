package room

import (
	"log/slog"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
)

// Team versus: every player is on the red or the blue team. The room carries the team list (a MatchRoomState)
// and each user their team (a MatchUserState); players choose their own team with ChangeTeamRequest. Rooms run by
// a match controller (ranked play) are never team rooms. Everything here runs with the room lock held.

var teamNames = []string{"Team Red", "Team Blue"}

func (r *Room) isTeamVersus() bool {
	return r.controller == nil && r.settings.MatchType == protocol.MatchTeamVersus
}

// teamRoomState is the MatchRoomState union of a team room: [teams, locked, slots].
func teamRoomState() protocol.Union {
	teams := make([]any, len(teamNames))
	for i, name := range teamNames {
		teams[i] = []any{int32(i), name}
	}
	return protocol.Union{Key: protocol.MatchRoomStateTeamVersus, Value: []any{teams, false, nil}}
}

// teamUserState is the MatchUserState union placing a user on a team.
func teamUserState(team int) protocol.Union {
	return protocol.Union{Key: 0, Value: []any{int32(team)}}
}

func rawUnion(u protocol.Union) *msgpack.RawMessage {
	data, err := msgpack.Marshal(u)
	if err != nil {
		slog.Error("encoding a team state failed", "error", err)
		return nil
	}
	raw := msgpack.RawMessage(data)
	return &raw
}

// teamOf returns the user's team, or -1 when they have none.
func teamOf(u *protocol.RoomUser) int {
	if u.MatchState == nil {
		return -1
	}
	var union struct {
		_    struct{} `msgpack:",as_array"`
		Key  int
		Team struct {
			_  struct{} `msgpack:",as_array"`
			ID int32
		}
	}
	if err := msgpack.Unmarshal(*u.MatchState, &union); err != nil {
		return -1
	}
	return int(union.Team.ID)
}

// smallerTeam is the team with the fewest players (red on a tie), not counting the user themselves.
func (r *Room) smallerTeam(except *protocol.RoomUser) int {
	counts := make([]int, len(teamNames))
	for _, u := range r.users {
		if u == except {
			continue
		}
		if t := teamOf(u); t >= 0 && t < len(counts) {
			counts[t]++
		}
	}
	best := 0
	for t := range counts {
		if counts[t] < counts[best] {
			best = t
		}
	}
	return best
}

// placeOnTeam puts a user on a team without telling anyone.
func (r *Room) placeOnTeam(u *protocol.RoomUser, team int) {
	u.MatchState = rawUnion(teamUserState(team))
}

// teamsForNewUser gives a joining user the smaller team, before the room announces them.
func (r *Room) teamsForNewUser(u *protocol.RoomUser) {
	if r.isTeamVersus() {
		r.placeOnTeam(u, r.smallerTeam(u))
	}
}

// matchTypeChanged runs after the host changed the room's match type: players are split into teams, or lose them.
func (r *Room) matchTypeChanged(before protocol.MatchType) {
	after := r.settings.MatchType
	if before == after || r.controller != nil {
		return
	}

	switch {
	case after == protocol.MatchTeamVersus:
		for _, u := range r.users {
			r.placeOnTeam(u, r.smallerTeam(u))
		}
		r.broadcast("MatchRoomStateChanged", teamRoomState())
		for _, u := range r.users {
			r.broadcast("MatchUserStateChanged", u.UserID, teamUserState(teamOf(u)))
		}

	case before == protocol.MatchTeamVersus:
		for _, u := range r.users {
			u.MatchState = nil
		}
		r.broadcast("MatchRoomStateChanged", nil)
		for _, u := range r.users {
			r.broadcast("MatchUserStateChanged", u.UserID, nil)
		}
	}
}

// changeTeam moves a user to the team they asked for.
func (r *Room) changeTeam(userID int32, team int32) error {
	if !r.isTeamVersus() {
		return invalidState("Can't change team in a room that is not a team match.")
	}
	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if team < 0 || int(team) >= len(teamNames) {
		return invalidState("Attempted to change to an invalid team.")
	}
	if teamOf(user) == int(team) {
		return nil
	}

	r.placeOnTeam(user, int(team))
	r.broadcast("MatchUserStateChanged", userID, teamUserState(int(team)))
	return nil
}
