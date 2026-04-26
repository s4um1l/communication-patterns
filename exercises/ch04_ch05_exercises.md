# Exercises: Ch04 Server-Sent Events & Ch05 WebSockets

---

## Exercise 1 -- SSE Reconnection Semantics [Beginner]

**Question:** A FoodDash restaurant dashboard is streaming orders via SSE. The event stream looks like:

```
id: 47
event: new_order
data: {"order_id": "abc", "items": ["burger"]}

id: 48
event: new_order
data: {"order_id": "def", "items": ["pizza"]}

id: 49
event: status_change
data: {"order_id": "abc", "status": "cooking"}
```

1. The restaurant's Wi-Fi drops for 10 seconds, then reconnects. What HTTP header does the browser automatically send on reconnection? What value does it contain?
2. The server sees this header. What must the server do to avoid the restaurant missing events 48 and 49?
3. What happens if the server does NOT implement replay? Does the browser retry? Does the client know events were lost?

<details>
<summary>Solution</summary>

1. The browser sends `Last-Event-ID: 47` (the `id` of the last successfully received event). This is automatic -- the `EventSource` API handles it.

2. The server must:
   - Parse the `Last-Event-ID` header
   - Query its event store for all events with `id > 47`
   - Replay events 48 and 49 before streaming new events
   - This requires the server to **buffer recent events** (e.g., in a ring buffer, Redis stream, or database)

3. If the server ignores `Last-Event-ID`:
   - The browser **does** retry the connection (with exponential backoff, configurable via `retry:` field)
   - The connection re-establishes successfully
   - But the stream resumes from the *current* point -- events 48 and 49 are silently lost
   - The client has **no built-in way** to detect the gap. The `EventSource` API fires `onopen` but doesn't tell you events were missed.
   - You'd need application-level sequence numbers and gap detection on the client.

</details>

---

## Exercise 2 -- SSE vs WebSocket Wire Format [Beginner]

**Question:** Compare the per-message overhead of SSE and WebSocket for sending this JSON payload:

```json
{"order_id": "abc123", "status": "cooking", "eta_minutes": 12}
```

1. Write out the exact bytes-on-the-wire for SSE (the text/event-stream format).
2. Write out the WebSocket frame structure for the same payload (describe the header bytes).
3. Calculate the overhead ratio for each. Which is more efficient for small messages? For large messages (1MB)?

<details>
<summary>Solution</summary>

**1. SSE on the wire:**
```
data: {"order_id": "abc123", "status": "cooking", "eta_minutes": 12}\n\n
```
That's `data: ` (6 bytes) + payload (62 bytes) + `\n\n` (2 bytes) = **70 bytes total**. If you add an `id` and `event` field:
```
id: 50\nevent: status\ndata: {"order_id":...}\n\n
```
That's ~90 bytes. Overhead: ~30 bytes (~48% overhead for this small message). Plus, SSE runs over HTTP, so there's no additional framing -- the chunked transfer encoding adds ~6 bytes per chunk.

**2. WebSocket frame:**
```
[1 byte: FIN + opcode 0x1 (text)]
[1 byte: MASK bit + payload length (62, fits in 7 bits)]
[4 bytes: masking key (client->server only)]
[62 bytes: masked payload]
```
Client-to-server: 2 + 4 + 62 = **68 bytes** (masking key required).
Server-to-client: 2 + 0 + 62 = **64 bytes** (no masking key).
Overhead: 2-6 bytes (~3-10%).

**3. Comparison:**
| Message Size | SSE Overhead | WebSocket Overhead |
|---|---|---|
| 62 bytes | ~30 bytes (48%) | 2-6 bytes (3-10%) |
| 1 KB | ~30 bytes (3%) | 4 bytes (0.4%) |
| 1 MB | ~30 bytes (~0%) | 4 bytes (~0%) |

WebSocket is more efficient per-message, especially for small messages. But SSE's overhead is *constant* (the `data: ` prefix), so it becomes negligible for larger payloads. The real cost difference comes from the protocol level: SSE is one-way over HTTP, WebSocket is bidirectional with its own framing. For server-to-client streaming, the practical difference is small.

</details>

---

## Exercise 3 -- WebSocket Chat Room Scaling [Intermediate]

**Coding Challenge:** The Ch05 FoodDash chat uses a simple in-memory set of WebSocket connections:

```python
connections: set[WebSocket] = set()

async def broadcast(message: str):
    for ws in connections:
        await ws.send_text(message)
```

This has three problems. Fix each one:

1. **Slow client problem:** If one client has a slow network, `await ws.send_text()` blocks, delaying the message to all subsequent clients. A broadcast to 100 clients takes as long as the slowest client.

