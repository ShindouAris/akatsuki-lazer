package room

import (
	"context"
	"log/slog"
	"strings"

	"akatsuki/multiplayer/internal/protocol"
)

// CreateRankedPlay creates the room of a ranked play match and starts it. Nobody is in the room yet: the players
// join like in any other room, with the returned password, and the match begins once both have.
//
// Clients cannot create these rooms themselves (Create refuses every match type but head to head); the
// matchmaking queue and duels call this.
func (m *Manager) CreateRankedPlay(ctx context.Context, params RankedPlayParams) (*Room, string, error) {
	if len(params.Players) != 2 {
		return nil, "", invalidState("A ranked play match needs exactly two players.")
	}
	if len(params.Deck) == 0 {
		return nil, "", invalidState("A ranked play match needs cards to play.")
	}

	settings := protocol.DefaultSettings()
	settings.Name = strings.TrimSpace(params.Name)
	if settings.Name == "" {
		settings.Name = "Ranked play"
	}
	settings.MatchType = protocol.MatchRankedPlay
	settings.Password = protocol.NewGUID()

	// What the database will hold for the new room: nobody has joined yet and there is no playlist item.
	state := newRoom(0, m.ev, m.opts, settings).stateRecord()

	storeCtx, cancel := context.WithTimeout(ctx, m.opts.StoreTimeout)
	defer cancel()

	created, err := m.store.CreateRoom(storeCtx, NewRoom{State: state})
	if err != nil {
		slog.Error("could not create the ranked play room", "error", err)
		return nil, "", invalidState("Failed to create the room, please try again.")
	}

	r := newRoom(created.RoomID, m.ev, m.opts, settings)
	r.channelID = int32(created.ChannelID)
	r.savedState = &state

	// The client insists on a current playlist item. This empty one only exists in memory until a card is played.
	placeholder := protocol.PlaylistItem{}
	placeholder.Normalise()
	r.playlist = []*protocol.PlaylistItem{&placeholder}
	r.savedItems[placeholder.ID] = r.itemRecord(&placeholder)

	r.controller = newRankedPlay(r, params)

	r.lock()
	r.controller.initialise()
	r.unlock()

	m.mu.Lock()
	m.rooms[r.id] = r
	m.mu.Unlock()

	return r, settings.Password, nil
}
