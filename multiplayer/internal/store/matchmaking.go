package store

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"akatsuki/multiplayer/internal/matchmaking"
	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// The matchmaking half of the client: reads of pools, cards, ratings and scores, and recording the result of a
// match, through app/api/internal/matchmaking.py. It makes Client a matchmaking.Directory and a
// matchmaking.Backend. The wire types are pinned by contract/matchmaking_*.json.

var (
	_ matchmaking.Directory = (*Client)(nil)
	_ matchmaking.Backend   = (*Client)(nil)
)

type poolJSON struct {
	ID                    int32   `json:"id"`
	RulesetID             int32   `json:"ruleset_id"`
	VariantID             int32   `json:"variant_id"`
	Name                  string  `json:"name"`
	Type                  string  `json:"type"`
	Active                bool    `json:"active"`
	LobbySize             int     `json:"lobby_size"`
	RatingSearchRadius    float64 `json:"rating_search_radius"`
	RatingSearchRadiusMax float64 `json:"rating_search_radius_max"`
	RatingSearchRadiusExp float64 `json:"rating_search_radius_exp"`
}

type cardJSON struct {
	BeatmapID      int32   `json:"beatmap_id"`
	Checksum       string  `json:"checksum"`
	Mode           int32   `json:"mode"`
	StarRating     float64 `json:"star_rating"`
	Mods           string  `json:"mods"`
	Rating         int     `json:"rating"`
	SelectionCount int     `json:"selection_count"`
}

type userRatingJSON struct {
	UserID int32   `json:"user_id"`
	Mu     float64 `json:"mu"`
	Sigma  float64 `json:"sigma"`
	Rank   *int32  `json:"rank"`
}

type userJSON struct {
	ID           int32  `json:"id"`
	Username     string `json:"username"`
	IsRestricted bool   `json:"is_restricted"`
}

type scoresJSON struct {
	Scores map[string]int64 `json:"scores"`
}

type ratingsJSON struct {
	Ratings []int32 `json:"ratings"`
}

type topRatingJSON struct {
	Rating int32 `json:"rating"`
}

type playerResultJSON struct {
	UserID int32 `json:"user_id"`
	Life   int32 `json:"life"`
}

type matchResultJSON struct {
	RoomID  int64              `json:"room_id"`
	Ranked  bool               `json:"ranked"`
	Played  bool               `json:"played"`
	Winner  *int32             `json:"winner"`
	Players []playerResultJSON `json:"players"`
}

type matchResultReplyJSON struct {
	Ratings map[string]int32 `json:"ratings"`
	Result  string           `json:"result"`
}

func poolTypeName(t protocol.PoolType) string {
	if t == protocol.PoolQuickPlay {
		return "quick_play"
	}
	return "ranked_play"
}

func toPool(p poolJSON) matchmaking.Pool {
	poolType := protocol.PoolQuickPlay
	if p.Type == "ranked_play" {
		poolType = protocol.PoolRankedPlay
	}

	return matchmaking.Pool{
		ID:              p.ID,
		RulesetID:       p.RulesetID,
		Variant:         p.VariantID,
		Name:            p.Name,
		Type:            poolType,
		Active:          p.Active,
		LobbySize:       p.LobbySize,
		SearchRadius:    p.RatingSearchRadius,
		SearchRadiusMax: p.RatingSearchRadiusMax,
		SearchRadiusExp: p.RatingSearchRadiusExp,
		Ranked:          true,
	}
}

func isNotFound(err error) bool {
	var status *StatusError
	return errors.As(err, &status) && status.Code == http.StatusNotFound
}

// Pools implements matchmaking.Directory.
func (c *Client) Pools(ctx context.Context, poolType protocol.PoolType) ([]matchmaking.Pool, error) {
	var reply []poolJSON
	if err := c.do(ctx, http.MethodGet, "/internal/matchmaking/pools?type="+poolTypeName(poolType), nil, &reply); err != nil {
		return nil, err
	}

	pools := make([]matchmaking.Pool, 0, len(reply))
	for _, p := range reply {
		pools = append(pools, toPool(p))
	}
	return pools, nil
}

// Pool implements matchmaking.Directory. A pool that does not exist is (nil, nil).
func (c *Client) Pool(ctx context.Context, poolID int32) (*matchmaking.Pool, error) {
	var reply poolJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/pools/%d", poolID), nil, &reply); err != nil {
		if isNotFound(err) {
			return nil, nil
		}
		return nil, err
	}

	pool := toPool(reply)
	return &pool, nil
}

// Cards implements matchmaking.Directory.
func (c *Client) Cards(ctx context.Context, poolID int32) ([]matchmaking.Card, error) {
	// A big pool is megabytes of JSON and a match is made every few seconds in busy times; ratings only drift
	// slowly, so a short-lived copy is plenty fresh.
	c.cardsMu.Lock()
	cached, ok := c.cardsCache[poolID]
	c.cardsMu.Unlock()
	if ok && time.Since(cached.at) < cardsCacheTTL {
		return cached.cards, nil
	}

	cards, err := c.loadCards(ctx, poolID)
	if err != nil {
		return nil, err
	}
	c.cardsMu.Lock()
	if c.cardsCache == nil {
		c.cardsCache = map[int32]cachedCards{}
	}
	c.cardsCache[poolID] = cachedCards{cards: cards, at: time.Now()}
	c.cardsMu.Unlock()
	return cards, nil
}

