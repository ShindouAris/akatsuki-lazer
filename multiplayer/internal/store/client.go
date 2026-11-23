// Package store persists rooms through the Python API's internal endpoints (app/api/internal/multiplayer.py).
//
// Python owns the database. Two calls block because they return database ids (CreateRoom, AddItem). Everything
// else is write-behind: callers hand over the *full* new state and return immediately; a background loop sends
// the latest state of each room and item, retrying on network errors. Because states are whole and idempotent,
// a newer state simply replaces an older one that was never delivered.
package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
)

// Config configures a Client.
type Config struct {
	BaseURL string // e.g. http://127.0.0.1:8000
	APIKey  string // INTERNAL_API_KEY
	// HTTPClient is optional; the default has a 10s timeout.
	HTTPClient *http.Client
	// RetryDelay is the first backoff after a failed write (doubles up to MaxRetryDelay). Default 250ms.
	RetryDelay    time.Duration
	MaxRetryDelay time.Duration // default 10s
}

type itemKey struct{ room, item int64 }

type pendingItem struct {
	delete bool
	record room.ItemRecord
}

// Client implements room.Store and room.BeatmapLookup.
type Client struct {
	cfg  Config
	http *http.Client

	mu       sync.Mutex
	rooms    map[int64]room.RoomState
	items    map[itemKey]pendingItem
	inFlight int
	wake     chan struct{}

	cancel context.CancelFunc
	done   chan struct{}

	cacheMu   sync.Mutex
	beatmaps  map[int32]cachedBeatmap
	cacheTime func() time.Time
}

type cachedBeatmap struct {
	beatmap *room.Beatmap
	expires time.Time
}

const beatmapCacheTTL = time.Minute

// New creates a client. Call Start to begin delivering write-behind updates.
func New(cfg Config) *Client {
	if cfg.RetryDelay <= 0 {
		cfg.RetryDelay = 250 * time.Millisecond
	}
	if cfg.MaxRetryDelay <= 0 {
		cfg.MaxRetryDelay = 10 * time.Second
	}
	httpClient := cfg.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	return &Client{
		cfg:       cfg,
		http:      httpClient,
		rooms:     map[int64]room.RoomState{},
		items:     map[itemKey]pendingItem{},
		wake:      make(chan struct{}, 1),
		beatmaps:  map[int32]cachedBeatmap{},
		cacheTime: time.Now,
	}
}

// ---------------------------------------------------------------------------------------------------
// Wire types. These are the contract with app/api/internal/multiplayer.py; contract/*.json pins them.

type modJSON struct {
	Acronym  string         `json:"acronym"`
	Settings map[string]any `json:"settings"`
}

type itemJSON struct {
	OwnerID       *int32    `json:"owner_id"`
	BeatmapID     int32     `json:"beatmap_id"`
	RulesetID     int32     `json:"ruleset_id"`
	RequiredMods  []modJSON `json:"required_mods"`
	AllowedMods   []modJSON `json:"allowed_mods"`
	PlaylistOrder int       `json:"playlist_order"`
	Expired       bool      `json:"expired"`
	PlayedAt      *string   `json:"played_at"`
}

type stateJSON struct {
	HostID                *int32  `json:"host_id"`
	Name                  string  `json:"name"`
	Password              *string `json:"password"`
	Type                  string  `json:"type"`
	QueueMode             string  `json:"queue_mode"`
	Status                string  `json:"status"`
	ParticipantCount      int     `json:"participant_count"`
	ParticipantIDs        []int32 `json:"participant_ids"`
	MaxParticipants       *int    `json:"max_participants"`
	AutoSkip              bool    `json:"auto_skip"`
	AutoStartSeconds      int     `json:"auto_start_seconds"`
	CurrentPlaylistItemID *int64  `json:"current_playlist_item_id"`
}

type createRoomJSON struct {
	State stateJSON  `json:"state"`
	Items []itemJSON `json:"items"`
}

