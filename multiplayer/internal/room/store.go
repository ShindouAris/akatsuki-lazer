package room

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// RoomStatus is the coarse room status kept in the database.
type RoomStatus string

const (
	StatusIdle    RoomStatus = "idle"
	StatusPlaying RoomStatus = "playing"
	StatusClosed  RoomStatus = "closed"
)

// RoomState is the persisted state of a room. It is always saved whole, which lets a Store coalesce
// updates (latest wins) and retry them safely. The type is comparable so unchanged state is never re-sent.
type RoomState struct {
	HostID           int32 // 0 when there is no host
	Name             string
	Password         string
	MatchType        protocol.MatchType
	QueueMode        protocol.QueueMode
	Status           RoomStatus
	ParticipantCount int
	// Participants are the ids of the users in the room, ascending. The chat of the room is open to exactly them.
	Participants     []int32
	MaxParticipants  int // 0 = no limit
	AutoSkip         bool
	AutoStartSeconds int
	CurrentItemID    int64 // 0 when unset
}

// ItemRecord is a playlist item as persisted.
type ItemRecord struct {
	Item     protocol.PlaylistItem
	PlayedAt *time.Time
}

// NewRoom is what is needed to create a room.
type NewRoom struct {
	State RoomState
	Items []protocol.PlaylistItem
}

// CreatedRoom carries the ids the database assigned.
type CreatedRoom struct {
	RoomID    int64
	ChannelID int64
	ItemIDs   []int64 // in the order of NewRoom.Items
}

// Store persists rooms. Two calls return database ids and block; the rest must never block and are
// delivered best effort by the implementation (see internal/store).
type Store interface {
	CreateRoom(ctx context.Context, room NewRoom) (CreatedRoom, error)
	AddItem(ctx context.Context, roomID int64, item ItemRecord) (int64, error)

	SaveRoom(roomID int64, state RoomState)
	SaveItem(roomID int64, item ItemRecord)
	DeleteItem(roomID, itemID int64)
}

// MemoryStore keeps nothing but hands out ids. It is the default when no database is configured.
type MemoryStore struct {
	rooms atomic.Int64
	items atomic.Int64
}

// NewMemoryStore creates a MemoryStore.
func NewMemoryStore() *MemoryStore { return &MemoryStore{} }

// CreateRoom implements Store.
func (m *MemoryStore) CreateRoom(_ context.Context, room NewRoom) (CreatedRoom, error) {
	id := m.rooms.Add(1)
	created := CreatedRoom{RoomID: id, ChannelID: id}
	for range room.Items {
		created.ItemIDs = append(created.ItemIDs, m.items.Add(1))
	}
	return created, nil
}

// AddItem implements Store.
func (m *MemoryStore) AddItem(context.Context, int64, ItemRecord) (int64, error) {
	return m.items.Add(1), nil
}

// SaveRoom implements Store.
func (*MemoryStore) SaveRoom(int64, RoomState) {}

// SaveItem implements Store.
func (*MemoryStore) SaveItem(int64, ItemRecord) {}

// DeleteItem implements Store.
func (*MemoryStore) DeleteItem(int64, int64) {}

// RecordingStore is a MemoryStore that also remembers what was saved, for tests.
type RecordingStore struct {
	MemoryStore

	mu         sync.Mutex
	Rooms      map[int64]RoomState
	Items      map[int64]map[int64]ItemRecord
	Deleted    []int64
	Saves      int
	FailAdds   bool // makes AddItem fail
	FailCreate bool // makes CreateRoom fail
}

// NewRecordingStore creates an empty RecordingStore.
func NewRecordingStore() *RecordingStore {
	return &RecordingStore{Rooms: map[int64]RoomState{}, Items: map[int64]map[int64]ItemRecord{}}
}

// CreateRoom implements Store.
func (s *RecordingStore) CreateRoom(ctx context.Context, room NewRoom) (CreatedRoom, error) {
	if s.FailCreate {
		return CreatedRoom{}, context.DeadlineExceeded
	}
	created, err := s.MemoryStore.CreateRoom(ctx, room)
	if err != nil {
		return created, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Rooms[created.RoomID] = room.State
	s.Items[created.RoomID] = map[int64]ItemRecord{}
	for i, item := range room.Items {
		item.ID = created.ItemIDs[i]
		s.Items[created.RoomID][item.ID] = ItemRecord{Item: item}
	}
	return created, nil
}

// AddItem implements Store.
func (s *RecordingStore) AddItem(ctx context.Context, roomID int64, item ItemRecord) (int64, error) {
	if s.FailAdds {
		return 0, context.DeadlineExceeded
	}
	id, err := s.MemoryStore.AddItem(ctx, roomID, item)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item.Item.ID = id
	if s.Items[roomID] == nil {
		s.Items[roomID] = map[int64]ItemRecord{}
	}
	s.Items[roomID][id] = item
	return id, nil
}

// SaveRoom implements Store.
func (s *RecordingStore) SaveRoom(roomID int64, state RoomState) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Rooms[roomID] = state
	s.Saves++
}

// SaveItem implements Store.
func (s *RecordingStore) SaveItem(roomID int64, item ItemRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Items[roomID] == nil {
		s.Items[roomID] = map[int64]ItemRecord{}
	}
	s.Items[roomID][item.Item.ID] = item
	s.Saves++
}

// DeleteItem implements Store.
func (s *RecordingStore) DeleteItem(roomID, itemID int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.Items[roomID], itemID)
	s.Deleted = append(s.Deleted, itemID)
	s.Saves++
}

// Room returns the last saved state of a room.
func (s *RecordingStore) Room(roomID int64) RoomState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.Rooms[roomID]
}

// Item returns the last saved state of an item.
func (s *RecordingStore) Item(roomID, itemID int64) (ItemRecord, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.Items[roomID][itemID]
	return item, ok
}

// SaveCount returns how many Save*/Delete calls were made.
func (s *RecordingStore) SaveCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.Saves
}
