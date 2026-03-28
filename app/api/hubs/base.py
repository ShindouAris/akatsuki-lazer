"""Shared SignalR hub utilities and base classes.

This module provides common functionality for SignalR WebSocket hubs:
- Connection ID generation
- Negotiate response creation
- Handshake handling
- Message parsing (JSON and MessagePack)
- Ping/pong handling
- Invocation sending
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from fastapi.websockets import WebSocketState

from app.protocol.serialization import pack_invocation
from app.protocol.serialization import pack_ping
from app.protocol.serialization import pack_void_completion
from app.protocol.serialization import serialize_argument
from app.protocol.serialization import unpack_messages

logger = logging.getLogger(__name__)

# SignalR protocol constants
SIGNALR_RECORD_SEPARATOR = "\x1e"


@dataclass
class SignalRConnection:
    """Base connection tracking for SignalR hubs."""

    connection_id: str
    websocket: WebSocket
    user_id: int = 0
    use_messagepack: bool = False


def generate_connection_id() -> str:
    """Generate a unique connection ID."""
    return secrets.token_urlsafe(16)


def create_negotiate_response() -> dict:
    """Create a SignalR negotiate response."""
    connection_id = generate_connection_id()
    return {
        "connectionId": connection_id,
        "connectionToken": connection_id,
        "negotiateVersion": 1,
        "availableTransports": [
            {
                "transport": "WebSockets",
                "transferFormats": ["Text", "Binary"],
            },
        ],
    }


def extract_access_token(websocket: WebSocket) -> str | None:
    """Extract bearer token from websocket headers or query parameters."""
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    return websocket.query_params.get("access_token") or websocket.query_params.get("token")


async def handle_handshake(websocket: WebSocket) -> tuple[bool, bool]:
    """Handle SignalR handshake.

    Returns:
        Tuple of (success, use_messagepack)
    """
    message = await websocket.receive()

    if "text" in message:
        handshake_data = message["text"]
    elif "bytes" in message:
        handshake_data = message["bytes"].decode("utf-8")
    else:
        return False, False

    handshake_msg = handshake_data.rstrip(SIGNALR_RECORD_SEPARATOR)

    try:
        handshake = json.loads(handshake_msg)
        use_messagepack = handshake.get("protocol") == "messagepack"
    except json.JSONDecodeError:
        return False, False

    # Send handshake response (empty object means success, always JSON)
    await websocket.send_text("{}" + SIGNALR_RECORD_SEPARATOR)

    return True, use_messagepack


def parse_messages(data: bytes | str, use_messagepack: bool) -> list[dict[str, Any]]:
    """Parse incoming SignalR messages.

    Args:
        data: Raw message data (bytes for MessagePack, str for JSON)
        use_messagepack: Whether to parse as MessagePack or JSON

    Returns:
        List of parsed message dicts
    """
    if use_messagepack:
        if not isinstance(data, bytes):
            return []
        return unpack_messages(data)
    else:
        if not isinstance(data, str):
            return []
        msgs = []
        for msg_str in data.split(SIGNALR_RECORD_SEPARATOR):
            if msg_str:
                try:
                    msgs.append(json.loads(msg_str))
                except json.JSONDecodeError:
                    pass
        return msgs


async def send_ping(websocket: WebSocket, use_messagepack: bool) -> None:
    """Send a SignalR ping message."""
    if use_messagepack:
        await websocket.send_bytes(pack_ping())
    else:
        await websocket.send_text(json.dumps({"type": 6}) + SIGNALR_RECORD_SEPARATOR)


async def send_invocation(
    websocket: WebSocket,
    use_messagepack: bool,
    target: str,
    arguments: list,
) -> None:
    """Send a SignalR invocation to a client."""
    serialized_args = [serialize_argument(arg) for arg in arguments]

    if use_messagepack:
        await websocket.send_bytes(pack_invocation(target, serialized_args))
    else:
        msg = {
            "type": 1,
            "target": target,
            "arguments": serialized_args,
        }
        await websocket.send_text(json.dumps(msg) + SIGNALR_RECORD_SEPARATOR)


async def send_completion(
    websocket: WebSocket,
    use_messagepack: bool,
    invocation_id: str | None,
    result: Any,
) -> None:
    """Send a SignalR completion message with result."""
    from app.protocol.serialization import pack_completion

    if use_messagepack:
        await websocket.send_bytes(pack_completion(invocation_id, result))
    else:
        completion_msg = {
            "type": 3,  # Completion
            "invocationId": invocation_id,
            "result": serialize_argument(result),
        }
        await websocket.send_text(json.dumps(completion_msg) + SIGNALR_RECORD_SEPARATOR)


async def send_void_completion(
    websocket: WebSocket,
    use_messagepack: bool,
    invocation_id: str | None,
) -> None:
    """Send a SignalR completion message without a result payload."""
    if use_messagepack:
        await websocket.send_bytes(pack_void_completion(invocation_id))
    else:
        completion_msg = {
            "type": 3,
            "invocationId": invocation_id,
        }
        await websocket.send_text(json.dumps(completion_msg) + SIGNALR_RECORD_SEPARATOR)


async def run_message_loop(
    websocket: WebSocket,
    use_messagepack: bool,
    message_handler,
    timeout: float = 15.0,
    on_ping=None,
) -> None:
    """Run the main SignalR message loop.

    Args:
        websocket: The WebSocket connection
        use_messagepack: Whether to use MessagePack protocol
        message_handler: Async function(parsed_message) to handle invocations
        timeout: Maximum seconds between server keepalive pings
        on_ping: Optional async function() called on each ping (for TTL refresh etc)
    """
    last_ping_sent_at = time.monotonic()

    while True:
        try:
            if websocket.client_state != WebSocketState.CONNECTED:
                break

            now = time.monotonic()
            if now - last_ping_sent_at >= timeout:
                if on_ping:
                    await on_ping()
                await send_ping(websocket, use_messagepack)
                last_ping_sent_at = time.monotonic()

            elapsed_since_ping = time.monotonic() - last_ping_sent_at
            receive_timeout = max(0.1, timeout - elapsed_since_ping)
            message = await asyncio.wait_for(websocket.receive(), timeout=receive_timeout)

            # Extract data based on protocol
            if use_messagepack:
                if "bytes" not in message:
                    continue
                data = message["bytes"]
            else:
                if "text" not in message:
                    continue
                data = message["text"]

            # Parse messages
            msgs = parse_messages(data, use_messagepack)

            for parsed in msgs:
                msg_type = parsed.get("type")

                if msg_type == 6:  # Ping
                    if on_ping:
                        await on_ping()
                    await send_ping(websocket, use_messagepack)
                    last_ping_sent_at = time.monotonic()

                elif msg_type == 1:  # Invocation
                    await message_handler(parsed)

        except asyncio.TimeoutError:
            # Send keepalive ping
            if on_ping:
                await on_ping()
            try:
                await send_ping(websocket, use_messagepack)
                last_ping_sent_at = time.monotonic()
            except Exception:
                break


def get_online_user_count(connections: dict) -> int:
    """Get count of unique online users from a connections dict."""
    user_ids = {conn.user_id for conn in connections.values() if conn.user_id}
    return len(user_ids)
