// Package hub exposes the room engine as the osu! client's multiplayer SignalR hub
// (IMultiplayerServer: IMultiplayerLoungeServer + IMultiplayerRoomServer).
package hub

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"

	"github.com/vmihailenco/msgpack/v5"

	"akatsuki/multiplayer/internal/protocol"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/signalr"
)

// Authenticator turns an HTTP request into a user id.
type Authenticator interface {
	Authenticate(r *http.Request) (int32, bool)
}

// Hub implements signalr.Handler and room.Events.
type Hub struct {
	auth  Authenticator
	rooms *room.Manager
	// matchmaking is nil until SetMatchmaking; the matchmaking calls then fail.
	matchmaking Matchmaking
	// relations is nil until SetRelations; invitations then skip the restriction and block checks.
	relations room.Relations

	mu       sync.RWMutex
	sessions map[int32]*signalr.Session
	joined   map[int32]*room.Room
}

// New creates a hub. opts configures the rooms it creates.
func New(auth Authenticator, opts room.Options) *Hub {
	h := &Hub{
		auth:     auth,
		sessions: map[int32]*signalr.Session{},
		joined:   map[int32]*room.Room{},
	}
	h.rooms = room.NewManager(h, opts)
	return h
}

// Rooms exposes the room manager (for health checks and tests).
func (h *Hub) Rooms() *room.Manager { return h.rooms }

// Send implements room.Events: it delivers a client callback to one user, if connected.
func (h *Hub) Send(userID int32, target string, args ...any) {
	h.mu.RLock()
	session := h.sessions[userID]
	h.mu.RUnlock()
	if session == nil {
		return
	}
	session.Invoke(target, args...)
}

// Authenticate implements signalr.Handler.
func (h *Hub) Authenticate(r *http.Request) (int32, bool) { return h.auth.Authenticate(r) }

// OnConnect implements signalr.Handler. A user has a single connection: a newer one replaces the old.
func (h *Hub) OnConnect(s *signalr.Session) {
	h.mu.Lock()
	previous := h.sessions[s.UserID]
	h.sessions[s.UserID] = s
	current := h.joined[s.UserID]
	delete(h.joined, s.UserID)
	h.mu.Unlock()

	if previous != nil {
		previous.Close()
		// The old connection's queue and lobby are gone with it; its disconnect is ignored (see OnDisconnect).
		if h.matchmaking != nil {
			h.matchmaking.Disconnected(s.UserID)
		}
		if current != nil {
			_ = h.rooms.Leave(current, s.UserID, s.UserID)
		}
	}
}

// OnDisconnect implements signalr.Handler.
func (h *Hub) OnDisconnect(s *signalr.Session) {
	h.mu.Lock()
	if h.sessions[s.UserID] != s {
		h.mu.Unlock() // already replaced by a newer connection, which handled the room
		return
	}
	delete(h.sessions, s.UserID)
	current := h.joined[s.UserID]
	delete(h.joined, s.UserID)
	h.mu.Unlock()

	if h.matchmaking != nil {
		h.matchmaking.Disconnected(s.UserID)
	}

	if current != nil {
		if err := h.rooms.Leave(current, s.UserID, s.UserID); err != nil {
			slog.Debug("leaving room on disconnect", "user", s.UserID, "error", err)
		}
	}
}

// Invoke implements signalr.Handler.
func (h *Hub) Invoke(ctx context.Context, s *signalr.Session, method string, args []msgpack.RawMessage) (any, bool, error) {
	result, hasResult, err := h.invoke(ctx, s, method, args)
	return result, hasResult, toHubError(err)
}

