"""Chapter 05 — WebSockets: FoodDash Chat Server

This server demonstrates WebSocket-based bidirectional communication.
The key endpoint is the WebSocket chat:

    ws://localhost:8005/ws/chat/{order_id}?role=customer&name=Alice

After the HTTP upgrade handshake, the connection becomes a persistent,
full-duplex channel. Both the client and server can send messages at
any time without waiting for the other side.

Key things to notice:
    - The WebSocket endpoint starts as GET, upgrades to WebSocket (101 Switching Protocols)
    - After upgrade, the connection is NO LONGER HTTP — it's raw WebSocket frames
    - The server holds STATEFUL connections — it knows who is connected to which room
    - Ping/pong keepalive prevents proxy timeouts and detects dead connections
    - The ChatRoomManager is in-process state — if this server dies, all connections die

Run with:
    uv run uvicorn chapters.ch05_websockets.server:app --port 8005
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

from chapters.ch05_websockets.chat_room import (
    ChatRoomManager,
    ConnectedClient,
)

# ---------------------------------------------------------------------------
# App, database & chat room manager
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch05 WebSockets",
    description=(
        "Driver-customer chat over WebSocket. Demonstrates full-duplex "
        "communication: both sides send messages independently over a "
        "single persistent connection."
    ),
    version="0.1.0",
)

db = DB()
chat_manager = ChatRoomManager()

# Ping interval for keepalive (seconds)
PING_INTERVAL = 25


# ---------------------------------------------------------------------------
# Request / response schemas (for the REST endpoints)
# ---------------------------------------------------------------------------


class PlaceOrderRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, examples=["Alice"])
    restaurant_id: str = Field(..., examples=["rest_01"])
    item_ids: list[str] = Field(..., min_length=1, examples=[["item_01", "item_02"]])


class PlaceOrderResponse(BaseModel):
    order_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float
    chat_url: str  # WebSocket URL for chat


class OrderResponse(BaseModel):
    order_id: str
    customer_name: str
    restaurant_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float
    updated_at: float
    chat_url: str


# ---------------------------------------------------------------------------
# REST endpoints (for order management — same as earlier chapters)
# ---------------------------------------------------------------------------


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order. Returns order details including the WebSocket chat URL."""
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"Restaurant '{req.restaurant_id}' not found")

    menu_by_id: dict[str, MenuItem] = {item.id: item for item in restaurant.menu}
    order_items: list[OrderItem] = []
    for item_id in req.item_ids:
        menu_item = menu_by_id.get(item_id)
        if menu_item is None:
            raise HTTPException(status_code=422, detail=f"Item '{item_id}' not found")
        existing = next((oi for oi in order_items if oi.menu_item.id == item_id), None)
        if existing:
            existing.quantity += 1
        else:
            order_items.append(OrderItem(menu_item=menu_item, quantity=1))

    customer = Customer(name=req.customer_name)
    order = Order(
        customer=customer,
        restaurant_id=req.restaurant_id,
        items=order_items,
        status=OrderStatus.PLACED,
    )
    await db.place_order(order)

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=[
            {"name": oi.menu_item.name, "quantity": oi.quantity, "subtotal_cents": oi.subtotal_cents}
            for oi in order.items
        ],
        total_cents=order.total_cents,
        created_at=order.created_at,
        chat_url=f"ws://localhost:8005/ws/chat/{order.id}",
    )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str) -> OrderResponse:
    """Get order details including chat URL."""
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")
    return OrderResponse(
        order_id=order.id,
        customer_name=order.customer.name,
        restaurant_id=order.restaurant_id,
        status=order.status,
        items=[
            {"name": oi.menu_item.name, "quantity": oi.quantity, "subtotal_cents": oi.subtotal_cents}
            for oi in order.items
        ],
        total_cents=order.total_cents,
        created_at=order.created_at,
        updated_at=order.updated_at,
        chat_url=f"ws://localhost:8005/ws/chat/{order.id}",
    )


# ---------------------------------------------------------------------------
# Chat rooms status (REST endpoint for monitoring)
# ---------------------------------------------------------------------------


