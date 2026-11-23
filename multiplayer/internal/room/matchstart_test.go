package room

import (
	"context"
	"testing"
	"time"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
)

// timerRoom is a room hosted by user 1 with user 2 joined, driven by a fake clock.
func timerRoom(t *testing.T, autoStart time.Duration) (*Room, *fakeClock) {
	t.Helper()
	clock := newFakeClock()
	mgr, _ := newManager(Options{Clock: clock, LoadTimeout: time.Hour})
	req := request(newItem(100))
	req.Settings.AutoStartDuration = protocol.Ticks(autoStart)
	r, _, err := mgr.Create(context.Background(), 1, req)
	must(t, err)
	_, _, err = mgr.Join(r.ID(), 2, "")
	must(t, err)
	return r, clock
}

func TestAnAutoStartRoomKeepsItsDuration(t *testing.T) {
	r, _ := timerRoom(t, 30*time.Second)
	if got := r.Snapshot().Settings.AutoStartDuration; got != protocol.Ticks(30*time.Second) {
		t.Fatalf("auto start duration = %d ticks, want 30s", got)
	}
}

func TestAutoStartBeginsWhenSomeoneReadiesAndStartsTheMatchWhenItElapses(t *testing.T) {
	r, clock := timerRoom(t, 30*time.Second)

	ready(t, r, 2)
	if !r.matchTimerRunning() {
		t.Fatal("readying up in an auto start room should start the countdown")
	}

	clock.Advance(29 * time.Second)
	if userState(r, 2) != protocol.UserReady {
		t.Fatal("the match started before the countdown ended")
	}
	clock.Advance(2 * time.Second)
	if got := userState(r, 2); got != protocol.UserWaitingForLoad {
		t.Fatalf("after the countdown the ready player is %v, want waiting for load", got)
	}
	if r.matchTimerRunning() {
		t.Fatal("the countdown should be gone once the match started")
	}
}

func TestAutoStartStopsWhenNobodyIsReadyAnyMore(t *testing.T) {
	r, clock := timerRoom(t, 30*time.Second)
	ready(t, r, 2)

	must(t, r.ChangeState(2, protocol.UserIdle))
	if r.matchTimerRunning() {
		t.Fatal("the countdown should stop when the last ready player unreadies")
	}
	clock.Advance(time.Minute)
	if userState(r, 2) != protocol.UserIdle {
		t.Fatal("nothing should start without ready players")
	}
}

func TestRoomsWithoutAutoStartDoNotCountDownOnTheirOwn(t *testing.T) {
	r, _ := timerRoom(t, 0)
	ready(t, r, 2)
	if r.matchTimerRunning() {
		t.Fatal("a room without an auto start duration must not start a countdown by itself")
	}
}

func TestTheHostCanStartAndStopACountdown(t *testing.T) {
	r, clock := timerRoom(t, 0)
	ready(t, r, 2)

	must(t, r.HandleMatchRequest(1, protocol.MatchRequestStartCountdown, &protocol.StartCountdownRequest{Duration: 10 * time.Second}))
	if !r.matchTimerRunning() {
		t.Fatal("the host's countdown request should start a countdown")
	}
	clock.Advance(11 * time.Second)
	if got := userState(r, 2); got != protocol.UserWaitingForLoad {
		t.Fatalf("the ready player is %v after the host's countdown, want waiting for load", got)
	}
}

func TestACountdownCanBeStoppedByItsId(t *testing.T) {
	r, clock := timerRoom(t, 0)
	ready(t, r, 2)
	must(t, r.HandleMatchRequest(1, protocol.MatchRequestStartCountdown, &protocol.StartCountdownRequest{Duration: 10 * time.Second}))

	r.lock()
	id := r.countdowns[0].info.ID
	r.unlock()
	must(t, r.HandleMatchRequest(1, protocol.MatchRequestStopCountdown, &protocol.StopCountdownRequest{ID: id}))

	clock.Advance(time.Minute)
	if r.matchTimerRunning() || userState(r, 2) != protocol.UserReady {
		t.Fatal("a stopped countdown must not start the match")
	}
}

func TestOnlyTheHostStartsCountdowns(t *testing.T) {
	r, _ := timerRoom(t, 0)
	err := r.HandleMatchRequest(2, protocol.MatchRequestStartCountdown, &protocol.StartCountdownRequest{Duration: 10 * time.Second})
	if err == nil {
		t.Fatal("a regular player started a countdown")
	}
	if r.matchTimerRunning() {
		t.Fatal("no countdown should be running")
	}
}

func TestACountdownDecodesFromTheClientsRequest(t *testing.T) {
	// StartMatchCountdownRequest is the union [1, [duration ticks]].
	raw, err := msgpack.Marshal([]any{protocol.MatchRequestStartCountdown, []any{protocol.Ticks(15 * time.Second)}})
	must(t, err)
	key, request, err := protocol.DecodeMatchUserRequest(raw)
	must(t, err)
	got, ok := request.(*protocol.StartCountdownRequest)
	if key != protocol.MatchRequestStartCountdown || !ok || got.Duration != 15*time.Second {
		t.Fatalf("decoded key=%d request=%#v", key, request)
	}

	raw, err = msgpack.Marshal([]any{protocol.MatchRequestStopCountdown, []any{int32(7)}})
	must(t, err)
	_, request, err = protocol.DecodeMatchUserRequest(raw)
	must(t, err)
	if stop, ok := request.(*protocol.StopCountdownRequest); !ok || stop.ID != 7 {
		t.Fatalf("decoded %#v", request)
	}
}