const cardsCacheTTL = time.Minute

type cachedCards struct {
	cards []matchmaking.Card
	at    time.Time
}

func (c *Client) loadCards(ctx context.Context, poolID int32) ([]matchmaking.Card, error) {
	var reply []cardJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/pools/%d/beatmaps", poolID), nil, &reply); err != nil {
		return nil, err
	}

	cards := make([]matchmaking.Card, 0, len(reply))
	for _, card := range reply {
		cards = append(cards, matchmaking.Card{
			BeatmapID:  card.BeatmapID,
			Checksum:   card.Checksum,
			Mode:       card.Mode,
			StarRating: card.StarRating,
			Mods:       card.Mods,
			Rating:     float64(card.Rating),
		})
	}
	return cards, nil
}

// UserRating implements matchmaking.Directory.
func (c *Client) UserRating(ctx context.Context, poolID, userID int32) (matchmaking.Rating, error) {
	var reply userRatingJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/pools/%d/users/%d", poolID, userID), nil, &reply); err != nil {
		return matchmaking.Rating{}, err
	}
	return matchmaking.Rating{Mu: reply.Mu, Sigma: reply.Sigma, Rank: reply.Rank}, nil
}

// Ratings implements matchmaking.Directory.
func (c *Client) Ratings(ctx context.Context, poolID int32) ([]int32, error) {
	var reply ratingsJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/pools/%d/ratings", poolID), nil, &reply); err != nil {
		return nil, err
	}
	return reply.Ratings, nil
}

// TopRating implements matchmaking.Directory.
func (c *Client) TopRating(ctx context.Context, poolID int32) (int32, error) {
	var reply topRatingJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/pools/%d/top-rating", poolID), nil, &reply); err != nil {
		return 0, err
	}
	return reply.Rating, nil
}

// User implements matchmaking.Directory.
func (c *Client) User(ctx context.Context, userID int32) (matchmaking.UserInfo, error) {
	var reply userJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/users/%d", userID), nil, &reply); err != nil {
		return matchmaking.UserInfo{}, err
	}
	return matchmaking.UserInfo{ID: reply.ID, Username: reply.Username, Restricted: reply.IsRestricted}, nil
}

type inviteCheckJSON struct {
	Restricted bool `json:"restricted"`
	Blocked    bool `json:"blocked"`
	BlocksPMs  bool `json:"blocks_pms"`
}

var _ room.Relations = (*Client)(nil)

// InviteCheck implements room.Relations.
func (c *Client) InviteCheck(ctx context.Context, from, to int32) (room.InviteCheck, error) {
	var reply inviteCheckJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/multiplayer/invites/%d/%d", from, to), nil, &reply); err != nil {
		return room.InviteCheck{}, err
	}
	return room.InviteCheck{Restricted: reply.Restricted, Blocked: reply.Blocked, BlocksPMs: reply.BlocksPMs}, nil
}

// Scores implements matchmaking.Backend.
func (c *Client) Scores(ctx context.Context, itemID int64) (room.RankedPlayScores, error) {
	var reply scoresJSON
	if err := c.do(ctx, http.MethodGet, fmt.Sprintf("/internal/matchmaking/items/%d/scores", itemID), nil, &reply); err != nil {
		return nil, err
	}

	scores := make(room.RankedPlayScores, len(reply.Scores))
	for id, total := range reply.Scores {
		userID, err := strconv.ParseInt(id, 10, 32)
		if err != nil {
			return nil, fmt.Errorf("store: unexpected user id %q in the scores of item %d", id, itemID)
		}
		scores[int32(userID)] = total
	}
	return scores, nil
}

// RecordResult implements matchmaking.Backend.
func (c *Client) RecordResult(ctx context.Context, poolID int32, result room.MatchResult) (map[int32]int32, error) {
	body := matchResultJSON{
		RoomID:  result.RoomID,
		Ranked:  result.Ranked,
		Played:  result.Played,
		Winner:  result.Winner,
		Players: make([]playerResultJSON, 0, len(result.Players)),
	}
	for _, p := range result.Players {
		body.Players = append(body.Players, playerResultJSON{UserID: p.UserID, Life: p.Life})
	}

	var reply matchResultReplyJSON
	if err := c.do(ctx, http.MethodPost, fmt.Sprintf("/internal/matchmaking/pools/%d/results", poolID), body, &reply); err != nil {
		return nil, err
	}

	ratings := make(map[int32]int32, len(reply.Ratings))
	for id, rating := range reply.Ratings {
		userID, err := strconv.ParseInt(id, 10, 32)
		if err != nil {
			return nil, fmt.Errorf("store: unexpected user id %q in the result of room %d", id, result.RoomID)
		}
		ratings[int32(userID)] = rating
	}
	return ratings, nil
}
