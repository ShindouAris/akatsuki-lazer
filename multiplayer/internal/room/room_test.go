package room

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"akatsuki/multiplayer/internal/protocol"
)

// recorder captures client callbacks as "user:Target(args)" strings.
type recorder struct {
	mu     sync.Mutex
	events []string
}

func (r *recorder) Send(userID int32, target string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, fmt.Sprintf("%d:%s", userID, describe(target, args)))
}

func describe(target string, args []any) string {
	parts := make([]string, 0, len(args))
	for _, a := range args {
		switch v := a.(type) {
		case protocol.RoomUser:
			parts = append(parts, fmt.Sprintf("user%d", v.UserID))
		case protocol.PlaylistItem:
			parts = append(parts, fmt.Sprintf("item%d", v.ID))
		case protocol.Settings:
			parts = append(parts, fmt.Sprintf("item%d", v.PlaylistItemID))
		default:
			parts = append(parts, fmt.Sprint(v))
		}
	}
	if len(parts) == 0 {
		return target
	}
	return target + "(" + strings.Join(parts, ",") + ")"
}

// take returns and clears what was recorded.
func (r *recorder) take() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.events
	r.events = nil
	return out
}

// to filters the events addressed to one user, without the "user:" prefix.
func to(events []string, user int32) []string {
	var out []string
	prefix := fmt.Sprintf("%d:", user)
	for _, e := range events {
		if strings.HasPrefix(e, prefix) {
			out = append(out, strings.TrimPrefix(e, prefix))
		}
	}
	return out
}

func contains(events []string, want string) bool {
	for _, e := range events {
		if e == want {
			return true
		}
	}
	return false
}

func indexOf(events []string, want string) int {
	for i, e := range events {
		if e == want {
			return i
		}
	}
	return -1
}

func newItem(beatmapID int32) protocol.PlaylistItem {
	return protocol.PlaylistItem{BeatmapID: beatmapID, BeatmapChecksum: fmt.Sprintf("md5-%d", beatmapID)}
}

func newManager(opts Options) (*Manager, *recorder) {
	rec := &recorder{}
	return NewManager(rec, opts), rec
}

func request(items ...protocol.PlaylistItem) protocol.Room {
	settings := protocol.DefaultSettings()
	settings.Name = "test room"
	return protocol.Room{Settings: settings, Playlist: items}
}

// lobby creates a room hosted by user 1 with the given extra players joined and callbacks cleared.
func lobby(t *testing.T, opts Options, players ...int32) (*Manager, *Room, *recorder) {
	t.Helper()
	m, rec := newManager(opts)
	r, _, err := m.Create(context.Background(), 1, request(newItem(100)))
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range players {
		if _, _, err := m.Join(r.ID(), p, ""); err != nil {
			t.Fatal(err)
		}
	}
	rec.take()
	return m, r, rec
}

