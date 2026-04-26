# Chapter 09 -- Multiplexing / Demultiplexing

## The Scene

Every FoodDash customer has three concurrent connections to the backend:

1. A **WebSocket** for real-time chat with the driver
2. An **SSE stream** for order status updates
3. **HTTP requests** for the REST API (menu browsing, placing orders, checking receipts)

At 100 users, that is 300 connections. Manageable. At 100,000 users, that is 300,000 connections. Each one has its own TCP handshake (1-3 round trips), TLS negotiation (another 1-2 round trips), kernel socket buffer (~87 KB default on Linux: 43 KB send + 44 KB receive), and file descriptor.

```
100K users x 3 connections = 300K connections

TCP handshakes:  300K x 3 round trips = 900K round trips
TLS handshakes:  300K x 2 round trips = 600K round trips
Socket buffers:  300K x 87 KB = ~25 GB of kernel memory
File descriptors: 300K (default ulimit is 1024 on most systems)
```

25 GB of kernel memory just to hold socket buffers. Before a single byte of application data flows.

Can we collapse all three streams into a single connection per customer? That is what multiplexing does.

---

## The Pattern -- Multiplexing / Demultiplexing

**Multiplexing** is interleaving multiple logical streams over a single physical connection. Instead of three connections carrying one stream each, one connection carries all three, with each message tagged by a **stream ID** that identifies which logical stream it belongs to.

**Demultiplexing** is the reverse: the receiver inspects the stream ID on each incoming frame and routes it to the correct handler.

```
BEFORE (3 connections per customer):

  Customer ---[WebSocket]---> Chat Server
  Customer ---[SSE]---------> Order Status Server
  Customer ---[HTTP]--------> API Server

  3 TCP connections, 3 TLS handshakes, 3 socket buffers

AFTER (1 multiplexed connection):

  Customer ---[Single WebSocket]---> Mux Server
                                       |
                                       +---> Chat Handler      (stream_id=1)
                                       +---> Order Handler     (stream_id=2)
                                       +---> Location Handler  (stream_id=3)

  1 TCP connection, 1 TLS handshake, 1 socket buffer
```

The concept is not new. The telephone network has done this since the 1960s -- Time Division Multiplexing (TDM) interleaved multiple voice calls over a single wire by assigning each call a time slot. HTTP/2 brought the concept to the web in 2015. gRPC uses it for all its calls.

### The Core Insight

Multiplexing trades **per-connection overhead** for **per-frame overhead**. Instead of paying a TCP+TLS handshake per stream, you pay a few extra bytes per message (the stream ID and frame header). The economics are overwhelmingly in favor of multiplexing:

```
3 separate connections:
  Setup cost:  3 x (TCP handshake + TLS handshake) = ~450ms total
  Per-message:  0 extra bytes (no framing needed)
  Ongoing:      3 x 87KB socket buffers = 261 KB

1 multiplexed connection:
  Setup cost:  1 x (TCP handshake + TLS handshake) = ~150ms
  Per-message:  5 extra bytes (stream_id + type + length)
  Ongoing:      1 x 87KB socket buffer = 87 KB
```

At 100K users, you save 200K TCP handshakes, 200K TLS handshakes, and ~17 GB of kernel memory. The cost is 5 extra bytes per message. That is not even a trade-off -- it is pure win.

---

## Frame Protocol Design

To multiplex, every message must be wrapped in a **frame** that tells the demultiplexer which stream it belongs to and how large the payload is.

Here is our educational frame format:

```
+-------------------+--------+------------------+---------------------------+
| stream_id (2B)    | type   | length (2B)      | payload (N bytes)         |
|                   | (1B)   |                  |                           |
+-------------------+--------+------------------+---------------------------+
| 0x00 0x01         | 0x01   | 0x00 0x1A        | {"msg": "Where are you?"} |
| (stream 1 = chat) | (CHAT) | (26 bytes)       |                           |
+-------------------+--------+------------------+---------------------------+

Total frame = 5 bytes header + N bytes payload
```

