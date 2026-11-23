package store

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

const apiKey = "secret-key"

// fakeAPI stands in for the Python internal API and records what it receives.
type fakeAPI struct {
	t  *testing.T
	mu sync.Mutex

	calls    []call
	failNext map[string][]int // "METHOD /path" -> statuses to answer with before succeeding
	// hook runs while a request is being handled, i.e. while the client has it in flight.
	hook     func(method, path string)
	nextRoom int64
	nextItem int64
}

type call struct {
	Method, Path string
	Key          string
	Body         any
}

func newFakeAPI(t *testing.T) (*fakeAPI, *Client) {
	t.Helper()
	api := &fakeAPI{t: t, failNext: map[string][]int{}, nextRoom: 700, nextItem: 5000}
	server := httptest.NewServer(api)
	t.Cleanup(server.Close)

	client := New(Config{BaseURL: server.URL, APIKey: apiKey, RetryDelay: 5 * time.Millisecond, MaxRetryDelay: 20 * time.Millisecond})
	return api, client
}

func (a *fakeAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var body any
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls = append(a.calls, call{Method: r.Method, Path: r.URL.Path, Key: r.Header.Get("X-Internal-Key"), Body: body})

	if a.hook != nil {
		a.hook(r.Method, r.URL.Path)
	}

	key := r.Method + " " + r.URL.Path
	if queue := a.failNext[key]; len(queue) > 0 {
		a.failNext[key] = queue[1:]
		http.Error(w, "injected failure", queue[0])
		return
	}
	if r.Header.Get("X-Internal-Key") != apiKey {
		http.Error(w, "bad key", http.StatusUnauthorized)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/internal/multiplayer/rooms":
		items := body.(map[string]any)["items"].([]any)
		ids := make([]int64, len(items))
		for i := range items {
			a.nextItem++
			ids[i] = a.nextItem
		}
		a.nextRoom++
		_ = json.NewEncoder(w).Encode(map[string]any{"room_id": a.nextRoom, "channel_id": a.nextRoom, "item_ids": ids})
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/items"):
		a.nextItem++
		_ = json.NewEncoder(w).Encode(map[string]any{"id": a.nextItem})
	case r.Method == http.MethodPost && r.URL.Path == "/internal/multiplayer/rooms/close-stale":
		_ = json.NewEncoder(w).Encode(map[string]any{"closed": 3})
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/internal/multiplayer/beatmaps/"):
		if strings.HasSuffix(r.URL.Path, "/404") {
			http.Error(w, "nope", http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": 1, "checksum": "abc", "mode": 3, "star_rating": 6.25})
	default:
		w.WriteHeader(http.StatusNoContent)
	}
}

func (a *fakeAPI) fail(method, path string, statuses ...int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.failNext[method+" "+path] = statuses
}

func (a *fakeAPI) callsTo(method, path string) []call {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []call
	for _, c := range a.calls {
		if c.Method == method && c.Path == path {
			out = append(out, c)
		}
	}
	return out
}

func (a *fakeAPI) count() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.calls)
}

func flush(t *testing.T, c *Client) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := c.Flush(ctx); err != nil {
		t.Fatal(err)
	}
}

func state(name string) room.RoomState {
	return room.RoomState{HostID: 1, Name: name, Status: room.StatusIdle, ParticipantCount: 1, MatchType: protocol.MatchHeadToHead}
}

// --- contract with the Python models ---------------------------------------------------------------------

