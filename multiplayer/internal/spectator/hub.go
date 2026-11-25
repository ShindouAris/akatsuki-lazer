// Package spectator is the live-gameplay hub: players stream their frames, spectators receive them.
//
// It replaces the Python /spectator hub. State that only matters while connections are open (who is playing,
// who watches whom) lives in memory; the Redis list of frames of a play is the one thing the Python service
// reads (to build the replay when the score is submitted), and the Python service decides whether a play may
// start at all (the score token) through its internal API.
package spectator

import (
	"context"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/signalr"
)

// Backend is what the hub needs from the Python service.
type Backend interface {
	// ValidatePlay says whether the player may start this play: the score token must be theirs, unused, and
	// match the beatmap and ruleset the state announces (either may be nil when the state leaves it out).
	ValidatePlay(ctx context.Context, userID int32, scoreToken int64, beatmapID, rulesetID *int32) (ok bool, reason string, err error)
	Username(ctx context.Context, userID int32) (string, error)
}

// FrameStore keeps the frames of a play for the replay. Open is called when a play begins.
type FrameStore interface {
	Open(scoreToken int64) FrameSink
}

// FrameSink receives the frames of one play, in order.
type FrameSink interface {
	// Append never blocks the caller for long: frames are queued and written in the background.
	Append(bundle []byte)
	// Close returns when everything appended has been written (or the context ends).
	Close(ctx context.Context) error
}

// Options configures the hub.
type Options struct {
	Backend Backend
	Frames  FrameStore
	// PresenceWatchers, when set, lists extra users who receive every play's stream (the old Python hub's
	// "metadata presence watchers" compatibility mode). Off by default: only explicit watchers get frames.
	PresenceWatchers func() []int32
	// ScoreProcessedRetry and ScoreProcessedWindow pace UserScoreProcessed: the client may register right
	// after submitting, so delivery is retried until the window ends.
	ScoreProcessedRetry  time.Duration
	ScoreProcessedWindow time.Duration
}

type play struct {
	token  int64
	state  msgpack.RawMessage // as sent by the player, announced to new watchers
	frames FrameSink
}

type conn struct {
	session *signalr.Session
	userID  int32
	play    *play // guarded by Hub.mu
}

// Hub implements signalr.Handler.
type Hub struct {
	verifier *auth.Verifier
	opts     Options

	mu       sync.RWMutex
	conns    map[*signalr.Session]*conn
	byUser   map[int32]map[*conn]struct{}
	watchers map[int32]map[int32]struct{} // target -> users watching them
	watching map[int32]map[int32]struct{} // user -> targets they watch

	names sync.Map // user id -> username
}

// New builds a hub.
func New(verifier *auth.Verifier, opts Options) *Hub {
	if opts.ScoreProcessedRetry <= 0 {
		opts.ScoreProcessedRetry = 500 * time.Millisecond
	}
	if opts.ScoreProcessedWindow <= 0 {
		opts.ScoreProcessedWindow = 5 * time.Second
	}
	return &Hub{
		verifier: verifier,
		opts:     opts,
		conns:    map[*signalr.Session]*conn{},
		byUser:   map[int32]map[*conn]struct{}{},
		watchers: map[int32]map[int32]struct{}{},
		watching: map[int32]map[int32]struct{}{},
	}
}

// Authenticate implements signalr.Handler.
func (h *Hub) Authenticate(r *http.Request) (int32, bool) { return h.verifier.Authenticate(r) }

// OnConnect implements signalr.Handler.
func (h *Hub) OnConnect(s *signalr.Session) {
	c := &conn{session: s, userID: s.UserID}
	h.mu.Lock()
	h.conns[s] = c
	if h.byUser[c.userID] == nil {
		h.byUser[c.userID] = map[*conn]struct{}{}
	}
	h.byUser[c.userID][c] = struct{}{}
	h.mu.Unlock()
	slog.Debug("spectator connected", "user", c.userID, "session", s.ID)
}

