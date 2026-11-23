// Package matchmaking is the ranked play queue: players wait in a pool, are paired by rating, invited to a match
// and sent to a ranked play room created for them. It also handles duels, which skip the queue and pair two
// chosen players for an unranked match.
//
// It is a port of osu-server-spectator's matchmaking queue (MatchmakingQueue, MatchmakingQueueBackgroundService,
// MatchmakingLobby, MatchmakingBeatmapSelector). What clients observe (callbacks and their order, timeouts,
// error texts) follows that server; where this port deliberately differs it says so.
package matchmaking

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// Pool is a matchmaking pool: which maps are played, by whom, and how players are paired.
type Pool struct {
	ID        int32
	RulesetID int32
	Variant   int32
	Name      string
	Type      protocol.PoolType
	// Active pools can be queued for; a pool that is switched off empties its queue.
	Active bool

	// LobbySize is how many players make a match; ranked play is one against one.
	LobbySize int
	// SearchRadius is the rating difference a waiting player accepts at first. It grows with the time spent
	// waiting (doubling every SearchRadiusExp seconds) up to SearchRadiusMax.
	SearchRadius    float64
	SearchRadiusMax float64
	SearchRadiusExp float64

	// Ranked matches change ratings. Pools are ranked; a duel plays the same maps unranked.
	Ranked bool
}

// Protocol is the pool as the client sees it.
func (p Pool) Protocol() protocol.Pool {
	return protocol.Pool{ID: p.ID, RulesetID: p.RulesetID, Variant: p.Variant, Name: p.Name, Type: p.Type}
}

// DisplayName is the pool's name with its ruleset, e.g. "osu! (1v1 RX)".
func (p Pool) DisplayName() string {
	switch p.RulesetID {
	case 0:
		return fmt.Sprintf("osu! (%s)", p.Name)
	case 1:
		return fmt.Sprintf("osu!taiko (%s)", p.Name)
	case 2:
		return fmt.Sprintf("osu!catch (%s)", p.Name)
	case 3:
		return fmt.Sprintf("osu!mania %dK (%s)", p.Variant, p.Name)
	}
	return p.Name
}

// Card is a beatmap of a pool, with the mods it is played with.
type Card struct {
	BeatmapID  int32
	Checksum   string
	Mode       int32
	StarRating float64
	// Mods is the JSON of the required mods, in the form the room serialises them to; empty for no mods.
	Mods string
	// Rating says how hard the card is; cards are drawn from the ones that match the players' ratings.
	Rating float64
}

// Rating is a player's rating.
type Rating struct {
	Mu    float64
	Sigma float64
	// Rank is the player's place in the pool; nil for somebody who has not finished a ranked match in it.
	Rank *int32
}

// UserInfo is what the queue needs to know about a player.
type UserInfo struct {
	ID         int32
	Username   string
	Restricted bool
}

// Directory is where pools, cards and ratings come from (the Python API).
type Directory interface {
	// Pools returns the active pools of a type.
	Pools(ctx context.Context, poolType protocol.PoolType) ([]Pool, error)
	// Pool returns one pool, or nil when there is none with that id.
	Pool(ctx context.Context, poolID int32) (*Pool, error)
	// Cards returns the beatmaps of a pool.
	Cards(ctx context.Context, poolID int32) ([]Card, error)
	// UserRating returns a player's rating in a pool; new players get an estimate.
	UserRating(ctx context.Context, poolID, userID int32) (Rating, error)
	// Ratings returns every rated player's rating in a pool, for the lobby's rating distribution.
	Ratings(ctx context.Context, poolID int32) ([]int32, error)
	// TopRating returns the rating of the 100th best player of a pool (0 when it has none).
	TopRating(ctx context.Context, poolID int32) (int32, error)
	// User returns a player's name and standing.
	User(ctx context.Context, userID int32) (UserInfo, error)
}

// Backend records what happens in matches.
type Backend interface {
	// Scores returns each player's total score on a playlist item.
	Scores(ctx context.Context, itemID int64) (room.RankedPlayScores, error)
	// RecordResult stores the result of a match and returns the ratings after it (empty for unranked matches).
	RecordResult(ctx context.Context, poolID int32, result room.MatchResult) (map[int32]int32, error)
}

// Rooms creates the rooms matches are played in.
type Rooms interface {
	CreateRankedPlay(ctx context.Context, params room.RankedPlayParams) (*room.Room, string, error)
}

// Notifier delivers callbacks to a connected player. It must not block.
type Notifier interface {
	Send(userID int32, target string, args ...any)
}

func invalid(format string, args ...any) error {
	return &room.Error{Type: "InvalidStateException", Message: fmt.Sprintf(format, args...)}
}

// PlaylistItem is what playing the card puts on the room: its beatmap and the mods it requires.
func (c Card) PlaylistItem() (protocol.PlaylistItem, error) {
	mods, err := parseMods(c.Mods)
	if err != nil {
		return protocol.PlaylistItem{}, err
	}

	item := protocol.PlaylistItem{
		BeatmapID:       c.BeatmapID,
		BeatmapChecksum: c.Checksum,
		RulesetID:       c.Mode,
		RequiredMods:    mods,
		StarRating:      c.StarRating,
	}
	item.Normalise()
	return item, nil
}

// parseMods reads the JSON a pool stores for a card's required mods (empty for none).
func parseMods(text string) ([]protocol.APIMod, error) {
	if strings.TrimSpace(text) == "" {
		return []protocol.APIMod{}, nil
	}

	var parsed []struct {
		Acronym  string         `json:"acronym"`
		Settings map[string]any `json:"settings"`
	}
	if err := json.Unmarshal([]byte(text), &parsed); err != nil {
		return nil, fmt.Errorf("reading the mods %q: %w", text, err)
	}

	mods := make([]protocol.APIMod, 0, len(parsed))
	for _, m := range parsed {
		mod := protocol.APIMod{Acronym: m.Acronym, Settings: m.Settings}
		mod.Normalise()
		mods = append(mods, mod)
	}
	return mods, nil
}
