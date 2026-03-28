"""Serialization utilities for SignalR protocol with MessagePack.

This module provides helpers for serializing protocol models to/from
MessagePack format compatible with the osu! client.
"""

from __future__ import annotations

from typing import Any
from typing import Protocol
from typing import TypeVar

import msgpack


class MessagePackSerializable(Protocol):
    """Protocol for objects that can be serialized to MessagePack arrays."""

    def to_msgpack(self) -> list: ...


T = TypeVar("T")


def serialize_argument(arg: Any) -> Any:
    """Recursively serialize an argument for SignalR.

    If the argument has a to_msgpack() method, call it.
    Otherwise, return the argument as-is.
    """
    if arg is None:
        return None

    if hasattr(arg, "to_msgpack"):
        return arg.to_msgpack()

    if isinstance(arg, list):
        return [serialize_argument(item) for item in arg]

    if isinstance(arg, dict):
        return {k: serialize_argument(v) for k, v in arg.items()}

    return arg


def serialize_arguments(arguments: list[Any]) -> list[Any]:
    """Serialize a list of arguments for SignalR invocation."""
    return [serialize_argument(arg) for arg in arguments]


def pack_invocation(target: str, arguments: list[Any]) -> bytes:
    """Pack a SignalR invocation message.

    Args:
        target: The method name to invoke
        arguments: The arguments to pass (will be serialized)

    Returns:
        Length-prefixed MessagePack bytes
    """
    serialized_args = serialize_arguments(arguments)
    # SignalR invocation format: [type=1, headers={}, invocationId=null, target, arguments]
    message = [1, {}, None, target, serialized_args]
    packed = msgpack.packb(message)
    return _write_varint(len(packed)) + packed


def pack_completion(invocation_id: str | None, result: Any) -> bytes:
    """Pack a SignalR completion message with a result.

    Args:
        invocation_id: The invocation ID to respond to
        result: The result value (will be serialized)

    Returns:
        Length-prefixed MessagePack bytes
    """
    serialized_result = serialize_argument(result)
    # SignalR completion format: [type=3, headers={}, invocationId, resultKind=3, result]
    # resultKind: 1=error, 2=void, 3=non-void (has result)
    message = [3, {}, invocation_id, 3, serialized_result]
    packed = msgpack.packb(message)
    return _write_varint(len(packed)) + packed


def pack_void_completion(invocation_id: str | None) -> bytes:
    """Pack a SignalR completion message without a result payload."""
    # SignalR completion format: [type=3, headers={}, invocationId, resultKind=2]
    # resultKind: 1=error, 2=void, 3=non-void (has result)
    message = [3, {}, invocation_id, 2]
    packed = msgpack.packb(message)
    return _write_varint(len(packed)) + packed


def pack_ping() -> bytes:
    """Pack a SignalR ping message."""
    packed = msgpack.packb([6])
    return _write_varint(len(packed)) + packed


def unpack_messages(data: bytes) -> list[dict[str, Any]]:
    """Unpack SignalR MessagePack protocol messages.

    Args:
        data: Raw bytes from WebSocket

    Returns:
        List of parsed message dicts
    """
    messages = []
    offset = 0

    while offset < len(data):
        try:
            # Read length prefix
            msg_len, varint_size = _read_varint(data, offset)
            offset += varint_size

            if offset + msg_len > len(data):
                break

            # Unpack the message
            msg_data = data[offset:offset + msg_len]
            offset += msg_len

            unpacked = msgpack.unpackb(msg_data, raw=False, strict_map_key=False)
            if isinstance(unpacked, list) and len(unpacked) > 0:
                msg_type = unpacked[0]
                msg: dict[str, Any] = {"type": msg_type}

                if msg_type == 1 and len(unpacked) >= 5:  # Invocation
                    msg["headers"] = unpacked[1] if len(unpacked) > 1 else {}
                    msg["invocationId"] = unpacked[2] if len(unpacked) > 2 else None
                    msg["target"] = unpacked[3] if len(unpacked) > 3 else ""
                    msg["arguments"] = unpacked[4] if len(unpacked) > 4 else []

                messages.append(msg)

        except Exception:
            break

    return messages


def _read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Read a variable-length integer from bytes."""
    result = 0
    shift = 0
    bytes_read = 0

    while True:
        if offset + bytes_read >= len(data):
            raise ValueError("Incomplete varint")
        byte = data[offset + bytes_read]
        bytes_read += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7

    return result, bytes_read


def _write_varint(value: int) -> bytes:
    """Write an integer as a variable-length integer."""
    result = bytearray()

    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        result.append(byte)
        if not value:
            break

    return bytes(result)