func ready(t *testing.T, r *Room, users ...int32) {
	t.Helper()
	for _, u := range users {
		must(t, r.ChangeBeatmapAvailability(u, protocol.BeatmapAvailability{State: protocol.DownloadLocallyAvailable}))
		must(t, r.ChangeState(u, protocol.UserReady))
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func wantError(t *testing.T, err error, typ, text string) {
	t.Helper()
	roomErr, ok := err.(*Error)
	if !ok {
		t.Fatalf("expected an *Error containing %q, got %v", text, err)
	}
	if roomErr.Type != typ || !strings.Contains(roomErr.Message, text) {
		t.Fatalf("got %s: %q, want %s containing %q", roomErr.Type, roomErr.Message, typ, text)
	}
}

func userState(r *Room, id int32) protocol.UserState {
	for _, u := range r.Snapshot().Users {
		if u.UserID == id {
			return u.State
		}
	}
	return -1
}

// --- creating and joining -----------------------------------------------------------------------------

func TestCreateRoomMakesCreatorHostAndSelectsFirstItem(t *testing.T) {
	m, _ := newManager(Options{})
	r, snapshot, err := m.Create(context.Background(), 7, request(newItem(100), newItem(101)))
	must(t, err)

	if snapshot.Host == nil || snapshot.Host.UserID != 7 || len(snapshot.Users) != 1 {
		t.Fatalf("host/users wrong: %+v", snapshot)
	}
	if len(snapshot.Playlist) != 2 || snapshot.Settings.PlaylistItemID != snapshot.Playlist[0].ID {
		t.Fatalf("current item must be the first queued item: %+v", snapshot)
	}
	for _, p := range snapshot.Playlist {
		if p.OwnerID != 7 || p.ID == 0 {
			t.Fatalf("items must be owned by the creator and get server ids: %+v", p)
		}
	}
	if snapshot.RoomID != r.ID() || snapshot.State != protocol.RoomOpen {
		t.Fatalf("room wrong: %+v", snapshot)
	}
}

func TestCreateRoomValidation(t *testing.T) {
	m, _ := newManager(Options{})

	_, _, err := m.Create(context.Background(), 1, request())
	wantError(t, err, "InvalidStateException", "at least one playlist item")

	bad := request(newItem(1))
	bad.Settings.MatchType = protocol.MatchPlaylists
	_, _, err = m.Create(context.Background(), 1, bad)
	wantError(t, err, "InvalidStateException", "not supported")

	if m.Count() != 0 {
		t.Fatal("failed creations must not leave rooms behind")
	}
}

func TestJoinNotifiesExistingMembersAndNotTheNewcomer(t *testing.T) {
	m, r, rec := lobby(t, Options{})
	_, snapshot, err := m.Join(r.ID(), 2, "")
	must(t, err)

	events := rec.take()
	if !contains(to(events, 1), "UserJoined(user2)") {
		t.Fatalf("host must be told about the newcomer: %v", events)
	}
	if len(to(events, 2)) != 0 {
		t.Fatalf("the newcomer learns about the room from the snapshot, not callbacks: %v", events)
	}
	if len(snapshot.Users) != 2 {
		t.Fatalf("snapshot must include the newcomer: %+v", snapshot.Users)
	}
}

func TestJoinRules(t *testing.T) {
	m, r, _ := lobby(t, Options{})

	_, _, err := m.Join(999, 2, "")
	wantError(t, err, "InvalidStateException", "Failed to join the room")

	_, _, err = m.Join(r.ID(), 1, "")
	wantError(t, err, "InvalidStateException", "already present")

	locked, _ := newManager(Options{})
	protected := request(newItem(1))
	protected.Settings.Password = "secret"
	pr, _, err := locked.Create(context.Background(), 1, protected)
	must(t, err)
	_, _, err = locked.Join(pr.ID(), 2, "wrong")
	wantError(t, err, "InvalidPasswordException", "Invalid password")
	_, _, err = locked.Join(pr.ID(), 2, "secret")
	must(t, err)
}

func TestMaxParticipants(t *testing.T) {
	m, _ := newManager(Options{})
	limited := request(newItem(1))
	limit := uint8(2)
	limited.Settings.MaxParticipants = &limit
	r, _, err := m.Create(context.Background(), 1, limited)
	must(t, err)

	_, _, err = m.Join(r.ID(), 2, "")
	must(t, err)
	_, _, err = m.Join(r.ID(), 3, "")
	wantError(t, err, "InvalidStateException", "Not eligible")
}

// --- leaving, host, kick ------------------------------------------------------------------------------

func TestLeavingHostTransfersHostAndNotifies(t *testing.T) {
	m, r, rec := lobby(t, Options{}, 2, 3)
	must(t, m.Leave(r, 1, 1))

	events := rec.take()
	if !contains(to(events, 2), "HostChanged(2)") || !contains(to(events, 3), "UserLeft(user1)") {
		t.Fatalf("unexpected events: %v", events)
	}
	if got := r.Snapshot(); got.Host == nil || got.Host.UserID != 2 || len(got.Users) != 2 {
		t.Fatalf("snapshot after host left: %+v", got)
	}
}

func TestLastUserLeavingDiscardsTheRoom(t *testing.T) {
	m, r, _ := lobby(t, Options{})
	must(t, m.Leave(r, 1, 1))

	if _, ok := m.Get(r.ID()); ok || m.Count() != 0 {
		t.Fatal("empty rooms must be discarded")
	}
	_, _, err := m.Join(r.ID(), 5, "")
	wantError(t, err, "InvalidStateException", "Failed to join")
}

func TestKick(t *testing.T) {
	m, r, rec := lobby(t, Options{}, 2, 3)

	wantError(t, m.Kick(r, 2, 3), "NotHostException", "host level operation")
	wantError(t, m.Kick(r, 1, 1), "InvalidStateException", "Can't kick self")
	wantError(t, m.Kick(r, 1, 99), "InvalidStateException", "not in the current room")

	must(t, m.Kick(r, 1, 3))
	events := rec.take()
	if !contains(to(events, 3), "UserKicked(user3)") || !contains(to(events, 2), "UserKicked(user3)") {
		t.Fatalf("kicked user and the rest must be told: %v", events)
	}
	if contains(to(events, 3), "UserLeft(user3)") || contains(to(events, 2), "UserLeft(user3)") {
		t.Fatal("a kick must not also announce a leave")
	}
	if len(r.Snapshot().Users) != 2 {
		t.Fatal("user was not removed")
	}
}

func TestTransferHost(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)

	wantError(t, r.TransferHost(2, 2), "NotHostException", "host level operation")
	wantError(t, r.TransferHost(1, 99), "InvalidStateException", "not in the room")

	must(t, r.TransferHost(1, 2))
	if !contains(to(rec.take(), 1), "HostChanged(2)") {
		t.Fatal("HostChanged not broadcast")
	}
	must(t, r.TransferHost(2, 1)) // the new host can hand it back
}

// --- user state ---------------------------------------------------------------------------------------

func TestStateTransitionsAreValidated(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)

	wantError(t, r.ChangeState(2, protocol.UserPlaying), "InvalidStateChangeException", "Cannot change from Idle to Playing")
	wantError(t, r.ChangeState(2, protocol.UserFinishedPlay), "InvalidStateChangeException", "Cannot change from Idle to FinishedPlay")
	wantError(t, r.ChangeState(2, protocol.UserResults), "InvalidStateChangeException", "Cannot change from Idle to Results")
	wantError(t, r.ChangeState(2, protocol.UserWaitingForLoad), "InvalidStateChangeException", "WaitingForLoad")

	must(t, r.ChangeState(2, protocol.UserReady))
	if userState(r, 2) != protocol.UserReady {
		t.Fatal("ready not applied")
	}
	must(t, r.ChangeState(2, protocol.UserReady)) // no-op, no error
	must(t, r.ChangeState(2, protocol.UserIdle))
	must(t, r.ChangeState(2, protocol.UserSpectating))
	must(t, r.ChangeState(1, protocol.UserLoaded)) // silently ignored outside gameplay
}

