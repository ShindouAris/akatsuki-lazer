package room

import (
	"context"
	"reflect"
	"sort"
	"testing"

	"akatsuki/multiplayer/internal/protocol"
)

func participantsOf(snap protocol.Room) []int32 {
	ids := make([]int32, 0, len(snap.Users))
	for _, u := range snap.Users {
		ids = append(ids, u.UserID)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

// assertMirrors is the invariant of the whole persistence design: whatever the store holds equals what the
// room's clients see. It is checked after every step of the scenarios below.
func assertMirrors(t *testing.T, store *RecordingStore, r *Room) {
	t.Helper()
	snap := r.Snapshot()
	state := store.Room(r.ID())

	if snap.Host == nil {
		t.Fatalf("room has no host: %+v", snap)
	}
	want := RoomState{
		HostID:           snap.Host.UserID,
		Name:             snap.Settings.Name,
		Password:         snap.Settings.Password,
		MatchType:        snap.Settings.MatchType,
		QueueMode:        snap.Settings.QueueMode,
		ParticipantCount: len(snap.Users),
		Participants:     participantsOf(snap),
		AutoSkip:         snap.Settings.AutoSkip,
		MaxParticipants:  maxParticipants(snap.Settings),
		CurrentItemID:    snap.Settings.PlaylistItemID,
	}
	switch snap.State {
	case protocol.RoomOpen:
		want.Status = StatusIdle
	default:
		want.Status = StatusPlaying
	}
	if !reflect.DeepEqual(state, want) {
		t.Fatalf("stored room state differs from the live room\n stored: %+v\n   live: %+v", state, want)
	}

	store.mu.Lock()
	defer store.mu.Unlock()
	stored := store.Items[r.ID()]
	if len(stored) != len(snap.Playlist) {
		t.Fatalf("store has %d items, room has %d", len(stored), len(snap.Playlist))
	}
	for _, p := range snap.Playlist {
		got, ok := stored[p.ID]
		if !ok {
			t.Fatalf("item %d missing from the store", p.ID)
		}
		if got.Item.BeatmapID != p.BeatmapID || got.Item.PlaylistOrder != p.PlaylistOrder ||
			got.Item.Expired != p.Expired || got.Item.OwnerID != p.OwnerID || got.Item.RulesetID != p.RulesetID {
			t.Fatalf("item %d differs\n stored: %+v\n   live: %+v", p.ID, got.Item, p)
		}
		if p.Expired && got.PlayedAt == nil {
			t.Fatalf("expired item %d must have a played-at time", p.ID)
		}
	}
}

func recordingManager(opts Options) (*Manager, *RecordingStore, *recorder) {
	store := NewRecordingStore()
	store.rooms.Store(500) // ids come from the store, not from a counter in the room package
	store.items.Store(9000)
	opts.Store = store
	m, rec := newManager(opts)
	return m, store, rec
}

func createRecorded(t *testing.T, m *Manager, items ...protocol.PlaylistItem) *Room {
	t.Helper()
	if len(items) == 0 {
		items = []protocol.PlaylistItem{newItem(100)}
	}
	r, _, err := m.Create(context.Background(), 1, request(items...))
	must(t, err)
	return r
}

func TestCreateRoomUsesTheIdsAssignedByTheStore(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m, newItem(100), newItem(101))

	snap := r.Snapshot()
	if snap.RoomID != 501 || snap.ChannelID != 501 {
		t.Fatalf("room id must come from the store: %+v", snap)
	}
	if snap.Playlist[0].ID != 9001 || snap.Playlist[1].ID != 9002 || snap.Settings.PlaylistItemID != 9001 {
		t.Fatalf("item ids must come from the store: %+v", snap.Playlist)
	}
	assertMirrors(t, store, r)
	if got := store.Room(501); got.ParticipantCount != 1 || got.HostID != 1 || got.CurrentItemID != 9001 {
		t.Fatalf("creator must be the stored host and sole participant: %+v", got)
	}
}

func TestCreateRoomFailureLeavesNothingBehind(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	store.FailCreate = true

	_, _, err := m.Create(context.Background(), 1, request(newItem(1)))

	wantError(t, err, "InvalidStateException", "Failed to create the room")
	if m.Count() != 0 {
		t.Fatal("a room that could not be saved must not exist")
	}
}

func TestBlankRoomNameGetsTheClientsDefault(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	blank := request(newItem(1))
	blank.Settings.Name = "   "
	r, snap, err := m.Create(context.Background(), 1, blank)
	must(t, err)

	if snap.Settings.Name != "Unnamed room" {
		t.Fatalf("name = %q", snap.Settings.Name)
	}
	assertMirrors(t, store, r)
}

func TestMembershipChangesAreMirrored(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)
	_, _, err = m.Join(r.ID(), 3, "")
	must(t, err)
	assertMirrors(t, store, r)
	if store.Room(r.ID()).ParticipantCount != 3 {
		t.Fatal("participant count not stored")
	}

	must(t, m.Kick(r, 1, 3))
	assertMirrors(t, store, r)

	must(t, m.Leave(r, 1, 1)) // the host leaves: host passes to user 2
	assertMirrors(t, store, r)
	if got := store.Room(r.ID()); got.HostID != 2 || got.ParticipantCount != 1 {
		t.Fatalf("host hand-over not stored: %+v", got)
	}

	must(t, m.Leave(r, 2, 2))
	if got := store.Room(r.ID()); got.Status != StatusClosed || got.ParticipantCount != 0 {
		t.Fatalf("the emptied room must be closed in the store: %+v", got)
	}
}

