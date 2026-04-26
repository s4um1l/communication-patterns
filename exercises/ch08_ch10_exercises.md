# Exercises: Ch08 Stateful vs Stateless, Ch09 Multiplexing, Ch10 Sidecar

---

## Exercise 1 -- Stateful Session Affinity [Beginner]

**Question:** FoodDash runs 4 WebSocket servers behind an AWS ALB. A customer connects to Server 2 and starts chatting with their driver (who is on Server 3).

1. Explain why the message doesn't reach the driver. Draw the path the message takes.
2. The ops team enables "sticky sessions" (cookie-based affinity) on the ALB. Does this fix the problem? Why or why not?
3. Name two approaches that actually solve cross-server WebSocket communication. What are the trade-offs?

<details>
<summary>Solution</summary>

**1. The broken path:**
```
Customer (Server 2) sends: "Which entrance?"
  -> Server 2 checks its local connections set
  -> Driver is NOT in Server 2's connections (driver is on Server 3)
  -> Message is dropped or returns "user not found"
```

The message never leaves Server 2. Each server only knows about its own connections.

**2. Sticky sessions do NOT fix this.** Sticky sessions ensure the *same client* always returns to the *same server*. The customer always hits Server 2, the driver always hits Server 3. That's the problem -- they're on *different* servers. Sticky sessions help with stateful HTTP sessions (e.g., server-side session store), not with cross-client communication.

**3. Two solutions:**

**(a) Shared pub/sub backplane (Redis Pub/Sub, NATS):**
```
Customer -> Server 2 -> publish to "chat:order_123" on Redis
                         Redis fans out to all subscribers
Server 3 <- subscribed to "chat:order_123" <- delivers to Driver
```
- Trade-off: Adds a dependency (Redis). All messages flow through Redis, adding ~1ms latency. If Redis goes down, cross-server chat breaks. Redis Pub/Sub is fire-and-forget (no persistence).

**(b) Consistent hashing / routing:**
Route both participants of a chat to the *same* server using consistent hashing on `order_id`:
```
hash("order_123") % 4 = Server 1
Both customer and driver are routed to Server 1
Messages stay in-process, no external backplane needed
```
- Trade-off: Requires a smart load balancer (or a connection router service). If Server 1 goes down, both connections must failover together. Rebalancing on server add/remove is complex.

Most production systems use (a) because it decouples routing from communication. Socket.IO uses Redis adapter, Phoenix uses PG2/CRDT, etc.

</details>

---

## Exercise 2 -- Stateless Token Design [Beginner]

**Question:** FoodDash's API uses server-side sessions stored in Redis. Every request includes a session cookie, and the server looks up the session in Redis to get the user_id, permissions, and cart contents.

1. What happens if Redis goes down? What happens if the Redis connection is slow (100ms per lookup)?
2. Redesign the auth layer to be stateless using JWTs. What goes in the token? What stays out?
3. The customer adds items to their cart. In the session-based design, the cart is in Redis. In the stateless design, where does the cart go? List three options with trade-offs.

<details>
<summary>Solution</summary>

**1. Redis failure modes:**
- **Redis down**: Every API request fails auth. The entire platform is down. Redis is a single point of failure for every request.
- **Redis slow (100ms)**: Every request adds 100ms latency. A 50ms API call becomes 150ms. Under load, this cascades: connections queue up, timeouts fire, the system collapses.

**2. JWT token contents:**

**Include (claims):**
```json
{
  "sub": "user_42",
  "role": "customer",
  "permissions": ["place_order", "view_menu", "chat"],
  "exp": 1705334400,
  "iat": 1705330800
}
```