func TestStateChangesAreBroadcastToEveryone(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)
	must(t, r.ChangeState(2, protocol.UserReady))

	events := rec.take()
	for _, user := range []int32{1, 2} {
		if !contains(to(events, user), "UserStateChanged(2,Ready)") {
			t.Fatalf("user %d missed the state change: %v", user, events)
		}
	}
}

func TestLateGameplayStatesAreIgnoredNotRejected(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)

	// A client can send these just after the server aborted gameplay; erroring would spam the user.
	must(t, r.ChangeState(2, protocol.UserLoaded))
	must(t, r.ChangeState(2, protocol.UserReadyForGameplay))
	if userState(r, 2) != protocol.UserIdle {
		t.Fatal("state must be unchanged")
	}
}

func TestCannotReadyUpWhileAllItemsPlayed(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)
	r.mu.Lock()
	for _, p := range r.playlist {
		p.Expired = true
	}
	r.mu.Unlock()

	wantError(t, r.ChangeState(2, protocol.UserReady), "InvalidStateException", "all items have been played")
}

// --- playing a match ----------------------------------------------------------------------------------

func TestStartMatchPermissions(t *testing.T) {
	m, r, _ := lobby(t, Options{}, 2)

	wantError(t, r.StartMatch(2), "NotHostException", "host level operation")
	wantError(t, r.StartMatch(1), "InvalidStateException", "host is not ready")

	// A spectating host may start, but only when somebody is actually ready.
	must(t, r.ChangeState(1, protocol.UserSpectating))
	wantError(t, r.StartMatch(1), "InvalidStateException", "no users are ready")

	ready(t, r, 2)
	must(t, r.StartMatch(1))
	wantError(t, r.StartMatch(1), "InvalidStateException", "no users are ready") // everyone is loading now

	// Someone who joins mid-match and readies up must not be able to restart the running match.
	_, _, err := m.Join(r.ID(), 3, "")
	must(t, err)
	ready(t, r, 3)
	wantError(t, r.StartMatch(1), "InvalidStateException", "already in a running state")
}