// OnDisconnect implements signalr.Handler. A play that was running is announced as over, and when it was the
// user's last connection the people they watched are told they left.
func (h *Hub) OnDisconnect(s *signalr.Session) {
	h.mu.Lock()
	c := h.conns[s]
	if c == nil {
		h.mu.Unlock()
		return
	}
	delete(h.conns, s)
	delete(h.byUser[c.userID], c)
	lastConnection := len(h.byUser[c.userID]) == 0
	if lastConnection {
		delete(h.byUser, c.userID)
	}
	abandoned := c.play
	c.play = nil

	var targets []int32
	if lastConnection {
		for target := range h.watching[c.userID] {
			targets = append(targets, target)
			delete(h.watchers[target], c.userID)
			if len(h.watchers[target]) == 0 {
				delete(h.watchers, target)
			}
		}
		delete(h.watching, c.userID)
	}
	h.mu.Unlock()

	if abandoned != nil {
		h.closeSink(abandoned)
		if final, err := finishedState(abandoned.state); err == nil {
			h.broadcast(c.userID, "UserFinishedPlaying", c.userID, final)
		}
	}
	for _, target := range targets {
		h.sendToUser(target, "UserEndedWatching", c.userID)
	}
	slog.Debug("spectator disconnected", "user", c.userID, "session", s.ID)
}

// Invoke implements signalr.Handler. Every method is void, and bad input is logged and ignored like the
// Python hub did, so a confused client keeps its connection.
func (h *Hub) Invoke(ctx context.Context, s *signalr.Session, method string, args []msgpack.RawMessage) (any, bool, error) {
	h.mu.RLock()
	c := h.conns[s]
	h.mu.RUnlock()
	if c == nil {
		return nil, false, nil
	}

	switch method {
	case "BeginPlaySession", "BeginPlaySessionV2":
		h.beginPlay(ctx, c, args)
	case "SendFrameData":
		h.sendFrames(c, args, 0)
	case "SendFrameDataV2":
		h.sendFrames(c, args, 1)
	case "EndPlaySession":
		h.endPlay(c, args, false)
	case "EndPlaySessionV2":
		h.endPlay(c, args, true)
	case "StartWatchingUser":
		h.startWatching(ctx, c, args)
	case "EndWatchingUser":
		h.endWatching(c, args)
	default:
		slog.Warn("spectator: unknown method", "method", method, "user", c.userID)
	}
	return nil, false, nil
}

func argAt(args []msgpack.RawMessage, index int) msgpack.RawMessage {
	if index < len(args) {
		return args[index]
	}
	return nil
}

func (h *Hub) beginPlay(ctx context.Context, c *conn, args []msgpack.RawMessage) {
	var token *int64
	if raw := argAt(args, 0); len(raw) > 0 {
		if err := msgpack.Unmarshal(raw, &token); err != nil {
			slog.Warn("spectator: BeginPlaySession with a bad score token", "user", c.userID, "error", err)
			return
		}
	}
	state := argAt(args, 1)
	if token == nil || len(state) == 0 {
		slog.Warn("spectator: BeginPlaySession without a score token or state", "user", c.userID)
		return
	}
	beatmapID, rulesetID, err := stateFields(state)
	if err != nil {
		slog.Warn("spectator: BeginPlaySession with a bad state", "user", c.userID, "error", err)
		return
	}

	ok, reason, err := h.opts.Backend.ValidatePlay(ctx, c.userID, *token, beatmapID, rulesetID)
	if err != nil {
		slog.Error("spectator: cannot validate the score token", "user", c.userID, "token", *token, "error", err)
		return
	}
	if !ok {
		slog.Warn("spectator: BeginPlaySession rejected", "user", c.userID, "token", *token, "reason", reason)
		return
	}

	started := &play{token: *token, state: state, frames: h.opts.Frames.Open(*token)}
	h.mu.Lock()
	previous := c.play
	c.play = started
	h.mu.Unlock()
	if previous != nil {
		h.closeSink(previous) // a play that never ended; its frames are not this play's
	}

	h.broadcast(c.userID, "UserBeganPlaying", c.userID, state)
	slog.Info("spectator: play began", "user", c.userID, "token", *token)
}

