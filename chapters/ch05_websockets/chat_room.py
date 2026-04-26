"""Chapter 05 — WebSockets: Chat Room Manager

This module is the heart of what makes WebSockets fundamentally different
from stateless HTTP. A ChatRoom holds PERSISTENT STATE:

    - Which clients are connected right now
    - Which order/room each client belongs to
    - The message history for that room

With HTTP request-response, the server forgets you exist between requests.
Here, the server maintains a live, bidirectional channel to each client and
knows exactly who is in each room. This statefulness enables real-time chat
but complicates horizontal scaling (see Ch08).

Key design decisions:
    - One ChatRoom per order (driver + customer communicate about a specific delivery)
    - Messages are broadcast to all OTHER participants (not echoed back to sender)
    - History is kept in memory so reconnecting clients can catch up
    - Join/leave events are broadcast as system messages
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import WebSocket


class MessageType(str, Enum):
    """Types of messages that flow through a chat room."""
    CHAT = "chat"           # Regular chat message from a participant
    SYSTEM = "system"       # System event (join, leave, etc.)
    HISTORY = "history"     # Batch of historical messages sent on join


@dataclass
class ChatMessage:
    """A single message in the chat history.

    Stored in memory — in production, you'd persist these to a database
    so they survive server restarts. But for demonstrating the WebSocket
    pattern, in-memory is perfect.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    room_id: str = ""
    sender_role: str = ""       # "customer", "driver", or "system"
    sender_name: str = ""
    content: str = ""
    timestamp: float = field(default_factory=time.time)
    message_type: MessageType = MessageType.CHAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "sender_role": self.sender_role,
            "sender_name": self.sender_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "message_type": self.message_type.value,
        }


@dataclass
class ConnectedClient:
    """A participant currently connected to a chat room.

    This is the per-connection state that makes WebSockets stateful.
    Each client has an identity (role + name) and a live WebSocket.
    If this server dies, all of these connections die with it.
    """
    websocket: WebSocket
    role: str               # "customer" or "driver"
    name: str
    connected_at: float = field(default_factory=time.time)

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.role})"


