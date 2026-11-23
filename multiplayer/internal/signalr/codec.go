// Package signalr implements the subset of the ASP.NET Core SignalR protocol that the osu! client uses:
// the JSON handshake followed by the MessagePack hub protocol over a WebSocket.
//
// MessagePack hub messages are arrays prefixed by a varint length; one WebSocket frame may carry several.
package signalr

import (
	"bytes"
	"errors"
	"fmt"

	"github.com/vmihailenco/msgpack/v5"
)

// Hub protocol message types.
const (
	TypeInvocation = 1
	TypeCompletion = 3
	TypePing       = 6
	TypeClose      = 7
)

// Completion result kinds.
const (
	resultError = 1
	resultVoid  = 2
	resultValue = 3
)

// RecordSeparator terminates JSON handshake messages.
const RecordSeparator = '\x1e'

// Message is a decoded incoming hub message.
type Message struct {
	Type         int
	InvocationID string // empty for fire-and-forget invocations
	Target       string
	Args         []msgpack.RawMessage
}

// Decode splits a binary frame into hub messages. Unknown message types are returned with only Type set.
func Decode(frame []byte) ([]Message, error) {
	var messages []Message
	for offset := 0; offset < len(frame); {
		size, n, err := readVarint(frame[offset:])
		if err != nil {
			return messages, err
		}
		offset += n
		if size > len(frame)-offset {
			return messages, errors.New("signalr: truncated message")
		}
		message, err := decodeOne(frame[offset : offset+size])
		if err != nil {
			return messages, err
		}
		messages = append(messages, message)
		offset += size
	}
	return messages, nil
}

func decodeOne(data []byte) (Message, error) {
	var elements []msgpack.RawMessage
	if err := msgpack.Unmarshal(data, &elements); err != nil {
		return Message{}, fmt.Errorf("signalr: malformed message: %w", err)
	}
	if len(elements) == 0 {
		return Message{}, errors.New("signalr: empty message")
	}

	var message Message
	if err := msgpack.Unmarshal(elements[0], &message.Type); err != nil {
		return Message{}, fmt.Errorf("signalr: bad message type: %w", err)
	}
	if message.Type != TypeInvocation {
		return message, nil
	}
	if len(elements) < 5 {
		return Message{}, errors.New("signalr: invocation too short")
	}

	// [1, headers, invocationId|nil, target, arguments, streamIds?]
	// The msgpack library hands back an empty RawMessage for nil elements, so empty means "null".
	if len(elements[2]) > 0 {
		var invocationID *string
		if err := msgpack.Unmarshal(elements[2], &invocationID); err != nil {
			return Message{}, fmt.Errorf("signalr: bad invocation id: %w", err)
		}
		if invocationID != nil {
			message.InvocationID = *invocationID
		}
	}
	if err := msgpack.Unmarshal(elements[3], &message.Target); err != nil {
		return Message{}, fmt.Errorf("signalr: bad target: %w", err)
	}
	if len(elements[4]) > 0 {
		if err := msgpack.Unmarshal(elements[4], &message.Args); err != nil {
			return Message{}, fmt.Errorf("signalr: bad arguments: %w", err)
		}
	}
	return message, nil
}

// EncodeInvocation builds a server -> client invocation (a client callback).
func EncodeInvocation(target string, args ...any) ([]byte, error) {
	if args == nil {
		args = []any{}
	}
	return frame([]any{TypeInvocation, map[string]any{}, nil, target, args})
}

// EncodeResult builds a completion carrying a return value.
func EncodeResult(invocationID string, result any) ([]byte, error) {
	return frame([]any{TypeCompletion, map[string]any{}, invocationID, resultValue, result})
}

// EncodeVoid builds a completion for a method without a return value.
func EncodeVoid(invocationID string) ([]byte, error) {
	return frame([]any{TypeCompletion, map[string]any{}, invocationID, resultVoid})
}

// EncodeError builds a failed completion.
func EncodeError(invocationID, message string) ([]byte, error) {
	return frame([]any{TypeCompletion, map[string]any{}, invocationID, resultError, message})
}

// EncodePing builds a keep-alive ping.
func EncodePing() []byte {
	data, _ := frame([]any{TypePing})
	return data
}

// DecodeArg decodes one invocation argument into v.
func DecodeArg(arg msgpack.RawMessage, v any) error {
	return msgpack.Unmarshal(arg, v)
}

func frame(message []any) ([]byte, error) {
	var body bytes.Buffer
	enc := msgpack.NewEncoder(&body)
	enc.SetCustomStructTag("msgpack")
	if err := enc.Encode(message); err != nil {
		return nil, err
	}
	out := appendVarint(nil, body.Len())
	return append(out, body.Bytes()...), nil
}

func readVarint(data []byte) (value, n int, err error) {
	var shift uint
	for n < len(data) {
		b := data[n]
		n++
		value |= int(b&0x7F) << shift
		if b&0x80 == 0 {
			return value, n, nil
		}
		shift += 7
		if shift > 28 {
			break
		}
	}
	return 0, 0, errors.New("signalr: bad length prefix")
}

func appendVarint(dst []byte, value int) []byte {
	for {
		b := byte(value & 0x7F)
		value >>= 7
		if value != 0 {
			dst = append(dst, b|0x80)
			continue
		}
		return append(dst, b)
	}
}
