package spectator

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
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/golang-jwt/jwt/v5"
	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/signalr"
)

type fakeBackend struct{ reject bool }

func (f fakeBackend) ValidatePlay(context.Context, int32, int64, *int32, *int32) (bool, string, error) {
	return !f.reject, "rejected", nil
}
func (fakeBackend) Username(_ context.Context, id int32) (string, error) {
	return "user" + strconv.Itoa(int(id)), nil
}

type fakeFrames struct {
	mu      sync.Mutex
	bundles [][]byte
	closed  bool
}

func (f *fakeFrames) Open(int64) FrameSink { return f }
func (f *fakeFrames) Append(b []byte) {
	f.mu.Lock()
	f.bundles = append(f.bundles, b)
	f.mu.Unlock()
}
func (f *fakeFrames) Close(context.Context) error {
	f.mu.Lock()
	f.closed = true
	f.mu.Unlock()
	return nil
}

type env struct {
	ts     *httptest.Server
	hub    *Hub
	key    *rsa.PrivateKey
	frames *fakeFrames
}

func newEnv(t *testing.T, backend Backend) *env {
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
	frames := &fakeFrames{}
	h := New(verifier, Options{Backend: backend, Frames: frames, ScoreProcessedRetry: 10 * time.Millisecond})
	server := &signalr.Server{Handler: h}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /spectator", server.ServeWS)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return &env{ts: ts, hub: h, key: key, frames: frames}
}

type client struct {
	t  *testing.T
	ws *websocket.Conn
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
	ws, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(e.ts.URL, "http")+"/spectator",
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

func (c *client) send(method string, args ...any) {
	body, err := msgpack.Marshal([]any{1, map[string]any{}, nil, method, args})
	if err != nil {
		c.t.Fatal(err)
	}
	framed := append(signalrLength(len(body)), body...)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := c.ws.Write(ctx, websocket.MessageBinary, framed); err != nil {
		c.t.Fatal(err)
	}
}

func signalrLength(n int) []byte {
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

// next returns the next callback target and its arguments, skipping pings.
func (c *client) next() (string, []msgpack.RawMessage) {
	c.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for {
		_, data, err := c.ws.Read(ctx)
		if err != nil {
			c.t.Fatalf("waiting for a callback: %v", err)
		}
		messages, err := signalr.Decode(data)
		if err != nil {
			c.t.Fatal(err)
		}
		for _, m := range messages {
			if m.Type == signalr.TypeInvocation {
				return m.Target, m.Args
			}
		}
	}
}

func (c *client) expect(target string) []msgpack.RawMessage {
	c.t.Helper()
	got, args := c.next()
	if got != target {
		c.t.Fatalf("got callback %q, want %q", got, target)
	}
	return args
}

func state(beatmap, ruleset, st int) []any {
	return []any{beatmap, ruleset, []any{}, st, map[int]int{}}
}

func bundle() []any {
	return []any{[]any{1, 1.0, 1, 1, map[int]int{}, nil, nil, []any{}}, []any{}}
}

func TestStreamReachesWatcher(t *testing.T) {
	e := newEnv(t, fakeBackend{})
	player, watcher := e.connect(t, 1), e.connect(t, 2)

	watcher.send("StartWatchingUser", 1)
	player.expect("UserStartedWatching")

	player.send("BeginPlaySessionV2", 99, state(10, 0, 1))
	watcher.expect("UserBeganPlaying")

	player.send("SendFrameDataV2", 99, bundle())
	args := watcher.expect("UserSentFrames")
	var parts []msgpack.RawMessage
	if err := msgpack.Unmarshal(args[1], &parts); err != nil {
		t.Fatal(err)
	}
	var header []msgpack.RawMessage
	if err := msgpack.Unmarshal(parts[0], &header); err != nil || len(header) != 8 {
		t.Fatalf("header %d entries, err=%v", len(header), err)
	}
	if len(header[6]) == 0 || header[6][0] == 0xc0 {
		t.Fatal("received time was not stamped")
	}

	player.send("EndPlaySessionV2", 99, 3)
	watcher.expect("UserFinishedPlaying")

	e.frames.mu.Lock()
	defer e.frames.mu.Unlock()
	if len(e.frames.bundles) != 1 || !e.frames.closed {
		t.Fatalf("replay: %d bundles, closed=%v", len(e.frames.bundles), e.frames.closed)
	}
}

func TestRejectedPlayIsNotBroadcast(t *testing.T) {
	e := newEnv(t, fakeBackend{reject: true})
	player, watcher := e.connect(t, 1), e.connect(t, 2)
	watcher.send("StartWatchingUser", 1)
	player.expect("UserStartedWatching")

	player.send("BeginPlaySessionV2", 99, state(10, 0, 1))
	player.send("SendFrameDataV2", 99, bundle())
	time.Sleep(200 * time.Millisecond)

	e.hub.mu.RLock()
	defer e.hub.mu.RUnlock()
	for c := range e.hub.conns {
		if c.UserID == 1 && e.hub.conns[c].play != nil {
			t.Fatal("a rejected play started")
		}
	}
}

func TestLateWatcherAndDisconnect(t *testing.T) {
	e := newEnv(t, fakeBackend{})
	player := e.connect(t, 1)
	player.send("BeginPlaySessionV2", 99, state(10, 0, 1))
	time.Sleep(100 * time.Millisecond)

	watcher := e.connect(t, 2)
	watcher.send("StartWatchingUser", 1)
	watcher.expect("UserBeganPlaying")

	player.ws.CloseNow()
	watcher.expect("UserFinishedPlaying")
}

func TestScoreProcessedIsRetriedUntilDelivered(t *testing.T) {
	e := newEnv(t, fakeBackend{})
	e.hub.NotifyScoreProcessed(1, 5)
	time.Sleep(50 * time.Millisecond)
	player := e.connect(t, 1)
	player.expect("UserScoreProcessed")
}