func TestSettingsChangesAreMirrored(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)

	next := r.Snapshot().Settings
	next.Name = "renamed"
	next.Password = "secret"
	next.QueueMode = protocol.QueueAllPlayersRoundRobin
	next.AutoSkip = true
	limit := uint8(4)
	next.MaxParticipants = &limit
	must(t, r.ChangeSettings(1, next))

	assertMirrors(t, store, r)
	got := store.Room(r.ID())
	if got.Name != "renamed" || got.Password != "secret" || got.QueueMode != protocol.QueueAllPlayersRoundRobin ||
		!got.AutoSkip || got.MaxParticipants != 4 {
		t.Fatalf("settings not stored: %+v", got)
	}
}

func TestAFullRoundIsMirroredStepByStep(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)
	ready(t, r, 1, 2)
	assertMirrors(t, store, r)

	must(t, r.StartMatch(1))
	assertMirrors(t, store, r)
	if store.Room(r.ID()).Status != StatusPlaying {
		t.Fatal("a loading match counts as playing")
	}

	for _, u := range []int32{1, 2} {
		must(t, r.ChangeState(u, protocol.UserLoaded))
		must(t, r.ChangeState(u, protocol.UserReadyForGameplay))
	}
	assertMirrors(t, store, r)

	played := r.Snapshot().Settings.PlaylistItemID
	for _, u := range []int32{1, 2} {
		must(t, r.ChangeState(u, protocol.UserFinishedPlay))
	}
	assertMirrors(t, store, r)

	record, ok := store.Item(r.ID(), played)
	if !ok || !record.Item.Expired || record.PlayedAt == nil {
		t.Fatalf("the played item must be stored as expired with a play time: %+v", record)
	}
	if got := store.Room(r.ID()); got.Status != StatusIdle || got.CurrentItemID == played {
		t.Fatalf("room must be idle on the requeued item: %+v", got)
	}
	if next, _ := store.Item(r.ID(), store.Room(r.ID()).CurrentItemID); next.Item.ID <= 9001 || next.Item.Expired {
		t.Fatalf("the requeued copy must have its own database id: %+v", next)
	}
}

func TestPlaylistEditsAreMirrored(t *testing.T) {
	ctx := context.Background()
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)

	settings := r.Snapshot().Settings
	settings.QueueMode = protocol.QueueAllPlayersRoundRobin
	must(t, r.ChangeSettings(1, settings))

	must(t, r.AddPlaylistItem(ctx, 2, newItem(21)))
	must(t, r.AddPlaylistItem(ctx, 2, newItem(22)))
	must(t, r.AddPlaylistItem(ctx, 1, newItem(12)))
	assertMirrors(t, store, r) // round robin renumbered several items

	snap := r.Snapshot()
	edited := newItem(300)
	edited.ID = snap.Playlist[1].ID
	must(t, r.EditPlaylistItem(ctx, 1, edited))
	assertMirrors(t, store, r)
	if stored, _ := store.Item(r.ID(), edited.ID); stored.Item.BeatmapID != 300 {
		t.Fatalf("edit not stored: %+v", stored)
	}

	gone := snap.Playlist[2].ID
	must(t, r.RemovePlaylistItem(1, gone))
	assertMirrors(t, store, r)
	if _, still := store.Item(r.ID(), gone); still || len(store.Deleted) != 1 || store.Deleted[0] != gone {
		t.Fatalf("removed item must be deleted from the store (deleted=%v)", store.Deleted)
	}
}

