"""Chapter 05 — WebSockets: FoodDash Chat Client

This client demonstrates the WebSocket pattern from the caller's side:

    1. Perform the HTTP → WebSocket upgrade handshake
    2. Send and receive messages over the persistent connection
    3. Show frame-level details (opcode, payload size, masking)
    4. Handle disconnection gracefully

The key difference from HTTP clients (Ch01-Ch03):
    - The connection is PERSISTENT — it stays open for the entire chat session
    - Communication is FULL-DUPLEX — we can send and receive simultaneously
    - There are no HTTP headers per message — just lightweight WebSocket frames

Run with:
    uv run python -m chapters.ch05_websockets.client

Requires the server to be running:
    uv run uvicorn chapters.ch05_websockets.server:app --port 8005
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets
from websockets.frames import Frame

BASE_URL = "http://localhost:8005"
WS_BASE_URL = "ws://localhost:8005"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_header(title: str) -> None:
    width = 70
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_frame_info(direction: str, data: str | bytes) -> None:
    """Print WebSocket frame-level details for educational purposes."""
    is_text = isinstance(data, str)
    payload_bytes = len(data.encode("utf-8")) if is_text else len(data)

    # Determine frame header size
    if payload_bytes <= 125:
        header_bytes = 2  # base header
    elif payload_bytes <= 65535:
        header_bytes = 4  # base + 2 bytes extended length
    else:
        header_bytes = 10  # base + 8 bytes extended length

    # Client-to-server frames are masked (+4 bytes for masking key)
    masked = direction == "SEND"
    if masked:
        header_bytes += 4

    total = header_bytes + payload_bytes

    opcode = "0x1 (text)" if is_text else "0x2 (binary)"

    print(f"  [{direction}] Frame details:")
    print(f"    Opcode:       {opcode}")
    print(f"    FIN:          1 (complete message)")
    print(f"    MASK:         {'1 (client→server)' if masked else '0 (server→client)'}")
    print(f"    Payload len:  {payload_bytes} bytes")
    print(f"    Frame header: {header_bytes} bytes")
    print(f"    Total wire:   {total} bytes")

    # Compare to equivalent HTTP overhead
    http_equivalent = 400 + payload_bytes  # ~400 bytes of HTTP headers minimum
    savings_pct = ((http_equivalent - total) / http_equivalent) * 100
    print(f"    HTTP equiv:   ~{http_equivalent} bytes ({savings_pct:.0f}% savings with WebSocket)")
    print()


def print_message(sender_role: str, sender_name: str, content: str, timestamp: float | None = None) -> None:
    """Pretty-print a chat message."""
    ts = ""
    if timestamp:
        ts = time.strftime("%H:%M:%S", time.localtime(timestamp))
        ts = f"[{ts}] "
    print(f"  {ts}{sender_name} ({sender_role}): {content}")


# ---------------------------------------------------------------------------
# WebSocket chat client
# ---------------------------------------------------------------------------


async def receive_messages(ws, show_frames: bool = True) -> None:
    """Background task: receive and display messages from the WebSocket.

    This runs concurrently with the send loop — true full-duplex.
    While the user is typing a message, incoming messages are still
    received and displayed immediately.
    """
    try:
        async for raw_message in ws:
            if show_frames:
                print_frame_info("RECV", raw_message)

            data = json.loads(raw_message)
            msg_type = data.get("type", "")

            if msg_type == "joined":
                print(f"\n  --- Joined room '{data['room_id']}' as {data['your_role']} ---")
                print(f"  Participants: {data.get('participants', [])}")
                if data.get("history_count", 0) > 0:
                    print(f"  ({data['history_count']} messages in history)")
                print()

            elif msg_type == "history":
                messages = data.get("messages", [])
                if messages:
                    print(f"\n  --- Chat History ({len(messages)} messages) ---")
                    for msg in messages:
                        print_message(
                            msg["sender_role"],
                            msg["sender_name"],
                            msg["content"],
                            msg.get("timestamp"),
                        )
                    print(f"  --- End of History ---\n")

            elif msg_type == "chat":
                print_message(
                    data["sender_role"],
                    data["sender_name"],
                    data["content"],
                    data.get("timestamp"),
                )

            elif msg_type == "system":
                print(f"  [SYSTEM] {data.get('content', '')}")

            elif msg_type == "delivered":
                # Our message was delivered
                pass  # Silently acknowledge

            elif msg_type == "typing":
                print(f"  ... {data.get('sender_name', '?')} is typing ...")

            elif msg_type == "ping":
                # Server-level ping (JSON, not WebSocket ping frame)
                pass

            else:
                print(f"  [UNKNOWN] {data}")

    except websockets.ConnectionClosed as e:
        print(f"\n  Connection closed: code={e.code}, reason='{e.reason}'")
    except Exception as e:
        print(f"\n  Receive error: {e}")


async def chat_session(order_id: str, role: str, name: str) -> None:
    """Run an interactive chat session over WebSocket.

    This demonstrates the full WebSocket lifecycle:
    1. Connect (HTTP upgrade handshake)
    2. Exchange messages (full-duplex)
    3. Disconnect (close handshake)
    """
    ws_url = f"{WS_BASE_URL}/ws/chat/{order_id}?role={role}&name={name}"

    print_header("WebSocket Handshake")
    print(f"\n  Connecting to: {ws_url}")
    print()
    print("  --- Upgrade Request (what the client sends) ---")
    print(f"  GET /ws/chat/{order_id}?role={role}&name={name} HTTP/1.1")
    print(f"  Host: localhost:8005")
    print(f"  Connection: Upgrade")
    print(f"  Upgrade: websocket")
    print(f"  Sec-WebSocket-Version: 13")
    print(f"  Sec-WebSocket-Key: <base64-random-16-bytes>")
    print()

    try:
        async with websockets.connect(ws_url) as ws:
            # Print the response headers from the upgrade
            print("  --- Upgrade Response (what the server sends) ---")
            print(f"  HTTP/1.1 101 Switching Protocols")
            response_headers = ws.response_headers if hasattr(ws, 'response_headers') else {}
            if response_headers:
                for key, value in response_headers.raw_items():
                    print(f"  {key}: {value}")
            print()
            print("  === Connection upgraded! Now speaking WebSocket protocol ===")
            print("  === HTTP is gone. Only frames from here. ===")
            print()

            # Calculate handshake overhead (one-time cost)
            print("  --- Handshake Cost Analysis ---")
            print("  Upgrade request:  ~200 bytes (one time)")
            print("  Upgrade response: ~150 bytes (one time)")
            print("  Total handshake:  ~350 bytes")
            print("  Per-message cost: ~6-20 bytes (vs ~400+ bytes for HTTP)")
            print("  Break-even:       After ~1 message, WebSocket wins on overhead")
            print()

            print_header("Chat Session")
            print(f"\n  You are: {name} ({role})")
            print("  Type messages and press Enter to send.")
            print("  Type 'quit' or 'exit' to disconnect.")
            print("  Type '/info' to see connection details.")
            print()

            # Start the receive loop in the background
            # This is true full-duplex: receiving runs concurrently with sending
            recv_task = asyncio.create_task(receive_messages(ws, show_frames=True))

            # Send loop — read from stdin
            loop = asyncio.get_event_loop()
            msg_count = 0
            total_bytes_sent = 0

            try:
                while True:
                    # Read input without blocking the event loop
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    line = line.strip()

                    if not line:
                        continue

                    if line.lower() in ("quit", "exit"):
                        print("\n  Initiating close handshake...")
                        print("  Sending Close frame: [opcode=0x8, code=1000, reason='Chat ended']")
                        break

                    if line == "/info":
                        print(f"\n  --- Connection Info ---")
                        print(f"  Server: {ws_url}")
                        print(f"  State: {'OPEN' if ws.open else 'CLOSED'}")
                        print(f"  Messages sent: {msg_count}")
                        print(f"  Bytes sent (payload): {total_bytes_sent}")
                        ws_overhead = msg_count * 6  # ~6 bytes frame header per message
                        http_equiv = msg_count * 400  # ~400 bytes HTTP headers per request
                        print(f"  WebSocket frame overhead: ~{ws_overhead} bytes")
                        print(f"  HTTP equivalent overhead: ~{http_equiv} bytes")
                        if http_equiv > 0:
                            print(f"  Overhead savings: {((http_equiv - ws_overhead) / http_equiv * 100):.0f}%")
                        print()
                        continue

                    # Send the message
                    payload = json.dumps({"type": "chat", "content": line})
                    print_frame_info("SEND", payload)
                    await ws.send(payload)

                    msg_count += 1
                    total_bytes_sent += len(line.encode("utf-8"))

            except (KeyboardInterrupt, EOFError):
                print("\n  Interrupted — closing connection...")

            # Cancel the receive task
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

            # Print session summary
            print_header("Session Summary")
            print(f"\n  Messages sent:        {msg_count}")
            print(f"  Payload bytes sent:   {total_bytes_sent}")
            ws_total = total_bytes_sent + (msg_count * 6)  # payload + frame headers
            http_total = msg_count * 400 + total_bytes_sent  # HTTP headers + payload
            print(f"  WebSocket total:      ~{ws_total} bytes (payload + frame headers)")
            print(f"  HTTP equivalent:      ~{http_total} bytes (if each message were a POST)")
            if http_total > 0:
                print(f"  Bandwidth savings:    {((http_total - ws_total) / http_total * 100):.0f}%")
            print(f"\n  The WebSocket connection was ONE persistent TCP connection.")
            print(f"  HTTP would have used {msg_count} separate request-response cycles.")
            print()

    except websockets.exceptions.InvalidHandshake as e:
        print(f"\n  Handshake failed: {e}")
        print("  The server rejected the WebSocket upgrade.")
        print("  Is the server running? Is the endpoint correct?")
    except ConnectionRefusedError:
        print(f"\n  ERROR: Cannot connect to server at {WS_BASE_URL}")
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch05_websockets.server:app --port 8005\n")
    except Exception as e:
        print(f"\n  Connection error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print_header("Chapter 05: WebSocket Chat Client")
    print("\n  FoodDash driver-customer chat over WebSocket.")
    print("  Full-duplex: send and receive messages simultaneously.")
    print()

    # Step 1: Create an order (via REST) so we have something to chat about
    print("  First, let's create an order via REST (good old request-response)...")
    print()

    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            response = await client.post("/orders", json={
                "customer_name": "Alice",
                "restaurant_id": "rest_01",
                "item_ids": ["item_01", "item_02"],
            })
            if response.status_code != 201:
                print(f"  Failed to create order: {response.status_code}")
                print(f"  {response.text}")
                return
            order_data = response.json()
            order_id = order_data["order_id"]
            print(f"  Order created: {order_id}")
            print(f"  Chat URL:     {order_data['chat_url']}")
    except httpx.ConnectError:
        print(f"\n  ERROR: Cannot connect to server at {BASE_URL}")
        print("  Start the server first:")
        print("    uv run uvicorn chapters.ch05_websockets.server:app --port 8005\n")
        return

    # Step 2: Choose role
    print()
    print("  Choose your role:")
    print("    1. Customer (Alice)")
    print("    2. Driver (Bob)")
    print()

    choice = input("  Enter 1 or 2 (default: 1): ").strip()
    if choice == "2":
        role = "driver"
        name = "Bob"
    else:
        role = "customer"
        name = "Alice"

    # Step 3: Start WebSocket chat
    await chat_session(order_id, role, name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Goodbye!")
