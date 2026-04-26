"""Appendix A — gRPC / Protocol Buffers Demo (pure Python, no grpcio).

This module demonstrates the *concepts* behind gRPC and Protocol Buffers
using only the Python standard library. We simulate:

1. Protobuf-style binary encoding with the `struct` module
2. JSON vs binary size comparison for a FoodDash Order
3. All four gRPC communication patterns with asyncio
4. Serialization speed benchmarks

Run with:
    uv run python -m appendices.appendix_a_grpc.grpc_demo
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import sys

# ── Ensure repo root is on sys.path so we can import shared models ──
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.models import (
    Customer,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
)

# ═══════════════════════════════════════════════════════════════════════
# Part 1: Protobuf-Style Binary Encoding
# ═══════════════════════════════════════════════════════════════════════
#
# Protocol Buffers uses a tag-length-value (TLV) binary encoding.
# Each field is encoded as:
#   [tag byte] [optional length] [value bytes]
#
# The tag byte encodes: (field_number << 3) | wire_type
#   wire_type 0 = varint (int32, int64, bool, enum)
#   wire_type 1 = 64-bit fixed (double, fixed64)
#   wire_type 2 = length-delimited (string, bytes, nested messages)
#   wire_type 5 = 32-bit fixed (float, fixed32)
#
# We simulate this with Python's struct module.

WIRE_TYPE_VARINT = 0
WIRE_TYPE_64BIT = 1
WIRE_TYPE_LENGTH_DELIMITED = 2
WIRE_TYPE_32BIT = 5


def encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a Protobuf field tag byte."""
    return encode_varint((field_number << 3) | wire_type)


def encode_varint(value: int) -> bytes:
    """Encode an integer as a Protobuf varint (variable-length integer).

    Small numbers use fewer bytes:
      0-127:       1 byte
      128-16383:   2 bytes
      16384+:      3+ bytes

    This is one reason Protobuf is compact — most field numbers and small
    integers fit in 1 byte.
    """
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)  # Set continuation bit
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def encode_string(field_number: int, value: str) -> bytes:
    """Encode a string field: tag + length + UTF-8 bytes."""
    encoded = value.encode("utf-8")
    return encode_tag(field_number, WIRE_TYPE_LENGTH_DELIMITED) + encode_varint(len(encoded)) + encoded


def encode_int32(field_number: int, value: int) -> bytes:
    """Encode an int32 field: tag + varint value."""
    if value == 0:
        return b""  # Proto3: default values are not encoded (saves space)
    return encode_tag(field_number, WIRE_TYPE_VARINT) + encode_varint(value)


def encode_int64(field_number: int, value: int) -> bytes:
    """Encode an int64 field as varint."""
    if value == 0:
        return b""
    return encode_tag(field_number, WIRE_TYPE_VARINT) + encode_varint(value)


def encode_enum(field_number: int, value: int) -> bytes:
    """Encode an enum field (same as int32 on the wire)."""
    return encode_int32(field_number, value)


def encode_nested(field_number: int, data: bytes) -> bytes:
    """Encode a nested message: tag + length + message bytes."""
    return encode_tag(field_number, WIRE_TYPE_LENGTH_DELIMITED) + encode_varint(len(data)) + data


# ── Encode a FoodDash Order using our Protobuf simulator ──

def encode_menu_item(item: MenuItem) -> bytes:
    """Encode a MenuItem as Protobuf binary."""
    result = bytearray()
    result += encode_string(1, item.id)
    result += encode_string(2, item.name)
    result += encode_int32(3, item.price_cents)
    if item.description:
        result += encode_string(4, item.description)
    return bytes(result)


def encode_customer(customer: Customer) -> bytes:
    """Encode a Customer as Protobuf binary."""
    result = bytearray()
    result += encode_string(1, customer.id)
    result += encode_string(2, customer.name)
    if customer.address:
        result += encode_string(3, customer.address)
    return bytes(result)


def encode_order_item(item: OrderItem) -> bytes:
    """Encode an OrderItem as Protobuf binary."""
    result = bytearray()
    result += encode_nested(1, encode_menu_item(item.menu_item))
    result += encode_int32(2, item.quantity)
    return bytes(result)


# Map Python OrderStatus enum to Protobuf enum integers
STATUS_TO_PROTO = {
    OrderStatus.PLACED: 1,
    OrderStatus.CONFIRMED: 2,
    OrderStatus.PREPARING: 3,
    OrderStatus.READY: 4,
    OrderStatus.PICKED_UP: 5,
    OrderStatus.EN_ROUTE: 6,
    OrderStatus.DELIVERED: 7,
    OrderStatus.CANCELLED: 8,
}