func TestFullMatchCallbackSequence(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)
	ready(t, r, 1, 2)
	rec.take()

	must(t, r.StartMatch(1))
	events := to(rec.take(), 2)
	want := []string{"UserStateChanged(1,WaitingForLoad)", "UserStateChanged(2,WaitingForLoad)", "RoomStateChanged(1)", "LoadRequested"}
	if fmt.Sprint(events) != fmt.Sprint(want) {
		t.Fatalf("start sequence\n got: %v\nwant: %v", events, want)
	}

	must(t, r.ChangeState(1, protocol.UserLoaded))
	must(t, r.ChangeState(1, protocol.UserReadyForGameplay))
	rec.take()
	if r.Snapshot().State != protocol.RoomWaitingForLoad {
		t.Fatal("must keep waiting for user 2")
	}

	must(t, r.ChangeState(2, protocol.UserLoaded))
	must(t, r.ChangeState(2, protocol.UserReadyForGameplay))
	events = rec.take()
	if !contains(to(events, 1), "GameplayStarted") || !contains(to(events, 2), "GameplayStarted") {
		t.Fatalf("everyone loaded, gameplay must start: %v", events)
	}
	if r.Snapshot().State != protocol.RoomPlaying || userState(r, 1) != protocol.UserPlaying {
		t.Fatalf("room must be playing: %+v", r.Snapshot())
	}

	before := r.Snapshot().Settings.PlaylistItemID
	must(t, r.ChangeState(1, protocol.UserFinishedPlay))
	rec.take()
	must(t, r.ChangeState(2, protocol.UserFinishedPlay))
	events = to(rec.take(), 1)

	for _, want := range []string{"UserStateChanged(1,Results)", "UserStateChanged(2,Results)", "RoomStateChanged(0)", "ResultsReady"} {
		if !contains(events, want) {
			t.Fatalf("missing %q in %v", want, events)
		}
	}
	if indexOf(events, "UserStateChanged(2,Results)") > indexOf(events, "RoomStateChanged(0)") ||
		indexOf(events, "RoomStateChanged(0)") > indexOf(events, "ResultsReady") {
		t.Fatalf("results must be announced before the room reopens, then ResultsReady: %v", events)
	}

	// The played item is expired and, in host-only mode, a fresh copy is queued for the next round.
	after := r.Snapshot()
	if after.State != protocol.RoomOpen || after.Settings.PlaylistItemID == before {
		t.Fatalf("a new current item must be selected: %+v", after.Settings)
	}
	if len(after.Playlist) != 2 || !after.Playlist[0].Expired || after.Playlist[1].Expired {
		t.Fatalf("playlist after the round: %+v", after.Playlist)
	}
}

func TestOnlyReadyPlayersWithTheBeatmapJoinTheMatch(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2, 3)
	ready(t, r, 1, 2)
	must(t, r.ChangeState(3, protocol.UserReady)) // ready, but never got the beatmap

	must(t, r.StartMatch(1))

	if userState(r, 1) != protocol.UserWaitingForLoad || userState(r, 2) != protocol.UserWaitingForLoad {
		t.Fatal("ready players with the beatmap must load")
	}
	if userState(r, 3) != protocol.UserReady {
		t.Fatalf("a player without the beatmap must be left out, got %s", userState(r, 3))
	}
}

func TestLoadTimeoutStartsWithThoseWhoLoadedAndAbortsTheRest(t *testing.T) {
	_, r, rec := lobby(t, Options{LoadTimeout: 50 * time.Millisecond}, 2)
	ready(t, r, 1, 2)
	must(t, r.StartMatch(1))
	must(t, r.ChangeState(1, protocol.UserLoaded))
	must(t, r.ChangeState(1, protocol.UserReadyForGameplay))
	rec.take()

	deadline := time.Now().Add(2 * time.Second)
	for r.Snapshot().State != protocol.RoomPlaying && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}

	events := rec.take()
	if r.Snapshot().State != protocol.RoomPlaying {
		t.Fatalf("the load timeout must start the match: %v", events)
	}
	if !contains(to(events, 1), "GameplayStarted") {
		t.Fatalf("loaded player must start: %v", events)
	}
	if !contains(to(events, 2), "GameplayAborted(0)") || userState(r, 2) != protocol.UserIdle {
		t.Fatalf("player who never loaded must be aborted with LoadTookTooLong: %v", events)
	}
}

func TestEveryoneAbortingDuringLoadReopensTheRoomAndSkipsTheItem(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)
	ready(t, r, 1, 2)
	must(t, r.StartMatch(1))
	first := r.Snapshot().Settings.PlaylistItemID

	must(t, r.AbortGameplay(1))
	must(t, r.AbortGameplay(2))

	got := r.Snapshot()
	if got.State != protocol.RoomOpen || got.Settings.PlaylistItemID == first {
		t.Fatalf("expected an open room on the next item: %+v", got)
	}
}

