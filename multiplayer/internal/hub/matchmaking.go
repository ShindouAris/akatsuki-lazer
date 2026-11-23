package hub

import (
	"context"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// Matchmaking is the queue the hub's matchmaking calls (IMatchmakingServer) are handed to; it is
// matchmaking.Service. Callbacks (queue status, invitations, ...) reach clients through Hub.Send.
type Matchmaking interface {
	Pools(ctx context.Context, poolType protocol.PoolType) ([]protocol.Pool, error)
	JoinLobby(ctx context.Context, userID, poolID int32) error
	LeaveLobby(userID int32)
	JoinQueue(ctx context.Context, userID, poolID int32) error
	LeaveQueue(userID int32)
	AcceptInvitation(userID int32)
	DeclineInvitation(userID int32)
	IssueDuel(ctx context.Context, userID int32, request protocol.IssueDuelRequest) error
	AcceptDuel(ctx context.Context, userID int32, request protocol.AcceptDuelRequest) error
	// Disconnected is told when a player goes offline.
	Disconnected(userID int32)
}

// SetRelations connects the lookup behind InvitePlayer's restriction and block checks.
func (h *Hub) SetRelations(r room.Relations) { h.relations = r }

// SetMatchmaking connects the matchmaking queue. It has to be called before the hub serves connections: the queue
// needs the hub (to notify players and to create rooms), and the hub needs the queue.
func (h *Hub) SetMatchmaking(m Matchmaking) { h.matchmaking = m }

// matchmakingMethods are the hub methods of IMatchmakingServer.
var matchmakingMethods = map[string]bool{
	"GetMatchmakingPoolsOfType":      true,
	"MatchmakingJoinLobbyWithParams": true,
	"MatchmakingLeaveLobby":          true,
	"MatchmakingJoinQueue":           true,
	"MatchmakingLeaveQueue":          true,
	"MatchmakingAcceptInvitation":    true,
	"MatchmakingDeclineInvitation":   true,
	"MatchmakingIssueDuel":           true,
	"MatchmakingAcceptDuel":          true,
	"MatchmakingToggleSelection":     true,
	"MatchmakingSkipToNextStage":     true,
}

// invokeMatchmaking handles a call of IMatchmakingServer; the method must be one of matchmakingMethods.
func (h *Hub) invokeMatchmaking(ctx context.Context, user int32, method string, args []msgpack.RawMessage) (any, bool, error) {
	m := h.matchmaking
	if m == nil {
		return nil, false, invalidState("Matchmaking is not available on this server.")
	}

	switch method {
	case "GetMatchmakingPoolsOfType":
		var poolType protocol.PoolType
		if err := arg(args, 0, &poolType); err != nil {
			return nil, false, err
		}
		pools, err := m.Pools(ctx, poolType)
		if err != nil {
			return nil, false, err
		}
		return pools, true, nil

	case "MatchmakingJoinLobbyWithParams":
		var request protocol.JoinLobbyRequest
		if err := arg(args, 0, &request); err != nil {
			return nil, false, err
		}
		if err := m.JoinLobby(ctx, user, request.PoolID); err != nil {
			return nil, false, err
		}
		return protocol.EmptyResponse(), true, nil

	case "MatchmakingLeaveLobby":
		m.LeaveLobby(user)
		return nil, false, nil

	case "MatchmakingJoinQueue":
		var poolID int32
		if err := arg(args, 0, &poolID); err != nil {
			return nil, false, err
		}
		return nil, false, m.JoinQueue(ctx, user, poolID)

	case "MatchmakingLeaveQueue":
		m.LeaveQueue(user)
		return nil, false, nil

	case "MatchmakingAcceptInvitation":
		m.AcceptInvitation(user)
		return nil, false, nil

	case "MatchmakingDeclineInvitation":
		m.DeclineInvitation(user)
		return nil, false, nil

	case "MatchmakingIssueDuel":
		var request protocol.IssueDuelRequest
		if err := arg(args, 0, &request); err != nil {
			return nil, false, err
		}
		if err := m.IssueDuel(ctx, user, request); err != nil {
			return nil, false, err
		}
		return protocol.EmptyResponse(), true, nil

	case "MatchmakingAcceptDuel":
		var request protocol.AcceptDuelRequest
		if err := arg(args, 0, &request); err != nil {
			return nil, false, err
		}
		if err := m.AcceptDuel(ctx, user, request); err != nil {
			return nil, false, err
		}
		return protocol.EmptyResponse(), true, nil
	}

	// MatchmakingToggleSelection and MatchmakingSkipToNextStage belong to the quick play rooms, which this server
	// does not run (ranked play picks cards through PlayCard).
	return nil, false, &room.Error{Type: "InvalidStateException", Message: method + " is not supported."}
}