def encode_order(order: Order) -> bytes:
    """Encode a full Order as Protobuf binary.

    This mirrors what `order.SerializeToString()` would do with real
    protobuf-generated code.
    """
    result = bytearray()
    result += encode_string(1, order.id)
    result += encode_nested(2, encode_customer(order.customer))
    result += encode_string(3, order.restaurant_id)
    for item in order.items:
        result += encode_nested(4, encode_order_item(item))  # repeated field
    result += encode_enum(5, STATUS_TO_PROTO.get(order.status, 0))
    if order.driver_id:
        result += encode_string(6, order.driver_id)
    result += encode_int64(7, int(order.created_at))
    result += encode_int64(8, int(order.updated_at))
    return bytes(result)


# ═══════════════════════════════════════════════════════════════════════
# Part 2: Size Comparison — JSON vs Binary
# ═══════════════════════════════════════════════════════════════════════

def create_sample_order() -> Order:
    """Create a realistic FoodDash order for benchmarking."""
    return Order(
        id="ord_a1b2",
        customer=Customer(id="cust_01", name="Alice", address="742 Evergreen Terrace"),
        restaurant_id="rest_01",
        items=[
            OrderItem(
                menu_item=MenuItem(id="item_01", name="Classic Burger", price_cents=899),
                quantity=2,
            ),
            OrderItem(
                menu_item=MenuItem(id="item_02", name="Fries", price_cents=399),
                quantity=1,
            ),
        ],
        status=OrderStatus.PLACED,
        created_at=1700000000.0,
        updated_at=1700000000.0,
    )


def demo_size_comparison() -> None:
    """Compare JSON and Protobuf-style binary encoding of the same Order."""
    print("=" * 68)
    print("  PART 1: Payload Size Comparison — JSON vs Protobuf")
    print("=" * 68)
    print()

    order = create_sample_order()

    # JSON encoding (what REST + JSON sends)
    json_bytes = order.model_dump_json().encode("utf-8")

    # Protobuf-style binary encoding (what gRPC + Protobuf sends)
    proto_bytes = encode_order(order)

    print(f"  Order: {order.id}")
    print(f"  Customer: {order.customer.name}")
    print(f"  Items: {len(order.items)} ({', '.join(i.menu_item.name for i in order.items)})")
    print()

    print("  JSON encoding:")
    print(f"    Size: {len(json_bytes)} bytes")
    # Show the JSON for readability
    pretty = json.dumps(json.loads(json_bytes), indent=2)
    for line in pretty.split("\n")[:15]:
        print(f"    {line}")
    if pretty.count("\n") > 15:
        print(f"    ... ({pretty.count(chr(10)) - 15} more lines)")
    print()

    print("  Protobuf-style binary encoding:")
    print(f"    Size: {len(proto_bytes)} bytes")
    # Show hex dump (first 60 bytes)
    hex_str = proto_bytes.hex()
    chunks = [hex_str[i:i+2] for i in range(0, min(len(hex_str), 120), 2)]
    for row_start in range(0, len(chunks), 16):
        row = chunks[row_start:row_start+16]
        hex_part = " ".join(row)
        # Show ASCII where printable
        ascii_part = ""
        for h in row:
            b = int(h, 16)
            ascii_part += chr(b) if 32 <= b < 127 else "."
        print(f"    {row_start:04x}  {hex_part:<48s}  {ascii_part}")
    if len(proto_bytes) > 60:
        print(f"    ... ({len(proto_bytes) - 60} more bytes)")
    print()

    ratio = len(json_bytes) / len(proto_bytes)
    saving_pct = (1 - len(proto_bytes) / len(json_bytes)) * 100
    print(f"  Comparison:")
    print(f"    JSON:     {len(json_bytes):>4d} bytes  {'█' * 40}")
    proto_bar_len = int(40 * len(proto_bytes) / len(json_bytes))
    print(f"    Protobuf: {len(proto_bytes):>4d} bytes  {'█' * proto_bar_len}")
    print(f"    Ratio:    {ratio:.1f}x smaller with Protobuf ({saving_pct:.0f}% reduction)")
    print()

    # HTTP header overhead comparison
    http11_headers = (
        "POST /fooddash.OrderService/PlaceOrder HTTP/1.1\r\n"
        "Host: kitchen-service.internal:8080\r\n"
        "Content-Type: application/json\r\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJvcmRlci1zZXJ2aWNlIn0\r\n"
        "Accept: application/json\r\n"
        "User-Agent: FoodDash-OrderService/2.1\r\n"
        "X-Request-ID: 550e8400-e29b-41d4-a716-446655440000\r\n"
        "\r\n"
    )
    http2_headers_first = 120   # Approximate: first request, full headers
    http2_headers_subsequent = 20  # Approximate: HPACK-compressed, only delta

    print("  HTTP header overhead:")
    print(f"    HTTP/1.1 headers:          {len(http11_headers):>4d} bytes (every request)")
    print(f"    HTTP/2 headers (1st req):  {http2_headers_first:>4d} bytes (builds HPACK table)")
    print(f"    HTTP/2 headers (2nd+ req): {http2_headers_subsequent:>4d} bytes (HPACK compressed)")
    print()
    total_rest = len(json_bytes) + len(http11_headers)
    total_grpc = len(proto_bytes) + http2_headers_subsequent
    print(f"  Total on-wire cost (headers + body):")
    print(f"    REST + JSON + HTTP/1.1:     {total_rest:>4d} bytes")
    print(f"    gRPC + Protobuf + HTTP/2:   {total_grpc:>4d} bytes (after 1st request)")
    print(f"    Total savings:              {total_rest / total_grpc:.1f}x smaller")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 3: Serialization Speed Benchmark