func golden(t *testing.T, name string) any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "contract", name))
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func asJSON(t *testing.T, v any) any {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func assertGolden(t *testing.T, name string, got any) {
	t.Helper()
	if want := golden(t, name); !reflect.DeepEqual(asJSON(t, got), want) {
		t.Fatalf("%s: the request body drifted from the contract the Python tests validate\n got: %v\nwant: %v", name, asJSON(t, got), want)
	}
}

func TestWireFormatMatchesTheSharedContract(t *testing.T) {
	limit := uint8(8)
	_ = limit
	create := createRoomJSON{
		State: toStateJSON(room.RoomState{
			HostID: 42, Name: "golden room", Password: "pw", MatchType: protocol.MatchHeadToHead,
			QueueMode: protocol.QueueAllPlayers, Status: room.StatusIdle, ParticipantCount: 1, Participants: []int32{42},
			MaxParticipants: 8, AutoSkip: true,
		}),
		Items: []itemJSON{toItemJSON(room.ItemRecord{Item: protocol.PlaylistItem{
			OwnerID: 42, BeatmapID: 5542525, RulesetID: 0, PlaylistOrder: 0,
			RequiredMods: []protocol.APIMod{{Acronym: "DT", Settings: map[string]any{"speed_change": 1.3}}},
			AllowedMods:  []protocol.APIMod{{Acronym: "HD"}},
		}})},
	}
	assertGolden(t, "create_room.json", create)

	assertGolden(t, "room_state.json", toStateJSON(room.RoomState{
		HostID: 7, Name: "renamed", Password: "secret", MatchType: protocol.MatchTeamVersus,
		QueueMode: protocol.QueueAllPlayersRoundRobin, Status: room.StatusPlaying, ParticipantCount: 3, Participants: []int32{7, 8, 9},
		MaxParticipants: 4, AutoStartSeconds: 15, CurrentItemID: 9001,
	}))

	assertGolden(t, "room_state_unset.json", toStateJSON(room.RoomState{
		Name: "closed room", MatchType: protocol.MatchHeadToHead, Status: room.StatusClosed,
	}))

	played := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	assertGolden(t, "item_played.json", toItemJSON(room.ItemRecord{
		Item:     protocol.PlaylistItem{OwnerID: 7, BeatmapID: 100, RulesetID: 3, PlaylistOrder: 2, Expired: true},
		PlayedAt: &played,
	}))
}

// --- blocking calls ----------------------------------------------------------------------------------------

func TestCreateRoomReturnsDatabaseIds(t *testing.T) {
	api, client := newFakeAPI(t)

	created, err := client.CreateRoom(context.Background(), room.NewRoom{
		State: state("r"),
		Items: []protocol.PlaylistItem{{BeatmapID: 1}, {BeatmapID: 2}},
	})
	if err != nil {
		t.Fatal(err)
	}

	if created.RoomID != 701 || created.ChannelID != 701 || len(created.ItemIDs) != 2 || created.ItemIDs[0] != 5001 || created.ItemIDs[1] != 5002 {
		t.Fatalf("created = %+v", created)
	}
	if got := api.callsTo("POST", "/internal/multiplayer/rooms"); len(got) != 1 || got[0].Key != apiKey {
		t.Fatalf("the shared key must be sent: %+v", got)
	}
}

func TestBlockingCallsReportFailures(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("POST", "/internal/multiplayer/rooms", 500)
	api.fail("POST", "/internal/multiplayer/rooms/9/items", 409)

	if _, err := client.CreateRoom(context.Background(), room.NewRoom{State: state("r"), Items: []protocol.PlaylistItem{{}}}); err == nil {
		t.Fatal("a 500 must be an error")
	}
	_, err := client.AddItem(context.Background(), 9, room.ItemRecord{})
	if se, ok := err.(*StatusError); !ok || se.Code != 409 {
		t.Fatalf("got %v", err)
	}

	wrong := New(Config{BaseURL: strings.TrimSuffix(client.cfg.BaseURL, "/"), APIKey: "wrong"})
	if _, err := wrong.AddItem(context.Background(), 9, room.ItemRecord{}); err == nil {
		t.Fatal("a wrong key must be rejected")
	}
}

func TestBlockingCallsHonourTheContext(t *testing.T) {
	// The handler never answers. It is released at the end so the server can shut down: it does not read the
	// request body, so it would not notice the client hanging up on its own.
	release := make(chan struct{})
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer slow.Close()
	defer close(release)
	client := New(Config{BaseURL: slow.URL, APIKey: apiKey})

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	started := time.Now()
	_, err := client.AddItem(ctx, 1, room.ItemRecord{})

	if err == nil || time.Since(started) > 2*time.Second {
		t.Fatalf("must give up when the context expires, err=%v after %v", err, time.Since(started))
	}
}

func TestCloseStaleRooms(t *testing.T) {
	_, client := newFakeAPI(t)
	n, err := client.CloseStaleRooms(context.Background())
	if err != nil || n != 3 {
		t.Fatalf("n=%d err=%v", n, err)
	}
}

func TestBeatmapLookupCachesAndTreats404AsMissing(t *testing.T) {
	api, client := newFakeAPI(t)
	now := time.Now()
	client.cacheTime = func() time.Time { return now }

	first, err := client.Lookup(context.Background(), 1)
	if err != nil || first == nil || first.Checksum != "abc" || first.Mode != 3 || first.StarRating != 6.25 {
		t.Fatalf("lookup = %+v err=%v", first, err)
	}
	if _, err := client.Lookup(context.Background(), 1); err != nil {
		t.Fatal(err)
	}
	if n := len(api.callsTo("GET", "/internal/multiplayer/beatmaps/1")); n != 1 {
		t.Fatalf("second lookup must be served from the cache, %d requests made", n)
	}

	now = now.Add(2 * time.Minute)
	if _, err := client.Lookup(context.Background(), 1); err != nil {
		t.Fatal(err)
	}
	if n := len(api.callsTo("GET", "/internal/multiplayer/beatmaps/1")); n != 2 {
		t.Fatalf("an expired entry must be fetched again, %d requests made", n)
	}

	missing, err := client.Lookup(context.Background(), 404)
	if missing != nil || err != nil {
		t.Fatalf("an unknown beatmap is (nil, nil), got %+v %v", missing, err)
	}
}

// --- write-behind ---------------------------------------------------------------------------------------------

func TestUpdatesAreDeliveredInTheBackground(t *testing.T) {
	api, client := newFakeAPI(t)
	client.Start()
	defer client.Stop(context.Background())

	client.SaveRoom(7, state("hello"))
	client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3, BeatmapID: 5}})
	client.DeleteItem(7, 4)

	deadline := time.Now().Add(3 * time.Second)
	for client.Pending() > 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if client.Pending() != 0 {
		t.Fatal("updates were never delivered")
	}
	if len(api.callsTo("PUT", "/internal/multiplayer/rooms/7")) != 1 ||
		len(api.callsTo("PUT", "/internal/multiplayer/rooms/7/items/3")) != 1 ||
		len(api.callsTo("DELETE", "/internal/multiplayer/rooms/7/items/4")) != 1 {
		t.Fatalf("calls: %+v", api.calls)
	}
}

