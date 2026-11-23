package protocol

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"os"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/vmihailenco/msgpack/v5"
	"github.com/vmihailenco/msgpack/v5/msgpcode"
)

const (
	cardA = "11111111-2222-3333-4444-555555555555"
	cardB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)

// vectors returns the bytes the osu! client produced for each named ranked play value (see testdata).
func vectors(t *testing.T) map[string][]byte { return loadVectors(t, "rankedplay_vectors.tsv") }

// loadVectors reads a testdata file of "name<TAB>hex" lines.
func loadVectors(t *testing.T, name string) map[string][]byte {
	t.Helper()

	file, err := os.Open("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()

	result := map[string][]byte{}
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 1<<20), 1<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, encoded, ok := strings.Cut(line, "\t")
		if !ok {
			t.Fatalf("malformed vector line %q", line)
		}
		bytes, err := hex.DecodeString(encoded)
		if err != nil {
			t.Fatalf("vector %s: %v", name, err)
		}
		result[name] = bytes
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

// decodeLoose decodes MessagePack into plain Go values: arrays become []any, maps become map[any]any, and every
// integer becomes an int64 (the client accepts any integer width and writes the smallest, so only the value
// matters). Floats keep their width, which the client does see.
func decodeLoose(t *testing.T, data []byte) any {
	t.Helper()

	dec := msgpack.NewDecoder(bytes.NewReader(data))
	value, err := decodeValue(dec)
	if err != nil {
		t.Fatalf("decoding %x: %v", data, err)
	}
	return value
}

func decodeValue(dec *msgpack.Decoder) (any, error) {
	code, err := dec.PeekCode()
	if err != nil {
		return nil, err
	}

	switch {
	case code == msgpcode.Nil:
		return nil, dec.DecodeNil()
	case code == msgpcode.True || code == msgpcode.False:
		return dec.DecodeBool()
	case code == msgpcode.Float:
		return dec.DecodeFloat32()
	case code == msgpcode.Double:
		return dec.DecodeFloat64()
	case msgpcode.IsString(code):
		return dec.DecodeString()
	case msgpcode.IsFixedArray(code) || code == msgpcode.Array16 || code == msgpcode.Array32:
		n, err := dec.DecodeArrayLen()
		if err != nil {
			return nil, err
		}
		out := make([]any, n)
		for i := range out {
			if out[i], err = decodeValue(dec); err != nil {
				return nil, err
			}
		}
		return out, nil
	case msgpcode.IsFixedMap(code) || code == msgpcode.Map16 || code == msgpcode.Map32:
		n, err := dec.DecodeMapLen()
		if err != nil {
			return nil, err
		}
		out := make(map[any]any, n)
		for i := 0; i < n; i++ {
			key, err := decodeValue(dec)
			if err != nil {
				return nil, err
			}
			if out[key], err = decodeValue(dec); err != nil {
				return nil, err
			}
		}
		return out, nil
	default:
		return dec.DecodeInt64()
	}
}

func int32Ptr(v int32) *int32 { return &v }

func sampleFrame() HandReplayFrame {
	return HandReplayFrame{
		Delay: 16.5,
		Cards: map[string]CardState{
			cardA: {Hovered: true, Pressed: false, Selected: true, Dragged: false, Order: 2, DragX: 1.25, DragY: -3.5},
		},
	}
}

func sampleCountdown() Countdown {
	return Countdown{Kind: CountdownRankedPlayStage, ID: 3, TimeRemaining: Ticks(45 * time.Second), Stage: StageCardPlay}
}

func TestEncodingMatchesTheBytesTheClientWrites(t *testing.T) {
	state := NewRankedPlayRoomState()
	state.Stage = StageCardPlay
	state.CurrentRound = 3
	state.DamageMultiplier = 1.5
	state.ActiveUserID = int32Ptr(1001)
	state.StarRating = 4.5

	state.Users[1001] = NewRankedPlayUserInfo(1500)
	state.Users[1001].Life = 900_000
	state.Users[1001].Hand = []CardItem{{ID: cardA}, {ID: cardB}}
	state.Users[1001].RatingAfter = 1510
	state.Users[1001].DamageInfo = &DamageInfo{Damage: 1, RawDamage: 2, OldLife: 3, NewLife: 4, DirectDamage: 5, Multiplier: 0.5, BonusDamage: 6}
	state.Users[1001].RoundsWon = 1
	state.Users[1002] = NewRankedPlayUserInfo(1400)

	cases := map[string]any{
		"card_item": CardItem{ID: cardA},
		"damage_info": DamageInfo{
			Damage: 120_000, RawDamage: 100_000, OldLife: 1_000_000, NewLife: 880_000, DirectDamage: 50_000, Multiplier: 1.5, BonusDamage: 50_000,
		},
		"room_state_union":              state.Union(),
		"countdown_union":               sampleCountdown(),
		"countdown_started_event_union": CountdownStartedEvent(sampleCountdown()),
		"countdown_stopped_event_union": CountdownStoppedEvent(3),
		"hand_replay_frame":             sampleFrame(),
		"hand_replay_event_union":       HandReplayEvent(5, []HandReplayFrame{sampleFrame()}),
		"timespan_45s":                  Ticks(45 * time.Second),
		"guid":                          cardA,
		"enum_stage_ended":              StageEnded,
	}

	golden := vectors(t)
	for name := range golden {
		if _, covered := cases[name]; !covered && name != "hand_replay_request_union" {
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

func TestDecodeHandReplayRequest(t *testing.T) {
	key, request, err := DecodeMatchUserRequest(vectors(t)["hand_replay_request_union"])
	if err != nil {
		t.Fatal(err)
	}
	if key != MatchRequestCardHandReplay {
		t.Fatalf("key = %d, want %d", key, MatchRequestCardHandReplay)
	}

	replay, ok := request.(*HandReplayRequest)
	if !ok || len(replay.Frames) != 1 {
		t.Fatalf("request = %#v", request)
	}
	if !reflect.DeepEqual(replay.Frames[0], sampleFrame()) {
		t.Errorf("frame = %#v, want %#v", replay.Frames[0], sampleFrame())
	}
}

func TestOtherMatchRequestsAreReportedButNotDecoded(t *testing.T) {
	raw := pack(t, Union{Key: MatchRequestAvatarAction, Value: []any{int64(1)}})

	key, request, err := DecodeMatchUserRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	if key != MatchRequestAvatarAction || request != nil {
		t.Errorf("key, request = %d, %#v", key, request)
	}
}

func TestChangeTeamRequestIsDecoded(t *testing.T) {
	_, request, err := DecodeMatchUserRequest(pack(t, Union{Key: MatchRequestChangeTeam, Value: []any{int32(1)}}))
	if err != nil {
		t.Fatal(err)
	}
	if change, ok := request.(*ChangeTeamRequest); !ok || change.TeamID != 1 {
		t.Errorf("request = %#v, want team 1", request)
	}
}

func TestCountdownRequestsAreDecoded(t *testing.T) {
	_, request, err := DecodeMatchUserRequest(pack(t, Union{Key: MatchRequestStartCountdown, Value: []any{int64(5_000_000_000)}}))
	if err != nil {
		t.Fatal(err)
	}
	if start, ok := request.(*StartCountdownRequest); !ok || start.Duration != 500*time.Second {
		t.Errorf("start request = %#v, want 500s", request)
	}

	_, request, err = DecodeMatchUserRequest(pack(t, Union{Key: MatchRequestStopCountdown, Value: []any{int32(9)}}))
	if err != nil {
		t.Fatal(err)
	}
	if stop, ok := request.(*StopCountdownRequest); !ok || stop.ID != 9 {
		t.Errorf("stop request = %#v, want id 9", request)
	}
}

func TestDecodeMatchUserRequestRejectsGarbage(t *testing.T) {
	for name, data := range map[string][]byte{
		"empty":     {},
		"not array": pack(t, "hello"),
		"one item":  pack(t, []any{4}),
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := DecodeMatchUserRequest(data); err == nil {
				t.Error("expected an error")
			}
		})
	}
}

func TestNewCardItemIsAUniqueGUID(t *testing.T) {
	pattern := regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		id := NewCardItem().ID
		if !pattern.MatchString(id) {
			t.Fatalf("%q is not a version 4 GUID", id)
		}
		if seen[id] {
			t.Fatalf("duplicate id %q", id)
		}
		seen[id] = true
	}
}

// The client reads these as non-nullable collections, so a null would crash it.
func TestCollectionsAreNeverEncodedAsNull(t *testing.T) {
	user := decodeLoose(t, pack(t, RankedPlayUserInfo{})).([]any)
	if hand, ok := user[2].([]any); !ok || len(hand) != 0 {
		t.Errorf("hand = %#v, want an empty array", user[2])
	}

	state := decodeLoose(t, pack(t, RankedPlayRoomState{})).([]any)
	if users, ok := state[3].(map[any]any); !ok || len(users) != 0 {
		t.Errorf("users = %#v, want an empty map", state[3])
	}

	frame := decodeLoose(t, pack(t, HandReplayFrame{})).([]any)
	if cards, ok := frame[1].(map[any]any); !ok || len(cards) != 0 {
		t.Errorf("cards = %#v, want an empty map", frame[1])
	}

	event := decodeLoose(t, pack(t, HandReplayEvent(1, nil))).([]any)
	if frames, ok := event[1].([]any)[1].([]any); !ok || len(frames) != 0 {
		t.Errorf("frames = %#v, want an empty array", event[1])
	}
}

func TestNewUserInfoUsesTheClientsDefaults(t *testing.T) {
	user := NewRankedPlayUserInfo(1500)
	if user.Life != 1_000_000 || user.DamageMultiplier != 0.5 || user.RatingAfter != 1500 || user.Hand == nil {
		t.Errorf("%+v", user)
	}
	if NewRankedPlayRoomState().DamageMultiplier != 0.5 {
		t.Error("room state damage multiplier must default to 0.5")
	}
}

func TestCloneSharesNothingWithTheOriginal(t *testing.T) {
	state := NewRankedPlayRoomState()
	state.ActiveUserID = int32Ptr(1)
	state.WinningUserID = int32Ptr(2)
	state.Users[1] = NewRankedPlayUserInfo(1500)
	state.Users[1].Hand = []CardItem{{ID: cardA}}
	state.Users[1].DamageInfo = &DamageInfo{Damage: 5}

	clone := state.Clone()

	state.Users[1].Life = 1
	state.Users[1].Hand[0].ID = "changed"
	state.Users[1].DamageInfo.Damage = 99
	*state.ActiveUserID = 9
	*state.WinningUserID = 9

	user := clone.Users[1]
	if user.Life != StartingLife || user.Hand[0].ID != cardA || user.DamageInfo.Damage != 5 || *clone.ActiveUserID != 1 || *clone.WinningUserID != 2 {
		t.Errorf("clone was changed through the original: %+v", clone)
	}
}
