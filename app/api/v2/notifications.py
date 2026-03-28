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

logger = logging.getLogger(__name__)
router = APIRouter()

NOTIFICATIONS_KEEPALIVE_TIMEOUT_SECONDS = 30.0


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
        logger.info("Notifications WebSocket connection closed")
