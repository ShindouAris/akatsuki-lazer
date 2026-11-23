package room

import (
	"akatsuki/multiplayer/internal/protocol"
)

// MatchController takes a room's match out of its host's hands: a server-side state machine decides what
// happens and when (ranked play). Rooms without one behave as ordinary head-to-head rooms. Every method runs
// with the room lock held, so implementations must only use the room's unexported, lock-free helpers.
type MatchController interface {
	// initialise runs once, when the room has been created.
	initialise()
	// userCanJoin reports whether a user may enter the room.
	userCanJoin(userID int32) bool
	// userJoined runs after a user was added and announced.
	userJoined(user *protocol.RoomUser)
	// userLeft runs after a user was removed. The value is a copy of the user as it was.
	userLeft(user protocol.RoomUser)
	// userStateChanged runs whenever a user's state or beatmap availability changed.
	userStateChanged(user *protocol.RoomUser)
	// gameplayCompleted runs when the match that was started has ended (or could not start).
	gameplayCompleted()
	// matchState is the room's MatchRoomState, as sent to clients.
	matchState() protocol.Union
}

// IsRankedPlay reports whether the room is a ranked play room.
func (r *Room) IsRankedPlay() bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	_, ok := r.controller.(*rankedPlay)
	return ok
}

// rankedPlayFor returns the ranked play controller for an operation by a user in the room.
func (r *Room) rankedPlayFor(userID int32) (*rankedPlay, error) {
	if r.user(userID) == nil {
		return nil, invalidState("User is not in the room.")
	}

	controller, ok := r.controller.(*rankedPlay)
	if !ok {
		return nil, invalidState("This is not a ranked play room.")
	}
	return controller, nil
}

// RankedPlayDiscardCards discards cards from the user's hand and draws as many to replace them.
func (r *Room) RankedPlayDiscardCards(userID int32, cards []protocol.CardItem) error {
	r.lock()
	defer r.unlock()

	controller, err := r.rankedPlayFor(userID)
	if err != nil {
		return err
	}
	return controller.stage.discardCards(userID, cards)
}

// RankedPlayPlayCard plays a card from the user's hand; only the active player can.
func (r *Room) RankedPlayPlayCard(userID int32, card protocol.CardItem) error {
	r.lock()
	defer r.unlock()

	controller, err := r.rankedPlayFor(userID)
	if err != nil {
		return err
	}
	return controller.stage.playCard(userID, card)
}

// HandleMatchRequest handles a MatchUserRequest the client sent with SendMatchRequest. key is the request's
// union key and request its decoded payload (nil for kinds that are not decoded).
func (r *Room) HandleMatchRequest(userID int32, key int, request any) error {
	r.lock()
	defer r.unlock()

	// Ordinary rooms only understand the start timer requests.
	if _, ranked := r.controller.(*rankedPlay); !ranked {
		return r.handleTimerRequest(userID, request)
	}

	controller, err := r.rankedPlayFor(userID)
	if err != nil {
		return err
	}

	controller.userRequest(userID, key, request)
	return nil
}