func (h *Hub) sendFrames(c *conn, args []msgpack.RawMessage, bundleIndex int) {
	raw := argAt(args, bundleIndex)
	if len(raw) == 0 {
		return
	}
	bundle, err := stampBundle(raw, time.Now())
	if err != nil {
		slog.Warn("spectator: dropping a bad frame bundle", "user", c.userID, "error", err)
		return
	}

	h.mu.RLock()
	current := c.play
	h.mu.RUnlock()
	if current != nil {
		if stored, err := storableBundle(raw); err == nil {
			current.frames.Append(stored)
		}
	} else {
		slog.Debug("spectator: frames without an active play", "user", c.userID)
	}

	h.broadcast(c.userID, "UserSentFrames", c.userID, bundle)
}

func (h *Hub) endPlay(c *conn, args []msgpack.RawMessage, v2 bool) {
	h.mu.Lock()
	ended := c.play
	c.play = nil
	h.mu.Unlock()

	var final msgpack.RawMessage
	var err error
	if v2 {
		// (scoreToken, SpectatedUserState): only the final state travels, the rest is what the play began with.
		finalState := stateQuit
		if raw := argAt(args, 1); len(raw) > 0 {
			if err := msgpack.Unmarshal(raw, &finalState); err != nil {
				finalState = stateQuit
			}
		}
		base := msgpack.RawMessage(nil)
		if ended != nil {
			base = ended.state
		} else if base, err = encodeValue([]any{nil, nil, []any{}, 0, map[int]int{}}); err != nil {
			return
		}
		if final, err = withState(base, finalState); err == nil {
			final, err = finishedState(final)
		}
	} else if raw := argAt(args, 0); len(raw) > 0 {
		final, err = finishedState(raw)
	} else {
		slog.Warn("spectator: EndPlaySession without a state", "user", c.userID)
		return
	}
	if err != nil {
		slog.Warn("spectator: EndPlaySession with a bad state", "user", c.userID, "error", err)
		return
	}

	if ended != nil {
		h.closeSink(ended) // the replay must be complete before the client submits the score
	}
	h.broadcast(c.userID, "UserFinishedPlaying", c.userID, final)
	slog.Info("spectator: play finished", "user", c.userID)
}

func (h *Hub) closeSink(p *play) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := p.frames.Close(ctx); err != nil {
		slog.Error("spectator: frames of a play may be incomplete", "token", p.token, "error", err)
	}
}

func (h *Hub) startWatching(ctx context.Context, c *conn, args []msgpack.RawMessage) {
	target, ok := userArg(args)
	if !ok {
		slog.Warn("spectator: StartWatchingUser without a user id", "user", c.userID)
		return
	}

	h.mu.Lock()
	if h.watchers[target] == nil {
		h.watchers[target] = map[int32]struct{}{}
	}
	h.watchers[target][c.userID] = struct{}{}
	if h.watching[c.userID] == nil {
		h.watching[c.userID] = map[int32]struct{}{}
	}
	h.watching[c.userID][target] = struct{}{}
	state := h.playingStateLocked(target)
	h.mu.Unlock()

	// Catch the new watcher up with a play that is already running.
	if state != nil {
		c.session.Invoke("UserBeganPlaying", target, state)
	}

	h.sendToUser(target, "UserStartedWatching", []any{[]any{c.userID, h.username(ctx, c.userID)}})
	slog.Info("spectator: started watching", "user", c.userID, "target", target)
}

