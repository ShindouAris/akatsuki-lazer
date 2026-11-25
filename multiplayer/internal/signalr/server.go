package signalr

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/vmihailenco/msgpack/v5"
)

const (
	keepAliveInterval = 15 * time.Second
	// A client that cannot drain this many queued messages is considered stuck and is disconnected.
	sendQueueSize = 512
)

// HubError is returned by handlers for failures the client should see. Type is the .NET exception name the
// real server would use; the client displays everything after the first colon.
type HubError struct {
	Type    string
	Message string
}

func (e *HubError) Error() string { return e.Type + ": " + e.Message }

// NewHubError creates a HubError.
func NewHubError(errType, message string) *HubError {
	return &HubError{Type: errType, Message: message}
}

// Session is one connected client.
type Session struct {
	ID     string
	UserID int32

	out    chan []byte
	cancel context.CancelFunc
	closed sync.Once
}

// Invoke queues a client callback. It never blocks: a client whose queue is full is disconnected instead
// of stalling the room that is broadcasting to it.
func (s *Session) Invoke(target string, args ...any) {
	data, err := EncodeInvocation(target, args...)
	if err != nil {
		slog.Error("encoding client callback failed", "target", target, "error", err)
		return
	}
	s.enqueue(data)
}

// SendEncoded queues a message that was already built with EncodeInvocation, so one encoding can be shared by
// many sessions (a spectator frame goes to every watcher). The bytes are never modified; do not change them
// afterwards. Like Invoke it never blocks.
func (s *Session) SendEncoded(data []byte) { s.enqueue(data) }

func (s *Session) enqueue(data []byte) {
	select {
	case s.out <- data:
	default:
		slog.Warn("client send queue full; disconnecting", "session", s.ID, "user", s.UserID)
		s.Close()
	}
}

// Close disconnects the client.
func (s *Session) Close() { s.closed.Do(s.cancel) }

// Handler is the hub behind the transport.
type Handler interface {
	// Authenticate returns the user id for a request, or ok=false to reject the connection.
	Authenticate(r *http.Request) (userID int32, ok bool)
	OnConnect(s *Session)
	OnDisconnect(s *Session)
	// Invoke runs a hub method. The returned value is sent back unless hasResult is false.
	Invoke(ctx context.Context, s *Session, method string, args []msgpack.RawMessage) (result any, hasResult bool, err error)
}

// Server serves the negotiate and WebSocket endpoints of one hub.
type Server struct {
	Handler Handler
}

// Negotiate answers POST {hub}/negotiate. Only WebSockets are offered.
func (srv *Server) Negotiate(w http.ResponseWriter, r *http.Request) {
	if _, ok := srv.Handler.Authenticate(r); !ok {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	id := newID()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"connectionId":     id,
		"connectionToken":  id,
		"negotiateVersion": 1,
		"availableTransports": []map[string]any{
			{"transport": "WebSockets", "transferFormats": []string{"Text", "Binary"}},
		},
	})
}

// ServeWS upgrades the request and runs the hub protocol until the client disconnects.
func (srv *Server) ServeWS(w http.ResponseWriter, r *http.Request) {
	userID, ok := srv.Handler.Authenticate(r)
	if !ok {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	ws, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		return
	}
	defer ws.CloseNow()
	ws.SetReadLimit(1 << 20)

	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	if err := handshake(ctx, ws); err != nil {
		slog.Debug("handshake failed", "error", err)
		return
	}

	session := &Session{ID: newID(), UserID: userID, out: make(chan []byte, sendQueueSize), cancel: cancel}

	writerDone := make(chan struct{})
	go func() {
		defer close(writerDone)
		session.writeLoop(ctx, ws)
	}()

	srv.Handler.OnConnect(session)
	defer func() {
		srv.Handler.OnDisconnect(session)
		session.Close()
		<-writerDone
	}()

	srv.readLoop(ctx, ws, session)
}

func handshake(ctx context.Context, ws *websocket.Conn) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	_, data, err := ws.Read(ctx)
	if err != nil {
		return err
	}
	var request struct {
		Protocol string `json:"protocol"`
		Version  int    `json:"version"`
	}
	if err := json.Unmarshal(bytes.TrimRight(data, string(RecordSeparator)), &request); err != nil {
		return fmt.Errorf("bad handshake: %w", err)
	}

	if request.Protocol != "messagepack" {
		reply, _ := json.Marshal(map[string]string{"error": "Only the messagepack protocol is supported."})
		_ = ws.Write(ctx, websocket.MessageText, append(reply, RecordSeparator))
		return errors.New("unsupported protocol " + request.Protocol)
	}
	return ws.Write(ctx, websocket.MessageText, []byte{'{', '}', RecordSeparator})
}

func (s *Session) writeLoop(ctx context.Context, ws *websocket.Conn) {
	ticker := time.NewTicker(keepAliveInterval)
	defer ticker.Stop()

	for {
		var data []byte
		select {
		case <-ctx.Done():
			return
		case data = <-s.out:
		case <-ticker.C:
			data = EncodePing()
		}

		writeCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		err := ws.Write(writeCtx, websocket.MessageBinary, data)
		cancel()
		if err != nil {
			s.Close()
			return
		}
	}
}

func (srv *Server) readLoop(ctx context.Context, ws *websocket.Conn, s *Session) {
	for {
		kind, data, err := ws.Read(ctx)
		if err != nil {
			return
		}
		if kind != websocket.MessageBinary {
			continue
		}

		messages, err := Decode(data)
		if err != nil {
			slog.Warn("dropping malformed frame", "session", s.ID, "error", err)
			return
		}
		for _, message := range messages {
			switch message.Type {
			case TypeInvocation:
				srv.dispatch(ctx, s, message)
			case TypeClose:
				return
			}
		}
	}
}

// dispatch runs invocations one at a time per connection, like SignalR's default.
func (srv *Server) dispatch(ctx context.Context, s *Session, message Message) {
	result, hasResult, err := srv.Handler.Invoke(ctx, s, message.Target, message.Args)

	if message.InvocationID == "" {
		return // fire and forget
	}

	var data []byte
	switch {
	case err != nil:
		data, _ = EncodeError(message.InvocationID, errorText(message.Target, err))
	case hasResult:
		data, err = EncodeResult(message.InvocationID, result)
		if err != nil {
			slog.Error("encoding result failed", "method", message.Target, "error", err)
			data, _ = EncodeError(message.InvocationID, errorText(message.Target, errors.New("internal error")))
		}
	default:
		data, _ = EncodeVoid(message.InvocationID)
	}
	s.enqueue(data)
}

// errorText formats errors the way ASP.NET Core SignalR does, which is what the client's
// GetHubExceptionMessage() expects: everything after the first ':' is shown to the user.
func errorText(method string, err error) string {
	var hubErr *HubError
	if errors.As(err, &hubErr) {
		return fmt.Sprintf("An unexpected error occurred invoking '%s' on the server. %s: %s", method, hubErr.Type, hubErr.Message)
	}
	slog.Error("unhandled hub error", "method", method, "error", err)
	return fmt.Sprintf("An unexpected error occurred invoking '%s' on the server.", method)
}

func newID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}
