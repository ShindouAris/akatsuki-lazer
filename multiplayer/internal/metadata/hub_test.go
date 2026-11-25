package metadata

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/golang-jwt/jwt/v5"
	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/signalr"
)

type fakeBackend struct{ friends map[int32][]int32 }

func (f fakeBackend) Friends(_ context.Context, id int32) ([]int32, error) { return f.friends[id], nil }
func (fakeBackend) RoomStats(context.Context, int32) ([]any, error) {
	return []any{[]any{int64(7), []int64{0, 1}, int64(100), int64(5)}}, nil
}
func (fakeBackend) DailyChallenge(context.Context) (int32, bool, error) { return 42, true, nil }

type fakeUpdates struct{}

func (fakeUpdates) Since(context.Context, int64) ([]int32, int64, error) {
	return []int32{1, 2}, 9, nil
}

type env struct {
	ts  *httptest.Server
	hub *Hub
	key *rsa.PrivateKey
}

func newEnv(t *testing.T, friends map[int32][]int32) *env {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, _ := x509.MarshalPKIXPublicKey(&key.PublicKey)
	verifier, err := auth.NewVerifier(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), "5")
	if err != nil {
		t.Fatal(err)
	}
	h := New(verifier, fakeBackend{friends: friends}, fakeUpdates{})
	server := &signalr.Server{Handler: h}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /metadata", server.ServeWS)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return &env{ts: ts, hub: h, key: key}
}

type client struct {
	t    *testing.T
	ws   *websocket.Conn
	next int
}

func (e *env) connect(t *testing.T, id int) *client {
	t.Helper()
	tok, err := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
		"sub": strconv.Itoa(id), "type": "access", "aud": "5", "exp": time.Now().Add(time.Hour).Unix(),
	}).SignedString(e.key)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(e.ts.URL, "http")+"/metadata",
		&websocket.DialOptions{HTTPHeader: http.Header{"Authorization": {"Bearer " + tok}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ws.CloseNow() })
	_ = ws.Write(ctx, websocket.MessageText, []byte(`{"protocol":"messagepack","version":1}`+"\x1e"))
	if _, _, err := ws.Read(ctx); err != nil {
		t.Fatal(err)
	}
	return &client{t: t, ws: ws}
}

func lengthPrefix(n int) []byte {
	var out []byte
	for {
		b := byte(n & 0x7f)
		n >>= 7
		if n > 0 {
			out = append(out, b|0x80)
			continue
		}
		return append(out, b)
	}
}

func (c *client) send(invocationID, method string, args ...any) {
	var id any
	if invocationID != "" {
		id = invocationID
	}
	body, err := msgpack.Marshal([]any{1, map[string]any{}, id, method, args})
	if err != nil {
		c.t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := c.ws.Write(ctx, websocket.MessageBinary, append(lengthPrefix(len(body)), body...)); err != nil {
		c.t.Fatal(err)
	}
}

// read returns the next message that is not a ping.
func (c *client) read() signalr.Message {
	c.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for {
		_, data, err := c.ws.Read(ctx)
		if err != nil {
			c.t.Fatalf("waiting for a message: %v", err)
		}
		messages, err := signalr.Decode(data)
		if err != nil {
			c.t.Fatal(err)
		}
		for _, m := range messages {
			if m.Type == signalr.TypeInvocation || m.Type == signalr.TypeCompletion {
				return m
			}
		}
	}
}

func (c *client) expect(target string) signalr.Message {
	c.t.Helper()
	m := c.read()
	if m.Type != signalr.TypeInvocation || m.Target != target {
		c.t.Fatalf("got %+v, want callback %q", m, target)
	}
	return m
}

func TestDailyChallengeOnConnect(t *testing.T) {
	e := newEnv(t, nil)
	c := e.connect(t, 1)
	c.expect("DailyChallengeUpdated")
}

func TestPresenceWatcherSeesOthers(t *testing.T) {
	e := newEnv(t, nil)
	a := e.connect(t, 1)
	a.expect("DailyChallengeUpdated")
	a.send("", "BeginWatchingUserPresence")
	time.Sleep(100 * time.Millisecond)

	b := e.connect(t, 2)
	b.expect("DailyChallengeUpdated")
	m := a.expect("UserPresenceUpdated")
	var user int32
	if err := msgpack.Unmarshal(m.Args[0], &user); err != nil || user != 2 {
		t.Fatalf("presence of user %d, err=%v", user, err)
	}

	b.ws.CloseNow()
	m = a.expect("UserPresenceUpdated")
	if len(m.Args[1]) != 0 && !(len(m.Args[1]) == 1 && m.Args[1][0] == 0xc0) { // the decoder drops nil to empty
		t.Fatalf("offline presence should be nil, got %v", m.Args[1])
	}
}

func TestFriendPresence(t *testing.T) {
	e := newEnv(t, map[int32][]int32{1: {2}})
	a := e.connect(t, 1)
	a.expect("DailyChallengeUpdated")
	time.Sleep(100 * time.Millisecond)

	b := e.connect(t, 2)
	b.expect("DailyChallengeUpdated")
	a.expect("FriendPresenceUpdated")
}

func TestRoomStatsAndChanges(t *testing.T) {
	e := newEnv(t, nil)
	c := e.connect(t, 1)
	c.expect("DailyChallengeUpdated")

	c.send("1", "BeginWatchingMultiplayerRoom", 5)
	if m := c.read(); m.Type != signalr.TypeCompletion {
		t.Fatalf("got %+v", m)
	}
	c.send("2", "GetChangesSince", 0)
	if m := c.read(); m.Type != signalr.TypeCompletion {
		t.Fatalf("got %+v", m)
	}

	if n := e.hub.NotifyRoomScoreSet(5, []any{5, 7, 1, 1, 100, nil}); n != 1 {
		t.Fatalf("notified %d", n)
	}
	c.expect("MultiplayerRoomScoreSet")
	if n := e.hub.NotifyBeatmapUpdates([]int32{3}, 10); n != 1 {
		t.Fatalf("notified %d", n)
	}
	c.expect("BeatmapSetsUpdated")
}
