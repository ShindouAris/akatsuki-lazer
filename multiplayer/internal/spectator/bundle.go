package spectator

import (
	"bytes"
	"errors"
	"fmt"
	"time"

	"github.com/vmihailenco/msgpack/v5"
)

// The messages of the spectator hub are MessagePack arrays whose index is the [Key(n)] of the C# class. The
// hub does not need to understand them, only to forward them, so they stay as raw bytes and only the few
// fields the server owns are rewritten.
//
//	SpectatorState: [beatmap_id, ruleset_id, mods, state, maximum_statistics]
//	FrameDataBundle: [header, frames]
//	FrameHeader: [total_score, accuracy, combo, max_combo, statistics, score_processor_statistics, received_time, mods]

// SpectatedUserState values (osu.Game.Online.Spectator.SpectatedUserState).
const (
	statePlaying = 1
	stateQuit    = 5
)

const (
	headerReceivedTime = 6
	headerLength       = 8 // the mods are the last key; older clients may leave it out
)

var errNotAnArray = errors.New("not a msgpack array")

// splitArray decodes an array into its raw elements. A nil element comes back as an empty slice.
func splitArray(raw msgpack.RawMessage) ([]msgpack.RawMessage, error) {
	var elements []msgpack.RawMessage
	if err := msgpack.Unmarshal(raw, &elements); err != nil {
		return nil, fmt.Errorf("%w: %v", errNotAnArray, err)
	}
	return elements, nil
}

// joinArray encodes raw elements back into an array. Empty elements are nil.
func joinArray(elements []msgpack.RawMessage) (msgpack.RawMessage, error) {
	var buf bytes.Buffer
	enc := msgpack.NewEncoder(&buf)
	if err := enc.EncodeArrayLen(len(elements)); err != nil {
		return nil, err
	}
	for _, element := range elements {
		if len(element) == 0 {
			if err := enc.EncodeNil(); err != nil {
				return nil, err
			}
			continue
		}
		if err := enc.Encode(element); err != nil {
			return nil, err
		}
	}
	return buf.Bytes(), nil
}

func encodeValue(v any) (msgpack.RawMessage, error) {
	return msgpack.Marshal(v)
}

// stampBundle sets the header's ReceivedTime to the server clock, as osu-server-spectator does. The client reads
// it as a DateTimeOffset: [msgpack timestamp (UTC), offset in minutes]. A string or a missing value would make
// the client drop the whole bundle, so spectators would never see a frame.
func stampBundle(bundle msgpack.RawMessage, now time.Time) (msgpack.RawMessage, error) {
	stamp, err := encodeValue([]any{now.UTC(), 0})
	if err != nil {
		return nil, err
	}
	return setReceivedTime(bundle, stamp)
}

// storableBundle is the bundle as kept for the replay: the receive time is left empty (a msgpack timestamp
// cannot live in the Python side's stored form, and it is set again when the frames are read back).
func storableBundle(bundle msgpack.RawMessage) (msgpack.RawMessage, error) {
	return setReceivedTime(bundle, nil)
}

func setReceivedTime(bundle msgpack.RawMessage, stamp msgpack.RawMessage) (msgpack.RawMessage, error) {
	parts, err := splitArray(bundle)
	if err != nil {
		return nil, err
	}
	if len(parts) < 2 {
		return nil, errors.New("frame bundle needs a header and frames")
	}

	header, err := splitArray(parts[0])
	if err != nil {
		return nil, fmt.Errorf("frame header: %w", err)
	}
	for len(header) < headerLength {
		header = append(header, nil)
	}
	header[headerReceivedTime] = stamp

	if parts[0], err = joinArray(header); err != nil {
		return nil, err
	}
	return joinArray(parts)
}

// stateFields reads the beatmap and ruleset a SpectatorState says the player is on. Either may be absent.
func stateFields(state msgpack.RawMessage) (beatmapID, rulesetID *int32, err error) {
	parts, err := splitArray(state)
	if err != nil {
		return nil, nil, err
	}
	read := func(index int) (*int32, error) {
		if len(parts) <= index || len(parts[index]) == 0 {
			return nil, nil
		}
		var value *int32
		if err := msgpack.Unmarshal(parts[index], &value); err != nil {
			return nil, err
		}
		return value, nil
	}
	if beatmapID, err = read(0); err != nil {
		return nil, nil, fmt.Errorf("beatmap id: %w", err)
	}
	if rulesetID, err = read(1); err != nil {
		return nil, nil, fmt.Errorf("ruleset id: %w", err)
	}
	return beatmapID, rulesetID, nil
}

// withState returns the SpectatorState with its State key (index 3) replaced.
func withState(state msgpack.RawMessage, value int) (msgpack.RawMessage, error) {
	parts, err := splitArray(state)
	if err != nil {
		return nil, err
	}
	for len(parts) < 4 {
		parts = append(parts, nil)
	}
	if parts[3], err = encodeValue(value); err != nil {
		return nil, err
	}
	return joinArray(parts)
}

// finishedState turns the state a play ended with into the one spectators are told: a play that was still
// "playing" at the end was abandoned.
func finishedState(state msgpack.RawMessage) (msgpack.RawMessage, error) {
	parts, err := splitArray(state)
	if err != nil {
		return nil, err
	}
	if len(parts) > 3 && len(parts[3]) > 0 {
		var current int
		if err := msgpack.Unmarshal(parts[3], &current); err == nil && current != statePlaying {
			return state, nil
		}
	}
	return withState(state, stateQuit)
}
