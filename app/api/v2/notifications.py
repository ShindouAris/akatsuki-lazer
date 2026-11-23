"""Notifications endpoints and WebSocket."""

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.websockets import WebSocketState

from app.api.deps import CurrentUser
from app.core.security import decode_token
from app.services.chat import connections

logger = logging.getLogger(__name__)
router = APIRouter()

NOTIFICATIONS_KEEPALIVE_TIMEOUT_SECONDS = 30.0


def _user_id_of(websocket: WebSocket) -> int | None:
    """The user of a notifications socket: the bearer token of the handshake (or ``access_token`` in the query)."""
    header = websocket.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else websocket.query_params.get("access_token")
    data = decode_token(token) if token else None
    return data.user_id if data is not None else None


@router.get("/notifications")
async def get_notifications(user: CurrentUser, request: Request) -> dict:
    """Get user notifications."""
    # Build WebSocket URL for notifications
    # Use ws:// for http:// and wss:// for https://
    scheme = "ws" if request.url.scheme == "http" else "wss"
    host = request.headers.get("host", "localhost:8000")
    notification_endpoint = f"{scheme}://{host}/api/v2/notifications/websocket"

    return {
        "has_more": False,
        "notifications": [],
        "notification_endpoint": notification_endpoint,
    }


@router.post("/notifications/mark-read")
async def mark_notifications_read(user: CurrentUser) -> dict:
    """Mark notifications as read."""
    return {}


@router.websocket("/notifications/websocket")
async def notifications_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time notifications.

    This uses a simple JSON protocol (not SignalR):
    - Messages are JSON objects with: event, data, error
    - No handshake required, just send/receive JSON
    - Server sends periodic ping events while idle
    """
    await websocket.accept()
    logger.info("Notifications WebSocket connected")

    # Who is connected decides who gets chat messages. Without a valid token the socket still works, without chat.
    user_id = _user_id_of(websocket)
    connection = connections.register(user_id, websocket) if user_id is not None else None

    try:
        while True:
            try:
                # Wait for messages with timeout, send keepalive if needed
                if websocket.client_state != WebSocketState.CONNECTED:
                    # WS is vanished into the socket abyss, stop trying to read
                    break

                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=NOTIFICATIONS_KEEPALIVE_TIMEOUT_SECONDS,
                )

                if "text" in message:
                    try:
                        data = json.loads(message["text"])
                        event = data.get("event", "")
                        logger.info(f"Notifications received event: {event}")

                        # Handle different event types
                        if event == "chat.start":
                            # Client wants to start receiving chat messages
                            if connection is not None:
                                connections.start_chat(connection)
                            await websocket.send_text(
                                json.dumps({
                                    "event": "chat.start",
                                    "data": {},
                                }),
                            )
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON in notifications: {message['text']!r}")

            except asyncio.TimeoutError:
                # Send a keepalive/ping to keep connection alive
                if websocket.client_state != WebSocketState.CONNECTED:
                    break

                await websocket.send_text(
                    json.dumps({
                        "event": "ping",
                        "data": {},
                    }),
                )
                logger.debug("Notifications WebSocket keepalive ping sent")

            except RuntimeError as e:
                logger.warning(f"Runtime error in notifications WebSocket: {e}")
                break

            except WebSocketDisconnect:
                logger.info("Notifications WebSocket disconnected")
                break

            except Exception as e:
                logger.exception(f"Error in notifications WebSocket: {e}")
                break

    except WebSocketDisconnect:
        logger.info("Notifications WebSocket disconnected")
    except Exception as e:
        logger.exception(f"Notifications WebSocket error: {e}")
    finally:
        if connection is not None and user_id is not None:
            connections.unregister(user_id, connection)
        logger.info("Notifications WebSocket connection closed")