@app.get("/chat/rooms")
def list_chat_rooms() -> dict:
    """List all active chat rooms and their participants.

    This is a REST endpoint that reads the stateful chat room data.
    It shows the fundamental tension: WebSocket state is queryable
    via HTTP but lives in-process memory.
    """
    return {
        "active_rooms": chat_manager.active_rooms,
        "total_connections": chat_manager.total_connections,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# WebSocket chat endpoint — the main event
# ---------------------------------------------------------------------------


@app.websocket("/ws/chat/{order_id}")
async def websocket_chat(
    websocket: WebSocket,
    order_id: str,
    role: str = Query(default="customer", pattern="^(customer|driver)$"),
    name: str = Query(default="Anonymous"),
):
    """WebSocket endpoint for driver-customer chat.

    Connection lifecycle:
        1. Client sends HTTP GET with Upgrade: websocket headers
        2. Server responds 101 Switching Protocols
        3. Connection is now WebSocket — full duplex, binary frames
        4. Both sides exchange JSON messages freely
        5. Either side can close with a Close frame

    The connection is STATEFUL — the server holds a reference to this
    specific client's WebSocket for the entire duration. This is
    fundamentally different from REST endpoints where the server
    forgets you after each response.

    Connect with:
        ws://localhost:8005/ws/chat/{order_id}?role=customer&name=Alice
    """
    # --- Step 1: Accept the WebSocket connection ---
    # This is where the HTTP → WebSocket upgrade happens.
    # FastAPI/Starlette handles the 101 Switching Protocols response.
    await websocket.accept()

    # --- Step 2: Register in the chat room ---
    room = chat_manager.get_or_create_room(order_id)
    client = ConnectedClient(websocket=websocket, role=role, name=name)
    await room.add_client(client)

    # --- Step 3: Start ping/pong keepalive task ---
    # This runs in the background and sends WebSocket pings at regular
    # intervals. If the client doesn't respond with a pong, the library
    # will raise an exception and we'll clean up.
    ping_task = asyncio.create_task(_ping_loop(websocket))

    try:
        # --- Step 4: Main message loop ---
        # This is the core of full-duplex communication. We await
        # incoming messages from THIS client. Meanwhile, OTHER clients'
        # messages are delivered to this client via room.broadcast_message()
        # running in their own message loops.
        while True:
            # Wait for a message from this client
            # This is non-blocking (asyncio) — other clients can send
            # and receive messages while we wait here
            data = await websocket.receive_json()

            # Process the message
            msg_type = data.get("type", "chat")
            content = data.get("content", "")

            if msg_type == "chat" and content:
                # Broadcast to all other clients in the room
                await room.broadcast_message(client, content)

            elif msg_type == "typing":
                # Forward typing indicator to other participants
                for other in room.clients:
                    if other is not client:
                        try:
                            await other.websocket.send_json({
                                "type": "typing",
                                "sender_role": client.role,
                                "sender_name": client.name,
                            })
                        except Exception:
                            pass

    except WebSocketDisconnect:
        # Client disconnected (closed tab, network failure, etc.)
        # The Close frame may include a status code and reason
        pass
    except Exception:
        # Unexpected error — log in production, clean up here
        pass
    finally:
        # --- Step 5: Cleanup ---
        # Cancel the ping task and remove the client from the room.
        # This is the "state cleanup" that stateful protocols require.
        # If we don't do this, we leak memory and send to dead sockets.
        ping_task.cancel()
        await room.remove_client(client)

        # If the room is empty, clean it up
        if room.is_empty:
            chat_manager.remove_room(order_id)


async def _ping_loop(websocket: WebSocket) -> None:
    """Send WebSocket pings at regular intervals.

    Why WebSocket-level pings instead of TCP keepalive?
    1. TCP keepalive default is 2 HOURS — way too slow for chat
    2. Intermediate proxies (Nginx, ALB) have 60-120s idle timeouts
    3. Application-level pings let us measure round-trip latency
    4. They detect dead connections faster than TCP keepalive

    The pong is handled automatically by the WebSocket library.
    If no pong is received, the library raises an exception that
    the main message loop catches for cleanup.
    """
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await websocket.send_json({
                "type": "ping",
                "timestamp": time.time(),
            })
    except asyncio.CancelledError:
        # Task was cancelled during cleanup — expected
        pass
    except Exception:
        # WebSocket closed — the main loop will handle cleanup
        pass


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Health check with WebSocket connection stats."""
    return {
        "status": "ok",
        "chapter": "05-websockets",
        "active_chat_rooms": len(chat_manager.active_rooms),
        "total_connections": chat_manager.total_connections,
        "timestamp": time.time(),
    }