func (h *Hub) endWatching(c *conn, args []msgpack.RawMessage) {
	target, ok := userArg(args)
	if !ok {
		slog.Warn("spectator: EndWatchingUser without a user id", "user", c.userID)
		return
	}

	h.mu.Lock()
	delete(h.watching[c.userID], target)
	delete(h.watchers[target], c.userID)
	if len(h.watchers[target]) == 0 {
		delete(h.watchers, target)
	}
	h.mu.Unlock()

	h.sendToUser(target, "UserEndedWatching", c.userID)
	slog.Info("spectator: stopped watching", "user", c.userID, "target", target)
}

func userArg(args []msgpack.RawMessage) (int32, bool) {
	raw := argAt(args, 0)
	if len(raw) == 0 {
		return 0, false
	}
	var id *int32
	if err := msgpack.Unmarshal(raw, &id); err != nil || id == nil {
		return 0, false
	}
	return *id, true
}

// playingStateLocked is the state announced by the target's running play, if they have one. h.mu must be held.
func (h *Hub) playingStateLocked(target int32) msgpack.RawMessage {
	for c := range h.byUser[target] {
		if c.play != nil {
			return c.play.state
		}
	}
	return nil
}

func (h *Hub) username(ctx context.Context, userID int32) string {
	if name, ok := h.names.Load(userID); ok {
		return name.(string)
	}
	name, err := h.opts.Backend.Username(ctx, userID)
	if err != nil || name == "" {
		slog.Warn("spectator: cannot resolve a username", "user", userID, "error", err)
		return "User " + itoa(userID)
	}
	h.names.Store(userID, name)
	return name
}

// broadcast sends a callback about target (their play) to everyone watching them. The message is encoded once
// and shared by all recipients.
func (h *Hub) broadcast(target int32, method string, args ...any) {
	data, err := signalr.EncodeInvocation(method, args...)
	if err != nil {
		slog.Error("spectator: encoding a broadcast failed", "method", method, "error", err)
		return
	}

	var extra []int32
	if h.opts.PresenceWatchers != nil {
		extra = h.opts.PresenceWatchers()
	}

	h.mu.RLock()
	defer h.mu.RUnlock()
	for watcher := range h.watchers[target] {
		h.sendLocked(watcher, data)
	}
	for _, watcher := range extra {
		if _, explicit := h.watchers[target][watcher]; !explicit && watcher != target {
			h.sendLocked(watcher, data)
		}
	}
}

func (h *Hub) sendLocked(user int32, data []byte) int {
	delivered := 0
	for c := range h.byUser[user] {
		c.session.SendEncoded(data)
		delivered++
	}
	return delivered
}

// sendToUser sends a callback to all connections of one user and returns how many received it.
func (h *Hub) sendToUser(user int32, method string, args ...any) int {
	data, err := signalr.EncodeInvocation(method, args...)
	if err != nil {
		slog.Error("spectator: encoding a callback failed", "method", method, "error", err)
		return 0
	}
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.sendLocked(user, data)
}

// NotifyScoreProcessed tells a user their submitted score has been processed, so the client refreshes its
// statistics. The client may not be registered yet right after the submission, so delivery is retried briefly.
func (h *Hub) NotifyScoreProcessed(userID int32, scoreID int64) {
	go func() {
		deadline := time.Now().Add(h.opts.ScoreProcessedWindow)
		time.Sleep(h.opts.ScoreProcessedRetry)
		for {
			if h.sendToUser(userID, "UserScoreProcessed", userID, scoreID) > 0 {
				return
			}
			if time.Now().After(deadline) {
				slog.Debug("spectator: UserScoreProcessed expired", "user", userID, "score", scoreID)
				return
			}
			time.Sleep(h.opts.ScoreProcessedRetry)
		}
	}()
}

func itoa(v int32) string {
	const digits = "0123456789"
	if v == 0 {
		return "0"
	}
	negative := v < 0
	if negative {
		v = -v
	}
	var buf [12]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = digits[v%10]
		v /= 10
	}
	if negative {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
