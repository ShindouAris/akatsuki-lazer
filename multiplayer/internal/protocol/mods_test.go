package protocol

import (
	"encoding/hex"
	"reflect"
	"testing"

	"github.com/vmihailenco/msgpack/v5"
)

// The client's ModSettingsDictionaryFormatter writes an array header holding the number of settings followed by
// that many key/value pairs, not a msgpack map. A map made the client reject every item that carried a mod
// ("Unexpected msgpack code 128 (fixmap)"), which left ranked play cards face down in the RX and AP pools.
func TestModSettingsUseTheClientsPairedArrayFormat(t *testing.T) {
	cases := []struct {
		name string
		mod  APIMod
		want string
	}{
		{"no settings", APIMod{Acronym: "RX"}, "92a2525890"},
		{"one setting", APIMod{Acronym: "DT", Settings: map[string]any{"speed_change": 1.5}}, "92a24454" + "91" + "ac" + hex.EncodeToString([]byte("speed_change")) + "cb3ff8000000000000"},
	}
	for _, c := range cases {
		got, err := msgpack.Marshal(c.mod)
		if err != nil {
			t.Fatal(err)
		}
		if hex.EncodeToString(got) != c.want {
			t.Errorf("%s: encoded %x, want %s", c.name, got, c.want)
		}
	}
}

func TestModsRoundTripThroughTheClientFormat(t *testing.T) {
	in := []APIMod{
		{Acronym: "RX", Settings: map[string]any{}},
		{Acronym: "DT", Settings: map[string]any{"speed_change": 1.25, "adjust_pitch": true}},
	}
	raw, err := msgpack.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	var out []APIMod
	if err := msgpack.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Errorf("round trip changed the mods: %v -> %v", in, out)
	}
}

func TestAPlaylistItemWithAModEncodesTheModLikeTheClientReadsIt(t *testing.T) {
	item := PlaylistItem{BeatmapChecksum: "x", RequiredMods: []APIMod{{Acronym: "RX"}}, AllowedMods: []APIMod{}}
	raw, err := msgpack.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	if !contains(hex.EncodeToString(raw), "9192a2525890") {
		t.Errorf("required mods are not [[RX, <no settings>]]: %x", raw)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