func TestAbortMatch(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)

	wantError(t, r.AbortMatch(1), "InvalidStateException", "hasn't started")
	ready(t, r, 1, 2)
	must(t, r.StartMatch(1))
	wantError(t, r.AbortMatch(2), "NotHostException", "host level operation")
	rec.take()

	must(t, r.AbortMatch(1))
	events := to(rec.take(), 2)
	if !contains(events, "GameplayAborted(1)") || userState(r, 1) != protocol.UserIdle || userState(r, 2) != protocol.UserIdle {
		t.Fatalf("host abort must idle everyone and say why: %v", events)
	}
	if r.Snapshot().State != protocol.RoomOpen {
		t.Fatal("room must reopen")
	}
}

func TestAbortGameplay(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)
	wantError(t, r.AbortGameplay(2), "InvalidStateException", "not in gameplay")
	wantError(t, r.AbortGameplay(99), "InvalidStateException", "not in the room")
}

func TestPlayerLeavingMidMatchCanFinishIt(t *testing.T) {
	m, r, rec := lobby(t, Options{}, 2)
	ready(t, r, 1, 2)
	must(t, r.StartMatch(1))
	must(t, r.ChangeState(1, protocol.UserLoaded))
	must(t, r.ChangeState(1, protocol.UserReadyForGameplay))
	must(t, r.ChangeState(2, protocol.UserLoaded))
	must(t, r.ChangeState(2, protocol.UserReadyForGameplay))
	must(t, r.ChangeState(1, protocol.UserFinishedPlay))
	rec.take()

	must(t, m.Leave(r, 2, 2)) // the last player still playing disconnects

	if r.Snapshot().State != protocol.RoomOpen || userState(r, 1) != protocol.UserResults {
		t.Fatalf("the match must complete for the remaining player: %+v", r.Snapshot())
	}
}

func TestVoteToSkipIntro(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2, 3)
	wantError(t, r.VoteToSkipIntro(2), "InvalidStateException", "not in gameplay")

	ready(t, r, 1, 2, 3)
	must(t, r.StartMatch(1))
	for _, u := range []int32{1, 2, 3} {
		must(t, r.ChangeState(u, protocol.UserLoaded))
		must(t, r.ChangeState(u, protocol.UserReadyForGameplay))
	}
	rec.take()

	must(t, r.VoteToSkipIntro(1))
	if contains(to(rec.take(), 1), "VoteToSkipIntroPassed") {
		t.Fatal("1 of 3 is not a majority")
	}
	must(t, r.VoteToSkipIntro(2))
	events := to(rec.take(), 3)
	if !contains(events, "UserVotedToSkipIntro(2,true)") || !contains(events, "VoteToSkipIntroPassed") {
		t.Fatalf("2 of 3 is a majority: %v", events)
	}
}

// --- settings -----------------------------------------------------------------------------------------

func TestChangeSettingsRules(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)
	current := r.Snapshot().Settings
	renamed := current
	renamed.Name = "renamed"

	wantError(t, r.ChangeSettings(2, renamed), "NotHostException", "host level operation")

	must(t, r.ChangeSettings(1, renamed))
	if r.Snapshot().Settings.Name != "renamed" {
		t.Fatal("name not applied")
	}
	if !contains(to(rec.take(), 2), fmt.Sprintf("SettingsChanged(item%d)", current.PlaylistItemID)) {
		t.Fatal("SettingsChanged not broadcast")
	}

	must(t, r.ChangeSettings(1, renamed))
	if len(rec.take()) != 0 {
		t.Fatal("identical settings must not produce events")
	}

	tampered := renamed
	tampered.PlaylistItemID = 424242 // the server is authoritative over the current item
	must(t, r.ChangeSettings(1, tampered))
	if r.Snapshot().Settings.PlaylistItemID != current.PlaylistItemID {
		t.Fatal("client must not be able to pick the current item")
	}

	invalid := renamed
	invalid.MatchType = protocol.MatchPlaylists
	invalid.Name = "x"
	wantError(t, r.ChangeSettings(1, invalid), "InvalidStateException", "not supported")
}

