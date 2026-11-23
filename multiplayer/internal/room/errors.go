package room

import "fmt"

// Error is a failure the client should see. Type is the .NET exception name used by the original server;
// the client shows the text after the first colon, so Message must be user readable.
type Error struct {
	Type    string
	Message string
}

func (e *Error) Error() string { return e.Type + ": " + e.Message }

func invalidState(format string, args ...any) *Error {
	return &Error{Type: "InvalidStateException", Message: fmt.Sprintf(format, args...)}
}

var (
	// ErrNotHost is returned when a host-only operation is attempted by someone else.
	ErrNotHost = &Error{Type: "NotHostException", Message: "User is attempting to perform a host level operation while not the host"}
	// ErrInvalidPassword is returned for a wrong room password.
	ErrInvalidPassword = &Error{Type: "InvalidPasswordException", Message: "Invalid password"}
	// ErrNotJoined is returned when a room operation is attempted outside a room.
	ErrNotJoined = &Error{Type: "NotJoinedRoomException", Message: "This user has not yet joined a multiplayer room."}
)
