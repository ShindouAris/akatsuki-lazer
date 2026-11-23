package room

import (
	"context"
	"log/slog"
	"strings"
	"sync"

	"akatsuki/multiplayer/internal/protocol"
)

// Manager owns all live rooms of this process.
type Manager struct {
	ev    Events
	opts  Options
	store Store

	mu    sync.Mutex
	rooms map[int64]*Room
}

// NewManager creates an empty manager. Without opts.Store rooms are kept in memory only.
func NewManager(ev Events, opts Options) *Manager {
	if opts.Store == nil {
		opts.Store = NewMemoryStore()
	}
	if opts.StoreTimeout <= 0 {
		opts.StoreTimeout = DefaultStoreTimeout
	}
	return &Manager{ev: ev, opts: opts, store: opts.Store, rooms: map[int64]*Room{}}
}

// Get returns a live room.
func (m *Manager) Get(roomID int64) (*Room, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	room, ok := m.rooms[roomID]
	return room, ok
}

// Count returns the number of live rooms.
func (m *Manager) Count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.rooms)
}

// Create builds a room from the client's request, persists it (which assigns the room and item ids) and joins
// the creator as its host.
func (m *Manager) Create(ctx context.Context, creator int32, request protocol.Room) (*Room, protocol.Room, error) {
	settings := request.Settings
	if err := validateMatchType(settings.MatchType); err != nil {
		return nil, protocol.Room{}, err
	}
	if len(request.Playlist) == 0 {
		return nil, protocol.Room{}, invalidState("Room must have at least one playlist item.")
	}
	settings.QueueMode = clampQueueMode(settings.QueueMode)
	settings.PlaylistItemID = 0
	if strings.TrimSpace(settings.Name) == "" {
		settings.Name = protocol.DefaultSettings().Name
	}

	items := append([]protocol.PlaylistItem(nil), request.Playlist...)
	sortByOrder(items)
	for i := range items {
		if err := validateItem(ctx, m.opts.Beatmaps, &items[i]); err != nil {
			return nil, protocol.Room{}, err
		}
		items[i].OwnerID = creator
		items[i].PlaylistOrder = uint16(i)
		items[i].Expired = false
		items[i].PlayedAt = nil
		items[i].Normalise()
	}

	// What the database will hold for the new room: the creator is its only participant.
	probe := newRoom(0, m.ev, m.opts, settings)
	probe.hostID = creator
	probe.users = []*protocol.RoomUser{{UserID: creator}}
	state := probe.stateRecord()

	storeCtx, cancel := context.WithTimeout(ctx, m.opts.StoreTimeout)
	defer cancel()
	created, err := m.store.CreateRoom(storeCtx, NewRoom{State: state, Items: items})
	if err != nil || len(created.ItemIDs) != len(items) {
		slog.Error("could not create the room", "user", creator, "error", err)
		return nil, protocol.Room{}, invalidState("Failed to create the room, please try again.")
	}

	r := newRoom(created.RoomID, m.ev, m.opts, settings)
	r.channelID = int32(created.ChannelID)
	for i := range items {
		items[i].ID = created.ItemIDs[i]
		stored := items[i]
		r.playlist = append(r.playlist, &stored)
		r.savedItems[stored.ID] = ItemRecord{Item: stored} // exactly what CreateRoom stored
	}
	r.savedState = &state
	r.settings.PlaylistItemID = r.playlist[0].ID

	snapshot, err := r.Join(creator, settings.Password, true)
	if err != nil {
		r.Close()
		return nil, protocol.Room{}, err
	}

	m.mu.Lock()
	m.rooms[r.id] = r
	m.mu.Unlock()
	return r, snapshot, nil
}

// Join adds a user to an existing room.
func (m *Manager) Join(roomID int64, userID int32, password string) (*Room, protocol.Room, error) {
	r, ok := m.Get(roomID)
	if !ok {
		return nil, protocol.Room{}, invalidState("Failed to join the room, please try again.")
	}
	snapshot, err := r.Join(userID, password, false)
	if err != nil {
		return nil, protocol.Room{}, err
	}
	return r, snapshot, nil
}

// Leave removes a user from a room and discards the room if it became empty.
func (m *Manager) Leave(r *Room, userID, removedBy int32) error {
	empty, err := r.Leave(userID, removedBy)
	if err != nil {
		return err
	}
	if empty {
		m.discard(r)
	}
	return nil
}

// Kick removes a user on behalf of a host or referee.
func (m *Manager) Kick(r *Room, by, target int32) error {
	empty, err := r.Kick(by, target)
	if err != nil {
		return err
	}
	if empty {
		m.discard(r)
	}
	return nil
}

// CloseAll closes every live room (marking it closed in the store). Used on shutdown.
func (m *Manager) CloseAll() {
	m.mu.Lock()
	rooms := make([]*Room, 0, len(m.rooms))
	for _, r := range m.rooms {
		rooms = append(rooms, r)
	}
	m.rooms = map[int64]*Room{}
	m.mu.Unlock()

	for _, r := range rooms {
		r.Close()
	}
}

func (m *Manager) discard(r *Room) {
	r.Close()
	m.mu.Lock()
	delete(m.rooms, r.id)
	m.mu.Unlock()
}

func clampQueueMode(q protocol.QueueMode) protocol.QueueMode {
	if q < protocol.QueueHostOnly || q > protocol.QueueAllPlayersRoundRobin {
		return protocol.QueueHostOnly
	}
	return q
}

func sortByOrder(items []protocol.PlaylistItem) {
	for i := 1; i < len(items); i++ {
		for j := i; j > 0 && items[j].PlaylistOrder < items[j-1].PlaylistOrder; j-- {
			items[j], items[j-1] = items[j-1], items[j]
		}
	}
}
