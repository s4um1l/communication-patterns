"""Multiplexing client -- sends interleaved frames over one WebSocket.

Demonstrates true multiplexing: chat messages, order status checks, and
location updates are sent over a SINGLE WebSocket connection, interleaved
in time. Each message is framed with a stream_id and type so the server
can demultiplex to the correct handler.

Run:
    # First start the server:
    uv run python -m chapters.ch09_multiplexing.demux_handler

    # Then run this client:
    uv run python -m chapters.ch09_multiplexing.client
"""

from __future__ import annotations

import asyncio
import struct
import time

import websockets

from chapters.ch09_multiplexing.mux_protocol import (
    Frame,
    StreamType,
    STREAM_NAMES,
    HEADER_FORMAT,
    HEADER_SIZE,
)

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

SERVER_URL = "ws://localhost:8009/ws"

# Colors for terminal output (ANSI escape codes)
BLUE = "\033[94m"    # Chat
GREEN = "\033[92m"   # Order status
ORANGE = "\033[93m"  # Location
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

STREAM_COLORS = {
    StreamType.CHAT: BLUE,
    StreamType.ORDER_STATUS: GREEN,
    StreamType.LOCATION: ORANGE,
}


# ---------------------------------------------------------------------------
# Frame inspection -- shows the binary-level details
# ---------------------------------------------------------------------------

def inspect_frame(frame: Frame, direction: str = "SEND") -> None:
    """Print frame-level details showing the multiplexing in action."""
    color = STREAM_COLORS.get(frame.stream_type, RESET)
    stream_name = STREAM_NAMES.get(frame.stream_type, "???")
    encoded = frame.encode()

    # Parse out the header bytes for display
    sid, stype, length = struct.unpack(HEADER_FORMAT, encoded[:HEADER_SIZE])

    header_hex = encoded[:HEADER_SIZE].hex()
    payload_preview = frame.payload[:60].decode("utf-8", errors="replace")
    if len(frame.payload) > 60:
        payload_preview += "..."

    print(
        f"  {color}[{direction}] {stream_name:12s}{RESET} | "
        f"stream_id={sid:<3d} type={stype} len={length:<5d} | "
        f"{DIM}header=[{header_hex}]{RESET} | "
        f"{payload_preview}"
    )


# ---------------------------------------------------------------------------
# The interleaved message sequence
# ---------------------------------------------------------------------------

# Each tuple: (stream_id, stream_type, payload_dict, delay_seconds)
# The delays simulate real-world timing where different streams send at
# different rates. Notice how they interleave -- chat, then order, then
# location, then chat again. This is the whole point of multiplexing.

MESSAGES = [
    # Customer opens the app and sends a chat message
    (1, StreamType.CHAT, {"msg": "Hey, how far are you?"}, 0.0),

    # Immediately checks order status
    (2, StreamType.ORDER_STATUS, {"order_id": "order_mux_01", "action": "check"}, 0.1),

    # Driver's phone sends a location update
    (3, StreamType.LOCATION, {"driver_id": "drv_01", "lat": 40.7130, "lng": -74.0062}, 0.1),

    # Customer sends another chat message (interleaved with other streams!)
    (1, StreamType.CHAT, {"msg": "I'm at the lobby entrance"}, 0.2),

    # Another location update -- driver is moving
    (3, StreamType.LOCATION, {"driver_id": "drv_01", "lat": 40.7135, "lng": -74.0058}, 0.1),

    # Advance the order status
    (2, StreamType.ORDER_STATUS, {"order_id": "order_mux_01", "action": "advance"}, 0.1),

    # More location updates at higher frequency
    (3, StreamType.LOCATION, {"driver_id": "drv_01", "lat": 40.7140, "lng": -74.0055}, 0.05),
    (3, StreamType.LOCATION, {"driver_id": "drv_01", "lat": 40.7145, "lng": -74.0050}, 0.05),

    # Check order status again
    (2, StreamType.ORDER_STATUS, {"order_id": "order_mux_01", "action": "check"}, 0.1),

    # Final chat message
    (1, StreamType.CHAT, {"msg": "Great, I can see you!"}, 0.2),
]


# ---------------------------------------------------------------------------
# Client main loop
# ---------------------------------------------------------------------------

async def run_client():
    """Connect to the server and send interleaved multiplexed frames."""

    print(f"\n{'='*60}")
    print("Chapter 09 -- Multiplexing Client")
    print(f"{'='*60}")
    print(f"Connecting to {SERVER_URL}")
    print(f"Sending {len(MESSAGES)} messages across 3 streams over ONE connection\n")

    total_frames_sent = 0
    total_frames_received = 0
    total_bytes_sent = 0
    total_bytes_received = 0
    start_time = time.time()

    async with websockets.connect(SERVER_URL) as ws:
        print(f"{BOLD}Connected! Single WebSocket carrying 3 logical streams.{RESET}\n")
        print(f"{'='*60}")
        print(f"{'Frame Log':^60s}")
        print(f"{'='*60}\n")

        for stream_id, stream_type, payload, delay in MESSAGES:
            if delay > 0:
                await asyncio.sleep(delay)

            # Create and send the frame
            frame = Frame.from_json(stream_id, stream_type, payload)
            encoded = frame.encode()
            await ws.send(encoded)
            total_frames_sent += 1
            total_bytes_sent += len(encoded)

            # Show what we sent
            inspect_frame(frame, "SEND")

            # Read the response
            response_data = await ws.recv()
            response_frame = Frame.decode(response_data)
            total_frames_received += 1
            total_bytes_received += len(response_data)

            # Show what we received
            inspect_frame(response_frame, "RECV")
            print()

    elapsed = time.time() - start_time

    # ---------------------------------------------------------------------------
    # Summary -- show the multiplexing advantage
    # ---------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"{'Session Summary':^60s}")
    print(f"{'='*60}")
    print(f"  Connections used:     {BOLD}1{RESET} (would be 3 without muxing)")
    print(f"  Frames sent:          {total_frames_sent}")
    print(f"  Frames received:      {total_frames_received}")
    print(f"  Total bytes sent:     {total_bytes_sent:,} bytes")
    print(f"  Total bytes received: {total_bytes_received:,} bytes")
    print(f"  Header overhead:      {total_frames_sent * HEADER_SIZE} bytes "
          f"({HEADER_SIZE} bytes/frame x {total_frames_sent} frames)")
    print(f"  Elapsed time:         {elapsed:.2f}s")
    print()

    # Compare with separate connections
    print(f"  {BOLD}Without multiplexing (3 connections):{RESET}")
    print(f"    TCP handshakes:     3 (150ms each @ 100ms RTT = 450ms)")
    print(f"    TLS handshakes:     3 (100ms each = 300ms)")
    print(f"    Socket buffers:     3 x 87KB = 261 KB")
    print(f"    Setup overhead:     750ms total")
    print()
    print(f"  {BOLD}With multiplexing (1 connection):{RESET}")
    print(f"    TCP handshakes:     1 (150ms)")
    print(f"    TLS handshakes:     1 (100ms)")
    print(f"    Socket buffers:     1 x 87KB = 87 KB")
    print(f"    Setup overhead:     250ms total")
    print(f"    Frame overhead:     {total_frames_sent * HEADER_SIZE} bytes total")
    print()
    print(f"  {GREEN}Savings: 500ms setup, 174 KB memory, 2 fewer TCP connections{RESET}")
    print(f"  {GREEN}Cost:    {total_frames_sent * HEADER_SIZE} bytes of frame headers{RESET}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run_client())