func TestOnlyTheLatestStateIsSent(t *testing.T) {
	api, client := newFakeAPI(t) // not started: nothing is sent until Flush

	for _, name := range []string{"a", "b", "c"} {
		client.SaveRoom(7, state(name))
	}
	client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3, BeatmapID: 1}})
	client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3, BeatmapID: 2}})
	flush(t, client)

	rooms := api.callsTo("PUT", "/internal/multiplayer/rooms/7")
	if len(rooms) != 1 || rooms[0].Body.(map[string]any)["name"] != "c" {
		t.Fatalf("expected a single update carrying the latest state: %+v", rooms)
	}
	items := api.callsTo("PUT", "/internal/multiplayer/rooms/7/items/3")
	if len(items) != 1 || items[0].Body.(map[string]any)["beatmap_id"] != float64(2) {
		t.Fatalf("expected a single update carrying the latest item: %+v", items)
	}
}

func TestDeleteSupersedesEarlierSaves(t *testing.T) {
	api, client := newFakeAPI(t)
	client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3}})
	client.DeleteItem(7, 3)
	flush(t, client)

	if len(api.callsTo("PUT", "/internal/multiplayer/rooms/7/items/3")) != 0 ||
		len(api.callsTo("DELETE", "/internal/multiplayer/rooms/7/items/3")) != 1 {
		t.Fatalf("an item that was deleted must not be written first: %+v", api.calls)
	}
}

func TestTransientFailuresAreRetried(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7", 503, 502)
	client.SaveRoom(7, state("x"))

	flush(t, client)

	if n := len(api.callsTo("PUT", "/internal/multiplayer/rooms/7")); n != 3 {
		t.Fatalf("two failures then a success = 3 attempts, got %d", n)
	}
}

func TestRejectedUpdatesAreDroppedNotRetriedForever(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7", 404, 404, 404, 404)
	api.fail("PUT", "/internal/multiplayer/rooms/8", 422, 422, 422, 422)

	client.SaveRoom(7, state("gone"))    // room deleted from the database
	client.SaveRoom(8, state("invalid")) // payload refused
	flush(t, client)

	if client.Pending() != 0 {
		t.Fatal("4xx answers are final")
	}
	if len(api.callsTo("PUT", "/internal/multiplayer/rooms/7")) != 1 || len(api.callsTo("PUT", "/internal/multiplayer/rooms/8")) != 1 {
		t.Fatal("a rejected update must not be resent")
	}
}

func TestRateLimitingIsRetried(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7", 429)
	client.SaveRoom(7, state("x"))
	flush(t, client)

	if n := len(api.callsTo("PUT", "/internal/multiplayer/rooms/7")); n != 2 {
		t.Fatalf("429 must be retried, got %d attempts", n)
	}
}

func TestFlushGivesUpWhenTheContextEnds(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7", 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500)
	client.SaveRoom(7, state("x"))

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()
	err := client.Flush(ctx)

	if err == nil || !strings.Contains(err.Error(), "not delivered") {
		t.Fatalf("an undeliverable update must be reported on shutdown, got %v", err)
	}
}

func TestStopDeliversWhatIsPending(t *testing.T) {
	api, client := newFakeAPI(t)
	client.Start()
	client.SaveRoom(7, state("last words"))

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := client.Stop(ctx); err != nil {
		t.Fatal(err)
	}
	if len(api.callsTo("PUT", "/internal/multiplayer/rooms/7")) != 1 {
		t.Fatal("pending state must be flushed on stop")
	}
	if api.count() == 0 || client.Pending() != 0 {
		t.Fatal("nothing may be left behind")
	}
}

