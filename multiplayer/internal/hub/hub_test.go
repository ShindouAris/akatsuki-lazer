package hub

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
	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/signalr"
)

const audience = "5"

type env struct {
	server *httptest.Server
	hub    *Hub
	key    *rsa.PrivateKey
}

func newEnv(t *testing.T) *env {
	t.Helper()
	return newEnvWith(t, room.Options{LoadTimeout: 200 * time.Millisecond})
}

func newEnvWith(t *testing.T, opts room.Options) *env {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := auth.NewVerifier(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), audience)
	if err != nil {
		t.Fatal(err)
	}

	h := New(verifier, opts)
	server := &signalr.Server{Handler: h}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /multiplayer/negotiate", server.Negotiate)
	mux.HandleFunc("GET /multiplayer", server.ServeWS)

	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return &env{server: ts, hub: h, key: key}
}

func (e *env) token(t *testing.T, userID string, mutate func(jwt.MapClaims)) string {
	t.Helper()
	claims := jwt.MapClaims{"sub": userID, "type": "access", "aud": audience, "exp": time.Now().Add(time.Hour).Unix()}
	if mutate != nil {
		mutate(claims)
	}
	signed, err := jwt.NewWithClaims(jwt.SigningMethodRS256, claims).SignedString(e.key)
	if err != nil {
		t.Fatal(err)
	}
	return signed
}

// client is a minimal osu! client: MessagePack hub protocol over a WebSocket.
type client struct {
	t    *testing.T
	ws   *websocket.Conn
	next int
	// callbacks received while waiting for a completion
	pending []signalr.Message
}

func (e *env) connect(t *testing.T, userID int32) *client {
	t.Helper()
	token := e.token(t, itoa(userID), nil)
	url := "ws" + strings.TrimPrefix(e.server.URL, "http") + "/multiplayer"

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws, _, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: http.Header{"Authorization": {"Bearer " + token}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ws.CloseNow() })

	if err := ws.Write(ctx, websocket.MessageText, []byte(`{"protocol":"messagepack","version":1}`+"\x1e")); err != nil {
		t.Fatal(err)
	}
	if _, reply, err := ws.Read(ctx); err != nil || string(reply) != "{}\x1e" {
		t.Fatalf("handshake reply %q err=%v", reply, err)
	}
	return &client{t: t, ws: ws}
}

func itoa(v int32) string { return strconv.Itoa(int(v)) }

func fmtInt(v int64) string { return strconv.FormatInt(v, 10) }

// call invokes a hub method and waits for its completion, stashing callbacks that arrive meanwhile.
func (c *client) call(method string, args ...any) (result any, failure string) {
	c.t.Helper()
	c.next++
	id := fmtInt(int64(c.next))

	body, err := msgpack.Marshal([]any{1, map[string]any{}, id, method, args})
	if err != nil {
		c.t.Fatal(err)
	}
	frame := append(varint(len(body)), body...)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := c.ws.Write(ctx, websocket.MessageBinary, frame); err != nil {
		c.t.Fatal(err)
	}

	for {
		raw := c.read(ctx)
		for _, element := range raw {
			if element.completionFor == id {
				return element.result, element.failure
			}
			if element.callback != nil {
				c.pending = append(c.pending, *element.callback)
			}
		}
	}
}

type incoming struct {
	completionFor string
	result        any
	failure       string
	callback      *signalr.Message
}

func (c *client) read(ctx context.Context) []incoming {
	c.t.Helper()
	out, err := c.tryRead(ctx)
	if err != nil {
		c.t.Fatalf("read: %v", err)
	}
	return out
}

// tryRead is read that reports a failure (a timeout, for one) instead of failing the test.
func (c *client) tryRead(ctx context.Context) ([]incoming, error) {
	c.t.Helper()
	_, data, err := c.ws.Read(ctx)
	if err != nil {
		return nil, err
	}

	var out []incoming
	for offset := 0; offset < len(data); {
		size, n := readVarint(data[offset:])
		body := data[offset+n : offset+n+size]
		offset += n + size

		var elements []msgpack.RawMessage
		if err := msgpack.Unmarshal(body, &elements); err != nil {
			c.t.Fatal(err)
		}
		var kind int
		_ = msgpack.Unmarshal(elements[0], &kind)

		switch kind {
		case signalr.TypePing:
			continue
		case signalr.TypeInvocation:
			var target string
			_ = msgpack.Unmarshal(elements[3], &target)
			var args []msgpack.RawMessage
			_ = msgpack.Unmarshal(elements[4], &args)
			out = append(out, incoming{callback: &signalr.Message{Type: kind, Target: target, Args: args}})
		case signalr.TypeCompletion:
			var id string
			var resultKind int
			_ = msgpack.Unmarshal(elements[2], &id)
			_ = msgpack.Unmarshal(elements[3], &resultKind)
			item := incoming{completionFor: id}
			if len(elements) > 4 {
				var value any
				_ = msgpack.Unmarshal(elements[4], &value)
				if resultKind == 1 {
					item.failure, _ = value.(string)
				} else {
					item.result = value
				}
			}
			out = append(out, item)
		}
	}
	return out, nil
}