func TestChangeSettingsUnreadiesPlayersAndIsRejectedMidMatch(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)
	ready(t, r, 1, 2)

	next := r.Snapshot().Settings
	next.Name = "changed"
	must(t, r.ChangeSettings(1, next))
	if userState(r, 1) != protocol.UserIdle || userState(r, 2) != protocol.UserIdle {
		t.Fatal("settings changes must unready everyone")
	}

	ready(t, r, 1)
	must(t, r.StartMatch(1))
	next.Name = "again"
	wantError(t, r.ChangeSettings(1, next), "InvalidStateException", "game is in progress")
}

// --- beatmap availability and mods --------------------------------------------------------------------

func TestBeatmapAvailabilityIsBroadcastOnlyWhenChanged(t *testing.T) {
	_, r, rec := lobby(t, Options{}, 2)
	have := protocol.BeatmapAvailability{State: protocol.DownloadLocallyAvailable}

	must(t, r.ChangeBeatmapAvailability(2, have))
	if !containsPrefix(to(rec.take(), 1), "UserBeatmapAvailabilityChanged(2,") {
		t.Fatal("availability change not broadcast")
	}
	must(t, r.ChangeBeatmapAvailability(2, have))
	if len(rec.take()) != 0 {
		t.Fatal("unchanged availability must stay quiet")
	}
	wantError(t, r.ChangeBeatmapAvailability(99, have), "InvalidStateException", "not in the room")
}

func TestUserModsMustBeAllowedByTheItem(t *testing.T) {
	m, rec := newManager(Options{})
	open := request(newItem(1))
	open.Playlist[0].AllowedMods = []protocol.APIMod{{Acronym: "HD"}, {Acronym: "HR"}}
	open.Playlist[0].RequiredMods = []protocol.APIMod{{Acronym: "DT"}}
	r, _, err := m.Create(context.Background(), 1, open)
	must(t, err)
	rec.take()

	must(t, r.ChangeUserMods(1, []protocol.APIMod{{Acronym: "HD"}}))
	if !containsPrefix(to(rec.take(), 1), "UserModsChanged(1,") || len(r.Snapshot().Users[0].Mods) != 1 {
		t.Fatal("allowed mod not applied")
	}

	wantError(t, r.ChangeUserMods(1, []protocol.APIMod{{Acronym: "FL"}}), "InvalidStateException", "Incompatible mods were selected: FL")
	wantError(t, r.ChangeUserMods(1, []protocol.APIMod{{Acronym: "DT"}}), "InvalidStateException", "DT")
	wantError(t, r.ChangeUserMods(1, []protocol.APIMod{{Acronym: "HD"}, {Acronym: "HD"}}), "InvalidStateException", "HD")

	must(t, r.ChangeUserMods(1, nil)) // clearing is always fine
	if len(r.Snapshot().Users[0].Mods) != 0 {
		t.Fatal("mods not cleared")
	}
}

func TestFreestyleStylesAreRejectedUntilImplemented(t *testing.T) {
	_, r, _ := lobby(t, Options{})
	must(t, r.ChangeUserStyle(1, nil, nil))

	beatmap := int32(5)
	wantError(t, r.ChangeUserStyle(1, &beatmap, nil), "InvalidStateException", "not supported")
}

// --- playlist -----------------------------------------------------------------------------------------

type fakeBeatmaps map[int32]*Beatmap

func (f fakeBeatmaps) Lookup(_ context.Context, id int32) (*Beatmap, error) { return f[id], nil }

func TestAddPlaylistItemPermissionsAndLimits(t *testing.T) {
	ctx := context.Background()
	_, r, rec := lobby(t, Options{}, 2)

	wantError(t, r.AddPlaylistItem(ctx, 2, newItem(5)), "NotHostException", "host level operation")

	must(t, r.AddPlaylistItem(ctx, 1, newItem(5)))
	if !contains(to(rec.take(), 2), "PlaylistItemAdded(item2)") {
		t.Fatal("PlaylistItemAdded not broadcast")
	}

	settings := r.Snapshot().Settings
	settings.QueueMode = protocol.QueueAllPlayers
	must(t, r.ChangeSettings(1, settings))

	for i := 0; i < GuestPlaylistLimit; i++ {
		must(t, r.AddPlaylistItem(ctx, 2, newItem(int32(10+i))))
	}
	wantError(t, r.AddPlaylistItem(ctx, 2, newItem(99)), "InvalidStateException", "Can't enqueue more than 3 items at once")
	must(t, r.AddPlaylistItem(ctx, 1, newItem(98))) // hosts have a higher limit
}

