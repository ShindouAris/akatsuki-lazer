// Package metadata is the metadata hub: user presence, friends' presence, multiplayer room score watching,
// the daily challenge and beatmap-update notifications.
//
// It replaces the Python /metadata hub. Presence lives in memory (it only matters while connections are open);
// friends, playlist statistics and the daily challenge come from the Python service's internal API, and the
// beatmap update queue is read from the Redis keys the Python service writes.
package metadata

import (
	"context"
	"log/slog"
	"net/http"
	"sync"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/signalr"
)

// Protocol UserStatus values (osu.Game.Users.UserStatus).
const (
	statusOffline = 0
	statusDND     = 1
	statusOnline  = 2
)

// Backend is what the hub needs from the Python service.
type Backend interface {
	Friends(ctx context.Context, userID int32) ([]int32, error)
	// RoomStats returns one msgpack-ready array per playlist item.
	RoomStats(ctx context.Context, roomID int32) ([]any, error)
	// DailyChallenge returns the active daily challenge room, or ok=false when there is none.
	DailyChallenge(ctx context.Context) (roomID int32, ok bool, err error)
}

// Updates is the queue of beatmap set updates.
type Updates interface {
	Since(ctx context.Context, queueID int64) (setIDs []int32, latest int64, err error)
}

type presence struct {
	activity msgpack.RawMessage // as sent by the client
	status   int
}

// payload is what spectators of presence receive: offline users are nil, like lazer.
func (p presence) payload() any {
	if p.status != statusOnline && p.status != statusDND {
		return nil
	}
	var activity any
	if len(p.activity) > 0 {
		activity = p.activity
	}
	return []any{activity, p.status}
}

type conn struct {
	session          *signalr.Session
	userID           int32
	watchingPresence bool
	friends          map[int32]struct{}
	rooms            map[int32]struct{}
}

// Hub implements signalr.Handler.
type Hub struct {
	verifier *auth.Verifier
	backend  Backend
	updates  Updates

	mu              sync.RWMutex
	conns           map[*signalr.Session]*conn
	byUser          map[int32]map[*conn]struct{}
	presence        map[int32]presence
	presenceWatcher map[*conn]struct{}
	friendWatchers  map[int32]map[*conn]struct{} // friend -> connections that have them as a friend
	roomWatchers    map[int32]map[*conn]struct{}
}

// New builds a hub.
func New(verifier *auth.Verifier, backend Backend, updates Updates) *Hub {
	return &Hub{
		verifier:        verifier,
		backend:         backend,
		updates:         updates,
		conns:           map[*signalr.Session]*conn{},
		byUser:          map[int32]map[*conn]struct{}{},
		presence:        map[int32]presence{},
		presenceWatcher: map[*conn]struct{}{},
		friendWatchers:  map[int32]map[*conn]struct{}{},
		roomWatchers:    map[int32]map[*conn]struct{}{},
	}
}

// Authenticate implements signalr.Handler.
func (h *Hub) Authenticate(r *http.Request) (int32, bool) { return h.verifier.Authenticate(r) }

// OnlineUsers is the number of users with at least one connection.
func (h *Hub) OnlineUsers() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.byUser)
}

// OnConnect implements signalr.Handler.
func (h *Hub) OnConnect(s *signalr.Session) {
	c := &conn{session: s, userID: s.UserID, friends: map[int32]struct{}{}, rooms: map[int32]struct{}{}}
	p := presence{status: statusOnline}

	h.mu.Lock()
	h.conns[s] = c
	if h.byUser[c.userID] == nil {
		h.byUser[c.userID] = map[*conn]struct{}{}
	}
	h.byUser[c.userID][c] = struct{}{}
	h.presence[c.userID] = p
	h.mu.Unlock()

	go func() {
		ctx := context.Background()
		h.sendDailyChallenge(ctx, c)
		h.refreshFriends(ctx, c)
		h.broadcastPresence(c.userID, p)
	}()
}

// OnDisconnect implements signalr.Handler.
func (h *Hub) OnDisconnect(s *signalr.Session) {
	h.mu.Lock()
	c := h.conns[s]
	if c == nil {
		h.mu.Unlock()
		return
	}
	delete(h.conns, s)
	delete(h.presenceWatcher, c)
	for friend := range c.friends {
		h.dropFriendWatcherLocked(friend, c)
	}
	for room := range c.rooms {
		h.dropRoomWatcherLocked(room, c)
	}
	delete(h.byUser[c.userID], c)
	last := len(h.byUser[c.userID]) == 0
	if last {
		delete(h.byUser, c.userID)
		delete(h.presence, c.userID)
	}
	h.mu.Unlock()

	if last {
		h.broadcastPresence(c.userID, presence{status: statusOffline})
	}
}

