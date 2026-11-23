package signalr

import (
	"strings"
	"testing"

	"github.com/vmihailenco/msgpack/v5"
)

// clientInvocation builds what the client sends: [1, {}, invocationId|nil, target, args].
func clientInvocation(t *testing.T, invocationID *string, target string, args ...any) []byte {
	t.Helper()
	body, err := msgpack.Marshal([]any{1, map[string]any{}, invocationID, target, args})
	if err != nil {
		t.Fatal(err)
	}
	return append(appendVarint(nil, len(body)), body...)
}

func TestDecodeInvocation(t *testing.T) {
	id := "7"
	messages, err := Decode(clientInvocation(t, &id, "JoinRoom", int64(42)))
	if err != nil || len(messages) != 1 {
		t.Fatalf("messages=%v err=%v", messages, err)
	}
	m := messages[0]
	if m.Type != TypeInvocation || m.InvocationID != "7" || m.Target != "JoinRoom" || len(m.Args) != 1 {
		t.Fatalf("decoded wrongly: %+v", m)
	}
	var roomID int64
	if err := DecodeArg(m.Args[0], &roomID); err != nil || roomID != 42 {
		t.Fatalf("arg = %d err=%v", roomID, err)
	}
}

func TestDecodeFireAndForgetHasNoInvocationID(t *testing.T) {
	messages, err := Decode(clientInvocation(t, nil, "LeaveRoom"))
	if err != nil || len(messages) != 1 || messages[0].InvocationID != "" {
		t.Fatalf("messages=%+v err=%v", messages, err)
	}
}

func TestDecodeSeveralMessagesInOneFrame(t *testing.T) {
	a, b := "1", "2"
	frame := append(clientInvocation(t, &a, "StartMatch"), clientInvocation(t, &b, "AbortMatch")...)
	frame = append(frame, EncodePing()...)

	messages, err := Decode(frame)
	if err != nil || len(messages) != 3 {
		t.Fatalf("messages=%+v err=%v", messages, err)
	}
	if messages[0].Target != "StartMatch" || messages[1].Target != "AbortMatch" || messages[2].Type != TypePing {
		t.Fatalf("wrong order: %+v", messages)
	}
}

// Messages longer than 127 bytes need a multi-byte length prefix.
func TestLargeMessageUsesMultiByteLengthPrefix(t *testing.T) {
	id := "1"
	long := strings.Repeat("x", 5000)
	frame := clientInvocation(t, &id, "CreateRoom", long)
	if frame[0]&0x80 == 0 {
		t.Fatal("expected a continuation bit in the length prefix")
	}

	messages, err := Decode(frame)
	if err != nil || len(messages) != 1 {
		t.Fatalf("err=%v", err)
	}
	var got string
	if err := DecodeArg(messages[0].Args[0], &got); err != nil || got != long {
		t.Fatalf("argument corrupted (len %d) err=%v", len(got), err)
	}
}

func TestDecodeRejectsGarbage(t *testing.T) {
	for name, frame := range map[string][]byte{
		"truncated body":   {0x10, 0x01},
		"unterminated":     {0x80},
		"not msgpack":      {0x02, 0xc1, 0xc1},
		"empty message":    {0x01, 0x90},
		"short invocation": append([]byte{0x04}, mustPack(t, []any{1, map[string]any{}, nil})...)[:4],
	} {
		if _, err := Decode(frame); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

func mustPack(t *testing.T, v any) []byte {
	t.Helper()
	data, err := msgpack.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// unframe strips the length prefix and decodes the array.
func unframe(t *testing.T, data []byte) []any {
	t.Helper()
	size, n, err := readVarint(data)
	if err != nil || size != len(data)-n {
		t.Fatalf("bad prefix: size=%d n=%d len=%d err=%v", size, n, len(data), err)
	}
	var out []any
	if err := msgpack.Unmarshal(data[n:], &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestEncodeInvocationMatchesSignalRLayout(t *testing.T) {
	data, err := EncodeInvocation("UserStateChanged", int32(5), 1)
	if err != nil {
		t.Fatal(err)
	}
	got := unframe(t, data)

	if len(got) != 5 || got[0] != int8(1) || got[2] != nil || got[3] != "UserStateChanged" {
		t.Fatalf("layout wrong: %#v", got)
	}
	if args := got[4].([]any); len(args) != 2 {
		t.Fatalf("args wrong: %#v", args)
	}
}

func TestEncodeInvocationWithoutArgsHasEmptyArray(t *testing.T) {
	data, _ := EncodeInvocation("LoadRequested")
	if args, ok := unframe(t, data)[4].([]any); !ok || len(args) != 0 {
		t.Fatalf("a nil args slice would encode as null and break the client: %#v", unframe(t, data)[4])
	}
}

func TestEncodeCompletions(t *testing.T) {
	result, _ := EncodeResult("9", "value")
	if got := unframe(t, result); got[0] != int8(3) || got[2] != "9" || got[3] != int8(3) || got[4] != "value" {
		t.Fatalf("result completion: %#v", got)
	}

	void, _ := EncodeVoid("9")
	if got := unframe(t, void); len(got) != 4 || got[3] != int8(2) {
		t.Fatalf("void completion: %#v", got)
	}

	failure, _ := EncodeError("9", "boom")
	if got := unframe(t, failure); got[3] != int8(1) || got[4] != "boom" {
		t.Fatalf("error completion: %#v", got)
	}
}

func TestVarintRoundTrip(t *testing.T) {
	for _, value := range []int{0, 1, 127, 128, 300, 16383, 16384, 1 << 20} {
		got, n, err := readVarint(appendVarint(nil, value))
		if err != nil || got != value || n < 1 {
			t.Errorf("%d: got %d (n=%d) err=%v", value, got, n, err)
		}
	}
}
