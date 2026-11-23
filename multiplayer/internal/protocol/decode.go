package protocol

import (
	"sort"

	"github.com/vmihailenco/msgpack/v5"
	"github.com/vmihailenco/msgpack/v5/msgpcode"
)

// The client serialises with MessagePack-CSharp, which tolerates arrays that are longer (a newer client with
// extra keys) or shorter (older data) than the type it reads into. The msgpack library is strict about that,
// so every type that arrives from a client decodes through decodeArray instead.

// decodeArray reads an array into fields in order. Extra elements are skipped; missing ones keep their
// current (zero) value. A nil array leaves everything untouched.
func decodeArray(dec *msgpack.Decoder, fields ...any) error {
	n, err := dec.DecodeArrayLen()
	if err != nil || n < 0 {
		return err
	}
	for i := 0; i < n; i++ {
		if i >= len(fields) {
			if err := dec.Skip(); err != nil {
				return err
			}
			continue
		}
		if err := dec.Decode(fields[i]); err != nil {
			return err
		}
	}
	return nil
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (m *APIMod) DecodeMsgpack(dec *msgpack.Decoder) error {
	var settings modSettings
	if err := decodeArray(dec, &m.Acronym, &settings); err != nil {
		return err
	}
	m.Settings = settings
	return nil
}

// modSettings is a mod's settings on the wire. The client's ModSettingsDictionaryFormatter writes an array
// header holding the number of settings, followed by that many key/value pairs (two elements each) - not a
// msgpack map - so a map-typed field cannot be read or written directly.
type modSettings map[string]any

// EncodeMsgpack implements msgpack.CustomEncoder. Keys are written in sorted order for stable output.
func (s modSettings) EncodeMsgpack(enc *msgpack.Encoder) error {
	if err := enc.EncodeArrayLen(len(s)); err != nil {
		return err
	}
	keys := make([]string, 0, len(s))
	for key := range s {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if err := enc.EncodeString(key); err != nil {
			return err
		}
		if err := enc.Encode(s[key]); err != nil {
			return err
		}
	}
	return nil
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (s *modSettings) DecodeMsgpack(dec *msgpack.Decoder) error {
	// Other implementations (the Python server's data) write a plain msgpack map; accept it too.
	if code, err := dec.PeekCode(); err == nil && (msgpcode.IsFixedMap(code) || code == msgpcode.Map16 || code == msgpcode.Map32) {
		var plain map[string]any
		if err := dec.Decode(&plain); err != nil {
			return err
		}
		*s = plain
		return nil
	}

	n, err := dec.DecodeArrayLen()
	if err != nil {
		return err
	}
	out := make(modSettings, max(n, 0))
	for i := 0; i < n; i++ {
		key, err := dec.DecodeString()
		if err != nil {
			return err
		}
		value, err := dec.DecodeInterface()
		if err != nil {
			return err
		}
		out[key] = value
	}
	*s = out
	return nil
}

// EncodeMsgpack implements msgpack.CustomEncoder.
func (m APIMod) EncodeMsgpack(enc *msgpack.Encoder) error {
	if err := enc.EncodeArrayLen(2); err != nil {
		return err
	}
	if err := enc.EncodeString(m.Acronym); err != nil {
		return err
	}
	return modSettings(m.Settings).EncodeMsgpack(enc) // not enc.Encode: a nil map would be written as nil
}

// DecodeMsgpack implements msgpack.CustomDecoder. The progress is read as float64 because the client writes
// float32 but other implementations (the Python server) write float64; both are valid for the client.
func (a *BeatmapAvailability) DecodeMsgpack(dec *msgpack.Decoder) error {
	var progress *float64
	if err := decodeArray(dec, &a.State, &progress); err != nil {
		return err
	}
	a.DownloadProgress = nil
	if progress != nil {
		value := float32(*progress)
		a.DownloadProgress = &value
	}
	return nil
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (s *Settings) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &s.Name, &s.PlaylistItemID, &s.Password, &s.MatchType, &s.QueueMode,
		&s.AutoStartDuration, &s.AutoSkip, &s.MaxParticipants)
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (p *PlaylistItem) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &p.ID, &p.OwnerID, &p.BeatmapID, &p.BeatmapChecksum, &p.RulesetID, &p.RequiredMods,
		&p.AllowedMods, &p.Expired, &p.PlaylistOrder, &p.PlayedAt, &p.StarRating, &p.Freestyle)
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (u *RoomUser) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &u.UserID, &u.State, &u.BeatmapAvailability, &u.Mods, &u.MatchState, &u.RulesetID,
		&u.BeatmapID, &u.VotedToSkipIntro, &u.Role)
}

// DecodeMsgpack implements msgpack.CustomDecoder.
func (r *Room) DecodeMsgpack(dec *msgpack.Decoder) error {
	return decodeArray(dec, &r.RoomID, &r.State, &r.Settings, &r.Users, &r.Host, &r.MatchState, &r.Playlist,
		&r.ActiveCountdowns, &r.ChannelID)
}
