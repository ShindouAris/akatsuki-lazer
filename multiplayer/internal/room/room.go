// Package room is the multiplayer room state machine, ported from osu-server-spectator's
// ServerMultiplayerRoom / StandardMatchController. Everything a client can observe (callback names, their
// order and arguments, error texts) follows that server, because the osu! client is written against it.
//
// A Room is safe for concurrent use: every exported method takes the room lock. Client callbacks are
// emitted while the lock is held, which keeps their order identical to the order of state changes.
package room

import (
	"context"
	"log/slog"
	"math"
	"reflect"
	"sort"
	"sync"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
)

// Limits on queued playlist items per user, as in the reference server.
const (
	HostPlaylistLimit  = 50
	GuestPlaylistLimit = 3
)

// DefaultLoadTimeout is how long players get to load gameplay before the match starts without them.
const DefaultLoadTimeout = 30 * time.Second

// Events delivers client callbacks. Implementations must not block.
type Events interface {
	Send(userID int32, target string, args ...any)
}

// Beatmap is what a room needs to know about a beatmap to validate playlist items.
type Beatmap struct {
	Checksum   string
	Mode       int32
	StarRating float64
}

// BeatmapLookup resolves beatmaps. It returns (nil, nil) for a beatmap that does not exist.
type BeatmapLookup interface {
	Lookup(ctx context.Context, beatmapID int32) (*Beatmap, error)
}

// Options tunes a room. The zero value is valid.
type Options struct {
	LoadTimeout time.Duration
	// Beatmaps validates playlist items. When nil the client's items are trusted as sent.
	Beatmaps BeatmapLookup
	// Store persists rooms and hands out database ids. When nil an in-memory store is used.
	Store Store
	// StoreTimeout bounds the blocking Store calls (creating rooms, adding items). Default 5s.
	StoreTimeout time.Duration
	// Clock drives countdowns. Default: the real clock.
	Clock Clock
}

// DefaultStoreTimeout bounds blocking Store calls.
const DefaultStoreTimeout = 5 * time.Second

// Room is one realtime multiplayer room.
type Room struct {
	mu    sync.Mutex
	id    int64
	ev    Events
	opts  Options
	store Store

	state     protocol.RoomState
	settings  protocol.Settings
	queueMode protocol.QueueMode
	users     []*protocol.RoomUser
	hostID    int32
	playlist  []*protocol.PlaylistItem
	playedSeq map[int64]int // order in which items were played, newest highest
	played    int
	playedAt  map[int64]time.Time
	channelID int32
	banned    map[int32]struct{}
	subs      []int32

	loadTimer *time.Timer
	closed    bool

	// controller drives the match of rooms that are not run by their host (ranked play); nil otherwise.
	controller      MatchController
	clock           Clock
	countdowns      []*countdown
	nextCountdownID int32
	countdownSeq    int // counts the countdowns ever started

	// What the store was last given; persist() only sends differences.
	savedState *RoomState
	savedItems map[int64]ItemRecord
}

func newRoom(id int64, ev Events, opts Options, settings protocol.Settings) *Room {
	if opts.LoadTimeout <= 0 {
		opts.LoadTimeout = DefaultLoadTimeout
	}
	if opts.StoreTimeout <= 0 {
		opts.StoreTimeout = DefaultStoreTimeout
	}
	if opts.Store == nil {
		opts.Store = NewMemoryStore()
	}
	if opts.Clock == nil {
		opts.Clock = realClock{}
	}
	return &Room{
		id:         id,
		ev:         ev,
		opts:       opts,
		store:      opts.Store,
		clock:      opts.Clock,
		settings:   settings,
		queueMode:  settings.QueueMode,
		playedSeq:  map[int64]int{},
		playedAt:   map[int64]time.Time{},
		banned:     map[int32]struct{}{},
		channelID:  int32(id),
		savedItems: map[int64]ItemRecord{},
	}
}

// lock takes the room lock; unlock releases it after handing any state change to the store, so every
// mutating method persists exactly what it changed without having to remember to.
func (r *Room) lock() { r.mu.Lock() }

func (r *Room) unlock() {
	r.persist()
	r.mu.Unlock()
}

// stateRecord is the persisted view of the room.
func (r *Room) stateRecord() RoomState {
	status := StatusIdle
	switch {
	case r.closed:
		status = StatusClosed
	case r.state == protocol.RoomWaitingForLoad || r.state == protocol.RoomPlaying:
		status = StatusPlaying
	}

	maxParticipants := 0
	if r.settings.MaxParticipants != nil {
		maxParticipants = int(*r.settings.MaxParticipants)
	}
	return RoomState{
		HostID:           r.hostID,
		Name:             r.settings.Name,
		Password:         r.settings.Password,
		MatchType:        r.settings.MatchType,
		QueueMode:        r.settings.QueueMode,
		Status:           status,
		ParticipantCount: len(r.users),
		Participants:     r.participantIDs(),
		MaxParticipants:  maxParticipants,
		AutoSkip:         r.settings.AutoSkip,
		AutoStartSeconds: int(r.settings.AutoStartDuration / 10_000_000),
		CurrentItemID:    r.settings.PlaylistItemID,
	}
}