func mods(in []protocol.APIMod) []modJSON {
	out := make([]modJSON, 0, len(in))
	for _, m := range in {
		settings := m.Settings
		if settings == nil {
			settings = map[string]any{}
		}
		out = append(out, modJSON{Acronym: m.Acronym, Settings: settings})
	}
	return out
}

func toItemJSON(record room.ItemRecord) itemJSON {
	item := record.Item
	// Items made by the server itself (a played ranked play card) have no owner; 0 would break the users key.
	var owner *int32
	if item.OwnerID != 0 {
		id := item.OwnerID
		owner = &id
	}
	out := itemJSON{
		OwnerID:       owner,
		BeatmapID:     item.BeatmapID,
		RulesetID:     item.RulesetID,
		RequiredMods:  mods(item.RequiredMods),
		AllowedMods:   mods(item.AllowedMods),
		PlaylistOrder: int(item.PlaylistOrder),
		Expired:       item.Expired,
	}
	if record.PlayedAt != nil {
		formatted := record.PlayedAt.UTC().Format(time.RFC3339Nano)
		out.PlayedAt = &formatted
	}
	return out
}

func toStateJSON(state room.RoomState) stateJSON {
	out := stateJSON{
		Name:             state.Name,
		Type:             matchType(state.MatchType),
		QueueMode:        queueMode(state.QueueMode),
		Status:           string(state.Status),
		ParticipantCount: state.ParticipantCount,
		ParticipantIDs:   append([]int32{}, state.Participants...),
		AutoSkip:         state.AutoSkip,
		AutoStartSeconds: state.AutoStartSeconds,
	}
	if state.HostID != 0 {
		host := state.HostID
		out.HostID = &host
	}
	if state.Password != "" {
		password := state.Password
		out.Password = &password
	}
	if state.MaxParticipants > 0 {
		limit := state.MaxParticipants
		out.MaxParticipants = &limit
	}
	if state.CurrentItemID != 0 {
		current := state.CurrentItemID
		out.CurrentPlaylistItemID = &current
	}
	return out
}

func matchType(t protocol.MatchType) string {
	switch t {
	case protocol.MatchPlaylists:
		return "playlists"
	case protocol.MatchTeamVersus:
		return "team_versus"
	case protocol.MatchRankedPlay:
		return "ranked_play"
	default:
		return "head_to_head"
	}
}

func queueMode(q protocol.QueueMode) string {
	switch q {
	case protocol.QueueAllPlayers:
		return "all_players"
	case protocol.QueueAllPlayersRoundRobin:
		return "all_players_round_robin"
	default:
		return "host_only"
	}
}

// ---------------------------------------------------------------------------------------------------
// Blocking calls

// CreateRoom implements room.Store.
func (c *Client) CreateRoom(ctx context.Context, newRoom room.NewRoom) (room.CreatedRoom, error) {
	body := createRoomJSON{State: toStateJSON(newRoom.State), Items: make([]itemJSON, 0, len(newRoom.Items))}
	for _, item := range newRoom.Items {
		body.Items = append(body.Items, toItemJSON(room.ItemRecord{Item: item}))
	}

	var reply struct {
		RoomID    int64   `json:"room_id"`
		ChannelID int64   `json:"channel_id"`
		ItemIDs   []int64 `json:"item_ids"`
	}
	if err := c.do(ctx, http.MethodPost, "/internal/multiplayer/rooms", body, &reply); err != nil {
		return room.CreatedRoom{}, err
	}
	if reply.RoomID <= 0 || len(reply.ItemIDs) != len(newRoom.Items) {
		return room.CreatedRoom{}, fmt.Errorf("store: unexpected create reply %+v", reply)
	}
	return room.CreatedRoom{RoomID: reply.RoomID, ChannelID: reply.ChannelID, ItemIDs: reply.ItemIDs}, nil
}