// Invoke implements signalr.Handler.
func (h *Hub) Invoke(ctx context.Context, s *signalr.Session, method string, args []msgpack.RawMessage) (any, bool, error) {
	h.mu.RLock()
	c := h.conns[s]
	h.mu.RUnlock()
	if c == nil {
		return nil, false, nil
	}

	switch method {
	case "BeginWatchingUserPresence":
		h.beginWatchingPresence(c)
	case "EndWatchingUserPresence":
		h.mu.Lock()
		c.watchingPresence = false
		delete(h.presenceWatcher, c)
		h.mu.Unlock()
	case "UpdateActivity":
		var activity msgpack.RawMessage
		if len(args) > 0 {
			activity = args[0]
		}
		h.updatePresence(c, func(p *presence) { p.activity = activity })
	case "UpdateStatus":
		status := statusOnline
		if len(args) > 0 {
			var value *int
			if err := msgpack.Unmarshal(args[0], &value); err == nil && value != nil && *value >= statusOffline && *value <= statusOnline {
				status = *value
			}
		}
		h.updatePresence(c, func(p *presence) { p.status = status })
	case "RefreshFriends":
		h.refreshFriends(ctx, c)
	case "BeginWatchingMultiplayerRoom":
		return h.beginWatchingRoom(ctx, c, args), true, nil
	case "EndWatchingMultiplayerRoom":
		if room := intArg(args, 0); room > 0 {
			h.mu.Lock()
			delete(c.rooms, room)
			h.dropRoomWatcherLocked(room, c)
			h.mu.Unlock()
		}
	case "GetChangesSince":
		return h.changesSince(ctx, intArg64(args, 0)), true, nil
	case "StartWatchingUser", "EndWatchingUser":
		slog.Warn("metadata: spectator method sent to the metadata hub, ignored", "method", method, "user", c.userID)
	default:
		slog.Warn("metadata: unknown method", "method", method, "user", c.userID)
	}
	return nil, false, nil
}

func intArg64(args []msgpack.RawMessage, index int) int64 {
	if index >= len(args) {
		return 0
	}
	var value *int64
	if err := msgpack.Unmarshal(args[index], &value); err != nil || value == nil {
		return 0
	}
	return *value
}

func intArg(args []msgpack.RawMessage, index int) int32 { return int32(intArg64(args, index)) }

func (h *Hub) beginWatchingPresence(c *conn) {
	h.mu.Lock()
	c.watchingPresence = true
	h.presenceWatcher[c] = struct{}{}
	type entry struct {
		user    int32
		payload any
	}
	var online []entry
	for user, p := range h.presence {
		if user == c.userID {
			continue
		}
		if payload := p.payload(); payload != nil {
			online = append(online, entry{user, payload})
		}
	}
	h.mu.Unlock()

	for _, e := range online {
		c.session.Invoke("UserPresenceUpdated", e.user, e.payload)
	}
}

func (h *Hub) updatePresence(c *conn, change func(*presence)) {
	h.mu.Lock()
	p := h.presence[c.userID]
	change(&p)
	h.presence[c.userID] = p
	h.mu.Unlock()

	h.broadcastPresence(c.userID, p)
	c.session.Invoke("UserPresenceUpdated", c.userID, p.payload())
}

// broadcastPresence tells everyone watching presence, and everyone with this user as a friend.
func (h *Hub) broadcastPresence(user int32, p presence) {
	payload := p.payload()
	general, err := signalr.EncodeInvocation("UserPresenceUpdated", user, payload)
	if err != nil {
		slog.Error("metadata: encoding presence failed", "error", err)
		return
	}
	friend, err := signalr.EncodeInvocation("FriendPresenceUpdated", user, payload)
	if err != nil {
		slog.Error("metadata: encoding presence failed", "error", err)
		return
	}

	h.mu.RLock()
	defer h.mu.RUnlock()
	for c := range h.presenceWatcher {
		c.session.SendEncoded(general)
	}
	for c := range h.friendWatchers[user] {
		c.session.SendEncoded(friend)
	}
}