// participantIDs lists the users in the room in ascending order; never nil.
func (r *Room) participantIDs() []int32 {
	ids := make([]int32, 0, len(r.users))
	for _, u := range r.users {
		ids = append(ids, u.UserID)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

func (r *Room) itemRecord(p *protocol.PlaylistItem) ItemRecord {
	record := ItemRecord{Item: *p}
	if at, ok := r.playedAt[p.ID]; ok {
		record.PlayedAt = &at
	}
	return record
}

// persist sends the store everything that changed since the last call. It never blocks.
func (r *Room) persist() {
	if state := r.stateRecord(); r.savedState == nil || !reflect.DeepEqual(*r.savedState, state) {
		r.store.SaveRoom(r.id, state)
		r.savedState = &state
	}

	live := make(map[int64]struct{}, len(r.playlist))
	for _, p := range r.playlist {
		live[p.ID] = struct{}{}
		record := r.itemRecord(p)
		if saved, ok := r.savedItems[p.ID]; ok && reflect.DeepEqual(saved, record) {
			continue
		}
		r.store.SaveItem(r.id, record)
		r.savedItems[p.ID] = record
	}
	for id := range r.savedItems {
		if _, ok := live[id]; !ok {
			r.store.DeleteItem(r.id, id)
			delete(r.savedItems, id)
		}
	}
}

func (r *Room) storeContext(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, r.opts.StoreTimeout)
}

// ID returns the room id.
func (r *Room) ID() int64 { return r.id }

// Snapshot returns a deep copy of the room as the client sees it.
func (r *Room) Snapshot() protocol.Room {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.snapshot()
}

// ---------------------------------------------------------------------------------------------------
// Joining and leaving

// Join adds a user. asHost makes them the host (used when the room is created).
func (r *Room) Join(userID int32, password string, asHost bool) (protocol.Room, error) {
	r.lock()
	defer r.unlock()

	if r.closed {
		return protocol.Room{}, invalidState("Failed to join the room, please try again.")
	}
	if _, banned := r.banned[userID]; banned {
		return protocol.Room{}, invalidState("Not eligible to join this room.")
	}
	if r.controller != nil && !r.controller.userCanJoin(userID) {
		return protocol.Room{}, invalidState("Not eligible to join this room.")
	}
	if r.settings.Password != "" && r.settings.Password != password {
		return protocol.Room{}, ErrInvalidPassword
	}
	if r.user(userID) != nil {
		return protocol.Room{}, invalidState("User %d attempted to join room %d they are already present in.", userID, r.id)
	}
	if r.settings.MaxParticipants != nil && r.playerCount() >= int(*r.settings.MaxParticipants) {
		return protocol.Room{}, invalidState("Not eligible to join this room.")
	}

	user := protocol.NewRoomUser(userID)
	if asHost {
		r.hostID = userID
	}

	r.teamsForNewUser(&user)

	// Existing members learn about the newcomer before they are added; the newcomer gets the snapshot.
	r.broadcast("UserJoined", user)
	r.users = append(r.users, &user)
	r.subs = append(r.subs, userID)

	// The match controller reacts before the snapshot is taken, so the newcomer's snapshot already shows it.
	if r.controller != nil {
		r.controller.userJoined(&user)
	}

	return r.snapshot(), nil
}

// Leave removes a user. removedBy is the acting user: equal to userID for leaving, otherwise a kick.
// empty reports that the room has no users left and must be discarded.
func (r *Room) Leave(userID, removedBy int32) (empty bool, err error) {
	r.lock()
	defer r.unlock()

	index := r.userIndex(userID)
	if index < 0 {
		return false, invalidState("User is not in the room.")
	}
	user := *r.users[index]

	r.unsubscribe(userID)
	r.users = append(r.users[:index], r.users[index+1:]...)

	r.checkVotesToSkipPassed()
	r.updateRoomStateIfRequired()

	// Runs even when the room just became empty: abandoning a match still has to be recorded.
	if r.controller != nil {
		r.controller.userLeft(user)
	}

	if len(r.users) == 0 {
		r.closed = true
		r.stopLoadTimer()
		return true, nil
	}

	// If the host left, hand the room to somebody so it can continue to exist.
	if r.hostID == userID {
		r.setHost(r.users[0].UserID)
	}

	if removedBy != userID {
		r.ev.Send(userID, "UserKicked", user)
		r.broadcast("UserKicked", user)
	} else {
		r.broadcast("UserLeft", user)
	}
	return false, nil
}

// Kick removes target on behalf of by (host or referee).
func (r *Room) Kick(by, target int32) (empty bool, err error) {
	r.mu.Lock()
	if target == by {
		r.mu.Unlock()
		return false, invalidState("Can't kick self")
	}
	if err := r.ensureHostOrReferee(by); err != nil {
		r.mu.Unlock()
		return false, err
	}
	victim := r.user(target)
	if victim == nil {
		r.mu.Unlock()
		return false, invalidState("Target user is not in the current room")
	}
	if victim.Role == protocol.RoleReferee {
		r.mu.Unlock()
		return false, invalidState("Can't kick a referee.")
	}
	r.mu.Unlock()

	return r.Leave(target, by)
}

// TransferHost gives the host role to target.
func (r *Room) TransferHost(by, target int32) error {
	r.lock()
	defer r.unlock()

	if err := r.ensureHostOrReferee(by); err != nil {
		return err
	}
	if r.user(target) == nil {
		return invalidState("User is not in the room.")
	}
	r.setHost(target)
	return nil
}

// Close stops background work. Called when the room is discarded.
func (r *Room) Close() {
	r.lock()
	defer r.unlock()
	r.closed = true
	r.stopLoadTimer()
	r.stopAllCountdowns()
}

// ---------------------------------------------------------------------------------------------------
// User state

// ChangeState applies a client requested state change.
func (r *Room) ChangeState(userID int32, newState protocol.UserState) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if user.State == newState {
		return nil
	}

	// A client may change state while a message from the server is still in transit. Block those silently.
	switch newState {
	case protocol.UserIdle:
		if user.State.IsGameplay() {
			return nil
		}
	case protocol.UserLoaded, protocol.UserReadyForGameplay:
		if !user.State.IsGameplay() {
			return nil
		}
	}

	if err := r.ensureValidStateSwitch(user.Role, user.State, newState); err != nil {
		return err
	}
	r.changeAndBroadcastUserState(user, newState)

	// Newly spectating users are asked to load gameplay if a match is in progress.
	if newState == protocol.UserSpectating &&
		(r.state == protocol.RoomWaitingForLoad || r.state == protocol.RoomPlaying) {
		r.ev.Send(userID, "LoadRequested")
	}

	r.updateRoomStateIfRequired()
	return nil
}

