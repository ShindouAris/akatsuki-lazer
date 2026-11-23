package protocol

// Ranked play wire types. Like the rest of this package they encode as MessagePack arrays in the client's
// `[Key(n)]` order, and a polymorphic value (the client's `[Union]` types) encodes as `[unionKey, payload]`.
// testdata/rankedplay_vectors.tsv holds the bytes the client itself produces for each of them; the tests
// check this package against those.

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"time"

	"github.com/vmihailenco/msgpack/v5"
)

// RankedPlayStage mirrors RankedPlayStage.
type RankedPlayStage int

const (
	StageWaitForJoin RankedPlayStage = iota
	StageRoundWarmup
	StageCardDiscard
	StageFinishCardDiscard
	StageCardPlay
	StageFinishCardPlay
	StageGameplayWarmup
	StageGameplay
	StageResults
	StageEnded
)

var stageNames = [...]string{
	"WaitForJoin", "RoundWarmup", "CardDiscard", "FinishCardDiscard", "CardPlay",
	"FinishCardPlay", "GameplayWarmup", "Gameplay", "Results", "Ended",
}

// String returns the .NET enum name.
func (s RankedPlayStage) String() string {
	if s >= 0 && int(s) < len(stageNames) {
		return stageNames[s]
	}
	return "Unknown"
}

// Union keys of the client's polymorphic types.
const (
	MatchRoomStateTeamVersus  = 0
	MatchRoomStateMatchmaking = 1
	MatchRoomStateRankedPlay  = 2
	MatchRoomStateStandard    = 3

	MatchEventCountdownStarted = 0
	MatchEventCountdownStopped = 1
	MatchEventAvatarAction     = 2
	MatchEventCardHandReplay   = 3
	MatchEventRoll             = 4
	MatchRequestChangeTeam     = 0
	MatchRequestStartCountdown = 1
	MatchRequestStopCountdown  = 2
	MatchRequestAvatarAction   = 3
	MatchRequestCardHandReplay = 4
	MatchRequestSetLockState   = 5
	MatchRequestRoll           = 6
	MatchRequestChangeSlot     = 7
)

// Union is a value of one of the client's polymorphic types: `[Key, Value]`.
type Union struct {
	Key   int
	Value any
}

// EncodeMsgpack implements msgpack.CustomEncoder.
func (u Union) EncodeMsgpack(enc *msgpack.Encoder) error {
	if err := enc.EncodeArrayLen(2); err != nil {
		return err
	}
	if err := enc.EncodeInt(int64(u.Key)); err != nil {
		return err
	}
	return enc.Encode(u.Value)
}

// EncodeUnion encodes a union value ready to be used as one of the Raw fields of Room.
func EncodeUnion(key int, value any) (msgpack.RawMessage, error) {
	return msgpack.Marshal(Union{Key: key, Value: value})
}

// CardItem mirrors RankedPlayCardItem. The client writes its Guid as a 36 character string.
type CardItem struct {
	_msgpack struct{} `msgpack:",as_array"`
	ID       string
}

// DecodeMsgpack implements msgpack.CustomDecoder; it tolerates arrays longer than the type, like the client does.
func (c *CardItem) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &c.ID)
}

// NewCardItem creates a card with a random id.
func NewCardItem() CardItem {
	return CardItem{ID: NewGUID()}
}

// NewGUID returns a random (version 4) GUID in the "D" format the client uses.
func NewGUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // the system random source is unusable; nothing sensible can continue
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// DamageInfo mirrors RankedPlayDamageInfo.
type DamageInfo struct {
	_msgpack     struct{} `msgpack:",as_array"`
	Damage       int32
	RawDamage    int32
	OldLife      int32
	NewLife      int32
	DirectDamage int32
	Multiplier   float64
	BonusDamage  int32
}

// Defaults of the client's RankedPlayUserInfo / RankedPlayRoomState.
const (
	StartingLife            = 1_000_000
	DefaultDamageMultiplier = 0.5
)

// RankedPlayUserInfo mirrors RankedPlayUserInfo.
type RankedPlayUserInfo struct {
	_msgpack         struct{} `msgpack:",as_array"`
	Rating           int32
	Life             int32
	Hand             []CardItem
	RatingAfter      int32
	DamageInfo       *DamageInfo
	RoundsWon        int32
	DamageMultiplier float64
}