func (h *Hub) refreshFriends(ctx context.Context, c *conn) {
	ids, err := h.backend.Friends(ctx, c.userID)
	if err != nil {
		slog.Warn("metadata: cannot load friends", "user", c.userID, "error", err)
		return
	}
	latest := make(map[int32]struct{}, len(ids))
	for _, id := range ids {
		if id != c.userID {
			latest[id] = struct{}{}
		}
	}

	type known struct {
		user    int32
		payload any
	}
	var added []known

	h.mu.Lock()
	for friend := range c.friends {
		if _, still := latest[friend]; !still {
			h.dropFriendWatcherLocked(friend, c)
		}
	}
	for friend := range latest {
		if _, had := c.friends[friend]; had {
			continue
		}
		if h.friendWatchers[friend] == nil {
			h.friendWatchers[friend] = map[*conn]struct{}{}
		}
		h.friendWatchers[friend][c] = struct{}{}
		if payload := h.presence[friend].payload(); payload != nil {
			if _, online := h.presence[friend]; online {
				added = append(added, known{friend, payload})
			}
		}
	}
	c.friends = latest
	h.mu.Unlock()

	for _, f := range added {
		c.session.Invoke("FriendPresenceUpdated", f.user, f.payload)
	}
}

func (h *Hub) dropFriendWatcherLocked(friend int32, c *conn) {
	delete(h.friendWatchers[friend], c)
	if len(h.friendWatchers[friend]) == 0 {
		delete(h.friendWatchers, friend)
	}
}

func (h *Hub) dropRoomWatcherLocked(room int32, c *conn) {
	delete(h.roomWatchers[room], c)
	if len(h.roomWatchers[room]) == 0 {
		delete(h.roomWatchers, room)
	}
}

func (h *Hub) sendDailyChallenge(ctx context.Context, c *conn) {
	room, ok, err := h.backend.DailyChallenge(ctx)
	if err != nil {
		slog.Warn("metadata: cannot load the daily challenge", "error", err)
		return
	}
	if ok {
		c.session.Invoke("DailyChallengeUpdated", []any{room})
	}
}

func (h *Hub) beginWatchingRoom(ctx context.Context, c *conn, args []msgpack.RawMessage) []any {
	room := intArg(args, 0)
	if room <= 0 {
		return []any{}
	}
	h.mu.Lock()
	c.rooms[room] = struct{}{}
	if h.roomWatchers[room] == nil {
		h.roomWatchers[room] = map[*conn]struct{}{}
	}
	h.roomWatchers[room][c] = struct{}{}
	h.mu.Unlock()

	stats, err := h.backend.RoomStats(ctx, room)
	if err != nil {
		slog.Warn("metadata: cannot load room stats", "room", room, "error", err)
		return []any{}
	}
	return stats
}

func (h *Hub) changesSince(ctx context.Context, queueID int64) []any {
	ids, latest, err := h.updates.Since(ctx, queueID)
	if err != nil {
		slog.Warn("metadata: cannot read beatmap updates", "error", err)
		return []any{[]int32{}, queueID}
	}
	if ids == nil {
		ids = []int32{}
	}
	return []any{ids, latest}
}

// NotifyRoomScoreSet tells the watchers of a room that a score was set. It returns how many were told.
func (h *Hub) NotifyRoomScoreSet(room int32, event []any) int {
	return h.sendTo(func() map[*conn]struct{} { return h.roomWatchers[room] }, "MultiplayerRoomScoreSet", event)
}

// NotifyBeatmapUpdates tells every client to re-fetch metadata of the given beatmap sets.
func (h *Hub) NotifyBeatmapUpdates(setIDs []int32, queueID int64) int {
	if setIDs == nil {
		setIDs = []int32{}
	}
	return h.sendTo(func() map[*conn]struct{} {
		all := make(map[*conn]struct{}, len(h.conns))
		for _, c := range h.conns {
			all[c] = struct{}{}
		}
		return all
	}, "BeatmapSetsUpdated", []any{setIDs, queueID})
}

func (h *Hub) sendTo(recipients func() map[*conn]struct{}, method string, arg any) int {
	data, err := signalr.EncodeInvocation(method, arg)
	if err != nil {
		slog.Error("metadata: encoding a notification failed", "method", method, "error", err)
		return 0
	}
	h.mu.RLock()
	defer h.mu.RUnlock()
	count := 0
	for c := range recipients() {
		c.session.SendEncoded(data)
		count++
	}
	return count
}