func (r *Room) ensureValidStateSwitch(role protocol.UserRole, old, next protocol.UserState) error {
	invalidChange := &Error{Type: "InvalidStateChangeException", Message: "Cannot change from " + old.String() + " to " + next.String()}

	switch next {
	case protocol.UserIdle:
		if old.IsGameplay() {
			return invalidState("Cannot return to idle without aborting gameplay.")
		}
	case protocol.UserReady:
		if old != protocol.UserIdle {
			return invalidChange
		}
		if r.currentItem().Expired {
			return invalidState("Cannot ready up while all items have been played.")
		}
	case protocol.UserWaitingForLoad, protocol.UserPlaying, protocol.UserResults:
		return invalidChange // managed by the server
	case protocol.UserLoaded:
		if old != protocol.UserWaitingForLoad {
			return invalidChange
		}
	case protocol.UserReadyForGameplay:
		if old != protocol.UserLoaded {
			return invalidChange
		}
	case protocol.UserFinishedPlay:
		if old != protocol.UserPlaying {
			return invalidChange
		}
	case protocol.UserSpectating:
		if old != protocol.UserIdle && old != protocol.UserReady {
			return invalidChange
		}
	default:
		return invalidChange
	}

	if role == protocol.RoleReferee && next != protocol.UserIdle && next != protocol.UserSpectating {
		return invalidState("Referees cannot participate in the match.")
	}
	return nil
}

func (r *Room) changeAndBroadcastUserState(user *protocol.RoomUser, state protocol.UserState) {
	user.State = state
	r.broadcast("UserStateChanged", user.UserID, state)

	if r.controller != nil {
		r.controller.userStateChanged(user)
	}
}

// ChangeBeatmapAvailability records whether the user has the current beatmap.
func (r *Room) ChangeBeatmapAvailability(userID int32, availability protocol.BeatmapAvailability) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	r.changeAndBroadcastAvailability(user, availability)
	return nil
}

func (r *Room) changeAndBroadcastAvailability(user *protocol.RoomUser, availability protocol.BeatmapAvailability) {
	if user.BeatmapAvailability.Equal(availability) {
		return
	}
	user.BeatmapAvailability = availability
	r.broadcast("UserBeatmapAvailabilityChanged", user.UserID, availability)

	if r.controller != nil {
		r.controller.userStateChanged(user)
	}
}

// ChangeUserMods sets the mods a user will play with.
func (r *Room) ChangeUserMods(userID int32, mods []protocol.APIMod) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if user.Role == protocol.RoleReferee {
		return invalidState("Referees do not participate in the match and cannot set their own mods.")
	}

	if invalid := r.currentItem().invalidUserMods(mods); len(invalid) > 0 {
		return invalidState("Incompatible mods were selected: %s", joinComma(invalid))
	}
	if sameMods(user.Mods, mods) {
		return nil
	}

	user.Mods = append([]protocol.APIMod{}, mods...)
	for i := range user.Mods {
		user.Mods[i].Normalise()
	}
	r.broadcast("UserModsChanged", user.UserID, user.Mods)
	return nil
}

// ChangeUserStyle is accepted only when it clears the style; per-user beatmap styles (freestyle) are not
// implemented yet.
func (r *Room) ChangeUserStyle(userID int32, beatmapID, rulesetID *int32) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if beatmapID != nil || rulesetID != nil {
		return invalidState("Freestyle is not supported yet.")
	}
	return nil
}

// ---------------------------------------------------------------------------------------------------
// Settings