// NewRankedPlayUserInfo returns a player with the client's defaults.
func NewRankedPlayUserInfo(rating int32) *RankedPlayUserInfo {
	return &RankedPlayUserInfo{
		Rating:           rating,
		Life:             StartingLife,
		Hand:             []CardItem{},
		RatingAfter:      rating,
		DamageMultiplier: DefaultDamageMultiplier,
	}
}

// EncodeMsgpack implements msgpack.CustomEncoder. The hand is never null for the client.
func (u RankedPlayUserInfo) EncodeMsgpack(enc *msgpack.Encoder) error {
	hand := u.Hand
	if hand == nil {
		hand = []CardItem{}
	}
	return enc.Encode([]any{u.Rating, u.Life, hand, u.RatingAfter, u.DamageInfo, u.RoundsWon, u.DamageMultiplier})
}

// RankedPlayRoomState mirrors RankedPlayRoomState.
type RankedPlayRoomState struct {
	_msgpack         struct{} `msgpack:",as_array"`
	Stage            RankedPlayStage
	CurrentRound     int32
	DamageMultiplier float64
	Users            map[int32]*RankedPlayUserInfo
	ActiveUserID     *int32
	StarRating       float64
	WinningUserID    *int32
}

// NewRankedPlayRoomState returns a state with the client's defaults.
func NewRankedPlayRoomState() *RankedPlayRoomState {
	return &RankedPlayRoomState{
		DamageMultiplier: DefaultDamageMultiplier,
		Users:            map[int32]*RankedPlayUserInfo{},
	}
}

// EncodeMsgpack implements msgpack.CustomEncoder. The user map is never null for the client.
func (s RankedPlayRoomState) EncodeMsgpack(enc *msgpack.Encoder) error {
	users := s.Users
	if users == nil {
		users = map[int32]*RankedPlayUserInfo{}
	}
	return enc.Encode([]any{s.Stage, s.CurrentRound, s.DamageMultiplier, users, s.ActiveUserID, s.StarRating, s.WinningUserID})
}

// Clone returns a copy that shares nothing with the original, so it can be kept while the match goes on.
func (s *RankedPlayRoomState) Clone() RankedPlayRoomState {
	clone := *s
	clone.Users = make(map[int32]*RankedPlayUserInfo, len(s.Users))
	for id, user := range s.Users {
		copied := *user
		copied.Hand = append([]CardItem{}, user.Hand...)
		if user.DamageInfo != nil {
			damage := *user.DamageInfo
			copied.DamageInfo = &damage
		}
		clone.Users[id] = &copied
	}
	if s.ActiveUserID != nil {
		active := *s.ActiveUserID
		clone.ActiveUserID = &active
	}
	if s.WinningUserID != nil {
		winner := *s.WinningUserID
		clone.WinningUserID = &winner
	}
	return clone
}

// Union returns the state as the client's MatchRoomState.
func (s *RankedPlayRoomState) Union() Union {
	return Union{Key: MatchRoomStateRankedPlay, Value: s}
}

// CountdownKind is the union key of a MultiplayerCountdown.
type CountdownKind int

const (
	CountdownMatchStart         CountdownKind = 0
	CountdownForceGameplayStart CountdownKind = 1
	CountdownServerShuttingDown CountdownKind = 2
	CountdownMatchmakingStage   CountdownKind = 3
	CountdownRankedPlayStage    CountdownKind = 4
)

// Ticks converts a duration to .NET TimeSpan ticks (100 ns).
func Ticks(d time.Duration) int64 { return int64(d / 100) }

// Countdown is a MultiplayerCountdown: `[ID, TimeRemaining]`, plus the stage for ranked play stage countdowns.
type Countdown struct {
	Kind          CountdownKind
	ID            int32
	TimeRemaining int64 // .NET TimeSpan ticks
	Stage         RankedPlayStage
}

// EncodeMsgpack implements msgpack.CustomEncoder.
func (c Countdown) EncodeMsgpack(enc *msgpack.Encoder) error {
	fields := []any{c.ID, c.TimeRemaining}
	if c.Kind == CountdownRankedPlayStage || c.Kind == CountdownMatchmakingStage {
		fields = append(fields, c.Stage)
	}
	return enc.Encode(Union{Key: int(c.Kind), Value: fields})
}