Field breakdown:

- **stream_id** (2 bytes, unsigned): Identifies the logical stream. Range 0-65535, supporting up to 65K concurrent streams per connection. For FoodDash: 1=chat, 2=order status, 3=driver location.
- **type** (1 byte): The stream type / message type. Allows the demux to route to the correct handler without parsing the payload.
- **length** (2 bytes, unsigned): Payload size in bytes. Range 0-65535. Max payload is 64 KB per frame. Larger messages must be fragmented across multiple frames.
- **payload** (N bytes): The actual message data. JSON in our implementation, but could be Protobuf, MessagePack, or raw bytes.

Why these specific sizes?

- 2-byte stream_id: 65K streams is more than enough for client-side muxing. HTTP/2 uses 4-byte stream IDs (4 billion streams) because servers handle millions of clients.
- 1-byte type: 256 message types is sufficient. We only use 3.
- 2-byte length: 64 KB max payload. Chat messages and status updates are well under this. For larger payloads (file uploads), you would fragment into multiple frames or use a separate channel.
- Total header: 5 bytes. HTTP/2 uses 9 bytes. gRPC uses HTTP/2 frames plus its own 5-byte prefix.

---

## HTTP/2 Connection Multiplexing

HTTP/2 implements multiplexing at the protocol level, making it the standard for web multiplexing. Understanding it illuminates the general pattern.

### Streams, Messages, and Frames

HTTP/2 has three layers of abstraction:

```
Connection (1 TCP connection)
  |
  +-- Stream 1 (request/response pair for GET /menu)
  |     +-- HEADERS frame (request headers, compressed with HPACK)
  |     +-- DATA frame (response body, chunk 1)
  |     +-- DATA frame (response body, chunk 2)
  |
  +-- Stream 3 (request/response pair for POST /order)
  |     +-- HEADERS frame (request headers)
  |     +-- DATA frame (request body)
  |     +-- HEADERS frame (response headers)
  |     +-- DATA frame (response body)
  |
  +-- Stream 5 (server push for /styles.css)
        +-- PUSH_PROMISE frame
        +-- HEADERS frame
        +-- DATA frame
```

All streams share one TCP connection. Frames from different streams are interleaved on the wire:

```
Wire bytes (time --->):

[Stream 1 HEADERS][Stream 3 HEADERS][Stream 1 DATA][Stream 3 DATA][Stream 1 DATA]

  Stream 1: ===HEADERS==========DATA================DATA===
  Stream 3: ============HEADERS==========DATA==============

Both streams make progress concurrently over the same connection.
```

### HPACK Header Compression

HTTP/1.1 sends headers as plain text with every request. The `Cookie`, `User-Agent`, `Accept`, and `Authorization` headers alone can be 1-2 KB, repeated identically on every request.

HPACK eliminates this redundancy:

1. **Static table**: 61 common header name-value pairs (`:method: GET`, `:status: 200`, etc.) are encoded as a single byte index.
2. **Dynamic table**: Headers seen previously in the connection are added to a per-connection lookup table. Subsequent references use the index instead of the full text.
3. **Huffman coding**: Header values are Huffman-encoded, reducing ASCII text by ~30%.

Result: after the first few requests, most headers compress to 5-10 bytes instead of 500-2000 bytes. For FoodDash with 100K users making 10 requests/minute each: saving ~1.5 KB per request = 1.5 GB/minute of bandwidth eliminated.

### Per-Stream Flow Control

Each stream has its own flow control window. A slow consumer on stream 1 does not throttle streams 2 and 3. The receiver advertises how many bytes it is willing to accept per stream via WINDOW_UPDATE frames.

```
Stream 1 (large file download):  window = 65535 bytes (default)
Stream 2 (chat message):         window = 65535 bytes
Stream 3 (small API call):       window = 65535 bytes

Stream 1 fills its window --> BLOCKED until receiver sends WINDOW_UPDATE
Stream 2 and 3 continue unimpeded
```