2. **Error handling:** If a client disconnects mid-broadcast, `send_text()` raises an exception, and remaining clients don't get the message.

3. **Ordering guarantee:** With the concurrent fix from (1), can two rapid messages arrive in different orders at different clients?

<details>
<summary>Hint</summary>

For (1), look at `asyncio.gather` or per-client send queues. For (2), wrap sends in try/except and collect dead connections. For (3), think about what "concurrent sends" means for ordering.

</details>

<details>
<summary>Solution</summary>

```python
import asyncio
from collections import defaultdict

connections: set[WebSocket] = set()
# Per-client send queue ensures ordering
send_queues: dict[WebSocket, asyncio.Queue] = {}

async def client_sender(ws: WebSocket):
    """Dedicated sender coroutine per client. Ensures FIFO ordering."""
    queue = send_queues[ws]
    try:
        while True:
            message = await queue.get()
            await asyncio.wait_for(ws.send_text(message), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        # Client is dead or too slow -- disconnect them
        connections.discard(ws)
        del send_queues[ws]
        await ws.close()

async def on_connect(ws: WebSocket):
    connections.add(ws)
    send_queues[ws] = asyncio.Queue(maxsize=100)  # Bounded!
    asyncio.create_task(client_sender(ws))

async def broadcast(message: str):
    dead = []
    for ws in connections:
        try:
            send_queues[ws].put_nowait(message)
        except asyncio.QueueFull:
            # Client is too far behind -- disconnect them
            dead.append(ws)

    for ws in dead:
        connections.discard(ws)
        send_queues.pop(ws, None)
        await ws.close()
```

**Key design decisions:**

1. **Per-client queue + dedicated sender**: `broadcast()` is now O(n) non-blocking `put_nowait()` calls. No slow client blocks others. Each client's sender drains its queue independently.

2. **Bounded queue**: If a client falls behind by 100 messages, we disconnect them rather than letting the queue grow unbounded (OOM risk).

3. **Ordering**: Because each client has a single sender coroutine reading from a FIFO queue, messages arrive in the order they were broadcast. Two rapid `broadcast()` calls enqueue in order, and the per-client sender dequeues in order. Ordering is preserved.

4. **Error isolation**: If `send_text` fails, only that client's sender task catches the error. Other clients are unaffected.

This is essentially the pattern used by production WebSocket servers (Django Channels, Phoenix, etc.).

</details>

---

## Exercise 4 -- SSE Through Corporate Proxies [Intermediate]

**Question:** A FoodDash restaurant is behind a corporate proxy (Squid) that:
- Buffers HTTP responses until they're "complete"
- Has a 120-second idle connection timeout
- Does SSL termination (MITM) for inspection

The restaurant's SSE dashboard stops working. Orders appear in batches every 120 seconds instead of in real-time.

1. Explain why the buffering proxy breaks SSE. What does "complete response" mean for an SSE stream?
2. The engineer tries adding `X-Accel-Buffering: no` and `Cache-Control: no-transform`. Will these help? What headers actually disable proxy buffering?
3. Propose two architectural workarounds that work even if you can't control the proxy.

<details>
<summary>Solution</summary>

**1. Why buffering breaks SSE:**
An SSE stream is an HTTP response with `Content-Type: text/event-stream` that *never completes* -- the server holds the connection open and sends events incrementally. A buffering proxy collects response bytes until it sees the end of the response (Content-Length reached or connection closed). Since SSE has no Content-Length and doesn't close, the proxy buffers indefinitely. The 120-second idle timeout eventually kills the connection, at which point the proxy flushes the entire buffer at once. Hence: events arrive in 120-second batches.

**2. Headers:**
- `X-Accel-Buffering: no` -- only works for Nginx, not Squid or generic proxies.
- `Cache-Control: no-transform` -- tells proxies not to modify the body (compression, etc.) but doesn't disable buffering.
- `Cache-Control: no-cache, no-store` -- may help some proxies avoid caching but doesn't affect response buffering.
- **None of these reliably disable buffering on all proxies.** There's no universal header. The HTTP spec doesn't define a "don't buffer this response" header.

**3. Architectural workarounds:**

**(a) Long polling fallback:** Detect SSE failure (no events within expected interval) and fall back to long polling. Long polling returns *complete* responses per update, so proxies forward them correctly. The `EventSource` API doesn't support this natively -- you'd need a custom client that tries SSE first, detects staleness, and switches.

**(b) WebSocket over TLS:** WebSockets use the `Upgrade` header and switch to a non-HTTP protocol. Many corporate proxies pass WebSocket connections through if they're over TLS (`wss://`) because they see it as an opaque TCP tunnel after the `CONNECT` method. This bypasses HTTP-level buffering entirely. Caveat: some proxies block WebSocket upgrades too -- test with your specific proxy.

