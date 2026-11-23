package protocol

import (
	"reflect"
	"testing"

	"github.com/vmihailenco/msgpack/v5"
)

func matchmakingVectors(t *testing.T) map[string][]byte {
	return loadVectors(t, "matchmaking_vectors.tsv")
}

func samplePool() Pool {
	return Pool{ID: 3, RulesetID: 0, Variant: 4, Name: "1v1 RX", Type: PoolRankedPlay}
}

// sampleRecentMatch is the state the client was asked to serialise for the lobby status vector.
func sampleRecentMatch(t *testing.T) msgpack.RawMessage {
	t.Helper()

	state := NewRankedPlayRoomState()
	state.Stage = StageEnded
	state.CurrentRound = 2
	state.ActiveUserID = int32Ptr(5)
	state.StarRating = 4.5
	state.WinningUserID = int32Ptr(5)

	state.Users[5] = NewRankedPlayUserInfo(1500)
	state.Users[5].Life = 800_000
	state.Users[5].RatingAfter = 1510
	state.Users[5].RoundsWon = 1
	state.Users[6] = NewRankedPlayUserInfo(1600)
	state.Users[6].Life = 0
	state.Users[6].RatingAfter = 1590

	return pack(t, state.Union())
}

func TestMatchmakingEncodingMatchesTheBytesTheClientWrites(t *testing.T) {
	userRating, userRank := int32(1512), int32(42)
	sigma := 140.5

	cases := map[string]any{
		"pool":                   samplePool(),
		"pool_quickplay_default": Pool{},
		"pools_array":            []Pool{samplePool(), {ID: 4, RulesetID: 3, Variant: 7, Name: "m", Type: PoolQuickPlay}},
		"queue_searching":        QueueSearching(),
		"queue_match_found":      QueueMatchFound(),
		"queue_joining_match":    QueueJoiningMatch(),
		"lobby_status_full": LobbyStatus{
			UsersInQueue:       []int32{5, 6},
			RatingDistribution: []RatingBucket{{Rating: 1500, Count: 3}, {Rating: 1525, Count: 1}},
			UserRating:         &userRating,
			RecentMatches:      []msgpack.RawMessage{sampleRecentMatch(t)},
			UserSigma:          &sigma,
			UserRank:           &userRank,
		},
		"lobby_status_defaults": LobbyStatus{},
		"invitation_params":     RoomInvitationParams{Type: PoolRankedPlay},
		"duel_issued":           DuelIssuedParams{ID: cardA, UserID: 9, Pool: samplePool()},
		"join_lobby_response":   EmptyResponse(),
		"issue_duel_response":   EmptyResponse(),
		"accept_duel_response":  EmptyResponse(),
	}

	golden := matchmakingVectors(t)
	for name := range golden {
		switch name {
		case "join_lobby_request", "issue_duel_request", "accept_duel_request":
			continue // client to server: decoded by TestDecodeMatchmakingRequests
		}
		if _, covered := cases[name]; !covered {
			t.Errorf("vector %q has no matching case", name)
		}
	}

	for name, value := range cases {
		t.Run(name, func(t *testing.T) {
			want, ok := golden[name]
			if !ok {
				t.Fatalf("no vector named %q", name)
			}

			got := decodeLoose(t, pack(t, value))
			if expected := decodeLoose(t, want); !reflect.DeepEqual(got, expected) {
				t.Errorf("encoded differently from the client\n got: %#v\nwant: %#v", got, expected)
			}
		})
	}
}

func TestDecodeMatchmakingRequests(t *testing.T) {
	golden := matchmakingVectors(t)

	var join JoinLobbyRequest
	if err := msgpack.Unmarshal(golden["join_lobby_request"], &join); err != nil || join.PoolID != 3 {
		t.Errorf("join lobby request = %+v err=%v", join, err)
	}

	var duel IssueDuelRequest
	if err := msgpack.Unmarshal(golden["issue_duel_request"], &duel); err != nil || duel.UserID != 9 || duel.PoolID != 3 {
		t.Errorf("issue duel request = %+v err=%v", duel, err)
	}

	var accept AcceptDuelRequest
	if err := msgpack.Unmarshal(golden["accept_duel_request"], &accept); err != nil || accept.ID != cardA {
		t.Errorf("accept duel request = %+v err=%v", accept, err)
	}
}

func TestMatchmakingRequestsTolerateLongerArrays(t *testing.T) {
	// A newer client may add members; the client's own deserialiser skips what it does not know.
	var duel IssueDuelRequest
	if err := msgpack.Unmarshal(pack(t, []any{9, 3, "future member"}), &duel); err != nil || duel.UserID != 9 || duel.PoolID != 3 {
		t.Errorf("issue duel request = %+v err=%v", duel, err)
	}
}

func TestDecodePool(t *testing.T) {
	var pool Pool
	if err := msgpack.Unmarshal(matchmakingVectors(t)["pool"], &pool); err != nil || pool != samplePool() {
		t.Errorf("pool = %+v err=%v", pool, err)
	}
}

func TestLobbyStatusCollectionsAreNeverNull(t *testing.T) {
	status := decodeLoose(t, pack(t, LobbyStatus{})).([]any)

	for _, index := range []int{0, 1, 3} {
		if items, ok := status[index].([]any); !ok || len(items) != 0 {
			t.Errorf("member %d = %#v, want an empty array", index, status[index])
		}
	}
	for _, index := range []int{2, 4, 5} {
		if status[index] != nil {
			t.Errorf("member %d = %#v, want null", index, status[index])
		}
	}
}