// ChangeSettings replaces the room settings (host or referee only).
func (r *Room) ChangeSettings(by int32, next protocol.Settings) error {
	r.lock()
	defer r.unlock()

	if err := r.ensureHostOrReferee(by); err != nil {
		return err
	}
	if r.state != protocol.RoomOpen {
		return invalidState("Attempted to change settings while a game is in progress.")
	}

	// The server is authoritative over the playlist item id.
	next.PlaylistItemID = r.settings.PlaylistItemID
	if r.settings.Equal(next) {
		return nil
	}
	if err := validateMatchType(next.MatchType); err != nil {
		return err
	}

	previousType := r.settings.MatchType
	r.settings = next

	r.handleQueueModeChanged()
	r.handleSettingsChanged(false)
	r.matchTypeChanged(previousType)
	r.updateRoomStateIfRequired()
	return nil
}

func validateMatchType(t protocol.MatchType) error {
	if t != protocol.MatchHeadToHead && t != protocol.MatchTeamVersus {
		return invalidState("This match type is not supported yet.")
	}
	return nil
}

func (r *Room) handleSettingsChanged(playlistItemChanged bool) {
	r.unreadyAll(playlistItemChanged)
	r.broadcast("SettingsChanged", r.settings)
}

func (r *Room) unreadyAll(resetAvailability bool) {
	for _, u := range r.users {
		if u.State == protocol.UserReady {
			r.changeAndBroadcastUserState(u, protocol.UserIdle)
		}
	}
	if resetAvailability {
		for _, u := range r.users {
			r.changeAndBroadcastAvailability(u, protocol.BeatmapAvailability{State: protocol.DownloadUnknown})
		}
	}
}

func (r *Room) setHost(userID int32) {
	r.hostID = userID
	r.broadcast("HostChanged", userID)
}

// ---------------------------------------------------------------------------------------------------
// Playing matches

// StartMatch starts loading gameplay for all ready users (host or referee only).
func (r *Room) StartMatch(by int32) error {
	r.lock()
	defer r.unlock()

	if err := r.ensureHostOrReferee(by); err != nil {
		return err
	}
	if host := r.user(r.hostID); host != nil &&
		host.State != protocol.UserSpectating && host.State != protocol.UserReady && host.Role != protocol.RoleReferee {
		return invalidState("Can't start match when the host is not ready.")
	}
	if !r.anyUser(func(u *protocol.RoomUser) bool { return u.State == protocol.UserReady }) {
		return invalidState("Can't start match when no users are ready.")
	}
	return r.startMatch()
}

func (r *Room) startMatch() error {
	if r.state != protocol.RoomOpen {
		return invalidState("Can't start match when already in a running state.")
	}
	if r.currentItem().Expired {
		return invalidState("Cannot start an expired playlist item.")
	}

	r.stopCountdownsOfKind(protocol.CountdownMatchStart)

	// This is the first time users get a gameplay state; reset the per gameplay session properties.
	for _, u := range r.users {
		r.setVotedToSkipIntro(u, false)
	}

	var ready []*protocol.RoomUser
	for _, u := range r.users {
		if isReadyForGameplay(u) {
			ready = append(ready, u)
		}
	}
	for _, u := range ready {
		r.changeAndBroadcastUserState(u, protocol.UserWaitingForLoad)
	}

	r.changeRoomState(protocol.RoomWaitingForLoad)
	r.broadcast("LoadRequested")

	r.stopLoadTimer()
	r.loadTimer = time.AfterFunc(r.opts.LoadTimeout, r.onLoadTimeout)
	return nil
}

func (r *Room) onLoadTimeout() {
	r.lock()
	defer r.unlock()
	if !r.closed && r.state == protocol.RoomWaitingForLoad {
		r.startOrStopGameplay()
	}
}

func (r *Room) startOrStopGameplay() {
	r.stopLoadTimer()

	anyPlaying := false
	for _, u := range r.users {
		switch {
		case canStartGameplay(u):
			r.changeAndBroadcastUserState(u, protocol.UserPlaying)
			r.ev.Send(u.UserID, "GameplayStarted")
			anyPlaying = true
		case u.State == protocol.UserWaitingForLoad:
			r.changeAndBroadcastUserState(u, protocol.UserIdle)
			r.ev.Send(u.UserID, "GameplayAborted", protocol.AbortLoadTookTooLong)
		}
	}

	if anyPlaying {
		r.changeRoomState(protocol.RoomPlaying)
		return
	}
	r.changeRoomState(protocol.RoomOpen)
	r.handleGameplayCompleted()
}

func (r *Room) changeRoomState(state protocol.RoomState) {
	r.state = state
	r.broadcast("RoomStateChanged", state)
}

