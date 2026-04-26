"""Multiplexing protocol -- framing multiple logical streams over one connection.

This module implements a simple frame-based multiplexing protocol.
Each message is wrapped in a frame with a stream_id, type, and length prefix
so the receiver can route it to the correct handler.

Frame format:
  +-------------------+--------+------------------+---------------------------+
  | stream_id (2B)    | type   | length (2B)      | payload (N bytes)         |
  |                   | (1B)   |                  |                           |
  +-------------------+--------+------------------+---------------------------+

This is the same idea behind HTTP/2 frames, gRPC length-prefixed messages,
and QUIC stream frames -- just stripped to the essentials for education.
"""

from __future__ import annotations

import asyncio
import enum
import json
import struct
from dataclasses import dataclass, field

from fastapi import WebSocket


# ---------------------------------------------------------------------------
# Stream types -- each maps to a FoodDash feature
# ---------------------------------------------------------------------------

class StreamType(int, enum.Enum):
    """Stream types for FoodDash multiplexed connection.

    Each type routes to a different handler on the server side.
    Using int enum so the value fits in a single byte.
    """

    CHAT = 1           # Customer <-> driver chat messages
    ORDER_STATUS = 2   # Order status queries and updates
    LOCATION = 3       # Driver location updates


# Human-readable names for logging
STREAM_NAMES: dict[int, str] = {
    StreamType.CHAT: "CHAT",
    StreamType.ORDER_STATUS: "ORDER_STATUS",
    StreamType.LOCATION: "LOCATION",
}


# ---------------------------------------------------------------------------
# Frame -- the unit of multiplexed communication
# ---------------------------------------------------------------------------

# struct format: ! = network byte order (big-endian)
#   H = unsigned short (2 bytes) for stream_id
#   B = unsigned char  (1 byte)  for type
#   H = unsigned short (2 bytes) for length
HEADER_FORMAT = "!HBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 5 bytes


@dataclass
class Frame:
    """A single multiplexed frame.

    Attributes:
        stream_id: Which logical stream this frame belongs to (0-65535).
        stream_type: The type of stream (CHAT, ORDER_STATUS, LOCATION).
        payload: The actual message data as bytes.
    """

    stream_id: int
    stream_type: StreamType
    payload: bytes

    def encode(self) -> bytes:
        """Serialize this frame into wire format.

        Returns bytes: [stream_id:2][type:1][length:2][payload:N]
        """
        header = struct.pack(
            HEADER_FORMAT,
            self.stream_id,
            self.stream_type.value,
            len(self.payload),
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "Frame":
        """Deserialize a frame from wire format.

        Raises:
            ValueError: If data is too short or length field mismatches.
        """
        if len(data) < HEADER_SIZE:
            raise ValueError(
                f"Frame too short: {len(data)} bytes, need at least {HEADER_SIZE}"
            )

        stream_id, type_byte, length = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )
        payload = data[HEADER_SIZE : HEADER_SIZE + length]

        if len(payload) != length:
            raise ValueError(
                f"Payload length mismatch: header says {length}, got {len(payload)}"
            )

        return cls(
            stream_id=stream_id,
            stream_type=StreamType(type_byte),
            payload=payload,
        )

    @classmethod
    def from_json(cls, stream_id: int, stream_type: StreamType, data: dict) -> "Frame":
        """Convenience: create a frame from a JSON-serializable dict."""
        payload = json.dumps(data).encode("utf-8")
        return cls(stream_id=stream_id, stream_type=stream_type, payload=payload)

    def payload_json(self) -> dict:
        """Convenience: parse the payload as JSON."""
        return json.loads(self.payload.decode("utf-8"))

    def describe(self) -> str:
        """Human-readable description for logging."""
        stream_name = STREAM_NAMES.get(self.stream_type, f"UNKNOWN({self.stream_type})")
        return (
            f"Frame(stream={self.stream_id}, type={stream_name}, "
            f"payload_len={len(self.payload)})"
        )


# ---------------------------------------------------------------------------
# Multiplexer -- sends frames from multiple streams over one connection
# ---------------------------------------------------------------------------

# Type alias for frame handler callbacks
FrameHandler = asyncio.coroutines = None  # just for docs; actual type below


@dataclass
class Multiplexer:
    """Sends messages from multiple logical streams over a single WebSocket.

    Usage:
        mux = Multiplexer(websocket)
        await mux.send(stream_id=1, stream_type=StreamType.CHAT,
                        data={"msg": "Hello!"})
        await mux.send(stream_id=2, stream_type=StreamType.ORDER_STATUS,
                        data={"order_id": "abc123", "action": "check"})

    The multiplexer frames each message and sends it over the shared WebSocket.
    The receiver's Demultiplexer reads the stream_id and routes to the correct
    handler.
    """

    ws: WebSocket
    _frame_count: int = field(default=0, init=False)

    async def send(
        self, stream_id: int, stream_type: StreamType, data: dict
    ) -> Frame:
        """Frame a message and send it over the WebSocket.

        Args:
            stream_id: Logical stream identifier.
            stream_type: Type of stream (CHAT, ORDER_STATUS, LOCATION).
            data: JSON-serializable payload.

        Returns:
            The Frame that was sent (useful for logging/debugging).
        """
        frame = Frame.from_json(stream_id, stream_type, data)
        encoded = frame.encode()
        await self.ws.send_bytes(encoded)
        self._frame_count += 1
        return frame

    @property
    def frames_sent(self) -> int:
        return self._frame_count


# ---------------------------------------------------------------------------
# Demultiplexer -- receives frames and routes to stream-specific handlers
# ---------------------------------------------------------------------------

@dataclass
class Demultiplexer:
    """Receives multiplexed frames from a WebSocket and routes to handlers.

    Register a handler for each stream type. When a frame arrives, the demux
    parses it and calls the appropriate handler based on stream_type.

    Usage:
        demux = Demultiplexer(websocket)
        demux.register(StreamType.CHAT, handle_chat)
        demux.register(StreamType.ORDER_STATUS, handle_order_status)
        demux.register(StreamType.LOCATION, handle_location)
        await demux.run()  # blocks, processing frames until connection closes
    """

    ws: WebSocket
    _handlers: dict[StreamType, object] = field(default_factory=dict, init=False)
    _frame_count: int = field(default=0, init=False)

    def register(self, stream_type: StreamType, handler) -> None:
        """Register an async handler for a stream type.

        The handler signature must be: async def handler(frame: Frame) -> dict | None
        If the handler returns a dict, it is sent back as a response frame.
        """
        self._handlers[stream_type] = handler

    async def run(self) -> None:
        """Main loop: receive frames and route to handlers.

        Runs until the WebSocket connection closes or an error occurs.
        """
        try:
            while True:
                data = await self.ws.receive_bytes()
                frame = Frame.decode(data)
                self._frame_count += 1

                handler = self._handlers.get(frame.stream_type)
                if handler is None:
                    print(
                        f"  [DEMUX] No handler for stream type "
                        f"{frame.stream_type}, dropping frame"
                    )
                    continue

                # Call the handler and optionally send a response
                result = await handler(frame)
                if result is not None:
                    response_frame = Frame.from_json(
                        stream_id=frame.stream_id,
                        stream_type=frame.stream_type,
                        data=result,
                    )
                    await self.ws.send_bytes(response_frame.encode())

        except Exception as exc:
            # WebSocket closure or unexpected error
            print(f"  [DEMUX] Connection ended: {exc}")

    @property
    def frames_received(self) -> int:
        return self._frame_count