// --- end to end with a real room --------------------------------------------------------------------------------

func TestARoomPersistsThroughTheHTTPStore(t *testing.T) {
	api, client := newFakeAPI(t)
	client.Start()

	rec := &discard{}
	manager := room.NewManager(rec, room.Options{Store: client, Beatmaps: client})
	r, snapshot, err := manager.Create(context.Background(), 1, protocol.Room{
		Settings: protocol.DefaultSettings(),
		Playlist: []protocol.PlaylistItem{{BeatmapID: 1, BeatmapChecksum: "abc", RulesetID: 3}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.RoomID != 701 || snapshot.Playlist[0].ID != 5001 || snapshot.Playlist[0].StarRating != 6.25 {
		t.Fatalf("ids and star rating must come from the API: %+v", snapshot)
	}

	if _, _, err := manager.Join(r.ID(), 2, ""); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := client.Stop(ctx); err != nil {
		t.Fatal(err)
	}

	puts := api.callsTo("PUT", "/internal/multiplayer/rooms/701")
	if len(puts) == 0 {
		t.Fatal("the room state was never saved")
	}
	last := puts[len(puts)-1].Body.(map[string]any)
	if last["participant_count"] != float64(2) || last["current_playlist_item_id"] != float64(5001) || last["status"] != "idle" {
		t.Fatalf("last saved state: %v", last)
	}
}

type discard struct{}

func (*discard) Send(int32, string, ...any) {}

func TestRetryNeverOverwritesANewerState(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7", 503)

	// While the first attempt is in flight (and about to fail), a newer state is saved.
	api.hook = func(method, path string) {
		if method == http.MethodPut && path == "/internal/multiplayer/rooms/7" {
			client.SaveRoom(7, state("new"))
			api.hook = nil
		}
	}
	client.SaveRoom(7, state("old"))
	flush(t, client)

	rooms := api.callsTo("PUT", "/internal/multiplayer/rooms/7")
	if len(rooms) != 2 {
		t.Fatalf("expected the failed attempt and one retry, got %d calls", len(rooms))
	}
	if retried := rooms[1].Body.(map[string]any)["name"]; retried != "new" {
		t.Fatalf("the retry must carry the newer state, but it re-sent %v", retried)
	}
}

func TestItemRetryNeverOverwritesANewerState(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("PUT", "/internal/multiplayer/rooms/7/items/3", 503)
	api.hook = func(method, path string) {
		if method == http.MethodPut && path == "/internal/multiplayer/rooms/7/items/3" {
			client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3, BeatmapID: 2}})
			api.hook = nil
		}
	}
	client.SaveItem(7, room.ItemRecord{Item: protocol.PlaylistItem{ID: 3, BeatmapID: 1}})
	flush(t, client)

	puts := api.callsTo("PUT", "/internal/multiplayer/rooms/7/items/3")
	if len(puts) != 2 || puts[1].Body.(map[string]any)["beatmap_id"] != float64(2) {
		t.Fatalf("the retry must carry the newer item state: %+v", puts)
	}
}

func TestDeleteRetryIsNotUndoneByAnOlderSave(t *testing.T) {
	api, client := newFakeAPI(t)
	api.fail("DELETE", "/internal/multiplayer/rooms/7/items/3", 503)
	client.DeleteItem(7, 3)
	flush(t, client)

	if len(api.callsTo("DELETE", "/internal/multiplayer/rooms/7/items/3")) != 2 || len(api.callsTo("PUT", "/internal/multiplayer/rooms/7/items/3")) != 0 {
		t.Fatalf("a failed delete is retried as a delete: %+v", api.calls)
	}
}

func TestItemsWithoutAnOwnerAreSentWithANullOwner(t *testing.T) {
	// A played ranked play card is made by the server: owner 0 would violate the users foreign key.
	owned := toItemJSON(room.ItemRecord{Item: protocol.PlaylistItem{OwnerID: 7, BeatmapID: 1}})
	if owned.OwnerID == nil || *owned.OwnerID != 7 {
		t.Fatalf("owned item: %v", owned.OwnerID)
	}
	server := toItemJSON(room.ItemRecord{Item: protocol.PlaylistItem{BeatmapID: 1}})
	if server.OwnerID != nil {
		t.Fatalf("server item owner = %d, want null", *server.OwnerID)
	}
	body, err := json.Marshal(server)
	if err != nil || !strings.Contains(string(body), `"owner_id":null`) {
		t.Fatalf("json %s err=%v", body, err)
	}
}