func (r *Room) updateRoomStateIfRequired() {
	switch r.state {
	case protocol.RoomWaitingForLoad:
		gameplay, ready := 0, 0
		for _, u := range r.users {
			if u.State.IsGameplay() {
				gameplay++
			}
			if u.State == protocol.UserReadyForGameplay {
				ready++
			}
		}
		// Start once no user needs to change state any more. If all users aborted, this aborts the match.
		if ready == gameplay {
			r.startOrStopGameplay()
		}

	case protocol.RoomPlaying:
		if r.anyUser(func(u *protocol.RoomUser) bool { return u.State == protocol.UserPlaying }) {
			return
		}
		anyFinished := false
		for _, u := range r.users {
			if u.State == protocol.UserFinishedPlay {
				anyFinished = true
				r.changeAndBroadcastUserState(u, protocol.UserResults)
			}
		}
		r.changeRoomState(protocol.RoomOpen)
		if anyFinished {
			r.broadcast("ResultsReady")
		}
		r.handleGameplayCompleted()
	}

	r.updateAutoStart()
}

// AbortMatch ends the running match for everyone (host or referee only).
func (r *Room) AbortMatch(by int32) error {
	r.lock()
	defer r.unlock()

	if err := r.ensureHostOrReferee(by); err != nil {
		return err
	}
	if r.state != protocol.RoomWaitingForLoad && r.state != protocol.RoomPlaying {
		return invalidState("Cannot abort a match that hasn't started.")
	}
	for _, u := range r.users {
		r.changeAndBroadcastUserState(u, protocol.UserIdle)
	}
	r.broadcast("GameplayAborted", protocol.AbortHostAbortedTheMatch)
	r.updateRoomStateIfRequired()
	return nil
}

// AbortGameplay lets one user quit their own gameplay.
func (r *Room) AbortGameplay(userID int32) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if !user.State.IsGameplay() {
		return invalidState("Cannot abort gameplay while not in gameplay.")
	}
	r.changeAndBroadcastUserState(user, protocol.UserIdle)
	r.updateRoomStateIfRequired()
	return nil
}

// VoteToSkipIntro registers the user's vote; when a majority agree clients are told to skip.
func (r *Room) VoteToSkipIntro(userID int32) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if user.Role == protocol.RoleReferee {
		return invalidState("Referees do not participate in the match and cannot vote to skip.")
	}
	if !user.State.IsGameplay() {
		return invalidState("Cannot skip while not in gameplay.")
	}
	r.setVotedToSkipIntro(user, true)
	r.checkVotesToSkipPassed()
	return nil
}

func (r *Room) setVotedToSkipIntro(user *protocol.RoomUser, voted bool) {
	if user.VotedToSkipIntro == voted {
		return
	}
	user.VotedToSkipIntro = voted
	r.broadcast("UserVotedToSkipIntro", user.UserID, voted)
}

func (r *Room) checkVotesToSkipPassed() {
	voted, playing := 0, 0
	for _, u := range r.users {
		if u.State == protocol.UserPlaying {
			playing++
			if u.VotedToSkipIntro {
				voted++
			}
		}
	}
	if voted >= playing/2+1 {
		r.broadcast("VoteToSkipIntroPassed")
	}
}

func (r *Room) stopLoadTimer() {
	if r.loadTimer != nil {
		r.loadTimer.Stop()
		r.loadTimer = nil
	}
}

// ---------------------------------------------------------------------------------------------------
// Playlist

// CurrentItem returns a copy of the playlist item selected in the room settings.
func (r *Room) CurrentItem() protocol.PlaylistItem {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.currentItem().PlaylistItem
}

// item wraps a playlist item with helpers.
type item struct{ protocol.PlaylistItem }

func (r *Room) currentItem() item {
	for _, p := range r.playlist {
		if p.ID == r.settings.PlaylistItemID {
			return item{*p}
		}
	}
	return item{}
}

func (r *Room) upcomingItems() []*protocol.PlaylistItem {
	var items []*protocol.PlaylistItem
	for _, p := range r.playlist {
		if !p.Expired {
			items = append(items, p)
		}
	}
	sort.SliceStable(items, func(i, j int) bool { return items[i].PlaylistOrder < items[j].PlaylistOrder })
	return items
}

func (r *Room) isHostOrReferee(u *protocol.RoomUser) bool {
	return u.UserID == r.hostID || u.Role == protocol.RoleReferee
}

// AddPlaylistItem queues an item.
func (r *Room) AddPlaylistItem(ctx context.Context, userID int32, candidate protocol.PlaylistItem) error {
	if err := r.validateCandidate(ctx, &candidate); err != nil {
		return err
	}

	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}
	if r.settings.QueueMode == protocol.QueueHostOnly && !r.isHostOrReferee(user) {
		return ErrNotHost
	}

	limit := GuestPlaylistLimit
	if r.isHostOrReferee(user) {
		limit = HostPlaylistLimit
	}
	queued := 0
	for _, p := range r.playlist {
		if p.OwnerID == userID && !p.Expired {
			queued++
		}
	}
	if queued >= limit {
		return invalidState("Can't enqueue more than %d items at once.", limit)
	}

	candidate.OwnerID = userID

	storeCtx, cancel := r.storeContext(ctx)
	defer cancel()
	if err := r.addItem(storeCtx, candidate); err != nil {
		slog.Error("could not save a playlist item", "room", r.id, "user", userID, "error", err)
		return invalidState("Failed to save the playlist item, please try again.")
	}
	if r.state == protocol.RoomOpen {
		r.updateCurrentItem()
	}
	return nil
}

