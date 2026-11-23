// Package protocol defines the multiplayer wire types shared with the osu! lazer client.
//
// Every struct is encoded as a MessagePack *array* whose element order is the client's `[Key(n)]` order
// (see osu.Game/Online/Multiplayer and osu.Game/Online/Rooms). Field order here is therefore part of the
// contract: do not reorder fields. Slices that the client treats as non-null must never be nil when
// encoded; use the New* helpers or Snapshot methods which guarantee that.
package protocol

import "github.com/vmihailenco/msgpack/v5"

// RoomState mirrors MultiplayerRoomState.
type RoomState int

const (
	RoomOpen RoomState = iota
	RoomWaitingForLoad
	RoomPlaying
	RoomClosed
)

// UserState mirrors MultiplayerUserState.
type UserState int

const (
	UserIdle UserState = iota
	UserReady
	UserWaitingForLoad
	UserLoaded
	UserReadyForGameplay
	UserPlaying
	UserFinishedPlay
	UserResults
	UserSpectating
)

// IsGameplay reports whether the state belongs to an in-progress gameplay session.
func (s UserState) IsGameplay() bool {
	switch s {
	case UserWaitingForLoad, UserLoaded, UserReadyForGameplay, UserPlaying:
		return true
	}
	return false
}

// MatchType mirrors MatchType.
type MatchType int

const (
	MatchPlaylists MatchType = iota
	MatchHeadToHead
	MatchTeamVersus
	MatchMatchmaking
	MatchRankedPlay
)

// QueueMode mirrors QueueMode.
type QueueMode int

const (
	QueueHostOnly QueueMode = iota
	QueueAllPlayers
	QueueAllPlayersRoundRobin
)

// UserRole mirrors MultiplayerRoomUserRole.
type UserRole int

const (
	RolePlayer UserRole = iota
	RoleReferee
)

// DownloadState mirrors DownloadState.
type DownloadState int

const (
	DownloadUnknown DownloadState = iota
	DownloadNotDownloaded
	DownloadDownloading
	DownloadImporting
	DownloadLocallyAvailable
)

// GameplayAbortReason mirrors GameplayAbortReason.
type GameplayAbortReason int

const (
	AbortLoadTookTooLong GameplayAbortReason = iota
	AbortHostAbortedTheMatch
)

// APIMod is a mod with its settings.
type APIMod struct {
	_msgpack struct{} `msgpack:",as_array"`

	Acronym  string
	Settings map[string]any
}

// BeatmapAvailability is a player's local availability of the current beatmap.
type BeatmapAvailability struct {
	_msgpack struct{} `msgpack:",as_array"`

	State            DownloadState
	DownloadProgress *float32
}

// Settings is MultiplayerRoomSettings.
type Settings struct {
	_msgpack struct{} `msgpack:",as_array"`

	Name              string
	PlaylistItemID    int64
	Password          string
	MatchType         MatchType
	QueueMode         QueueMode
	AutoStartDuration int64 // .NET TimeSpan ticks (100ns); zero disables auto start
	AutoSkip          bool
	MaxParticipants   *uint8
}

// PlaylistItem is MultiplayerPlaylistItem.
type PlaylistItem struct {
	_msgpack struct{} `msgpack:",as_array"`

	ID              int64
	OwnerID         int32
	BeatmapID       int32
	BeatmapChecksum string
	RulesetID       int32
	RequiredMods    []APIMod
	AllowedMods     []APIMod
	Expired         bool
	PlaylistOrder   uint16
	PlayedAt        *msgpack.RawMessage // DateTimeOffset, passed through untouched
	StarRating      float64
	Freestyle       bool
}

// RoomUser is MultiplayerRoomUser.
type RoomUser struct {
	_msgpack struct{} `msgpack:",as_array"`

	UserID              int32
	State               UserState
	BeatmapAvailability BeatmapAvailability
	Mods                []APIMod
	MatchState          *msgpack.RawMessage // MatchUserState union, passed through untouched
	RulesetID           *int32
	BeatmapID           *int32
	VotedToSkipIntro    bool
	Role                UserRole
}

