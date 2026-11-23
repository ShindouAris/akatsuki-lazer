package protocol

import (
	"encoding/hex"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/vmihailenco/msgpack/v5"
)

func pack(t *testing.T, v any) []byte {
	t.Helper()
	data, err := msgpack.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func generic(t *testing.T, data []byte) any {
	t.Helper()
	var out any
	if err := msgpack.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// goldenRoom is the room produced by the Python server's models (testdata/room.hex), which is known to work
// with the client. The Go types must decode it and re-encode it identically, except that Go also writes the
// user's Role (Key 8), which the Python models omit.
func TestRoomMatchesPythonEncoding(t *testing.T) {
	raw, err := os.ReadFile("testdata/room.hex")
	if err != nil {
		t.Fatal(err)
	}
	python, err := hex.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil {
		t.Fatal(err)
	}

	var room Room
	if err := msgpack.Unmarshal(python, &room); err != nil {
		t.Fatalf("decoding the Python encoding: %v", err)
	}

	if room.RoomID != 7 || room.Settings.Name != "golden room" || room.Settings.PlaylistItemID != 1001 ||
		room.Settings.QueueMode != QueueAllPlayers || !room.Settings.AutoSkip ||
		room.Settings.AutoStartDuration != 300_000_000 {
		t.Fatalf("settings decoded wrongly: %+v", room.Settings)
	}
	if len(room.Users) != 1 || room.Users[0].BeatmapAvailability.State != DownloadDownloading ||
		*room.Users[0].BeatmapAvailability.DownloadProgress != 0.5 || room.Users[0].Mods[0].Acronym != "HD" {
		t.Fatalf("user decoded wrongly: %+v", room.Users)
	}
	if room.Playlist[0].StarRating != 4.5 || room.Playlist[0].PlaylistOrder != 3 || room.Playlist[0].RequiredMods[0].Acronym != "DT" {
		t.Fatalf("playlist decoded wrongly: %+v", room.Playlist)
	}

	room.Normalise()
	got := generic(t, pack(t, room)).([]any)
	want := generic(t, python).([]any)

	// Go also writes the keys the Python models omit: Role (Key 8) on every user, MaxParticipants (Key 7).
	want[2] = append(want[2].([]any), nil)
	appendRole := func(user any) any { return append(user.([]any), int8(0)) }
	want[3] = []any{appendRole(want[3].([]any)[0])}
	want[4] = appendRole(want[4])

	if !reflect.DeepEqual(normaliseInts(got), normaliseInts(want)) {
		t.Fatalf("encoding differs from the Python server\n got: %#v\nwant: %#v", got, want)
	}
}

// normaliseInts makes numeric types comparable (msgpack picks the narrowest int type per decoder).
func normaliseInts(v any) any {
	switch x := v.(type) {
	case []any:
		out := make([]any, len(x))
		for i := range x {
			out[i] = normaliseInts(x[i])
		}
		return out
	case map[string]any:
		// The Python models write a mod's (empty) settings as a map; the client reads an array
		// (see mods_test.go), which is what Go writes, so an empty map and an empty array are equal here.
		if len(x) == 0 {
			return []any{}
		}
		out := map[string]any{}
		for k, e := range x {
			out[k] = normaliseInts(e)
		}
		return out
	case int8:
		return int64(x)
	case int16:
		return int64(x)
	case int32:
		return int64(x)
	case float32:
		return float64(x)
	case int64:
		return x
	case uint8:
		return int64(x)
	case uint16:
		return int64(x)
	case uint32:
		return int64(x)
	case uint64:
		return int64(x)
	}
	return v
}

func TestStructsEncodeAsArraysInKeyOrder(t *testing.T) {
	user := NewRoomUser(5)
	got := generic(t, pack(t, user)).([]any)
	if len(got) != 9 {
		t.Fatalf("RoomUser has %d elements, the client defines Key(0)..Key(8)", len(got))
	}
	for _, index := range []int{4, 5, 6} { // MatchState, RulesetId, BeatmapId default to null
		if got[index] != nil {
			t.Fatalf("element %d should be null, got %v", index, got[index])
		}
	}

	if n := len(generic(t, pack(t, Settings{})).([]any)); n != 8 {
		t.Fatalf("Settings has %d elements, want 8", n)
	}
	if n := len(generic(t, pack(t, PlaylistItem{})).([]any)); n != 12 {
		t.Fatalf("PlaylistItem has %d elements, want 12", n)
	}
	if n := len(generic(t, pack(t, Room{})).([]any)); n != 9 {
		t.Fatalf("Room has %d elements, want 9", n)
	}
}

// The client treats collections as non-null; a nil slice would encode as nil and crash it.
func TestNormaliseNeverLeavesNullCollections(t *testing.T) {
	room := Room{Users: nil, Playlist: []PlaylistItem{{}}, Host: &RoomUser{}}
	room.Normalise()

	got := generic(t, pack(t, room)).([]any)
	for _, index := range []int{3, 6, 7} {
		if _, ok := got[index].([]any); !ok {
			t.Errorf("room element %d is %T, want an array", index, got[index])
		}
	}
	item := got[6].([]any)[0].([]any)
	for _, index := range []int{5, 6} {
		if _, ok := item[index].([]any); !ok {
			t.Errorf("playlist item element %d is %T, want an array", index, item[index])
		}
	}
	if _, ok := got[4].([]any)[3].([]any); !ok {
		t.Error("host mods must be an array")
	}
}

func TestUserStateHelpers(t *testing.T) {
	gameplay := map[UserState]bool{UserWaitingForLoad: true, UserLoaded: true, UserReadyForGameplay: true, UserPlaying: true}
	for state := UserIdle; state <= UserSpectating; state++ {
		if state.IsGameplay() != gameplay[state] {
			t.Errorf("%s: IsGameplay = %v", state, state.IsGameplay())
		}
	}
	if UserReadyForGameplay.String() != "ReadyForGameplay" || UserState(99).String() != "Unknown" {
		t.Error("state names must match the .NET enum names")
	}
}

func TestSettingsEqual(t *testing.T) {
	a := DefaultSettings()
	b := DefaultSettings()
	if !a.Equal(b) {
		t.Fatal("identical settings must be equal")
	}
	limit := uint8(8)
	b.MaxParticipants = &limit
	if a.Equal(b) {
		t.Fatal("max participants must be compared")
	}
	a.MaxParticipants = new(uint8)
	*a.MaxParticipants = 8
	if !a.Equal(b) {
		t.Fatal("equal pointees must be equal")
	}
}

// A newer client may add keys, an older one may omit them; decoding must not fail either way.
func TestDecodeToleratesArrayLengthMismatch(t *testing.T) {
	longer := pack(t, []any{int32(9), 1, []any{2, 0.5}, []any{}, nil, nil, nil, true, 1, "future key", 123})
	var user RoomUser
	if err := msgpack.Unmarshal(longer, &user); err != nil {
		t.Fatalf("extra trailing keys must be ignored: %v", err)
	}
	if user.UserID != 9 || user.State != UserReady || !user.VotedToSkipIntro || user.Role != RoleReferee {
		t.Fatalf("decoded wrongly: %+v", user)
	}

	shorter := pack(t, []any{int32(9), 1})
	user = RoomUser{}
	if err := msgpack.Unmarshal(shorter, &user); err != nil {
		t.Fatalf("missing trailing keys must keep defaults: %v", err)
	}
	if user.UserID != 9 || user.State != UserReady || user.Role != RolePlayer {
		t.Fatalf("decoded wrongly: %+v", user)
	}
}

func TestDecodeNullAndNestedNulls(t *testing.T) {
	var room Room
	data := pack(t, []any{int64(1), 0, DefaultSettings(), []any{}, nil, nil, []any{}, []any{}, 0})
	if err := msgpack.Unmarshal(data, &room); err != nil {
		t.Fatal(err)
	}
	if room.Host != nil || room.MatchState != nil {
		t.Fatalf("null host / match state must stay nil: %+v", room)
	}
}