// EditPlaylistItem replaces a queued item.
func (r *Room) EditPlaylistItem(ctx context.Context, userID int32, candidate protocol.PlaylistItem) error {
	if err := r.validateCandidate(ctx, &candidate); err != nil {
		return err
	}

	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}

	var existing *protocol.PlaylistItem
	for _, p := range r.playlist {
		if p.ID == candidate.ID {
			existing = p
		}
	}
	if existing == nil {
		return invalidState("Attempted to change an item that doesn't exist.")
	}
	if existing.ID == r.settings.PlaylistItemID && r.state != protocol.RoomOpen {
		return invalidState("The current item in the room cannot be edited when currently being played.")
	}
	if existing.OwnerID != userID && !r.isHostOrReferee(user) {
		return invalidState("Attempted to change an item which is not owned by the user.")
	}
	if existing.Expired {
		return invalidState("Attempted to change an item which has already been played.")
	}

	candidate.OwnerID = userID
	candidate.PlaylistOrder = existing.PlaylistOrder // the order must not change
	candidate.Expired = false
	candidate.PlayedAt = nil
	beatmapChanged := existing.BeatmapChecksum != candidate.BeatmapChecksum
	*existing = candidate

	r.handlePlaylistItemChanged(existing, beatmapChanged)
	return nil
}

// RemovePlaylistItem removes a queued item.
func (r *Room) RemovePlaylistItem(userID int32, itemID int64) error {
	r.lock()
	defer r.unlock()

	user := r.user(userID)
	if user == nil {
		return invalidState("User is not in the room.")
	}

	index := -1
	for i, p := range r.playlist {
		if p.ID == itemID {
			index = i
		}
	}
	if index < 0 {
		return invalidState("Item does not exist in the room.")
	}
	target := r.playlist[index]

	if target.ID == r.settings.PlaylistItemID {
		if len(r.upcomingItems()) == 1 {
			return invalidState("The only item in the room cannot be removed.")
		}
		if r.state != protocol.RoomOpen {
			return invalidState("The current item in the room cannot be removed when currently being played.")
		}
	}
	if target.OwnerID != userID && !r.isHostOrReferee(user) {
		return invalidState("Attempted to remove an item which is not owned by the user.")
	}
	if target.Expired {
		return invalidState("Attempted to remove an item which has already been played.")
	}

	r.playlist = append(r.playlist[:index], r.playlist[index+1:]...)
	if r.state == protocol.RoomOpen {
		r.updatePlaylistOrder()
		r.updateCurrentItem()
	}
	// Clients are told after the settings changed, so PlaylistItemId always points at a valid item.
	r.broadcast("PlaylistItemRemoved", itemID)
	return nil
}

func (r *Room) validateCandidate(ctx context.Context, candidate *protocol.PlaylistItem) error {
	return validateItem(ctx, r.opts.Beatmaps, candidate)
}

// validateItem checks a client supplied playlist item and fills in the server authoritative fields.
func validateItem(ctx context.Context, beatmaps BeatmapLookup, candidate *protocol.PlaylistItem) error {
	candidate.Normalise()
	if candidate.Freestyle && len(candidate.AllowedMods) > 0 {
		return invalidState("Cannot enqueue freestyle item with mods.")
	}
	if candidate.RulesetID < 0 || candidate.RulesetID > 3 {
		return invalidState("Attempted to select an unsupported ruleset.")
	}
	if beatmaps == nil {
		return nil
	}

	beatmap, err := beatmaps.Lookup(ctx, candidate.BeatmapID)
	if err != nil {
		return err
	}
	if beatmap == nil {
		return invalidState("Attempted to add a beatmap which does not exist online.")
	}
	if beatmap.Checksum != candidate.BeatmapChecksum {
		return invalidState("Attempted to add a beatmap which has been modified.")
	}
	if beatmap.Mode != 0 && candidate.RulesetID != beatmap.Mode {
		return invalidState("Attempted to select an invalid beatmap and ruleset combination.")
	}
	candidate.StarRating = beatmap.StarRating
	return nil
}

// addItem persists a new item (which assigns its database id), appends it to the end of the queue and
// announces it. Nothing changes if the store fails.
func (r *Room) addItem(ctx context.Context, p protocol.PlaylistItem) error {
	p.PlaylistOrder = math.MaxUint16
	p.Expired = false
	p.PlayedAt = nil
	p.Normalise()

	id, err := r.store.AddItem(ctx, r.id, ItemRecord{Item: p})
	if err != nil {
		return err
	}
	p.ID = id

	stored := p
	r.playlist = append(r.playlist, &stored)
	r.savedItems[id] = ItemRecord{Item: stored} // the store already has exactly this state
	r.broadcast("PlaylistItemAdded", stored)

	if r.state == protocol.RoomOpen {
		r.updatePlaylistOrder()
	}
	return nil
}

// requeue duplicates an item for a following round. A failure is logged and leaves the queue as it was:
// the room then has no upcoming item until the host adds one, which is better than crashing the match flow.
func (r *Room) requeue(p protocol.PlaylistItem) {
	ctx, cancel := r.storeContext(context.Background())
	defer cancel()
	if err := r.addItem(ctx, p); err != nil {
		slog.Error("could not queue the next playlist item", "room", r.id, "error", err)
	}
}