func TestPlaylistItemsAreValidatedAgainstTheBeatmap(t *testing.T) {
	ctx := context.Background()
	beatmaps := fakeBeatmaps{
		5: {Checksum: "md5-5", Mode: 0, StarRating: 5.5},
		6: {Checksum: "md5-6", Mode: 3},
	}
	m, _ := newManager(Options{Beatmaps: beatmaps})
	r, _, err := m.Create(ctx, 1, request(newItem(5)))
	must(t, err)

	wantError(t, r.AddPlaylistItem(ctx, 1, newItem(404)), "InvalidStateException", "does not exist online")

	stale := newItem(5)
	stale.BeatmapChecksum = "old"
	wantError(t, r.AddPlaylistItem(ctx, 1, stale), "InvalidStateException", "has been modified")

	wrongMode := newItem(6)
	wrongMode.RulesetID = 1
	wantError(t, r.AddPlaylistItem(ctx, 1, wrongMode), "InvalidStateException", "invalid beatmap and ruleset")

	outOfRange := newItem(5)
	outOfRange.RulesetID = 9
	wantError(t, r.AddPlaylistItem(ctx, 1, outOfRange), "InvalidStateException", "unsupported ruleset")

	freestyle := newItem(5)
	freestyle.Freestyle = true
	freestyle.AllowedMods = []protocol.APIMod{{Acronym: "HD"}}
	wantError(t, r.AddPlaylistItem(ctx, 1, freestyle), "InvalidStateException", "freestyle item with mods")

	good := newItem(6)
	good.RulesetID = 3
	must(t, r.AddPlaylistItem(ctx, 1, good))
	if snapshot := r.Snapshot(); snapshot.Playlist[0].StarRating != 5.5 {
		t.Fatalf("star rating must come from the server, not the client: %+v", snapshot.Playlist[0])
	}
}

func TestRemovePlaylistItem(t *testing.T) {
	ctx := context.Background()
	_, r, rec := lobby(t, Options{}, 2)
	only := r.Snapshot().Settings.PlaylistItemID

	wantError(t, r.RemovePlaylistItem(1, 99999), "InvalidStateException", "does not exist")
	wantError(t, r.RemovePlaylistItem(1, only), "InvalidStateException", "only item in the room cannot be removed")

	must(t, r.AddPlaylistItem(ctx, 1, newItem(7)))
	second := r.Snapshot().Playlist[1].ID
	wantError(t, r.RemovePlaylistItem(2, second), "InvalidStateException", "not owned by the user")
	rec.take()

	must(t, r.RemovePlaylistItem(1, only)) // removing the current item selects the next one
	events := to(rec.take(), 2)
	if r.Snapshot().Settings.PlaylistItemID != second {
		t.Fatalf("the next item must become current: %+v", r.Snapshot().Settings)
	}
	removed := fmt.Sprintf("PlaylistItemRemoved(%d)", only)
	changed := fmt.Sprintf("SettingsChanged(item%d)", second)
	if indexOf(events, changed) < 0 || indexOf(events, removed) < 0 || indexOf(events, changed) > indexOf(events, removed) {
		t.Fatalf("clients must learn the new current item before the removal: %v", events)
	}
}

func TestEditPlaylistItem(t *testing.T) {
	ctx := context.Background()
	_, r, rec := lobby(t, Options{}, 2)
	settings := r.Snapshot().Settings
	settings.QueueMode = protocol.QueueAllPlayers
	must(t, r.ChangeSettings(1, settings))
	must(t, r.AddPlaylistItem(ctx, 2, newItem(7)))
	theirs := r.Snapshot().Playlist[1]
	rec.take()

	edited := newItem(8)
	edited.ID = theirs.ID
	wantError(t, r.EditPlaylistItem(ctx, 1, protocol.PlaylistItem{ID: 31337, BeatmapID: 8, BeatmapChecksum: "md5-8"}),
		"InvalidStateException", "doesn't exist")

	must(t, r.EditPlaylistItem(ctx, 1, edited)) // the host may edit anyone's item
	got := r.Snapshot().Playlist[1]
	if got.BeatmapID != 8 || got.OwnerID != 1 || got.PlaylistOrder != theirs.PlaylistOrder {
		t.Fatalf("edit result: %+v", got)
	}
	if !contains(to(rec.take(), 2), fmt.Sprintf("PlaylistItemChanged(item%d)", theirs.ID)) {
		t.Fatal("PlaylistItemChanged not broadcast")
	}

	stranger := newItem(9)
	stranger.ID = r.Snapshot().Playlist[0].ID
	wantError(t, r.EditPlaylistItem(ctx, 2, stranger), "InvalidStateException", "not owned by the user")
}