// AddItem implements room.Store.
func (c *Client) AddItem(ctx context.Context, roomID int64, record room.ItemRecord) (int64, error) {
	var reply struct {
		ID int64 `json:"id"`
	}
	path := fmt.Sprintf("/internal/multiplayer/rooms/%d/items", roomID)
	if err := c.do(ctx, http.MethodPost, path, toItemJSON(record), &reply); err != nil {
		return 0, err
	}
	if reply.ID <= 0 {
		return 0, errors.New("store: server returned no item id")
	}
	return reply.ID, nil
}

// CloseStaleRooms closes realtime rooms a previous run left open. Call once at startup.
func (c *Client) CloseStaleRooms(ctx context.Context) (int, error) {
	var reply struct {
		Closed int `json:"closed"`
	}
	if err := c.do(ctx, http.MethodPost, "/internal/multiplayer/rooms/close-stale", nil, &reply); err != nil {
		return 0, err
	}
	return reply.Closed, nil
}

// Lookup implements room.BeatmapLookup with a short cache so a busy lobby does not hammer the API.
func (c *Client) Lookup(ctx context.Context, beatmapID int32) (*room.Beatmap, error) {
	c.cacheMu.Lock()
	if hit, ok := c.beatmaps[beatmapID]; ok && c.cacheTime().Before(hit.expires) {
		c.cacheMu.Unlock()
		return hit.beatmap, nil
	}
	c.cacheMu.Unlock()

	var reply struct {
		Checksum   string  `json:"checksum"`
		Mode       int32   `json:"mode"`
		StarRating float64 `json:"star_rating"`
	}
	err := c.do(ctx, http.MethodGet, "/internal/multiplayer/beatmaps/"+strconv.Itoa(int(beatmapID)), nil, &reply)
	var statusErr *StatusError
	if errors.As(err, &statusErr) && statusErr.Code == http.StatusNotFound {
		return nil, nil // does not exist; not cached so a later import is noticed straight away
	}
	if err != nil {
		return nil, err
	}

	beatmap := &room.Beatmap{Checksum: reply.Checksum, Mode: reply.Mode, StarRating: reply.StarRating}
	c.cacheMu.Lock()
	c.beatmaps[beatmapID] = cachedBeatmap{beatmap: beatmap, expires: c.cacheTime().Add(beatmapCacheTTL)}
	c.cacheMu.Unlock()
	return beatmap, nil
}

// StatusError is a non-2xx reply.
type StatusError struct {
	Code int
	Body string
}

func (e *StatusError) Error() string { return fmt.Sprintf("store: HTTP %d: %s", e.Code, e.Body) }

func (c *Client) do(ctx context.Context, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		data, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(data)
	}

	// JoinPath would escape a "?" in the path, so the query is kept apart from it.
	path, query, _ := strings.Cut(path, "?")
	endpoint, err := url.JoinPath(c.cfg.BaseURL, path)
	if err != nil {
		return err
	}
	if query != "" {
		endpoint += "?" + query
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return err
	}
	req.Header.Set("X-Internal-Key", c.cfg.APIKey)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		snippet := string(data)
		if len(snippet) > 300 {
			snippet = snippet[:300]
		}
		return &StatusError{Code: resp.StatusCode, Body: snippet}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

// ---------------------------------------------------------------------------------------------------
// Write-behind

// SaveRoom implements room.Store. It never blocks.
func (c *Client) SaveRoom(roomID int64, state room.RoomState) {
	c.mu.Lock()
	c.rooms[roomID] = state
	c.mu.Unlock()
	c.signal()
}

// SaveItem implements room.Store. It never blocks.
func (c *Client) SaveItem(roomID int64, record room.ItemRecord) {
	c.mu.Lock()
	c.items[itemKey{roomID, record.Item.ID}] = pendingItem{record: record}
	c.mu.Unlock()
	c.signal()
}

// DeleteItem implements room.Store. It never blocks.
func (c *Client) DeleteItem(roomID, itemID int64) {
	c.mu.Lock()
	c.items[itemKey{roomID, itemID}] = pendingItem{delete: true}
	c.mu.Unlock()
	c.signal()
}

func (c *Client) signal() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

// Pending reports how many room/item updates have not been delivered yet.
func (c *Client) Pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.rooms) + len(c.items) + c.inFlight
}