func (r *Room) handlePlaylistItemChanged(p *protocol.PlaylistItem, beatmapChanged bool) {
	if p.ID == r.settings.PlaylistItemID {
		r.unreadyAll(beatmapChanged)
	}
	r.broadcast("PlaylistItemChanged", *p)
}

// handleQueueModeChanged mirrors StandardMatchController.HandleSettingsChanged.
func (r *Room) handleQueueModeChanged() {
	if r.queueMode == r.settings.QueueMode {
		return
	}
	r.queueMode = r.settings.QueueMode

	// In host-only mode at least one non-expired item must exist; duplicate the current one if needed.
	if r.settings.QueueMode == protocol.QueueHostOnly && r.allExpired() {
		r.requeue(r.currentItem().PlaylistItem)
	}
	if r.state == protocol.RoomOpen {
		r.updatePlaylistOrder()
		r.updateCurrentItem()
	}
}

func (r *Room) allExpired() bool {
	for _, p := range r.playlist {
		if !p.Expired {
			return false
		}
	}
	return true
}

// handleGameplayCompleted expires the played item and selects the next one.
func (r *Room) handleGameplayCompleted() {
	if r.controller != nil {
		r.controller.gameplayCompleted()
		return
	}

	for _, p := range r.playlist {
		if p.ID == r.settings.PlaylistItemID {
			p.Expired = true
			r.played++
			r.playedSeq[p.ID] = r.played
			r.playedAt[p.ID] = time.Now().UTC()
			r.handlePlaylistItemChanged(p, true)
			break
		}
	}
	r.updatePlaylistOrder()

	// In host-only mode, duplicate the item for the next round if no other non-expired item exists.
	if r.settings.QueueMode == protocol.QueueHostOnly && r.allExpired() {
		r.requeue(r.currentItem().PlaylistItem)
	}
	r.updateCurrentItem()
}

func (r *Room) updateCurrentItem() {
	var next *protocol.PlaylistItem
	if upcoming := r.upcomingItems(); len(upcoming) > 0 {
		next = upcoming[0]
	} else {
		// Everything was played: fall back to the most recently played item.
		for _, p := range r.playlist {
			if next == nil || r.playedSeq[p.ID] > r.playedSeq[next.ID] {
				next = p
			}
		}
	}
	if next == nil {
		return
	}

	last := r.settings.PlaylistItemID
	r.settings.PlaylistItemID = next.ID
	if next.ID != last {
		r.handleSettingsChanged(true)
	}
}

// updatePlaylistOrder renumbers the unplayed items according to the queue mode.
func (r *Room) updatePlaylistOrder() {
	if r.state != protocol.RoomOpen {
		return
	}

	var active []*protocol.PlaylistItem
	for _, p := range r.playlist {
		if !p.Expired {
			active = append(active, p)
		}
	}
	sort.SliceStable(active, func(i, j int) bool { return active[i].ID < active[j].ID })

	ordered := active
	if r.settings.QueueMode == protocol.QueueAllPlayersRoundRobin {
		ordered = roundRobin(active)
	}

	for i, p := range ordered {
		if int(p.PlaylistOrder) == i {
			continue
		}
		p.PlaylistOrder = uint16(i)
		r.handlePlaylistItemChanged(p, false)
	}
}

// roundRobin interleaves each owner's items: everyone's first item, then everyone's second, and so on.
// The owner order of the first round (existing order, ties by id) is kept for all later rounds.
func roundRobin(active []*protocol.PlaylistItem) []*protocol.PlaylistItem {
	var owners []int32
	byOwner := map[int32][]*protocol.PlaylistItem{}
	for _, p := range active {
		if _, seen := byOwner[p.OwnerID]; !seen {
			owners = append(owners, p.OwnerID)
		}
		byOwner[p.OwnerID] = append(byOwner[p.OwnerID], p)
	}

	var out []*protocol.PlaylistItem
	var firstOrder map[int32]int
	for round := 0; ; round++ {
		var set []*protocol.PlaylistItem
		for _, owner := range owners {
			if round < len(byOwner[owner]) {
				set = append(set, byOwner[owner][round])
			}
		}
		if len(set) == 0 {
			return out
		}

		if round == 0 {
			sort.SliceStable(set, func(i, j int) bool {
				if set[i].PlaylistOrder != set[j].PlaylistOrder {
					return set[i].PlaylistOrder < set[j].PlaylistOrder
				}
				return set[i].ID < set[j].ID
			})
			firstOrder = map[int32]int{}
			for i, p := range set {
				firstOrder[p.OwnerID] = i
			}
		} else {
			sort.SliceStable(set, func(i, j int) bool { return firstOrder[set[i].OwnerID] < firstOrder[set[j].OwnerID] })
		}
		out = append(out, set...)
	}
}

// ---------------------------------------------------------------------------------------------------
// Helpers