class ChatRoom:
    """Manages WebSocket clients for a single order's chat room.

    This is the core state management abstraction. It tracks:
    - Connected clients (by their WebSocket reference)
    - Message history (for reconnecting clients)
    - Room lifecycle (creation, join, leave, close)

    In a multi-server deployment, each server would have its own ChatRoom
    instances for its local connections. Cross-server message delivery
    requires an external pub/sub system (Redis, etc.) — see Ch08.
    """

    def __init__(self, room_id: str, max_history: int = 200) -> None:
        self.room_id = room_id
        self.max_history = max_history
        self.clients: list[ConnectedClient] = []
        self.history: list[ChatMessage] = []
        self.created_at: float = time.time()

    @property
    def client_count(self) -> int:
        return len(self.clients)

    @property
    def is_empty(self) -> bool:
        return len(self.clients) == 0

    def get_participants(self) -> list[dict[str, str]]:
        """Return a summary of who's currently in the room."""
        return [
            {"role": c.role, "name": c.name}
            for c in self.clients
        ]

    async def add_client(self, client: ConnectedClient) -> None:
        """Add a client to the room and notify others.

        This is where statefulness happens: the server now holds a reference
        to this client's WebSocket and will maintain it indefinitely.
        """
        self.clients.append(client)

        # Send history to the newly joined client so they can catch up
        if self.history:
            await client.websocket.send_json({
                "type": MessageType.HISTORY.value,
                "messages": [msg.to_dict() for msg in self.history],
            })

        # Notify all OTHER clients that someone joined
        join_msg = ChatMessage(
            room_id=self.room_id,
            sender_role="system",
            sender_name="system",
            content=f"{client.display_name} joined the chat",
            message_type=MessageType.SYSTEM,
        )
        self._append_history(join_msg)
        await self._broadcast(join_msg, exclude=client)

        # Also send the join confirmation to the joining client
        await client.websocket.send_json({
            "type": "joined",
            "room_id": self.room_id,
            "your_role": client.role,
            "your_name": client.name,
            "participants": self.get_participants(),
            "history_count": len(self.history),
        })

    async def remove_client(self, client: ConnectedClient) -> None:
        """Remove a client from the room and notify others.

        After removal, the server no longer holds a reference to this
        client's WebSocket. The connection can be garbage collected.
        """
        if client in self.clients:
            self.clients.remove(client)

        leave_msg = ChatMessage(
            room_id=self.room_id,
            sender_role="system",
            sender_name="system",
            content=f"{client.display_name} left the chat",
            message_type=MessageType.SYSTEM,
        )
        self._append_history(leave_msg)
        await self._broadcast(leave_msg)

    async def broadcast_message(
        self, sender: ConnectedClient, content: str
    ) -> ChatMessage:
        """Broadcast a chat message from one client to all others.

        The sender does NOT receive their own message back (they already
        have it locally). This prevents echo and reduces bandwidth.

        Returns the ChatMessage for logging/confirmation.
        """
        msg = ChatMessage(
            room_id=self.room_id,
            sender_role=sender.role,
            sender_name=sender.name,
            content=content,
            message_type=MessageType.CHAT,
        )
        self._append_history(msg)

        # Broadcast to all clients EXCEPT the sender
        await self._broadcast(msg, exclude=sender)

        # Send delivery confirmation to the sender
        await sender.websocket.send_json({
            "type": "delivered",
            "message_id": msg.id,
            "timestamp": msg.timestamp,
        })

        return msg

    async def _broadcast(
        self, message: ChatMessage, exclude: ConnectedClient | None = None
    ) -> None:
        """Send a message to all connected clients, optionally excluding one.

        This is O(N) where N is the number of clients in the room. For our
        two-person chat (driver + customer), N is always 2. For group chats
        or large rooms, this becomes a performance consideration.

        Failed sends (disconnected clients) are silently caught — the
        disconnect will be handled by the main WebSocket loop.
        """
        payload = {
            "type": message.message_type.value,
            **message.to_dict(),
        }

        dead_clients: list[ConnectedClient] = []

        for client in self.clients:
            if client is exclude:
                continue
            try:
                await client.websocket.send_json(payload)
            except Exception:
                # Client disconnected — mark for removal
                # (don't modify the list while iterating)
                dead_clients.append(client)

        # Clean up dead connections
        for dead in dead_clients:
            self.clients.remove(dead)

    def _append_history(self, message: ChatMessage) -> None:
        """Append a message to history, enforcing the max size.

        In production, you'd write to a database here. The in-memory
        history is just for reconnecting clients to catch up on recent
        messages they missed.
        """
        self.history.append(message)
        if len(self.history) > self.max_history:
            # Drop oldest messages
            self.history = self.history[-self.max_history:]


class ChatRoomManager:
    """Manages all active chat rooms across the server.

    This is the top-level registry. It maps order IDs to ChatRoom instances
    and handles room creation/cleanup.

    In a multi-server deployment, each server has its own ChatRoomManager
    with rooms only for its locally-connected clients. The missing piece
    is cross-server coordination — addressed in Ch08.
    """

    def __init__(self) -> None:
        self._rooms: dict[str, ChatRoom] = {}

    def get_or_create_room(self, order_id: str) -> ChatRoom:
        """Get an existing room or create a new one for this order.

        Rooms are created lazily — the first person to connect for an
        order creates the room.
        """
        if order_id not in self._rooms:
            self._rooms[order_id] = ChatRoom(room_id=order_id)
        return self._rooms[order_id]

    def get_room(self, order_id: str) -> ChatRoom | None:
        """Get a room if it exists, or None."""
        return self._rooms.get(order_id)

    def remove_room(self, order_id: str) -> None:
        """Remove a room (e.g., when the order is delivered and chat ends)."""
        self._rooms.pop(order_id, None)

    def cleanup_empty_rooms(self) -> int:
        """Remove rooms with no connected clients. Returns count removed."""
        empty = [
            room_id for room_id, room in self._rooms.items()
            if room.is_empty
        ]
        for room_id in empty:
            del self._rooms[room_id]
        return len(empty)

    @property
    def active_rooms(self) -> dict[str, dict]:
        """Summary of all active rooms — useful for monitoring."""
        return {
            room_id: {
                "client_count": room.client_count,
                "message_count": len(room.history),
                "participants": room.get_participants(),
                "created_at": room.created_at,
            }
            for room_id, room in self._rooms.items()
        }

    @property
    def total_connections(self) -> int:
        """Total WebSocket connections across all rooms."""
        return sum(room.client_count for room in self._rooms.values())