This is critical for multiplexing correctness. Without per-stream flow control, one heavy stream could monopolize the connection, starving other streams.

---

## Systems Constraints Analysis

### CPU

**Framing overhead**: Each message requires 5 bytes of header construction on send and header parsing on receive. At 100K messages/second: 500K bytes of framing data and 200K parse operations. The parse is trivial -- read 5 fixed-size fields. Total CPU cost: negligible (microseconds per frame).

**Demux routing**: The demultiplexer reads the stream_id and type from each frame and routes to the correct handler. This is a dictionary lookup -- O(1). At 100K frames/second: 100K dictionary lookups. Total: ~0.1ms aggregate.

**Real overhead**: HPACK compression/decompression in HTTP/2 is the most CPU-intensive part of multiplexing. Maintaining the dynamic table, performing Huffman coding, and managing table eviction requires more cycles than simple framing. But it is still far cheaper than the TLS overhead on the duplicate connections it eliminates.

### Memory

**3x reduction in socket buffers**. This is the headline win. Each TCP connection consumes ~87 KB of kernel memory for socket buffers (configurable via `setsockopt`). Three connections per user at 100K users: 25 GB. One multiplexed connection: 8.5 GB. Savings: 17 GB.

**Frame buffer memory**: The multiplexer needs a small buffer for assembling and parsing frames. For our 5-byte header + 64KB max payload: ~65 KB per connection worst case. In practice, most frames are much smaller (chat messages are <1 KB).

**Stream state**: Each active stream requires a small amount of state -- flow control window, priority, handler reference. For HTTP/2: ~200 bytes per stream. At 100 concurrent streams per connection: 20 KB. Negligible.

### Network

**Eliminated duplicate TCP/TLS handshakes**: The biggest network win. A TCP handshake is 1.5 round trips (SYN, SYN-ACK, ACK). TLS 1.2 adds 2 more round trips (TLS 1.3 adds 1). At 100ms round-trip time:

```
3 connections:  3 x (1.5 + 1) x 100ms = 750ms total setup time
1 connection:   1 x (1.5 + 1) x 100ms = 250ms total setup time
Savings: 500ms -- the user sees the app load 500ms faster
```

**Reduced header overhead**: Without HPACK, each HTTP request carries ~500 bytes of redundant headers. With HPACK after warmup: ~20 bytes. At 10 requests/second: 4.8 KB/second saved per user. At 100K users: 480 MB/second of bandwidth saved.

**Frame overhead**: The 5-byte frame header on every message. At 100K messages/second with average 200-byte payloads: 500 KB of framing overhead vs 20 MB of payload. The framing overhead is 2.5% -- negligible.

### Latency

**Head-of-line blocking: HTTP/2's Achilles heel.** This is the critical constraint. HTTP/2 multiplexes streams over a single TCP connection. TCP guarantees in-order byte delivery. If a single TCP packet is lost, ALL streams are blocked until that packet is retransmitted.

```
TCP packet sequence: [A1][B1][A2][B2][A3][B3]
                      ^-- Stream A, frame 1
                           ^-- Stream B, frame 1

If packet [B1] is lost:
  TCP retransmits [B1]
  Until retransmission arrives:
    [A2] is buffered but NOT delivered (TCP in-order guarantee)
    [B2] is buffered but NOT delivered
    [A3] is buffered but NOT delivered

  ALL streams are blocked by ONE lost packet on ONE stream.
```

This is TCP-level head-of-line (HOL) blocking. It is fundamentally worse than HTTP/1.1's HOL blocking:

- **HTTP/1.1 HOL blocking**: One request blocks subsequent requests on the same connection. Solution: open multiple connections (browsers open 6 per domain). The blocked request only affects its own connection.
- **HTTP/2 HOL blocking**: One lost packet blocks ALL streams on the shared connection. There is no workaround within TCP -- the guarantee is in the kernel.