// Room is MultiplayerRoom.
type Room struct {
	_msgpack struct{} `msgpack:",as_array"`

	RoomID           int64
	State            RoomState
	Settings         Settings
	Users            []RoomUser
	Host             *RoomUser
	MatchState       *msgpack.RawMessage // MatchRoomState union, passed through untouched
	Playlist         []PlaylistItem
	ActiveCountdowns []msgpack.RawMessage // MultiplayerCountdown unions
	ChannelID        int32
}

// DefaultSettings returns the settings of a freshly constructed MultiplayerRoomSettings.
func DefaultSettings() Settings {
	return Settings{Name: "Unnamed room", MatchType: MatchHeadToHead, QueueMode: QueueHostOnly}
}

// NewRoomUser returns a user with the client's defaults (all slices non-nil).
func NewRoomUser(userID int32) RoomUser {
	return RoomUser{
		UserID:              userID,
		State:               UserIdle,
		BeatmapAvailability: BeatmapAvailability{State: DownloadUnknown},
		Mods:                []APIMod{},
	}
}

// Normalise replaces nil slices/maps with empty ones so the value encodes the way the client expects.
func (r *Room) Normalise() {
	if r.Users == nil {
		r.Users = []RoomUser{}
	}
	for i := range r.Users {
		r.Users[i].Normalise()
	}
	if r.Host != nil {
		r.Host.Normalise()
	}
	if r.Playlist == nil {
		r.Playlist = []PlaylistItem{}
	}
	for i := range r.Playlist {
		r.Playlist[i].Normalise()
	}
	if r.ActiveCountdowns == nil {
		r.ActiveCountdowns = []msgpack.RawMessage{}
	}
}

// Normalise makes the user safe to encode.
func (u *RoomUser) Normalise() {
	if u.Mods == nil {
		u.Mods = []APIMod{}
	}
	for i := range u.Mods {
		u.Mods[i].Normalise()
	}
}

// Normalise makes the item safe to encode.
func (p *PlaylistItem) Normalise() {
	if p.RequiredMods == nil {
		p.RequiredMods = []APIMod{}
	}
	if p.AllowedMods == nil {
		p.AllowedMods = []APIMod{}
	}
	for i := range p.RequiredMods {
		p.RequiredMods[i].Normalise()
	}
	for i := range p.AllowedMods {
		p.AllowedMods[i].Normalise()
	}
}

// Normalise makes the mod safe to encode.
func (m *APIMod) Normalise() {
	if m.Settings == nil {
		m.Settings = map[string]any{}
	}
}

// Equal reports whether two availabilities describe the same state.
func (a BeatmapAvailability) Equal(b BeatmapAvailability) bool {
	if a.State != b.State {
		return false
	}
	if (a.DownloadProgress == nil) != (b.DownloadProgress == nil) {
		return false
	}
	return a.DownloadProgress == nil || *a.DownloadProgress == *b.DownloadProgress
}

// Equal reports whether two settings are equal (like MultiplayerRoomSettings.Equals).
func (s Settings) Equal(o Settings) bool {
	if s.Name != o.Name || s.PlaylistItemID != o.PlaylistItemID || s.Password != o.Password ||
		s.MatchType != o.MatchType || s.QueueMode != o.QueueMode || s.AutoStartDuration != o.AutoStartDuration ||
		s.AutoSkip != o.AutoSkip {
		return false
	}
	if (s.MaxParticipants == nil) != (o.MaxParticipants == nil) {
		return false
	}
	return s.MaxParticipants == nil || *s.MaxParticipants == *o.MaxParticipants
}

var userStateNames = [...]string{
	"Idle", "Ready", "WaitingForLoad", "Loaded", "ReadyForGameplay", "Playing", "FinishedPlay", "Results", "Spectating",
}

// String returns the .NET enum name, which the client shows in error messages.
func (s UserState) String() string {
	if s >= 0 && int(s) < len(userStateNames) {
		return userStateNames[s]
	}
	return "Unknown"
}
