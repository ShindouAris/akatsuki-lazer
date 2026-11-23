package protocol

// Matchmaking wire types (IMatchmakingClient / IMatchmakingServer). Like the ranked play types they encode as
// MessagePack arrays in the client's `[Key(n)]` order, and testdata/matchmaking_vectors.tsv holds the bytes the
// client itself produces for each of them.

import "github.com/vmihailenco/msgpack/v5"

// PoolType mirrors MatchmakingPoolType.
type PoolType int

const (
	PoolQuickPlay PoolType = iota
	PoolRankedPlay
)

// Pool mirrors MatchmakingPool.
type Pool struct {
	_msgpack  struct{} `msgpack:",as_array"`
	ID        int32
	RulesetID int32
	Variant   int32
	Name      string
	Type      PoolType
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (p *Pool) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &p.ID, &p.RulesetID, &p.Variant, &p.Name, &p.Type)
}

// Union keys of MatchmakingQueueStatus.
const (
	queueStatusSearching = iota
	queueStatusMatchFound
	queueStatusJoiningMatch
)

// QueueSearching is the MatchmakingQueueStatus of a player waiting for an opponent.
func QueueSearching() Union { return Union{Key: queueStatusSearching, Value: []any{}} }

// QueueMatchFound is the MatchmakingQueueStatus of a player who has been invited to a match.
func QueueMatchFound() Union { return Union{Key: queueStatusMatchFound, Value: []any{}} }

// QueueJoiningMatch is the MatchmakingQueueStatus of a player who accepted the invitation.
func QueueJoiningMatch() Union { return Union{Key: queueStatusJoiningMatch, Value: []any{}} }

// RatingBucket is one entry of the rating distribution: how many players have a rating around Rating. The
// client's type is a (int, int) tuple, which MessagePack writes as a two element array.
type RatingBucket struct {
	_msgpack struct{} `msgpack:",as_array"`
	Rating   int32
	Count    int32
}

// LobbyStatus mirrors MatchmakingLobbyStatus.
type LobbyStatus struct {
	_msgpack           struct{} `msgpack:",as_array"`
	UsersInQueue       []int32
	RatingDistribution []RatingBucket
	UserRating         *int32
	// RecentMatches are MatchRoomState unions, encoded already.
	RecentMatches []msgpack.RawMessage
	UserSigma     *float64
	UserRank      *int32
}

// EncodeMsgpack implements msgpack.CustomEncoder. The collections are never null for the client.
func (s LobbyStatus) EncodeMsgpack(enc *msgpack.Encoder) error {
	users, distribution, recent := s.UsersInQueue, s.RatingDistribution, s.RecentMatches
	if users == nil {
		users = []int32{}
	}
	if distribution == nil {
		distribution = []RatingBucket{}
	}
	if recent == nil {
		recent = []msgpack.RawMessage{}
	}
	return enc.Encode([]any{users, distribution, s.UserRating, recent, s.UserSigma, s.UserRank})
}

// RoomInvitationParams mirrors MatchmakingRoomInvitationParams.
type RoomInvitationParams struct {
	_msgpack struct{} `msgpack:",as_array"`
	Type     PoolType
}

// DuelIssuedParams mirrors MatchmakingDuelIssuedParams. The id is a GUID in the client's string form.
type DuelIssuedParams struct {
	_msgpack struct{} `msgpack:",as_array"`
	ID       string
	UserID   int32
	Pool     Pool
}

// JoinLobbyRequest mirrors MatchmakingJoinLobbyRequest.
type JoinLobbyRequest struct {
	PoolID int32
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (r *JoinLobbyRequest) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &r.PoolID)
}

// IssueDuelRequest mirrors MatchmakingIssueDuelRequest.
type IssueDuelRequest struct {
	UserID int32
	PoolID int32
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (r *IssueDuelRequest) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &r.UserID, &r.PoolID)
}

// AcceptDuelRequest mirrors MatchmakingAcceptDuelRequest.
type AcceptDuelRequest struct {
	ID string
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (r *AcceptDuelRequest) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &r.ID)
}

// EmptyResponse is the result of the matchmaking calls whose response type has no members: an empty array.
func EmptyResponse() []any { return []any{} }