On a clean network (data center, fiber), packet loss is rare (<0.01%) and HOL blocking is negligible. On lossy networks (mobile, WiFi), packet loss can be 1-5%, and HTTP/2 can actually perform worse than HTTP/1.1 with 6 connections.

---

## Production Depth

### HTTP/3 and QUIC: Solving Head-of-Line Blocking

QUIC (Quick UDP Internet Connections) moves multiplexing from TCP to UDP. Each QUIC stream is independently ordered. A lost packet on stream A does not block stream B because QUIC handles retransmission per-stream, not per-connection.

```
QUIC packet sequence: [A1][B1][A2][B2][A3][B3]

If packet [B1] is lost:
  QUIC retransmits [B1]
  Meanwhile:
    [A2] is delivered immediately (stream A is unaffected)
    [A3] is delivered immediately
    [B2] is buffered (only stream B waits for [B1])
    [B3] is buffered (only stream B waits)

  Only stream B is blocked. Stream A continues unimpeded.
```

This is the fundamental advance. HTTP/3 = HTTP semantics over QUIC. It provides true independent stream multiplexing without TCP's HOL blocking. The trade-off: QUIC is implemented in userspace (not the kernel), so it has slightly higher CPU overhead. And UDP-based protocols are sometimes blocked by enterprise firewalls and middleboxes.

### Stream Prioritization

Not all streams are equal. A chat message ("Where is my food?") should have higher priority than a background location update. HTTP/2 supports stream prioritization via a dependency tree:

```
Stream priority tree:
  Stream 1 (chat):     weight=256 (highest)
  Stream 2 (orders):   weight=128
  Stream 3 (location): weight=64  (lowest)

When bandwidth is constrained:
  Chat gets 256/(256+128+64) = 57% of bandwidth
  Orders get 128/(256+128+64) = 28%
  Location gets 64/(256+128+64) = 14%
```

In practice, stream prioritization is poorly implemented in most HTTP/2 servers. Chrome removed its complex priority tree in favor of a simpler scheme. HTTP/3 replaced the priority tree with a simpler "urgency" and "incremental" signaling model.

### gRPC Multiplexing

gRPC uses HTTP/2 as its transport. Every gRPC call is an HTTP/2 stream. This means:

- Multiple gRPC calls between the same client and server share one TCP connection
- gRPC streaming (server-stream, client-stream, bidirectional) maps directly to HTTP/2 stream frames
- gRPC channels maintain a connection pool (typically 1 connection) and multiplex all calls over it

```
gRPC Channel (1 HTTP/2 connection):
  Stream 1: PlaceOrder() RPC
  Stream 3: GetOrderStatus() RPC
  Stream 5: SubscribeToUpdates() server-streaming RPC
  Stream 7: ChatWithDriver() bidirectional-streaming RPC

All four RPCs execute concurrently over one connection.
```

The implication: a single gRPC channel to a service can handle thousands of concurrent RPCs. You do not need connection pooling in the traditional sense. The HTTP/2 connection IS the pool.

### Flow Control Per Stream

HTTP/2 implements two levels of flow control:

1. **Connection-level**: Total bytes in flight across all streams. Default window: 65,535 bytes.
2. **Stream-level**: Bytes in flight for each individual stream. Default window: 65,535 bytes.

The receiver sends WINDOW_UPDATE frames to increase the window. The sender must not exceed the window. This prevents a fast sender from overwhelming a slow receiver.

```
Sender:
  Stream 1: window=65535, sent 60000 bytes, remaining=5535
  Stream 2: window=65535, sent 1000 bytes, remaining=64535

  Stream 1 can send 5535 more bytes before blocking
  Stream 2 can send 64535 more bytes

Receiver processes stream 1 data:
  Sends WINDOW_UPDATE for stream 1: increment=60000
  Stream 1 window is now 65535 again
```