func TestEditingTheCurrentItemUnreadiesPlayersAndResetsAvailability(t *testing.T) {
	ctx := context.Background()
	_, r, _ := lobby(t, Options{}, 2)
	ready(t, r, 1, 2)

	current := r.CurrentItem()
	current.BeatmapID = 200
	current.BeatmapChecksum = "md5-200"
	must(t, r.EditPlaylistItem(ctx, 1, current))

	for _, u := range r.Snapshot().Users {
		if u.State != protocol.UserIdle || u.BeatmapAvailability.State != protocol.DownloadUnknown {
			t.Fatalf("a changed beatmap invalidates readiness and availability: %+v", u)
		}
	}
}

func TestRoundRobinOrdersItemsByOwnerTurns(t *testing.T) {
	ctx := context.Background()
	_, r, _ := lobby(t, Options{}, 2)
	settings := r.Snapshot().Settings
	settings.QueueMode = protocol.QueueAllPlayersRoundRobin
	must(t, r.ChangeSettings(1, settings))

	// Host owns item 1 (from creation); queue A1 A2 for user 1 and B1 B2 for user 2 in a lopsided order.
	must(t, r.AddPlaylistItem(ctx, 2, newItem(21)))
	must(t, r.AddPlaylistItem(ctx, 2, newItem(22)))
	must(t, r.AddPlaylistItem(ctx, 1, newItem(12)))

	byOrder := map[uint16]int32{}
	for _, p := range r.Snapshot().Playlist {
		byOrder[p.PlaylistOrder] = p.BeatmapID
	}
	want := map[uint16]int32{0: 100, 1: 21, 2: 12, 3: 22}
	if fmt.Sprint(byOrder) != fmt.Sprint(want) {
		t.Fatalf("round robin order\n got: %v\nwant: %v", byOrder, want)
	}
}

func TestHostOnlyModeRequeuesTheItemAfterEachRound(t *testing.T) {
	_, r, _ := lobby(t, Options{}, 2)
	for round := 1; round <= 3; round++ {
		ready(t, r, 1, 2)
		must(t, r.StartMatch(1))
		for _, u := range []int32{1, 2} {
			must(t, r.ChangeState(u, protocol.UserLoaded))
			must(t, r.ChangeState(u, protocol.UserReadyForGameplay))
		}
		for _, u := range []int32{1, 2} {
			must(t, r.ChangeState(u, protocol.UserFinishedPlay))
		}
		// leave the Results state so players can ready up again
		for _, u := range []int32{1, 2} {
			must(t, r.ChangeState(u, protocol.UserIdle))
		}

		upcoming := 0
		for _, p := range r.Snapshot().Playlist {
			if !p.Expired {
				upcoming++
			}
		}
		if upcoming != 1 || len(r.Snapshot().Playlist) != round+1 {
			t.Fatalf("round %d: exactly one upcoming item expected, playlist=%+v", round, r.Snapshot().Playlist)
		}
	}
}

// --- concurrency --------------------------------------------------------------------------------------

func TestConcurrentUseIsRaceFree(t *testing.T) {
	m, rec := newManager(Options{})
	r, _, err := m.Create(context.Background(), 1, request(newItem(1)))
	must(t, err)
	_ = rec

	var wg sync.WaitGroup
	for u := int32(2); u < 40; u++ {
		wg.Add(1)
		go func(user int32) {
			defer wg.Done()
			if _, _, err := m.Join(r.ID(), user, ""); err != nil {
				return
			}
			_ = r.ChangeBeatmapAvailability(user, protocol.BeatmapAvailability{State: protocol.DownloadLocallyAvailable})
			_ = r.ChangeState(user, protocol.UserReady)
			_ = r.Snapshot()
			_ = r.ChangeState(user, protocol.UserIdle)
			_ = m.Leave(r, user, user)
		}(u)
	}
	wg.Wait()

	if got := len(r.Snapshot().Users); got != 1 {
		t.Fatalf("only the host should remain, got %d users", got)
	}
}

func containsPrefix(events []string, prefix string) bool {
	for _, e := range events {
		if strings.HasPrefix(e, prefix) {
			return true
		}
	}
	return false
}