// expect waits for a callback with the given target and returns it.
func (c *client) expect(target string) signalr.Message {
	c.t.Helper()
	for i, m := range c.pending {
		if m.Target == target {
			c.pending = append(c.pending[:i], c.pending[i+1:]...)
			return m
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for {
		for _, element := range c.read(ctx) {
			if element.callback == nil {
				continue
			}
			if element.callback.Target == target {
				return *element.callback
			}
			c.pending = append(c.pending, *element.callback)
		}
	}
}

func varint(value int) []byte {
	var out []byte
	for {
		b := byte(value & 0x7F)
		value >>= 7
		if value != 0 {
			out = append(out, b|0x80)
			continue
		}
		return append(out, b)
	}
}

func readVarint(data []byte) (value, n int) {
	var shift uint
	for n < len(data) {
		b := data[n]
		n++
		value |= int(b&0x7F) << shift
		if b&0x80 == 0 {
			return
		}
		shift += 7
	}
	return
}

// roomPayload is the exact array the real client sent in a captured CreateRoom call (see the Python tests).
func roomPayload() []any {
	return []any{
		0, 0,
		[]any{"chisadin_2's awesome room", 0, "", 1, 0, 0, false, nil},
		[]any{}, nil, nil,
		[]any{[]any{0, 0, 5542525, "a7882ec3260c99c93d243bbdb3defd3e", 0, []any{}, []any{}, false, 0, nil, 4.489249268165526, true}},
		[]any{}, 0,
	}
}

func TestCreateRoomWithTheClientsRealPayload(t *testing.T) {
	e := newEnv(t)
	c := e.connect(t, 42)

	result, failure := c.call("CreateRoom", roomPayload())
	if failure != "" {
		t.Fatalf("CreateRoom failed: %s", failure)
	}

	array, ok := result.([]any)
	if !ok || len(array) != 9 {
		t.Fatalf("result must be a 9 element MultiplayerRoom array: %#v", result)
	}
	host, _ := array[4].([]any)
	users, _ := array[3].([]any)
	playlist, _ := array[6].([]any)
	if len(host) != 9 || len(users) != 1 || len(playlist) != 1 {
		t.Fatalf("host/users/playlist wrong: %#v", array)
	}
	if id := toInt(host[0]); id != 42 {
		t.Fatalf("host id = %d", id)
	}
	settings := array[2].([]any)
	if settings[0] != "chisadin_2's awesome room" || toInt(settings[1]) != toInt(playlist[0].([]any)[0]) {
		t.Fatalf("settings must point at the server assigned item id: %#v vs %#v", settings, playlist)
	}
	if e.hub.Rooms().Count() != 1 {
		t.Fatal("room not registered")
	}
}

func toInt(v any) int64 {
	switch x := v.(type) {
	case int8:
		return int64(x)
	case int16:
		return int64(x)
	case int32:
		return int64(x)
	case int64:
		return x
	case uint8:
		return int64(x)
	case uint16:
		return int64(x)
	case uint32:
		return int64(x)
	case uint64:
		return int64(x)
	}
	return -1
}

func TestTwoPlayersPlayAMatchThroughTheWire(t *testing.T) {
	e := newEnv(t)
	host := e.connect(t, 1)
	guest := e.connect(t, 2)

	created, failure := host.call("CreateRoom", roomPayload())
	if failure != "" {
		t.Fatal(failure)
	}
	roomID := toInt(created.([]any)[0])

	joined, failure := guest.call("JoinRoom", roomID)
	if failure != "" {
		t.Fatal(failure)
	}
	if len(joined.([]any)[3].([]any)) != 2 {
		t.Fatalf("the guest's snapshot must include both users: %#v", joined)
	}
	userJoined := host.expect("UserJoined")
	var newcomer protocol.RoomUser
	if err := msgpack.Unmarshal(userJoined.Args[0], &newcomer); err != nil || newcomer.UserID != 2 {
		t.Fatalf("host must be told user 2 joined: %+v err=%v", newcomer, err)
	}

	for _, c := range []*client{host, guest} {
		if _, failure := c.call("ChangeBeatmapAvailability", []any{4, nil}); failure != "" {
			t.Fatal(failure)
		}
		if _, failure := c.call("ChangeState", int(protocol.UserReady)); failure != "" {
			t.Fatal(failure)
		}
	}

	if _, failure := guest.call("StartMatch"); !strings.Contains(failure, "NotHostException") {
		t.Fatalf("a guest must not start the match, got %q", failure)
	}
	if _, failure := host.call("StartMatch"); failure != "" {
		t.Fatal(failure)
	}
	host.expect("LoadRequested")
	guest.expect("LoadRequested")

	for _, c := range []*client{host, guest} {
		c.call("ChangeState", int(protocol.UserLoaded))
		c.call("ChangeState", int(protocol.UserReadyForGameplay))
	}
	host.expect("GameplayStarted")
	guest.expect("GameplayStarted")

	for _, c := range []*client{host, guest} {
		if _, failure := c.call("ChangeState", int(protocol.UserFinishedPlay)); failure != "" {
			t.Fatal(failure)
		}
	}
	host.expect("ResultsReady")
	guest.expect("ResultsReady")
}

func TestErrorsReachTheClientInTheFormatItParses(t *testing.T) {
	e := newEnv(t)
	c := e.connect(t, 1)

	_, failure := c.call("ChangeState", int(protocol.UserReady))
	if !strings.Contains(failure, "NotJoinedRoomException: This user has not yet joined a multiplayer room.") {
		t.Fatalf("not in a room: %q", failure)
	}

	c.call("CreateRoom", roomPayload())
	_, failure = c.call("CreateRoom", roomPayload())

	// The client shows what follows the first ':' (GetHubExceptionMessage).
	const prefix = "An unexpected error occurred invoking 'CreateRoom' on the server. InvalidStateException: "
	if failure != prefix+"Can't join a room when already in another room." {
		t.Fatalf("got %q", failure)
	}
	shown := strings.TrimSpace(failure[strings.Index(failure, ":")+1:])
	if shown != "Can't join a room when already in another room." {
		t.Fatalf("client-visible message: %q", shown)
	}

	if _, failure := c.call("NoSuchMethod"); failure == "" {
		t.Fatal("unknown methods must fail")
	}
	if _, failure := c.call("JoinRoomWithPassword"); failure == "" {
		t.Fatal("missing arguments must fail, not crash the connection")
	}
	if _, failure := c.call("LeaveRoom"); failure != "" {
		t.Fatal(failure)
	}
	if _, failure := c.call("LeaveRoom"); failure != "" {
		t.Fatal("leaving twice is a no-op")
	}
}

func TestPasswordProtectedRoom(t *testing.T) {
	e := newEnv(t)
	host := e.connect(t, 1)
	guest := e.connect(t, 2)

	payload := roomPayload()
	payload[2].([]any)[2] = "hunter2"
	created, failure := host.call("CreateRoom", payload)
	if failure != "" {
		t.Fatal(failure)
	}
	roomID := toInt(created.([]any)[0])

	if _, failure := guest.call("JoinRoom", roomID); !strings.Contains(failure, "Invalid password") {
		t.Fatalf("got %q", failure)
	}
	if _, failure := guest.call("JoinRoomWithPassword", roomID, "hunter2"); failure != "" {
		t.Fatal(failure)
	}
}

func TestDisconnectLeavesTheRoomAndPassesHost(t *testing.T) {
	e := newEnv(t)
	host := e.connect(t, 1)
	guest := e.connect(t, 2)

	created, _ := host.call("CreateRoom", roomPayload())
	guest.call("JoinRoom", toInt(created.([]any)[0]))
	host.expect("UserJoined")

	host.ws.Close(websocket.StatusNormalClosure, "bye")

	guest.expect("HostChanged")
	left := guest.expect("UserLeft")
	var who protocol.RoomUser
	if err := msgpack.Unmarshal(left.Args[0], &who); err != nil || who.UserID != 1 {
		t.Fatalf("UserLeft must name the host: %+v err=%v", who, err)
	}

	guest.call("LeaveRoom")
	deadline := time.Now().Add(2 * time.Second)
	for e.hub.Rooms().Count() != 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if e.hub.Rooms().Count() != 0 {
		t.Fatal("the room must be discarded once everybody left")
	}
}

func TestSecondConnectionReplacesTheFirst(t *testing.T) {
	e := newEnv(t)
	first := e.connect(t, 1)
	other := e.connect(t, 2)

	created, _ := first.call("CreateRoom", roomPayload())
	other.call("JoinRoom", toInt(created.([]any)[0]))
	first.expect("UserJoined")

	second := e.connect(t, 1) // same user reconnects (e.g. after a network drop)

	other.expect("UserLeft") // the stale connection's room membership was cleaned up
	if _, failure := second.call("ChangeState", int(protocol.UserReady)); !strings.Contains(failure, "NotJoinedRoomException") {
		t.Fatalf("the new connection starts outside any room: %q", failure)
	}
}

// --- transport security ---------------------------------------------------------------------------------

func TestNegotiateAndWebSocketRequireAValidToken(t *testing.T) {
	e := newEnv(t)
	base := e.server.URL + "/multiplayer"

	post := func(token string) int {
		req, _ := http.NewRequest(http.MethodPost, base+"/negotiate", nil)
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}

	if code := post(""); code != http.StatusUnauthorized {
		t.Fatalf("no token: %d", code)
	}
	if code := post("garbage"); code != http.StatusUnauthorized {
		t.Fatalf("garbage token: %d", code)
	}
	if code := post(e.token(t, "1", func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Minute).Unix() })); code != http.StatusUnauthorized {
		t.Fatalf("expired token: %d", code)
	}
	if code := post(e.token(t, "1", func(c jwt.MapClaims) { c["aud"] = "other-client" })); code != http.StatusUnauthorized {
		t.Fatalf("wrong audience: %d", code)
	}
	if code := post(e.token(t, "1", func(c jwt.MapClaims) { c["type"] = "refresh" })); code != http.StatusUnauthorized {
		t.Fatalf("refresh tokens must not authenticate: %d", code)
	}
	if code := post(e.token(t, "abc", nil)); code != http.StatusUnauthorized {
		t.Fatalf("non numeric subject: %d", code)
	}
	if code := post(e.token(t, "1", nil)); code != http.StatusOK {
		t.Fatalf("valid token: %d", code)
	}

	// A token signed by somebody else must be rejected, even with every claim right.
	other, _ := rsa.GenerateKey(rand.Reader, 2048)
	forged, _ := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
		"sub": "1", "type": "access", "aud": audience, "exp": time.Now().Add(time.Hour).Unix(),
	}).SignedString(other)
	if code := post(forged); code != http.StatusUnauthorized {
		t.Fatalf("forged token: %d", code)
	}

	// `alg: none` and HMAC "confusion" attacks must fail too.
	none, _ := jwt.NewWithClaims(jwt.SigningMethodNone, jwt.MapClaims{
		"sub": "1", "type": "access", "aud": audience, "exp": time.Now().Add(time.Hour).Unix(),
	}).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if code := post(none); code != http.StatusUnauthorized {
		t.Fatalf("alg none: %d", code)
	}
}

func TestAccessTokenInQueryStringIsAccepted(t *testing.T) {
	e := newEnv(t)
	url := "ws" + strings.TrimPrefix(e.server.URL, "http") + "/multiplayer?access_token=" + e.token(t, "9", nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatalf("browsers and some clients pass the token in the query string: %v", err)
	}
	ws.CloseNow()
}

func TestOnlyTheMessagePackProtocolIsOffered(t *testing.T) {
	e := newEnv(t)
	url := "ws" + strings.TrimPrefix(e.server.URL, "http") + "/multiplayer"

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws, _, err := websocket.Dial(ctx, url, &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer " + e.token(t, "1", nil)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer ws.CloseNow()

	_ = ws.Write(ctx, websocket.MessageText, []byte(`{"protocol":"json","version":1}`+"\x1e"))
	_, reply, err := ws.Read(ctx)
	if err != nil || !strings.Contains(string(reply), `"error"`) {
		t.Fatalf("json protocol must be refused with an error message, got %q err=%v", reply, err)
	}
}