func TestUnchangedStateIsNotResent(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)
	before := store.SaveCount()

	_ = r.Snapshot()
	_ = r.CurrentItem()
	must(t, r.ChangeState(2, protocol.UserIdle))                                    // already idle
	must(t, r.ChangeUserMods(2, nil))                                               // nothing to change
	must(t, r.ChangeSettings(1, r.Snapshot().Settings))                             // identical settings
	must(t, r.ChangeBeatmapAvailability(2, protocol.BeatmapAvailability{State: 4})) // not persisted state

	if after := store.SaveCount(); after != before {
		t.Fatalf("%d redundant saves", after-before)
	}
}

func TestPlayerStateChangesAreNotPersisted(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)
	before := store.SaveCount()

	ready(t, r, 2) // readiness and beatmap availability are live-only
	if store.SaveCount() != before {
		t.Fatal("per-player state must not hit the database")
	}
}

func TestAddPlaylistItemFailsCleanlyWhenTheStoreFails(t *testing.T) {
	m, store, rec := recordingManager(Options{})
	r := createRecorded(t, m)
	rec.take()
	before := r.Snapshot()
	store.FailAdds = true

	err := r.AddPlaylistItem(context.Background(), 1, newItem(5))

	wantError(t, err, "InvalidStateException", "Failed to save the playlist item")
	if len(r.Snapshot().Playlist) != len(before.Playlist) {
		t.Fatal("the item must not exist if it could not be saved")
	}
	if len(rec.take()) != 0 {
		t.Fatal("clients must not be told about an item that was not saved")
	}
	store.FailAdds = false
	must(t, r.AddPlaylistItem(context.Background(), 1, newItem(5))) // and it works again afterwards
	assertMirrors(t, store, r)
}

func TestARoundStillCompletesWhenRequeueingFails(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	r := createRecorded(t, m)
	_, _, err := m.Join(r.ID(), 2, "")
	must(t, err)
	ready(t, r, 1, 2)
	must(t, r.StartMatch(1))
	for _, u := range []int32{1, 2} {
		must(t, r.ChangeState(u, protocol.UserLoaded))
		must(t, r.ChangeState(u, protocol.UserReadyForGameplay))
	}
	store.FailAdds = true

	for _, u := range []int32{1, 2} {
		must(t, r.ChangeState(u, protocol.UserFinishedPlay))
	}

	snap := r.Snapshot()
	if snap.State != protocol.RoomOpen || len(snap.Playlist) != 1 || !snap.Playlist[0].Expired {
		t.Fatalf("the round must still finish, just without a follow up item: %+v", snap)
	}
	assertMirrors(t, store, r)
	must(t, r.ChangeState(2, protocol.UserIdle)) // leave the results screen first
	wantError(t, r.ChangeState(2, protocol.UserReady), "InvalidStateException", "all items have been played")

	store.FailAdds = false
	must(t, r.AddPlaylistItem(context.Background(), 1, newItem(7))) // the host can recover the room
	if next := r.Snapshot().Settings.PlaylistItemID; next == snap.Playlist[0].ID {
		t.Fatal("the new item must become current")
	}
	assertMirrors(t, store, r)
}

func TestCloseAllMarksEveryRoomClosed(t *testing.T) {
	m, store, _ := recordingManager(Options{})
	a := createRecorded(t, m)
	b := createRecorded(t, m)

	m.CloseAll()

	if m.Count() != 0 {
		t.Fatal("no live rooms must remain")
	}
	for _, r := range []*Room{a, b} {
		if store.Room(r.ID()).Status != StatusClosed {
			t.Fatalf("room %d not closed in the store", r.ID())
		}
	}
}

func maxParticipants(s protocol.Settings) int {
	if s.MaxParticipants == nil {
		return 0
	}
	return int(*s.MaxParticipants)
}