# ═══════════════════════════════════════════════════════════════════════

def demo_speed_benchmark() -> None:
    """Benchmark JSON vs Protobuf-style binary serialization speed."""
    print("=" * 68)
    print("  PART 2: Serialization Speed — JSON vs Protobuf-style Binary")
    print("=" * 68)
    print()

    order = create_sample_order()
    iterations = 10_000

    # JSON serialization
    start = time.perf_counter()
    for _ in range(iterations):
        order.model_dump_json().encode("utf-8")
    json_encode_time = time.perf_counter() - start

    json_bytes = order.model_dump_json().encode("utf-8")
    start = time.perf_counter()
    for _ in range(iterations):
        json.loads(json_bytes)
    json_decode_time = time.perf_counter() - start

    # Protobuf-style binary serialization
    start = time.perf_counter()
    for _ in range(iterations):
        encode_order(order)
    proto_encode_time = time.perf_counter() - start

    # Note: we don't implement full Protobuf decoding (that requires
    # generated code or a schema-aware parser). We show encode speed only
    # and note that real Protobuf decode is similarly fast.

    print(f"  Iterations: {iterations:,}")
    print()
    print(f"  Encoding (Python object -> bytes):")
    print(f"    JSON (Pydantic model_dump_json): {json_encode_time*1000:.1f}ms total, "
          f"{json_encode_time/iterations*1_000_000:.1f}us/op")
    print(f"    Protobuf-style (struct):         {proto_encode_time*1000:.1f}ms total, "
          f"{proto_encode_time/iterations*1_000_000:.1f}us/op")
    if json_encode_time > proto_encode_time:
        print(f"    Binary is {json_encode_time/proto_encode_time:.1f}x faster at encoding")
    else:
        print(f"    JSON is {proto_encode_time/json_encode_time:.1f}x faster at encoding")
        print(f"    (Note: real protobuf-generated code is typically 5-10x faster than JSON)")
    print()
    print(f"  Decoding (bytes -> Python object):")
    print(f"    JSON (json.loads):               {json_decode_time*1000:.1f}ms total, "
          f"{json_decode_time/iterations*1_000_000:.1f}us/op")
    print(f"    Protobuf (real):                 ~{json_decode_time/5*1000:.1f}ms estimated "
          f"(typically 5x faster, using compiled C extension)")
    print()
    print("  Note: Our pure-Python binary encoder simulates Protobuf's wire format")
    print("  but lacks the C-extension optimization of the real protobuf library.")
    print("  Real Protobuf (compiled) is typically 5-10x faster than JSON for both")
    print("  encoding and decoding.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 4: Simulated gRPC Communication Patterns
# ═══════════════════════════════════════════════════════════════════════
#
# These simulations use asyncio queues to model gRPC streams.
# No actual network calls — we're demonstrating the communication
# patterns, not the transport.

async def demo_unary_rpc() -> None:
    """Pattern 1: Unary RPC — one request, one response.

    Like Ch01 request-response, but imagine the payloads are binary
    Protobuf over HTTP/2 instead of JSON over HTTP/1.1.
    """
    print("-" * 68)
    print("  Pattern 1: Unary RPC (Request-Response)")
    print("-" * 68)
    print()

    # Simulate: Order Service calls Kitchen Service
    async def kitchen_service(request: dict) -> dict:
        """Simulated gRPC server handler."""
        await asyncio.sleep(0.05)  # Simulate network + processing
        return {
            "order_id": request["order_id"],
            "status": "confirmed",
            "message": f"Kitchen received order {request['order_id']}",
        }

    request = {"order_id": "ord_a1b2", "customer": "Alice", "items": ["Classic Burger x2", "Fries x1"]}
    print(f"  Client -> Server: PlaceOrder({request['order_id']})")

    response = await kitchen_service(request)
    print(f"  Server -> Client: {response}")
    print()
    print("  This is identical to REST request-response, but:")
    print("    - Payload is ~4x smaller (Protobuf binary)")
    print("    - Headers are HPACK-compressed (~20 bytes vs ~400 bytes)")
    print("    - Multiplexed on shared HTTP/2 connection")
    print()


async def demo_server_streaming_rpc() -> None:
    """Pattern 2: Server-Streaming RPC — one request, stream of responses.

    Like Ch04 SSE, but with typed Protobuf messages and flow control.
    """
    print("-" * 68)
    print("  Pattern 2: Server-Streaming RPC (like SSE)")
    print("-" * 68)
    print()

    status_updates = [
        ("confirmed", "Restaurant accepted your order", 0.3),
        ("preparing", "Kitchen is making your food", 0.5),
        ("ready", "Your food is ready for pickup", 0.4),
        ("picked_up", "Driver Bob picked up your order", 0.3),
        ("en_route", "Driver Bob is on the way (ETA: 12 min)", 0.6),
        ("delivered", "Your food has been delivered!", 0.0),
    ]

    async def stream_order_updates(order_id: str):
        """Simulated server-streaming RPC."""
        for status, message, delay in status_updates:
            await asyncio.sleep(delay * 0.1)  # Speed up for demo
            yield {"order_id": order_id, "status": status, "message": message}

    print(f"  Client -> Server: StreamOrderUpdates(ord_a1b2)")
    print()
    async for update in stream_order_updates("ord_a1b2"):
        print(f"  Server -> Client: [{update['status']:>10s}] {update['message']}")
    print()
    print("  Unlike SSE (text/event-stream), each update is a typed Protobuf message.")
    print("  The client deserializes into an OrderUpdate object, not a raw string.")
    print()


async def demo_client_streaming_rpc() -> None:
    """Pattern 3: Client-Streaming RPC — stream of requests, one response.

    No HTTP equivalent! The client sends a stream of orders,
    and the server responds with a batch summary.
    """
    print("-" * 68)
    print("  Pattern 3: Client-Streaming RPC (no HTTP equivalent)")
    print("-" * 68)
    print()

    orders_to_place = [
        {"order_id": f"catering_{i:03d}", "items": ["Burger x10", "Fries x10"]}
        for i in range(1, 6)
    ]

    placed: list[str] = []

    async def batch_place_orders(order_stream):
        """Simulated client-streaming RPC server handler."""
        async for order in order_stream:
            placed.append(order["order_id"])
            await asyncio.sleep(0.01)  # Process each as it arrives
        return {"total_placed": len(placed), "order_ids": placed}

    async def order_generator():
        """Client streams orders one at a time."""
        for order in orders_to_place:
            print(f"  Client -> Server: PlaceOrder({order['order_id']})")
            await asyncio.sleep(0.02)
            yield order

    print(f"  Catering company placing {len(orders_to_place)} orders via client-streaming:")
    print()

    response = await batch_place_orders(order_generator())
    print()
    print(f"  Server -> Client: BatchOrderResponse(total_placed={response['total_placed']})")
    print()
    print("  Why this is better than REST alternatives:")
    print(f"    - vs 5 separate POSTs: 1 round trip instead of 5")
    print(f"    - vs 1 POST with array body: server processes each order as it arrives")
    print(f"      (no need to buffer all 5 in memory before processing)")
    print()


async def demo_bidirectional_streaming_rpc() -> None:
    """Pattern 4: Bidirectional Streaming RPC — both sides stream.

    Like Ch05 WebSockets, but with typed messages, deadline propagation,
    and gRPC interceptors.
    """
    print("-" * 68)
    print("  Pattern 4: Bidirectional Streaming RPC (like WebSockets)")
    print("-" * 68)
    print()

    # Use asyncio queues to simulate bidirectional streams
    client_to_server: asyncio.Queue = asyncio.Queue()
    server_to_client: asyncio.Queue = asyncio.Queue()

    async def support_agent():
        """Simulated support service (server side)."""
        responses = {
            "Where is my order?": "Let me check your order status...",
            "It's been 30 minutes!": "I see your order is being prepared. The kitchen is busy tonight.",
            "Can I get a refund?": "I've initiated a partial refund for the delay. You'll see it in 3-5 business days.",
        }
        while True:
            msg = await client_to_server.get()
            if msg is None:
                break
            response = responses.get(msg, "Let me look into that for you.")
            await asyncio.sleep(0.03)  # "Thinking" time
            await server_to_client.put(response)

    customer_messages = [
        "Where is my order?",
        "It's been 30 minutes!",
        "Can I get a refund?",
    ]

    agent_task = asyncio.create_task(support_agent())

    for msg in customer_messages:
        print(f"  Customer -> Support: \"{msg}\"")
        await client_to_server.put(msg)
        response = await server_to_client.get()
        print(f"  Support -> Customer: \"{response}\"")
        print()

    # Signal end of conversation
    await client_to_server.put(None)
    await agent_task

    print("  Unlike WebSockets, gRPC bidirectional streaming gives you:")
    print("    - Typed messages (ChatMessage protobuf, not raw text)")
    print("    - Deadline propagation (auto-cancel if client disconnects)")
    print("    - Interceptors (auth, logging, metrics on every message)")
    print("    - HTTP/2 flow control (backpressure if either side is slow)")
    print()


async def demo_all_patterns() -> None:
    """Run all four gRPC pattern demonstrations."""
    print()
    print("=" * 68)
    print("  PART 3: Four gRPC Communication Patterns (Simulated)")
    print("=" * 68)
    print()

    await demo_unary_rpc()
    await demo_server_streaming_rpc()
    await demo_client_streaming_rpc()
    await demo_bidirectional_streaming_rpc()


# ═══════════════════════════════════════════════════════════════════════
# Part 5: HTTP/2 Multiplexing Demonstration
# ═══════════════════════════════════════════════════════════════════════

async def demo_multiplexing() -> None:
    """Demonstrate HTTP/2 multiplexing vs HTTP/1.1 sequential requests."""
    print("=" * 68)
    print("  PART 4: HTTP/2 Multiplexing vs HTTP/1.1 Sequential")
    print("=" * 68)
    print()

    num_calls = 5
    call_duration = 0.05  # 50ms per call

    async def simulate_rpc(call_id: int, duration: float) -> tuple[int, float, float]:
        """Simulate an RPC call that takes `duration` seconds."""
        start = time.perf_counter()
        await asyncio.sleep(duration)
        end = time.perf_counter()
        return call_id, start, end

    # HTTP/1.1: Sequential (one at a time per connection)
    print(f"  HTTP/1.1 — {num_calls} sequential RPC calls:")
    seq_start = time.perf_counter()
    seq_results = []
    for i in range(num_calls):
        result = await simulate_rpc(i, call_duration)
        seq_results.append(result)
    seq_total = time.perf_counter() - seq_start

    for call_id, start, end in seq_results:
        offset = start - seq_results[0][1]
        bar_start = int(offset / seq_total * 40)
        bar_len = max(1, int((end - start) / seq_total * 40))
        bar = " " * bar_start + "█" * bar_len
        print(f"    Call {call_id}: {bar}")
    print(f"    Total time: {seq_total*1000:.0f}ms")
    print()

    # HTTP/2: Multiplexed (all concurrent on one connection)
    print(f"  HTTP/2 — {num_calls} multiplexed RPC calls (one connection):")
    mux_start = time.perf_counter()
    tasks = [simulate_rpc(i, call_duration) for i in range(num_calls)]
    mux_results = await asyncio.gather(*tasks)
    mux_total = time.perf_counter() - mux_start

    for call_id, start, end in mux_results:
        offset = start - mux_results[0][1]
        bar_len = max(1, int(call_duration / (seq_total) * 40))
        bar = "█" * bar_len
        print(f"    Call {call_id}: {bar}")
    print(f"    Total time: {mux_total*1000:.0f}ms")
    print()

    print(f"  Speedup: {seq_total/mux_total:.1f}x faster with multiplexing")
    print(f"  ({num_calls} calls * {call_duration*1000:.0f}ms = {num_calls*call_duration*1000:.0f}ms sequential "
          f"vs ~{call_duration*1000:.0f}ms concurrent)")
    print()
    print("  In gRPC, all 5 calls share a single TCP connection.")
    print("  Each is an independent HTTP/2 stream. No head-of-line blocking.")
    print("  No connection pool needed.")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Part 6: Wire Format Deep Dive
# ═══════════════════════════════════════════════════════════════════════

def demo_wire_format() -> None:
    """Show the Protobuf wire format encoding step by step."""
    print("=" * 68)
    print("  PART 5: Protobuf Wire Format — Step by Step")
    print("=" * 68)
    print()

    # Encode a simple MenuItem to show exactly what each byte means
    item = MenuItem(id="item_01", name="Classic Burger", price_cents=899)

    print(f"  MenuItem: id={item.id!r}, name={item.name!r}, price_cents={item.price_cents}")
    print()

    # Field 1: string id = "item_01"
    tag1 = encode_tag(1, WIRE_TYPE_LENGTH_DELIMITED)
    val1 = b"item_01"
    field1 = tag1 + encode_varint(len(val1)) + val1

    # Field 2: string name = "Classic Burger"
    tag2 = encode_tag(2, WIRE_TYPE_LENGTH_DELIMITED)
    val2 = b"Classic Burger"
    field2 = tag2 + encode_varint(len(val2)) + val2

    # Field 3: int32 price_cents = 899
    tag3 = encode_tag(3, WIRE_TYPE_VARINT)
    val3 = encode_varint(899)
    field3 = tag3 + val3

    full = field1 + field2 + field3

    def hex_bytes(b: bytes) -> str:
        return " ".join(f"{x:02x}" for x in b)

    print("  Encoding breakdown:")
    print()
    print(f"  Field 1 (id = \"item_01\"):")
    print(f"    Tag:    {hex_bytes(tag1):>8s}  (field_number=1, wire_type=2=length-delimited)")
    print(f"    Length: {hex_bytes(encode_varint(len(val1))):>8s}  ({len(val1)} bytes)")
    print(f"    Value:  {hex_bytes(val1)}  (\"item_01\" as UTF-8)")
    print(f"    Total:  {len(field1)} bytes")
    print()
    print(f"  Field 2 (name = \"Classic Burger\"):")
    print(f"    Tag:    {hex_bytes(tag2):>8s}  (field_number=2, wire_type=2=length-delimited)")
    print(f"    Length: {hex_bytes(encode_varint(len(val2))):>8s}  ({len(val2)} bytes)")
    print(f"    Value:  {hex_bytes(val2)}")
    print(f"            (\"Classic Burger\" as UTF-8)")
    print(f"    Total:  {len(field2)} bytes")
    print()
    print(f"  Field 3 (price_cents = 899):")
    print(f"    Tag:    {hex_bytes(tag3):>8s}  (field_number=3, wire_type=0=varint)")
    print(f"    Value:  {hex_bytes(val3)}     (899 as varint: {899 & 0x7f | 0x80} {899 >> 7})")
    print(f"    Total:  {len(field3)} bytes")
    print()
    print(f"  Complete binary message: {len(full)} bytes")
    print(f"    {hex_bytes(full)}")
    print()

    json_bytes = item.model_dump_json().encode()
    print(f"  Same data as JSON: {len(json_bytes)} bytes")
    print(f"    {item.model_dump_json()}")
    print()
    print(f"  Protobuf: {len(full)} bytes vs JSON: {len(json_bytes)} bytes "
          f"({len(json_bytes)/len(full):.1f}x reduction)")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    """Run all demonstrations."""
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║     Appendix A — gRPC / Protocol Buffers Demo (Pure Python)    ║")
    print("║                                                                ║")
    print("║  This demo simulates gRPC and Protobuf concepts without the   ║")
    print("║  grpcio dependency. All binary encoding uses Python's struct   ║")
    print("║  module to replicate Protobuf's wire format.                  ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    # Part 1: Size comparison
    demo_size_comparison()

    # Part 2: Speed benchmark
    demo_speed_benchmark()

    # Part 3: Four communication patterns
    asyncio.run(demo_all_patterns())

    # Part 4: HTTP/2 multiplexing
    asyncio.run(demo_multiplexing())

    # Part 5: Wire format deep dive
    demo_wire_format()

    print("=" * 68)
    print("  Demo complete. See README.md for full educational content.")
    print("  See fooddash.proto for the complete Protobuf schema.")
    print("  See visual.html for interactive visualizations.")
    print("=" * 68)
    print()


if __name__ == "__main__":
    main()