func (r *Room) snapshot() protocol.Room {
	snapshot := protocol.Room{
		RoomID:    r.id,
		State:     r.state,
		Settings:  r.settings,
		Users:     make([]protocol.RoomUser, 0, len(r.users)),
		Playlist:  make([]protocol.PlaylistItem, 0, len(r.playlist)),
		ChannelID: r.channelID,
	}
	for _, u := range r.users {
		snapshot.Users = append(snapshot.Users, *u)
	}
	if host := r.user(r.hostID); host != nil {
		copied := *host
		snapshot.Host = &copied
	}
	for _, p := range r.playlist {
		snapshot.Playlist = append(snapshot.Playlist, *p)
	}

	if r.controller != nil {
		state, err := msgpack.Marshal(r.controller.matchState())
		if err != nil {
			slog.Error("encoding the match state failed", "room", r.id, "error", err)
		} else {
			raw := msgpack.RawMessage(state)
			snapshot.MatchState = &raw
		}
	}
	if r.isTeamVersus() {
		snapshot.MatchState = rawUnion(teamRoomState())
	}
	snapshot.ActiveCountdowns = r.activeCountdowns()

	snapshot.Normalise()
	return snapshot
}

// addServerItem adds a playlist item on behalf of the server (ranked play): the item is persisted, appended to
// the playlist and announced, without the queue-mode reordering that items added by players go through.
func (r *Room) addServerItem(ctx context.Context, p protocol.PlaylistItem) (protocol.PlaylistItem, error) {
	p.OwnerID = 0
	p.Expired = false
	p.PlayedAt = nil
	p.Normalise()

	id, err := r.store.AddItem(ctx, r.id, ItemRecord{Item: p})
	if err != nil {
		return protocol.PlaylistItem{}, err
	}

	p.ID = id
	stored := p
	r.playlist = append(r.playlist, &stored)
	r.savedItems[id] = ItemRecord{Item: stored} // the store already has exactly this state
	r.broadcast("PlaylistItemAdded", stored)
	return stored, nil
}

// removePlaceholderItem drops the empty item a ranked play room starts with. It only ever existed in memory.
func (r *Room) removePlaceholderItem() {
	for i, p := range r.playlist {
		if p.ID == 0 {
			r.playlist = append(r.playlist[:i], r.playlist[i+1:]...)
			delete(r.savedItems, 0)
			r.broadcast("PlaylistItemRemoved", int64(0))
			return
		}
	}
}

func (r *Room) user(userID int32) *protocol.RoomUser {
	if i := r.userIndex(userID); i >= 0 {
		return r.users[i]
	}
	return nil
}

func (r *Room) userIndex(userID int32) int {
	for i, u := range r.users {
		if u.UserID == userID {
			return i
		}
	}
	return -1
}

func (r *Room) anyUser(match func(*protocol.RoomUser) bool) bool {
	for _, u := range r.users {
		if match(u) {
			return true
		}
	}
	return false
}

func (r *Room) playerCount() int {
	n := 0
	for _, u := range r.users {
		if u.Role == protocol.RolePlayer {
			n++
		}
	}
	return n
}

func (r *Room) ensureHostOrReferee(userID int32) error {
	user := r.user(userID)
	if user == nil {
		return ErrNotJoined
	}
	if !r.isHostOrReferee(user) {
		return ErrNotHost
	}
	return nil
}

func (r *Room) unsubscribe(userID int32) {
	for i, id := range r.subs {
		if id == userID {
			r.subs = append(r.subs[:i], r.subs[i+1:]...)
			return
		}
	}
}

// broadcast sends a callback to everyone currently subscribed to the room, in join order.
func (r *Room) broadcast(target string, args ...any) {
	for _, id := range r.subs {
		r.ev.Send(id, target, args...)
	}
}

func isReadyForGameplay(u *protocol.RoomUser) bool {
	return u.Role == protocol.RolePlayer &&
		u.BeatmapAvailability.State == protocol.DownloadLocallyAvailable &&
		(u.State == protocol.UserReady || u.State == protocol.UserIdle)
}

func canStartGameplay(u *protocol.RoomUser) bool {
	return u.State == protocol.UserLoaded || u.State == protocol.UserReadyForGameplay
}

// invalidUserMods returns the acronyms of mods a user may not select for this item.
func (i item) invalidUserMods(mods []protocol.APIMod) []string {
	if i.Freestyle {
		return nil
	}
	allowed := map[string]bool{}
	for _, m := range i.AllowedMods {
		allowed[m.Acronym] = true
	}
	required := map[string]bool{}
	for _, m := range i.RequiredMods {
		required[m.Acronym] = true
	}

	var invalid []string
	seen := map[string]bool{}
	for _, m := range mods {
		if seen[m.Acronym] || required[m.Acronym] || !allowed[m.Acronym] {
			invalid = append(invalid, m.Acronym)
		}
		seen[m.Acronym] = true
	}
	return invalid
}

func sameMods(a, b []protocol.APIMod) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Acronym != b[i].Acronym || len(a[i].Settings) != len(b[i].Settings) {
			return false
		}
		for k, v := range a[i].Settings {
			if w, ok := b[i].Settings[k]; !ok || w != v {
				return false
			}
		}
	}
	return true
}

func joinComma(values []string) string {
	out := ""
	for i, v := range values {
		if i > 0 {
			out += ","
		}
		out += v
	}
	return out
}