**Exclude:**
- Cart contents (too large, changes frequently -- would require new token on every cart change)
- Payment methods (sensitive PII, shouldn't travel in a token)
- Order history (too large)

**Trade-off**: JWTs can't be revoked instantly. If a user is banned, their JWT is valid until expiry. Mitigation: short expiry (15 min) + refresh token flow, or a small blacklist check (much smaller than a full session store).

**3. Cart storage options:**

| Option | Pros | Cons |
|---|---|---|
| **Client-side (localStorage)** | Zero server state, survives server restarts | Lost if user clears browser, not synced across devices |
| **Database (per-user cart table)** | Persistent, synced across devices, survives everything | DB query per cart operation, schema to maintain |
| **Hybrid: client + lazy sync** | Fast (local), eventually synced to DB | Conflict resolution needed if edited on two devices |

For FoodDash, **database-backed cart** is standard. Cart operations are infrequent (a few per session), so the DB cost is negligible. Users expect their cart to persist across devices and sessions.

</details>

---

## Exercise 3 -- HTTP/2 Multiplexing Analysis [Intermediate]

**Coding Challenge:** A FoodDash customer's browser loads the order tracking page, which requires:

```
GET /api/order/123           (200 bytes response, 50ms server time)
GET /api/order/123/items     (2 KB response, 30ms server time)
GET /api/driver/456/location (100 bytes response, 20ms server time)
GET /static/map-tile-a.png   (50 KB response, 5ms server time)
GET /static/map-tile-b.png   (50 KB response, 5ms server time)
GET /static/map-tile-c.png   (50 KB response, 5ms server time)
```

Network RTT is 40ms.

1. Calculate total page load time with HTTP/1.1 (6 connections max, no pipelining) vs HTTP/2 (1 connection, full multiplexing).
2. HTTP/2 multiplexes all 6 responses on one TCP connection. But the map tiles (50KB each) are large. How does HTTP/2 prevent them from starving the small API responses? What mechanism controls this?
3. Write a Python script using `httpx` that makes all 6 requests concurrently over HTTP/2 and measures the time-to-first-byte for each.

<details>
<summary>Solution</summary>

**1. Load time calculation:**

**HTTP/1.1 (6 connections, sequential per connection):**
Each connection: TCP handshake (1 RTT = 40ms) + TLS (1 RTT = 40ms) = 80ms setup.
With 6 connections, all 6 requests go out in parallel after setup.
Longest response: 50ms (server) + 40ms (RTT) = 90ms.
Total: 80ms (setup) + 90ms (longest request) = **170ms**.
(Optimization: connection reuse on subsequent navigations removes the 80ms setup.)

**HTTP/2 (1 connection, multiplexed):**
Setup: 1 TCP handshake (40ms) + 1 TLS with ALPN (40ms) = 80ms.
All 6 requests sent immediately as streams on the single connection.
Responses interleaved. Longest: 50ms + 40ms = 90ms.
Total: 80ms + 90ms = **170ms**.

Wait -- same result? For 6 requests, yes! HTTP/2's advantage appears when:
- Requests exceed the browser's 6-connection limit (HTTP/1.1 queues the 7th+)
- Many small requests (header compression via HPACK saves bytes)
- Subsequent requests (no additional handshakes)

**2. Stream prioritization and flow control:**
HTTP/2 uses **flow control** at the stream level. Each stream has a **window size** (default 64KB). The sender can't send more than the window allows without the receiver acknowledging.

For the map tiles, HTTP/2 interleaves frames from all streams. A 50KB tile is sent as multiple DATA frames (~16KB each). Between tile frames, API response frames are sent. The result: API responses (small, fast) arrive quickly even though large tiles are in flight.

The **PRIORITY** frame (deprecated in HTTP/2, replaced by Extensible Priorities in HTTP/3) let clients hint that API responses should be prioritized over images. In practice, browsers assign higher priority to XHR/fetch than to images.

**3. Concurrent HTTP/2 requests:**

```python
import asyncio
import time
import httpx

URLS = [
    "https://localhost:8000/api/order/123",
    "https://localhost:8000/api/order/123/items",
    "https://localhost:8000/api/driver/456/location",
    "https://localhost:8000/static/map-tile-a.png",
    "https://localhost:8000/static/map-tile-b.png",
    "https://localhost:8000/static/map-tile-c.png",
]

async def fetch(client: httpx.AsyncClient, url: str, start: float):
    resp = await client.get(url)
    ttfb = time.monotonic() - start
    return url.split("/")[-1], ttfb, len(resp.content)

async def main():
    start = time.monotonic()
    async with httpx.AsyncClient(http2=True, verify=False) as client:
        tasks = [fetch(client, url, start) for url in URLS]
        results = await asyncio.gather(*tasks)

    print(f"{'Resource':<25} {'TTFB (ms)':<12} {'Size':<10}")
    print("-" * 47)
    for name, ttfb, size in sorted(results, key=lambda r: r[1]):
        print(f"{name:<25} {ttfb*1000:<12.1f} {size:<10}")
    print(f"\nTotal wall time: {(time.monotonic() - start)*1000:.1f}ms")

asyncio.run(main())
```

Expected output: all 6 resources arrive within ~10ms of each other because they're multiplexed. The small API responses should have slightly lower TTFB than the large tiles.

</details>

---

## Exercise 4 -- Sidecar vs Library [Intermediate]

**Question:** FoodDash has 5 microservices. Each needs: (a) mTLS termination, (b) request retries with circuit breaking, (c) distributed tracing (inject trace headers), (d) rate limiting.

The team debates two approaches:

**Option A -- Shared library:**
A Python package `fooddash-middleware` with decorators: `@mtls`, `@retry`, `@trace`, `@rate_limit`. Each service imports it.

**Option B -- Sidecar proxy (Envoy/Linkerd):**
Each service gets a sidecar container that handles all cross-cutting concerns. The service itself just makes plain HTTP calls to `localhost`.

1. The billing service is rewritten in Go. What happens to each option?
2. The security team discovers a TLS vulnerability and needs to patch all services within 1 hour. Compare the rollout for each option.
3. The sidecar adds 1-2ms latency per hop. FoodDash's order flow has 5 hops. Is this acceptable? When does sidecar latency become a problem?
4. Under what circumstances would you choose the library over the sidecar?

<details>
<summary>Solution</summary>

**1. Go rewrite:**
- **Library**: Must rewrite `fooddash-middleware` in Go. Every decorator, every retry policy, every tracing integration -- reimplemented. Two codebases to maintain. Bugs in one may not exist in the other. Testing doubles.
- **Sidecar**: No change. The Go service makes plain HTTP calls to `localhost:15001` (the sidecar). The sidecar handles mTLS, retries, tracing, rate limiting. Language-agnostic.

**2. TLS patch rollout:**
- **Library**: Update the library version. Every service must: bump the dependency, rebuild, run tests, deploy. 5 services * (5 min build + 5 min deploy) = ~50 minutes best case. If any service has dependency conflicts, it blocks.
- **Sidecar**: Update the sidecar image version. Rolling restart of sidecar containers only -- application containers untouched. 5 services * 1 min rolling restart = ~5 minutes. No application code changes, no rebuilds, no dependency conflicts.

**3. Sidecar latency:**
- Per-hop overhead: 1-2ms (localhost TCP + proxy processing)
- 5 hops: 5-10ms additional latency
- For FoodDash order flow (total ~500ms): 5-10ms = 1-2% overhead. **Absolutely acceptable.**
- Becomes a problem when:
  - Ultra-low latency requirements (< 1ms, e.g., HFT). Sidecar is a no-go.
  - Very deep call chains (20+ hops). 20-40ms overhead starts to matter.
  - The sidecar itself becomes a bottleneck under extreme load (unlikely with Envoy, which handles 100K+ req/s per instance).

**4. Choose library over sidecar when:**
- **Single language shop**: If everything is Python and always will be, a well-maintained library is simpler than running sidecar infrastructure.
- **Simple deployment**: No Kubernetes, no container orchestration. Sidecar patterns assume container-level isolation. On bare VMs, libraries are easier.
- **Performance-critical inner loops**: If you're processing 1M events/sec in a pipeline, the sidecar's per-message overhead matters. Embed the logic.
- **Team size < 5**: The operational overhead of a service mesh (Istio, Linkerd) requires dedicated platform engineering. A small team is better served by a library.
- **Custom business logic in middleware**: If retries depend on business context (e.g., "retry payment only if amount < $100"), a library is more expressive than sidecar config.

</details>

---

## Exercise 5 -- Multi-Pattern Architecture Audit [Principal]

**Design Problem:** You inherit FoodDash's production architecture. Audit it for correctness, efficiency, and failure modes.

```
                    Internet
                       |
                   CloudFront (CDN)
                       |
                   AWS ALB (sticky sessions ON)
                  /    |    \
               App1   App2   App3   (Python, uvicorn)
                 |      |      |
                 +------+------+
                        |
                   Redis Cluster
                   (sessions, pub/sub, cache)
```

Each App server handles:
- REST API (stateless, JWT auth)
- SSE streams for order tracking (in-memory connection list)
- WebSocket for chat (in-memory connection set, no backplane)
- Long polling for legacy mobile clients

Problems to find:

1. **Sticky sessions + JWT**: Why is this contradictory? What should be changed?
2. **SSE connections in memory**: What happens when App2 is restarted during a deploy?
3. **WebSocket without backplane**: Draw a scenario where chat fails.
4. **Redis as pub/sub for WebSocket**: Redis Pub/Sub is fire-and-forget. What happens if a subscriber disconnects for 5 seconds?
5. **Single Redis cluster**: Identify the failure blast radius.

For each problem, propose a specific fix with the minimum architectural change.

<details>
<summary>Solution</summary>

**1. Sticky sessions + JWT is contradictory:**
JWTs make the API **stateless** -- any server can validate the token without shared state. Sticky sessions force a client to always hit the same server. This is wasteful: it creates uneven load distribution (one server may have all the "heavy" users) and makes scaling harder (can't just add servers; existing sessions are pinned).

**Fix**: Remove sticky sessions. If they were added for WebSocket/SSE (stateful connections), use a **connection routing** layer instead: hash `order_id` to determine which server handles that order's real-time connections. REST API requests go to any server.

**2. SSE connections lost on deploy:**
When App2 restarts, all in-memory SSE connections are dropped. Clients reconnect (SSE auto-reconnect), but they're routed to App1 or App3 (no sticky sessions, or different sticky cookie). If App2 had buffered events for replay, that buffer is lost.

**Fix**: Store recent events in Redis Streams (not just Pub/Sub). When a client reconnects with `Last-Event-ID`, any server can query Redis Streams for missed events. The SSE connection list remains in-memory (fine), but the event history is externalized.

**3. WebSocket chat without backplane -- failure scenario:**
```
1. Customer connects to App1, driver connects to App3
2. Customer sends "I'm at the side door"
3. App1 looks for driver in its local connection set -- not found
4. Message is silently dropped
5. Driver never receives it
```

**Fix**: Add Redis Pub/Sub as a WebSocket backplane. When App1 receives a chat message, it publishes to `chat:{order_id}`. App3 is subscribed, receives it, and forwards to the driver.

**4. Redis Pub/Sub message loss:**
Redis Pub/Sub has **no persistence**. If App3 disconnects from Redis for 5 seconds (e.g., GC pause, network blip), all messages published during those 5 seconds are lost forever. The driver misses chat messages with no way to recover.

**Fix**: Replace Redis Pub/Sub with **Redis Streams** for the chat backplane. Streams persist messages with IDs. When App3 reconnects, it reads from its last known ID: `XREAD BLOCK 0 STREAMS chat:order_123 1705334400000-0`. No messages lost.

**5. Single Redis cluster blast radius:**
Redis is used for 3 things: sessions, pub/sub, cache. If Redis goes down:
- Sessions: already fixed by JWT (problem 1). No impact.
- Pub/Sub (WebSocket backplane): all cross-server chat fails. In-server chat still works.
- Cache: all requests hit the database directly. Latency spikes, possible DB overload.
- SSE event buffer (if moved to Redis per fix #2): no event replay on reconnect.

**Fix**: Separate Redis instances by function:
- **Redis A (Streams)**: WebSocket backplane + SSE event history. Persistence enabled (AOF).
- **Redis B (Cache)**: Menu data, session cache. No persistence. If it dies, the app falls back to DB.
- This limits blast radius: Cache failure = slow but functional. Streams failure = real-time features degraded but REST API unaffected.

</details>