The default 65 KB window is often too small for high-bandwidth streams. Production gRPC servers typically set the initial window to 1-16 MB using the SETTINGS frame. Too large a window risks memory exhaustion; too small throttles throughput.

### When NOT to Multiplex

Multiplexing is not always the answer:

1. **Independent failure domains**: If your chat server crashes, you want order updates to keep flowing. With three separate connections, the SSE stream survives the WebSocket crash. With one multiplexed connection, everything dies.

2. **Different QoS requirements**: If chat needs low-latency and analytics can tolerate batching, separate connections let you tune TCP buffers, timeouts, and retry policies independently.

3. **Load balancing granularity**: With separate connections, you can route chat to chat servers and orders to order servers. With one multiplexed connection, the demux server must forward to all backends -- it becomes a router.

4. **Debugging complexity**: Three separate connections are easy to inspect in browser DevTools. One multiplexed connection with binary frames requires custom tooling.

The rule of thumb: multiplex when connections are the bottleneck (high user counts, mobile networks). Keep separate connections when operational simplicity matters more (small scale, different failure domains).

---

## Trade-offs at a Glance

| Dimension | Separate Connections | Multiplexed Connection | HTTP/2 | HTTP/3 (QUIC) |
|-----------|---------------------|----------------------|--------|----------------|
| **Connections per user** | N (one per stream) | 1 | 1 | 1 |
| **Setup latency** | N x (TCP + TLS) | 1 x (TCP + TLS) | 1 x (TCP + TLS) | 1 x (0-RTT possible) |
| **Socket buffers** | N x 87 KB | 1 x 87 KB | 1 x 87 KB | 1 x (userspace) |
| **Header overhead** | Full headers per request | Frame header (5 bytes) | Frame header (9B) + HPACK | Frame header + QPACK |
| **HOL blocking** | Per-connection only | Per-connection (all streams) | Per-connection (all streams) | Per-stream only |
| **Failure isolation** | Independent per stream | All streams share fate | All streams share fate | Better (per-stream loss) |
| **Flow control** | Per-connection (TCP) | Application-level | Per-stream + per-connection | Per-stream |
| **Implementation** | Simple (use existing protocols) | Custom framing required | Browser/server support | Growing support |
| **Debugging** | Easy (standard tools) | Hard (custom frames) | Medium (HTTP/2 tools) | Medium (QUIC tools) |

---

## Running the Code

### Start the multiplexing server

```bash
# From the repo root
uv run python -m chapters.ch09_multiplexing.demux_handler
```

The server starts on `ws://localhost:8009/ws` and accepts multiplexed frames over a single WebSocket connection.

### Run the client (sends interleaved messages)

```bash
# In another terminal
uv run python -m chapters.ch09_multiplexing.client
```

The client sends chat messages, order status checks, and location updates interleaved over one connection. Watch the frame-level details showing stream IDs and types.

### Open the visualization

Open `chapters/ch09_multiplexing/visual.html` in a browser to see:
- Three color-coded streams flowing through a single connection
- Frame inspector showing the binary-level details
- Comparison with three separate connections

---

## Bridge to Chapter 10

Multiplexing solved our connection explosion: 100K users now use 100K connections instead of 300K. Every service in FoodDash now benefits from fewer connections, less memory, faster setup.

But look at those services. The order service has auth middleware, request logging, and rate limiting. The kitchen service has... the same auth middleware, the same request logging, the same rate limiting. The billing service? Same. The driver service? Same. The notification service? Same.

Five services, five copies of the same cross-cutting concerns. When you update the auth logic (say, migrating from API keys to JWT), you deploy five services. When you add request tracing, you modify five codebases. When a rate limiter bug is found, you patch five services.

There must be a way to extract these common concerns into a single place, without coupling the services to a shared library. The answer is the **sidecar pattern**: a helper process that runs alongside each service, intercepting all traffic and handling auth, logging, and rate limiting transparently.

Next: [Chapter 10 -- Sidecar Pattern](../ch10_sidecar/).