// Start begins delivering updates in the background.
func (c *Client) Start() {
	if c.done != nil {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	c.cancel = cancel
	c.done = make(chan struct{})
	go c.loop(ctx)
}

// Stop ends the background loop and then delivers what is left, until ctx expires.
func (c *Client) Stop(ctx context.Context) error {
	if c.cancel != nil {
		c.cancel()
		<-c.done
		c.cancel, c.done = nil, nil
	}
	return c.Flush(ctx)
}

func (c *Client) loop(ctx context.Context) {
	defer close(c.done)
	delay := c.cfg.RetryDelay
	for {
		select {
		case <-ctx.Done():
			return
		case <-c.wake:
		}

		for c.Pending() > 0 {
			if retry := c.flushOnce(ctx); retry == 0 {
				delay = c.cfg.RetryDelay
				continue
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(delay):
			}
			if delay *= 2; delay > c.cfg.MaxRetryDelay {
				delay = c.cfg.MaxRetryDelay
			}
		}
	}
}

// Flush delivers everything pending, retrying until it succeeds or ctx ends.
func (c *Client) Flush(ctx context.Context) error {
	delay := c.cfg.RetryDelay
	for c.Pending() > 0 {
		if c.flushOnce(ctx) == 0 {
			continue
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("store: %d updates were not delivered: %w", c.Pending(), ctx.Err())
		case <-time.After(delay):
		}
		if delay *= 2; delay > c.cfg.MaxRetryDelay {
			delay = c.cfg.MaxRetryDelay
		}
	}
	return nil
}

// flushOnce sends everything currently pending once and returns how many sends must be retried.
func (c *Client) flushOnce(ctx context.Context) (retry int) {
	c.mu.Lock()
	rooms, items := c.rooms, c.items
	c.rooms, c.items = map[int64]room.RoomState{}, map[itemKey]pendingItem{}
	c.inFlight = len(rooms) + len(items)
	c.mu.Unlock()

	defer func() {
		c.mu.Lock()
		c.inFlight = 0
		c.mu.Unlock()
	}()

	for key, op := range items {
		var err error
		path := fmt.Sprintf("/internal/multiplayer/rooms/%d/items/%d", key.room, key.item)
		if op.delete {
			err = c.do(ctx, http.MethodDelete, path, nil, nil)
		} else {
			err = c.do(ctx, http.MethodPut, path, toItemJSON(op.record), nil)
		}
		if c.handle(err, "item", key.item) {
			c.requeueItem(key, op)
			retry++
		}
	}
	for id, state := range rooms {
		err := c.do(ctx, http.MethodPut, fmt.Sprintf("/internal/multiplayer/rooms/%d", id), toStateJSON(state), nil)
		if c.handle(err, "room", id) {
			c.requeueRoom(id, state)
			retry++
		}
	}
	return retry
}

// handle logs a failure and reports whether it is worth retrying. Rejections (4xx) are final: resending
// the same state cannot succeed and would block newer updates behind it.
func (c *Client) handle(err error, what string, id int64) (retry bool) {
	if err == nil {
		return false
	}
	var statusErr *StatusError
	if errors.As(err, &statusErr) && statusErr.Code >= 400 && statusErr.Code < 500 && statusErr.Code != http.StatusTooManyRequests {
		slog.Warn("dropping rejected update", "kind", what, "id", id, "status", statusErr.Code, "body", statusErr.Body)
		return false
	}
	if errors.Is(err, context.Canceled) {
		return true // shutting down; Flush will pick it up
	}
	slog.Warn("update not delivered, will retry", "kind", what, "id", id, "error", err)
	return true
}

// requeue* put an undelivered update back unless a newer one arrived meanwhile.
func (c *Client) requeueRoom(id int64, state room.RoomState) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, newer := c.rooms[id]; !newer {
		c.rooms[id] = state
	}
}

func (c *Client) requeueItem(key itemKey, op pendingItem) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, newer := c.items[key]; !newer {
		c.items[key] = op
	}
}
