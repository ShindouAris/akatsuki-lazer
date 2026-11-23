package store

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// matchmakingAPI stands in for app/api/internal/matchmaking.py: it answers with the documents of the shared
// contract and records what it was asked.
type matchmakingAPI struct {
	t      *testing.T
	server *httptest.Server
	mu     sync.Mutex
	calls  []call
	// status overrides the answer of a route ("GET /path") with an error status
	status map[string]int
}

func newMatchmakingAPI(t *testing.T) (*matchmakingAPI, *Client) {
	t.Helper()

	api := &matchmakingAPI{t: t, status: map[string]int{}}
	api.server = httptest.NewServer(http.HandlerFunc(api.handle))
	t.Cleanup(api.server.Close)

	return api, New(Config{BaseURL: api.server.URL, APIKey: apiKey})
}

func (a *matchmakingAPI) handle(w http.ResponseWriter, r *http.Request) {
	var body any
	if r.Body != nil && r.ContentLength != 0 {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}

	route := r.Method + " " + r.URL.Path
	a.mu.Lock()
	a.calls = append(a.calls, call{Method: r.Method, Path: r.URL.RequestURI(), Key: r.Header.Get("X-Internal-Key"), Body: body})
	override := a.status[route]
	a.mu.Unlock()

	if r.Header.Get("X-Internal-Key") != apiKey {
		http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
		return
	}
	if override != 0 {
		http.Error(w, `{"error":"nope"}`, override)
		return
	}

	sample := func(name string) any { return golden(a.t, name) }

	var reply any
	switch {
	case route == "GET /internal/matchmaking/pools":
		reply = []any{sample("matchmaking_pool.json")}
	case route == "GET /internal/matchmaking/pools/3":
		reply = sample("matchmaking_pool.json")
	case route == "GET /internal/matchmaking/pools/3/beatmaps":
		reply = []any{sample("matchmaking_card.json")}
	case route == "GET /internal/matchmaking/pools/3/users/7":
		reply = sample("matchmaking_user_rating.json")
	case route == "GET /internal/matchmaking/pools/3/ratings":
		reply = sample("matchmaking_ratings.json")
	case route == "GET /internal/matchmaking/pools/3/top-rating":
		reply = sample("matchmaking_top_rating.json")
	case route == "GET /internal/multiplayer/invites/1/7":
		reply = sample("invite_check.json")
	case route == "GET /internal/matchmaking/users/7":
		reply = sample("matchmaking_user.json")
	case route == "GET /internal/matchmaking/users/8":
		restricted := sample("matchmaking_user.json").(map[string]any)
		restricted["id"], restricted["is_restricted"] = 8, true
		reply = restricted
	case route == "GET /internal/matchmaking/items/55/scores":
		reply = sample("matchmaking_scores.json")
	case route == "POST /internal/matchmaking/pools/3/results":
		reply = sample("matchmaking_result_response.json")
	case route == "POST /internal/multiplayer/rooms":
		reply = map[string]any{"room_id": 9, "channel_id": 9, "item_ids": []int64{}}
	default:
		http.NotFound(w, r)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(reply)
}

func (a *matchmakingAPI) last() call {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.calls[len(a.calls)-1]
}

func TestPoolsAreReadWithTheirTypeAndSettings(t *testing.T) {
	api, c := newMatchmakingAPI(t)

	pools, err := c.Pools(context.Background(), protocol.PoolRankedPlay)
	if err != nil {
		t.Fatal(err)
	}

	if got := api.last(); got.Path != "/internal/matchmaking/pools?type=ranked_play" || got.Key != apiKey {
		t.Errorf("request = %+v, want the type as a query and the internal key", got)
	}
	if len(pools) != 1 {
		t.Fatalf("pools = %+v", pools)
	}
	pool := pools[0]
	if pool.ID != 3 || pool.Name != "1v1 RX" || pool.Type != protocol.PoolRankedPlay || !pool.Active || !pool.Ranked ||
		pool.LobbySize != 2 || pool.SearchRadius != 200 || pool.SearchRadiusMax != 9999 || pool.SearchRadiusExp != 30 {
		t.Errorf("pool = %+v", pool)
	}
}

func TestQuickPlayPoolsAreAskedForByTheirOwnType(t *testing.T) {
	api, c := newMatchmakingAPI(t)
	_, _ = c.Pools(context.Background(), protocol.PoolQuickPlay)

	if got := api.last().Path; got != "/internal/matchmaking/pools?type=quick_play" {
		t.Errorf("path = %q", got)
	}
}

func TestAPoolThatDoesNotExistIsNilNotAnError(t *testing.T) {
	api, c := newMatchmakingAPI(t)
	api.status["GET /internal/matchmaking/pools/3"] = http.StatusNotFound

	pool, err := c.Pool(context.Background(), 3)

	if err != nil || pool != nil {
		t.Errorf("pool = %+v err = %v, want nothing and no error", pool, err)
	}
}

func TestOtherFailuresReadingAPoolAreErrors(t *testing.T) {
	api, c := newMatchmakingAPI(t)
	api.status["GET /internal/matchmaking/pools/3"] = http.StatusInternalServerError

	if _, err := c.Pool(context.Background(), 3); err == nil {
		t.Error("a server error must not look like a missing pool")
	}
}

func TestACardKeepsItsModsAndRating(t *testing.T) {
	_, c := newMatchmakingAPI(t)

	cards, err := c.Cards(context.Background(), 3)
	if err != nil {
		t.Fatal(err)
	}

	if len(cards) != 1 {
		t.Fatalf("cards = %+v", cards)
	}
	card := cards[0]
	if card.BeatmapID != 259 || card.Checksum != "ea0df9f890e7e9e7ad4d3862a7823359" || card.Mods != `[{"acronym":"RX"}]` || card.Rating != 1238 || card.StarRating != 3.93 {
		t.Errorf("card = %+v", card)
	}
}

func TestPlayersRatingsAndInformation(t *testing.T) {
	_, c := newMatchmakingAPI(t)
	ctx := context.Background()

	rating, err := c.UserRating(ctx, 3, 7)
	if err != nil || rating.Mu != 1523.5 || rating.Sigma != 141.5 {
		t.Errorf("rating = %+v err=%v", rating, err)
	}

	user, err := c.User(ctx, 7)
	if err != nil || user.ID != 7 || user.Username != "alice" || user.Restricted {
		t.Errorf("user = %+v err=%v", user, err)
	}

	ratings, err := c.Ratings(ctx, 3)
	if err != nil || !reflect.DeepEqual(ratings, []int32{1500, 1525}) {
		t.Errorf("ratings = %v err=%v", ratings, err)
	}

	top, err := c.TopRating(ctx, 3)
	if err != nil || top != 2100 {
		t.Errorf("top rating = %d err=%v", top, err)
	}
}

func TestScoresAreKeyedByUser(t *testing.T) {
	_, c := newMatchmakingAPI(t)

	scores, err := c.Scores(context.Background(), 55)
	if err != nil {
		t.Fatal(err)
	}

	if want := (room.RankedPlayScores{7: 900_000, 8: 700_000}); !reflect.DeepEqual(scores, want) {
		t.Errorf("scores = %v, want %v", scores, want)
	}
}

func TestResultsAreSentInTheSharedContractFormat(t *testing.T) {
	api, c := newMatchmakingAPI(t)
	winner := int32(7)

	ratings, err := c.RecordResult(context.Background(), 3, room.MatchResult{
		RoomID:  42,
		PoolID:  3,
		Ranked:  true,
		Played:  true,
		Winner:  &winner,
		Players: []room.PlayerResult{{UserID: 7, Rating: 1500, Life: 600_000}, {UserID: 8, Rating: 1600, Life: 0}},
	})
	if err != nil {
		t.Fatal(err)
	}

	assertGolden(t, "matchmaking_result_request.json", api.last().Body)
	if want := map[int32]int32{7: 1566, 8: 1534}; !reflect.DeepEqual(ratings, want) {
		t.Errorf("ratings = %v, want %v", ratings, want)
	}
}

func TestARestrictedPlayerIsRestricted(t *testing.T) {
	_, c := newMatchmakingAPI(t)

	user, err := c.User(context.Background(), 8)
	if err != nil || user.ID != 8 || !user.Restricted {
		t.Errorf("user = %+v err=%v", user, err)
	}
}

func TestUnrankedAndUnplayedMatchesAreSentAsSuch(t *testing.T) {
	api, c := newMatchmakingAPI(t)

	if _, err := c.RecordResult(context.Background(), 3, room.MatchResult{RoomID: 1}); err != nil {
		t.Fatal(err)
	}

	body := api.last().Body.(map[string]any)
	if body["ranked"] != false || body["played"] != false {
		t.Errorf("ranked = %v played = %v, want both false", body["ranked"], body["played"])
	}
}

func TestADrawIsSentWithoutAWinner(t *testing.T) {
	api, c := newMatchmakingAPI(t)

	if _, err := c.RecordResult(context.Background(), 3, room.MatchResult{RoomID: 1, Played: true}); err != nil {
		t.Fatal(err)
	}

	body := api.last().Body.(map[string]any)
	if winner, present := body["winner"]; !present || winner != nil {
		t.Errorf("winner = %v (present %v), want an explicit null", winner, present)
	}
	if players, ok := body["players"].([]any); !ok || len(players) != 0 {
		t.Errorf("players = %v, want an empty list and not null", body["players"])
	}
}

func TestTheInternalKeyIsRequired(t *testing.T) {
	api, _ := newMatchmakingAPI(t)
	c := New(Config{BaseURL: api.server.URL, APIKey: "wrong"})

	_, err := c.Pool(context.Background(), 3)

	var status *StatusError
	if err == nil || !strings.Contains(err.Error(), "401") || !asStatus(err, &status) || status.Code != http.StatusUnauthorized {
		t.Errorf("err = %v, want an unauthorized error", err)
	}
}

func asStatus(err error, target **StatusError) bool {
	s, ok := err.(*StatusError)
	*target = s
	return ok
}

func TestRankedPlayRoomsAreCreatedWithoutItems(t *testing.T) {
	api, c := newMatchmakingAPI(t)

	created, err := c.CreateRoom(context.Background(), room.NewRoom{State: room.RoomState{
		Name:      "Ranked play: a vs b",
		Password:  "secret",
		MatchType: protocol.MatchRankedPlay,
		QueueMode: protocol.QueueHostOnly,
		Status:    room.StatusIdle,
	}})
	if err != nil {
		t.Fatal(err)
	}

	assertGolden(t, "create_ranked_play_room.json", api.last().Body)
	if created.RoomID != 9 || len(created.ItemIDs) != 0 {
		t.Errorf("created = %+v", created)
	}
}

func TestInviteChecksAreReadFromTheSharedContract(t *testing.T) {
	_, c := newMatchmakingAPI(t)

	check, err := c.InviteCheck(context.Background(), 1, 7)
	if err != nil {
		t.Fatal(err)
	}

	if want := (room.InviteCheck{Blocked: true}); check != want {
		t.Errorf("check = %+v, want %+v", check, want)
	}
}

func TestARatingCarriesItsRank(t *testing.T) {
	_, c := newMatchmakingAPI(t)

	rating, err := c.UserRating(context.Background(), 3, 7)

	if err != nil || rating.Rank == nil || *rating.Rank != 12 {
		t.Errorf("rating = %+v err=%v, want rank 12", rating, err)
	}
}