// CountdownStartedEvent is the MatchServerEvent announcing a countdown.
func CountdownStartedEvent(c Countdown) Union {
	return Union{Key: MatchEventCountdownStarted, Value: []any{c}}
}

// CountdownStoppedEvent is the MatchServerEvent announcing that a countdown ended or was cancelled.
func CountdownStoppedEvent(id int32) Union {
	return Union{Key: MatchEventCountdownStopped, Value: []any{id}}
}

// CardState mirrors RankedPlayCardState.
type CardState struct {
	_msgpack struct{} `msgpack:",as_array"`
	Hovered  bool
	Pressed  bool
	Selected bool
	Dragged  bool
	Order    int32
	DragX    float32
	DragY    float32
}

// HandReplayFrame mirrors RankedPlayCardHandReplayFrame; the cards are keyed by card id.
type HandReplayFrame struct {
	_msgpack struct{} `msgpack:",as_array"`
	Delay    float64
	Cards    map[string]CardState
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (c *CardState) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &c.Hovered, &c.Pressed, &c.Selected, &c.Dragged, &c.Order, &c.DragX, &c.DragY)
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (f *HandReplayFrame) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &f.Delay, &f.Cards)
}

// EncodeMsgpack implements msgpack.CustomEncoder. The card map is never null for the client.
func (f HandReplayFrame) EncodeMsgpack(enc *msgpack.Encoder) error {
	cards := f.Cards
	if cards == nil {
		cards = map[string]CardState{}
	}
	return enc.Encode([]any{f.Delay, cards})
}

// HandReplayEvent is the MatchServerEvent relaying a player's hand movements to the room.
func HandReplayEvent(userID int32, frames []HandReplayFrame) Union {
	if frames == nil {
		frames = []HandReplayFrame{}
	}
	return Union{Key: MatchEventCardHandReplay, Value: []any{userID, frames}}
}

// StartCountdownRequest is the decoded StartMatchCountdownRequest: the host asks for a match start timer.
type StartCountdownRequest struct {
	Duration time.Duration
}

// ChangeTeamRequest is the decoded ChangeTeamRequest of a team versus room.
type ChangeTeamRequest struct {
	TeamID int32
}

// StopCountdownRequest is the decoded StopCountdownRequest.
type StopCountdownRequest struct {
	ID int32
}

// HandReplayRequest is the decoded RankedPlayCardHandReplayRequest.
type HandReplayRequest struct {
	Frames []HandReplayFrame
}

// DecodeMatchUserRequest decodes the MatchUserRequest union the client sends with SendMatchRequest. request is
// a *HandReplayRequest for hand replays and nil for every other (unsupported here) kind of request.
func DecodeMatchUserRequest(raw msgpack.RawMessage) (key int, request any, err error) {
	dec := msgpack.NewDecoder(bytes.NewReader(raw))
	n, err := dec.DecodeArrayLen()
	if err != nil {
		return 0, nil, err
	}
	if n != 2 {
		return 0, nil, fmt.Errorf("a union must be an array of 2 elements, got %d", n)
	}
	if key, err = dec.DecodeInt(); err != nil {
		return 0, nil, err
	}
	switch key {
	case MatchRequestChangeTeam:
		var team int32
		if err := decodeArray(dec, &team); err != nil {
			return key, nil, err
		}
		return key, &ChangeTeamRequest{TeamID: team}, nil
	case MatchRequestStartCountdown:
		var ticks int64
		if err := decodeArray(dec, &ticks); err != nil {
			return key, nil, err
		}
		return key, &StartCountdownRequest{Duration: time.Duration(ticks) * 100}, nil
	case MatchRequestStopCountdown:
		var id int32
		if err := decodeArray(dec, &id); err != nil {
			return key, nil, err
		}
		return key, &StopCountdownRequest{ID: id}, nil
	case MatchRequestCardHandReplay:
		// decoded below
	default:
		return key, nil, nil
	}

	var frames []HandReplayFrame
	if err := decodeArray(dec, &frames); err != nil {
		return key, nil, err
	}
	return key, &HandReplayRequest{Frames: frames}, nil
}