**(c) Chunked encoding with padding:** Send a large comment block (e.g., `:` followed by 2KB of spaces) as the first SSE event. Some proxies flush after receiving a threshold amount of data. Then send a comment ping every 15 seconds to prevent idle timeout. This is a hack but works in practice.

</details>

---

## Exercise 5 -- Design a Hybrid SSE + WebSocket System [Principal]

**Design Problem:** FoodDash needs to support three real-time features simultaneously for each customer:

| Feature | Direction | Messages/min | Payload Size | Latency Req |
|---|---|---|---|---|
| Order status updates | Server -> Client | 1-2 | 200 bytes | < 2 seconds |
| Live map (driver location) | Server -> Client | 60 (every 1s) | 100 bytes | < 1 second |
| Chat with driver | Bidirectional | Variable (0-30) | 50-500 bytes | < 500ms |

The naive approach uses one WebSocket for everything. Design a system that uses the *right protocol for each feature*. Consider:

1. Which features use SSE? Which use WebSocket? Why not use one protocol for everything?
2. How many TCP connections does each customer have? Is this acceptable?
3. The customer's phone switches from Wi-Fi to cellular. What happens to each connection? How does each protocol handle reconnection?
4. At 100K concurrent customers, calculate the total connection count and memory. Is this feasible on a single server? What's your scaling strategy?
5. A product manager asks: "Can we add read receipts to chat?" How does this change your protocol choice?

<details>
<summary>Solution</summary>

**1. Protocol assignment:**

| Feature | Protocol | Rationale |
|---|---|---|
| Order status | SSE | Server-to-client only. 1-2 msg/min is low frequency. SSE's auto-reconnect with `Last-Event-ID` handles Wi-Fi drops gracefully. HTTP-based, so it works through all proxies and CDNs. |
| Driver location | SSE | Server-to-client only. 60 msg/min is medium frequency but still unidirectional. Could share the same SSE connection as order status via different `event:` types. |
| Chat | WebSocket | Bidirectional. Both sides send messages. Low latency matters. |

**Why not one WebSocket for everything?** You *could*, and many companies do. But:
- SSE connections are cheaper (no frame masking, no ping/pong overhead).
- SSE auto-reconnects with `Last-Event-ID` for free -- WebSocket reconnection is manual and you must implement your own catch-up logic.
- SSE works through HTTP/2 multiplexing -- one TCP connection for both SSE streams.
- Separation of concerns: the "order" microservice can serve SSE without knowing about the "chat" service.

**2. Connections per customer:**
- 1 SSE connection (multiplexing order status + driver location via `event:` types)
- 1 WebSocket connection (chat)
- Total: **2 TCP connections per customer**

With HTTP/2, the SSE stream shares the same TCP connection as regular API requests, so it could be just 1 TCP connection + 1 WebSocket.

**3. Wi-Fi to cellular transition:**
- **SSE**: The TCP connection dies. `EventSource` detects this and auto-reconnects with `Last-Event-ID: <last_seen>`. The server replays missed events. Seamless -- the client sees a brief gap then catches up.
- **WebSocket**: The TCP connection dies. There's no built-in reconnection. Your client code must: detect the `onclose` event, reconnect, re-authenticate, and request missed messages (e.g., "give me chat messages after timestamp X"). You need server-side message buffering.
- **Key insight**: SSE handles network transitions gracefully out of the box. WebSocket requires significant client-side reconnection logic.

**4. Scale at 100K customers:**
- Connections: `100K * 2 = 200K` TCP connections
- Memory per connection: ~20KB (SSE) + ~30KB (WebSocket) = ~50KB per customer
- Total: `100K * 50KB = 5GB` -- feasible on a single 16GB server for just the connections
- But: 100K SSE connections streaming driver location at 1/s = 100K messages/second outbound. At 100 bytes each, that's 10MB/s outbound bandwidth. Feasible but significant.
- **Scaling strategy**: Horizontally scale with sticky sessions (hash customer_id to server). Use Redis Pub/Sub or NATS to fan out events across servers. The SSE servers subscribe to a `driver_location:{order_id}` channel and forward to connected clients.

**5. Adding read receipts:**
Read receipts are client-to-server ("I read message #47"). This confirms WebSocket is the right choice for chat. In SSE, you'd have to POST read receipts via a separate HTTP request, which adds latency and complexity. With WebSocket, it's just another message type on the existing connection. The design doesn't change -- read receipts are a message subtype within the chat WebSocket.

</details>
