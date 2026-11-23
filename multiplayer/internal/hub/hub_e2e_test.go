package hub

import (
	"context"
	"os"
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/store"
)

// TestAgainstTheRealPythonAPI plays a whole match through the hub with the real HTTP store pointed at the
// Python internal API. It needs a running API (see ../../e2e/README.md) and is skipped otherwise.
func TestAgainstTheRealPythonAPI(t *testing.T) {
	apiURL, key := os.Getenv("E2E_API_URL"), os.Getenv("E2E_API_KEY")
	if apiURL == "" || key == "" {
		t.Skip("set E2E_API_URL and E2E_API_KEY to run against the Python internal API")
	}

	persistence := store.New(store.Config{BaseURL: apiURL, APIKey: key})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	closed, err := persistence.CloseStaleRooms(ctx)
	if err != nil {
		t.Fatalf("the API must be reachable and accept the key: %v", err)
	}
	t.Logf("closed %d stale rooms", closed)
	persistence.Start()

	e := newEnvWith(t, room.Options{Store: persistence, Beatmaps: persistence, LoadTimeout: 5 * time.Second})
	host, guest := e.connect(t, 1), e.connect(t, 2)

	payload := roomPayload()
	payload[2].([]any)[0] = "e2e room"
	payload[6].([]any)[0].([]any)[2] = 1        // beatmap id seeded by the harness
	payload[6].([]any)[0].([]any)[3] = "abc123" // and its checksum
	created, failure := host.call("CreateRoom", payload)
	if failure != "" {
		t.Fatalf("CreateRoom against the real API: %s", failure)
	}
	roomID := toInt(created.([]any)[0])
	firstItem := toInt(created.([]any)[6].([]any)[0].([]any)[0])
	t.Logf("E2E_ROOM_ID=%d E2E_FIRST_ITEM=%d", roomID, firstItem)

	if _, failure := host.call("CreateRoom", payload); failure == "" {
		t.Fatal("creating a second room while in one must fail")
	}
	if _, failure := guest.call("JoinRoom", roomID); failure != "" {
		t.Fatal(failure)
	}

	// A beatmap the API does not know is refused before anything is stored.
	bad := newPlaylistPayload(777777, "nope")
	if _, failure := host.call("AddPlaylistItem", bad); failure == "" {
		t.Fatal("an unknown beatmap must be refused")
	}

	for _, c := range []*client{host, guest} {
		c.call("ChangeBeatmapAvailability", []any{4, nil})
		c.call("ChangeState", int(protocol.UserReady))
	}
	if _, failure := host.call("StartMatch"); failure != "" {
		t.Fatal(failure)
	}
	for _, c := range []*client{host, guest} {
		c.call("ChangeState", int(protocol.UserLoaded))
		c.call("ChangeState", int(protocol.UserReadyForGameplay))
	}
	for _, c := range []*client{host, guest} {
		if _, failure := c.call("ChangeState", int(protocol.UserFinishedPlay)); failure != "" {
			t.Fatal(failure)
		}
	}
	host.expect("ResultsReady")

	// Back in the lobby the host queues one more item, then everybody leaves.
	if _, failure := host.call("AddPlaylistItem", newPlaylistPayload(1, "abc123")); failure != "" {
		t.Fatalf("AddPlaylistItem against the real API: %s", failure)
	}
	for _, c := range []*client{guest, host} {
		if _, failure := c.call("LeaveRoom"); failure != "" {
			t.Fatal(failure)
		}
	}

	if err := persistence.Stop(ctx); err != nil {
		t.Fatalf("pending updates must all be delivered: %v", err)
	}
}

func newPlaylistPayload(beatmapID int, checksum string) []any {
	return []any{0, 0, beatmapID, checksum, 0, []any{}, []any{}, false, 0, nil, 0.0, false}
}