func (h *Hub) invoke(ctx context.Context, s *signalr.Session, method string, args []msgpack.RawMessage) (any, bool, error) {
	user := s.UserID

	// Matchmaking works with or without being in a room.
	if matchmakingMethods[method] {
		return h.invokeMatchmaking(ctx, user, method, args)
	}

	switch method {
	// --- lounge ---
	case "CreateRoom":
		var request protocol.Room
		if err := arg(args, 0, &request); err != nil {
			return nil, false, err
		}
		return h.createRoom(ctx, user, request)

	case "JoinRoom":
		var roomID int64
		if err := arg(args, 0, &roomID); err != nil {
			return nil, false, err
		}
		return h.joinRoom(user, roomID, "")

	case "JoinRoomWithPassword":
		var roomID int64
		var password string
		if err := arg(args, 0, &roomID); err != nil {
			return nil, false, err
		}
		if err := arg(args, 1, &password); err != nil {
			return nil, false, err
		}
		return h.joinRoom(user, roomID, password)

	case "LeaveRoom":
		current := h.currentRoom(user)
		if current == nil {
			return nil, false, nil // leaving while not in a room is a no-op
		}
		h.clearRoom(user)
		return nil, false, h.rooms.Leave(current, user, user)
	}

	// --- everything below requires being in a room ---
	current := h.currentRoom(user)
	if current == nil {
		return nil, false, room.ErrNotJoined
	}

	switch method {
	case "TransferHost":
		var target int32
		if err := arg(args, 0, &target); err != nil {
			return nil, false, err
		}
		return nil, false, current.TransferHost(user, target)

	case "KickUser":
		var target int32
		if err := arg(args, 0, &target); err != nil {
			return nil, false, err
		}
		err := h.rooms.Kick(current, user, target)
		if err == nil {
			h.clearRoomIf(target, current)
		}
		return nil, false, err

	case "ChangeSettings":
		var settings protocol.Settings
		if err := arg(args, 0, &settings); err != nil {
			return nil, false, err
		}
		return nil, false, current.ChangeSettings(user, settings)

	case "ChangeState":
		var state protocol.UserState
		if err := arg(args, 0, &state); err != nil {
			return nil, false, err
		}
		return nil, false, current.ChangeState(user, state)

	case "ChangeBeatmapAvailability":
		var availability protocol.BeatmapAvailability
		if err := arg(args, 0, &availability); err != nil {
			return nil, false, err
		}
		return nil, false, current.ChangeBeatmapAvailability(user, availability)

	case "ChangeUserStyle":
		var beatmapID, rulesetID *int32
		if err := arg(args, 0, &beatmapID); err != nil {
			return nil, false, err
		}
		if err := arg(args, 1, &rulesetID); err != nil {
			return nil, false, err
		}
		return nil, false, current.ChangeUserStyle(user, beatmapID, rulesetID)

	case "ChangeUserMods":
		var mods []protocol.APIMod
		if err := arg(args, 0, &mods); err != nil {
			return nil, false, err
		}
		return nil, false, current.ChangeUserMods(user, mods)

	case "StartMatch":
		return nil, false, current.StartMatch(user)

	case "AbortMatch":
		return nil, false, current.AbortMatch(user)

	case "AbortGameplay":
		return nil, false, current.AbortGameplay(user)

	case "VoteToSkipIntro":
		return nil, false, current.VoteToSkipIntro(user)

	case "AddPlaylistItem", "EditPlaylistItem":
		var item protocol.PlaylistItem
		if err := arg(args, 0, &item); err != nil {
			return nil, false, err
		}
		if method == "AddPlaylistItem" {
			return nil, false, current.AddPlaylistItem(ctx, user, item)
		}
		return nil, false, current.EditPlaylistItem(ctx, user, item)

	case "RemovePlaylistItem":
		var itemID int64
		if err := arg(args, 0, &itemID); err != nil {
			return nil, false, err
		}
		return nil, false, current.RemovePlaylistItem(user, itemID)

	// --- ranked play (IRankedPlayServer) ---
	case "DiscardCards":
		var cards []protocol.CardItem
		if err := arg(args, 0, &cards); err != nil {
			return nil, false, err
		}
		return nil, false, current.RankedPlayDiscardCards(user, cards)

	case "PlayCard":
		var card protocol.CardItem
		if err := arg(args, 0, &card); err != nil {
			return nil, false, err
		}
		return nil, false, current.RankedPlayPlayCard(user, card)

	case "SendMatchRequest":
		if len(args) == 0 {
			return nil, false, invalidState("Missing argument 0.")
		}
		key, request, err := protocol.DecodeMatchUserRequest(args[0])
		if err != nil {
			return nil, false, invalidState("Invalid argument 0: %v", err)
		}
		return nil, false, current.HandleMatchRequest(user, key, request)

	case "InvitePlayer":
		var target int32
		if err := arg(args, 0, &target); err != nil {
			return nil, false, err
		}
		if h.relations != nil {
			check, err := h.relations.InviteCheck(ctx, user, target)
			if err != nil {
				return nil, false, invalidState("Player not found.")
			}
			if err := room.CheckInvite(check); err != nil {
				return nil, false, err
			}
		}
		return nil, false, current.Invite(user, target)
	}

	return nil, false, &room.Error{Type: "HubException", Message: fmt.Sprintf("Method does not exist: %s", method)}
}

func (h *Hub) createRoom(ctx context.Context, user int32, request protocol.Room) (any, bool, error) {
	if h.currentRoom(user) != nil {
		return nil, false, invalidState("Can't join a room when already in another room.")
	}
	created, snapshot, err := h.rooms.Create(ctx, user, request)
	if err != nil {
		return nil, false, err
	}
	h.setRoom(user, created)
	return snapshot, true, nil
}

func (h *Hub) joinRoom(user int32, roomID int64, password string) (any, bool, error) {
	if h.currentRoom(user) != nil {
		return nil, false, invalidState("Can't join a room when already in another room.")
	}
	joined, snapshot, err := h.rooms.Join(roomID, user, password)
	if err != nil {
		return nil, false, err
	}
	h.setRoom(user, joined)
	return snapshot, true, nil
}

func (h *Hub) currentRoom(user int32) *room.Room {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.joined[user]
}

func (h *Hub) setRoom(user int32, r *room.Room) {
	h.mu.Lock()
	h.joined[user] = r
	h.mu.Unlock()
}

func (h *Hub) clearRoom(user int32) {
	h.mu.Lock()
	delete(h.joined, user)
	h.mu.Unlock()
}

// clearRoomIf forgets the room of a user who was removed from it by someone else (kick).
func (h *Hub) clearRoomIf(user int32, r *room.Room) {
	h.mu.Lock()
	if h.joined[user] == r {
		delete(h.joined, user)
	}
	h.mu.Unlock()
}

func arg(args []msgpack.RawMessage, index int, v any) error {
	if index >= len(args) {
		return invalidState("Missing argument %d.", index)
	}
	if err := signalr.DecodeArg(args[index], v); err != nil {
		return invalidState("Invalid argument %d: %v", index, err)
	}
	return nil
}

func invalidState(format string, args ...any) error {
	return &room.Error{Type: "InvalidStateException", Message: fmt.Sprintf(format, args...)}
}

// toHubError converts room errors into errors the SignalR layer reports to the client.
func toHubError(err error) error {
	var roomErr *room.Error
	if errors.As(err, &roomErr) {
		return signalr.NewHubError(roomErr.Type, roomErr.Message)
	}
	return err
}
