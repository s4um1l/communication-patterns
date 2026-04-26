# Exercises: Ch11 Synthesis -- System Design Combining Multiple Patterns

---

## Exercise 1 -- Stock Trading Platform [Advanced]

**Design Problem:** Design the notification system for a stock trading platform where:

- Market data updates 100x/second per ticker (5,000 tickers total = 500K updates/sec)
- Users have custom alert thresholds (e.g., "notify me when AAPL drops below $150")
- Latency from price change to alert delivery must be < 100ms
- Users may be on mobile with the app backgrounded or closed
- 2M registered users, 200K concurrent during market hours
- Alerts are financial: a missed or delayed alert could cost a user real money

**Questions:**

1. Which communication pattern delivers market data from the exchange feed to your alert evaluation engine? Why not the others?
2. Which pattern streams live prices to users who have the app open?
3. Which pattern reaches users whose app is closed?
4. A user sets 50 alerts across 50 different tickers. How do you evaluate all 50 without scanning every user for every price update?
5. At market open (9:30 AM), all 200K users connect simultaneously. How do you handle this thundering herd?
6. The exchange feed sends duplicate price updates (at-least-once). Your alert engine fires twice for the same price crossing. How do you deduplicate?

<details>
<summary>Solution</summary>

**1. Exchange feed to alert engine: Pub/Sub (Ch07) with partitioned topics.**

The exchange publishes to a topic per ticker (or a single topic partitioned by ticker symbol). The alert engine subscribes and processes in real-time. Why not:
- Request-response: 500K req/sec polling is absurd.
- SSE/WebSocket: These are client-facing. The exchange feed is server-to-server.
- Long polling: Adds reconnection latency incompatible with 100ms budget.

Specific tech: Kafka with ticker-based partitioning. One partition per ticker (or hash of ticker to N partitions). Allows parallel consumption while maintaining per-ticker ordering.

**2. Live prices to open apps: WebSocket (Ch05).**

Server-to-client, high frequency (user might watch 10 tickers at 100 updates/sec = 1,000 msg/sec). SSE could work (server-to-client only), but WebSocket is better because:
- Users send subscription changes ("watch AAPL", "unwatch MSFT") -- bidirectional.
- Binary frames for compact price encoding (SSE is text-only).
- Per-user subscription filtering on the server reduces unnecessary data.

**3. Closed app: Push Notifications (Ch06).**

APNs / FCM with `collapse_key: "alert_{ticker}_{direction}"` so rapid price oscillations around the threshold don't spam the user. Set `priority: high` (APNs) / `priority: "high"` (FCM) to wake the device immediately.

**4. Efficient alert evaluation: Inverted index.**

```
# Instead of: for each price update, check all 2M users
# Build: ticker -> sorted list of (threshold, user_id, direction)

alert_index = {
    "AAPL": SortedList([
        (148.00, "user_42", "below"),
        (150.00, "user_99", "below"),
        (200.00, "user_13", "above"),
    ]),
    # ...
}
```

When AAPL price changes to $149.50:
- Binary search the sorted list for thresholds crossed.
- Only evaluate relevant users. O(log N) per ticker per update instead of O(users).
- At 500K updates/sec, this is critical. Scanning 2M users per update = 1 trillion checks/sec.

**5. Thundering herd at market open:**

- **Connection queuing**: Accept TCP connections but throttle the WebSocket upgrade handshake. Use a token bucket (e.g., 10K upgrades/sec). Clients retry with exponential backoff + jitter.
- **Staggered reconnect**: Send `retry: {random 1-10}000` in the SSE stream or a "reconnect_after" field in the WebSocket close frame so clients don't all reconnect at the same instant.
- **Pre-warm**: Scale up WebSocket servers at 9:25 AM (predictable load).
- **CDN for initial state**: The first request ("give me all my watchlist prices") hits a CDN-cached snapshot, not the real-time engine. Then the WebSocket takes over for incremental updates.

**6. Alert deduplication:**

```python
# Per-user, per-alert cooldown
alert_cooldowns: dict[str, float] = {}  # key -> last_fired_timestamp

def should_fire(user_id: str, ticker: str, threshold: float) -> bool:
    key = f"{user_id}:{ticker}:{threshold}"
    last_fired = alert_cooldowns.get(key, 0)
    if time.time() - last_fired < COOLDOWN_SECONDS:  # e.g., 60s
        return False
    alert_cooldowns[key] = time.time()
    return True
```

Also deduplicate at the exchange feed level: each price update has a sequence number. If the alert engine sees sequence N twice, it skips the second. Use a sliding window bitmap per ticker.

</details>

---

## Exercise 2 -- Collaborative Document Editor [Advanced]

**Design Problem:** Build the real-time communication layer for a Google Docs-like collaborative editor.

Requirements:
- 50 concurrent editors per document
- Keystrokes appear on other users' screens within 200ms
- Works on flaky mobile connections (offline for 30+ seconds is common)
- Must handle conflicting edits (two users type at the same cursor position)
- Presence indicators ("Alice is viewing paragraph 3", "Bob is typing...")
- Document can be 100+ pages

**Questions:**

1. What protocol carries real-time edits between clients and server? Justify over alternatives.
2. How do you handle the "offline for 30 seconds" case? What queues up where?
3. Two users type at the same position simultaneously. Describe how Operational Transform (OT) or CRDTs resolve this, and what the communication pattern looks like on the wire.
4. Presence indicators update every 500ms per user. With 50 users, that's 100 presence updates/sec. How do you broadcast efficiently without drowning the edit stream?
5. A user opens a 100-page document. Do you send all 100 pages over the WebSocket? Design the loading strategy.

<details>
<summary>Solution</summary>

**1. WebSocket (Ch05) for real-time edits.**

- Bidirectional: both clients and server send edits.
- Low overhead: keystrokes are tiny (< 100 bytes). WebSocket frame overhead is 2-6 bytes vs SSE's `data: ` prefix (minor, but WebSocket is still the right semantic fit).
- Server needs to push edits AND the client needs to send edits -- SSE + HTTP POST would work but adds complexity (two channels to manage, correlate, and reconnect independently).
- The entire industry agrees: Google Docs, Notion, Figma all use WebSocket.

**2. Offline handling:**

Client side:
- Queue local edits in an ordered buffer (IndexedDB or in-memory).
- Each edit is tagged with a logical timestamp (Lamport clock or vector clock).
- Continue editing offline -- the local document diverges from the server.

Reconnection:
- Client sends `Last-Seen-Version: 47` on reconnect.
- Server sends all operations since version 47.
- Client rebases its queued local edits on top of the server's edits (OT transform or CRDT merge).
- Client sends its buffered edits to the server.
- Server transforms and broadcasts them to other clients.

Server side:
- Operation log is persisted (database or append-only file).
- At least 24 hours of operation history for offline clients.
- Beyond that, client must fetch a fresh document snapshot and rebase.

**3. Conflict resolution on the wire:**

Scenario: Alice inserts "A" at position 5, Bob inserts "B" at position 5, simultaneously.

**OT approach:**
```
Alice -> Server: Insert("A", pos=5, baseVersion=10)
Bob   -> Server: Insert("B", pos=5, baseVersion=10)

Server receives Alice first:
  Apply: Insert("A", 5) -> doc version 11
  Transform Bob's op against Alice's:
    Bob intended pos=5, but Alice inserted at 5
    Transformed: Insert("B", pos=6)  (shift right by 1)
  Apply: Insert("B", 6) -> doc version 12

Server -> Alice: Insert("B", 6)   (already has "A")
Server -> Bob:   Insert("A", 5)   (already has "B", but at wrong position)
  Bob transforms: his "B" was at 5, Alice's "A" at 5 shifts "B" to 6
  Result: same document state on both clients
```

Wire format:
```json
{"op": "insert", "char": "A", "pos": 5, "base": 10, "client": "alice"}
```

**4. Presence broadcasting:**

Separate presence from edits:
- **Edits**: high priority, sent immediately, must be ordered.
- **Presence**: low priority, can be throttled and lossy.

Strategy:
- Each client sends presence updates to the server every 500ms.
- Server aggregates: batch all 50 users' presence into one message.
- Server broadcasts aggregated presence every 500ms (not per-user).
- One broadcast every 500ms to 50 users = 100 messages/sec total (50 from clients + 50 broadcasts).
- Use a separate WebSocket message type: `{"type": "presence", "users": [{"id": "alice", "cursor": {"page": 3, "offset": 142}, "status": "typing"}, ...]}`.
- If the client misses a presence update, the next one (500ms later) corrects it. No replay needed.

**5. Large document loading:**

Do NOT send 100 pages over the WebSocket.

Strategy:
1. **HTTP GET** to fetch the initial document snapshot. This is a large payload (maybe 1-5MB) -- use HTTP with proper caching, compression (gzip), and range requests.
2. **Viewport-based loading**: Only fetch pages the user can see (pages 1-2). Fetch additional pages on scroll via HTTP.
3. **WebSocket** connects after initial load and handles only incremental edits (tiny payloads).
4. If a remote edit arrives for a page the user hasn't loaded, queue it. When the user scrolls to that page, fetch the page content + apply queued edits.

```
1. HTTP GET /docs/abc/pages?range=1-2     -> Initial render (fast)
2. WebSocket connect to /docs/abc/live     -> Incremental edits
3. HTTP GET /docs/abc/pages?range=3-10     -> Background prefetch
4. User scrolls to page 50:
   HTTP GET /docs/abc/pages?range=48-52   -> On-demand fetch
```

This is how Google Docs works: initial load is HTTP, real-time sync is WebSocket, and content is lazily loaded by viewport.

</details>

---

## Exercise 3 -- IoT Fleet Management [Intermediate]

**Design Problem:** FoodDash expands into autonomous delivery robots. Design the communication system for a fleet of 10,000 robots.

| Communication Need | Direction | Frequency | Reliability | Latency |
|---|---|---|---|---|
| GPS telemetry | Robot -> Server | Every 1s | Best effort | < 5s |
| Battery/health status | Robot -> Server | Every 30s | Guaranteed | < 60s |
| Route commands | Server -> Robot | On demand | Guaranteed, ordered | < 2s |
| Emergency stop | Server -> Robot | Rare | Guaranteed | < 200ms |
| Firmware updates | Server -> Robot | Monthly | Guaranteed, resumable | Minutes |
| Video stream (on demand) | Robot -> Server | 30fps when active | Best effort | < 500ms |

For each communication need:
1. Choose the pattern (Request-Response, Polling, SSE, WebSocket, Push, Pub/Sub).
2. Justify your choice.
3. Identify the failure mode (robot goes through a tunnel for 60 seconds) and your recovery strategy.

<details>
<summary>Solution</summary>

| Need | Pattern | Justification | Tunnel Recovery |
|---|---|---|---|
| **GPS telemetry** | **UDP fire-and-forget** (or MQTT QoS 0 over lightweight connection) | 1s updates, best effort. Missing a few is fine (next one comes in 1s). TCP's retransmit overhead wastes cellular bandwidth. | No recovery needed. Resume sending after tunnel. Server interpolates missing positions. |
| **Battery/health** | **MQTT QoS 1** (at-least-once pub/sub) | Guaranteed delivery, low frequency. MQTT is designed for IoT constrained networks. QoS 1 ensures the broker ACKs receipt. If no ACK, robot retransmits. | MQTT client buffers unsent messages. On reconnect, buffered messages are sent. Broker deduplicates via message ID. |
| **Route commands** | **MQTT QoS 2** (exactly-once) + **retained messages** | Guaranteed AND ordered. A route command must not be duplicated (robot drives the route twice) or lost (robot stops). Retained messages ensure a robot reconnecting gets the latest command. | Robot reconnects to broker, receives retained route command. Sequence numbers detect gaps -- robot requests replay of missed commands. |
| **Emergency stop** | **MQTT QoS 1** + **WebSocket (redundant path)** | Must arrive within 200ms. Use MQTT with `priority: high` flag AND a direct WebSocket command as redundancy. If either path delivers, the robot stops. | If in a tunnel, emergency stop cannot be delivered. **Hardware failsafe**: robot has local obstacle detection + geofence. If it loses connectivity for > 10s, it stops autonomously. Never rely solely on network for safety-critical commands. |
| **Firmware updates** | **HTTP with range requests** (Ch01 request-response) | Large file (100MB+), resumable. HTTP range requests allow the robot to resume a download after disconnection without starting over. Robot pulls the update (not pushed) to control timing. | Robot stores download progress. After tunnel, resumes from last byte received: `Range: bytes=52428800-`. No data wasted. |
| **Video stream** | **WebRTC** (or RTP/RTSP over UDP) | 30fps video is high bandwidth, real-time, best effort. WebRTC handles adaptive bitrate, packet loss concealment, and NAT traversal. TCP-based protocols (WebSocket) would stall on packet loss. | Video stream stops. No buffering during tunnel (would be stale by the time it arrives). After reconnect, stream resumes from current frame. Server sees "last frame 60s ago" and knows robot was disconnected. |

**Key architectural insight**: This system uses 4+ different protocols simultaneously. No single pattern fits all IoT needs. The common mistake is trying to put everything over WebSocket or HTTP. IoT demands protocol diversity matched to each requirement's reliability, latency, and bandwidth characteristics.

**Connection budget**: Each robot maintains:
- 1 MQTT connection (persistent, ~5KB memory on server)
- 0-1 WebRTC connection (on-demand video, ~50KB when active)
- Periodic HTTP requests (firmware, non-persistent)

At 10K robots: ~50MB for MQTT connections. Trivial. Video is the expensive part -- limit concurrent streams to 100 robots at a time.

</details>

---

## Exercise 4 -- Pattern Selection Decision Matrix [Intermediate]

**Exercise:** Complete this decision matrix. For each scenario, choose the BEST pattern and write ONE sentence justifying your choice.

| Scenario | Pattern | Justification |
|---|---|---|
| 1. User uploads a CSV, server processes it for 2 minutes, user wants progress updates | ? | ? |
| 2. A dashboard shows the top 10 trending restaurants, updated every 5 minutes | ? | ? |
| 3. A multiplayer mobile game with 4 players needs < 50ms action sync | ? | ? |
| 4. An e-commerce checkout must notify the warehouse, billing, and email services | ? | ? |
| 5. A CI/CD pipeline streams build logs to a browser in real-time | ? | ? |
| 6. A mobile app needs "You have a new message" while the app is closed | ? | ? |
| 7. A stock ticker shows prices updating 10x per second | ? | ? |
| 8. A microservice needs to call 3 downstream APIs in parallel and aggregate results | ? | ? |

<details>
<summary>Solution</summary>

| # | Scenario | Pattern | Justification |
|---|---|---|---|
| 1 | CSV processing progress | **SSE** | Server-to-client only, auto-reconnect handles flaky connections, and `Last-Event-ID` lets the client resume progress tracking if disconnected mid-processing. |
| 2 | Trending restaurants (5-min refresh) | **Short Polling** | Data changes on a predictable schedule; polling every 5 minutes with `Cache-Control: max-age=300` means most requests are served from CDN cache at near-zero server cost. |
| 3 | Multiplayer game, < 50ms sync | **WebSocket (or WebRTC DataChannel for P2P)** | Bidirectional, minimal framing overhead, persistent connection avoids per-message handshake latency; WebRTC adds P2P for even lower latency. |
| 4 | Checkout notifies 3 services | **Pub/Sub** | Decouples the checkout service from downstream consumers; adding a new consumer (e.g., loyalty points) requires zero changes to checkout; guaranteed delivery via durable queues. |
| 5 | CI/CD build log streaming | **SSE** | Server-to-client, text-based (log lines map naturally to SSE events), auto-reconnect with `Last-Event-ID` resumes from the last log line -- exactly what you want for a long-running build. |
| 6 | New message while app closed | **Push Notification** | The only pattern that can wake a closed mobile app; APNs/FCM deliver to the OS notification tray without the app running. |
| 7 | Stock ticker at 10 updates/sec | **WebSocket** | High-frequency server-to-client updates with client-initiated subscriptions ("watch AAPL") make this bidirectional; binary WebSocket frames are more efficient than SSE text for compact numeric data. |
| 8 | Parallel downstream API calls | **Request-Response with async fan-out** | This is synchronous aggregation, not real-time streaming; use `asyncio.gather()` with HTTP/2 multiplexing to call all 3 APIs concurrently over a single connection. |

</details>

---

## Exercise 5 -- Failure Mode Analysis [Advanced]

**Design Problem:** FoodDash is running the full stack. On Black Friday, traffic is 10x normal. Analyze the failure cascade and design circuit breakers.

Timeline of failures:

```
17:00 - Traffic at 10x. All systems nominal.
17:05 - Redis (session cache) response time goes from 2ms to 200ms.
17:06 - API servers' thread pools saturate waiting on Redis.
17:07 - ALB health checks fail (API servers not responding in time).
17:08 - ALB removes all API servers from rotation. 502 for all users.
17:09 - SSE connections drop (server restart). 200K clients reconnect simultaneously.
17:10 - The reconnection thundering herd kills the freshly restarted servers.
17:15 - Engineers manually disable SSE. REST API recovers.
17:20 - Engineers discover the root cause: Redis hit max connections (10K limit).
```

**Questions:**

1. Why did a cache slowdown (Redis) cascade to total platform failure? Identify every link in the chain.
2. Design a circuit breaker for the Redis connection. What are the three states? What triggers each transition?
3. How should the API servers behave when the Redis circuit breaker is OPEN? (Hint: graceful degradation, not hard failure.)
4. The SSE thundering herd at 17:09 could have been prevented. Design a reconnection strategy that prevents this.
5. Write a post-mortem action item list: what changes prevent this exact cascade from recurring?

<details>
<summary>Solution</summary>

**1. Cascade chain:**

```
Redis slow (200ms)
  -> Every API request blocks 200ms waiting for cache
  -> Thread/connection pool exhaustion (100 threads * 200ms = 20 req/s max)
  -> Request queue grows -> latency spikes to seconds
  -> ALB health check (GET /health) also blocked (it checks Redis!)
  -> ALB marks all servers unhealthy after 3 failed checks
  -> 502 for all traffic
  -> SSE connections severed (server process recycles)
  -> 200K EventSource auto-reconnects fire simultaneously
  -> Fresh servers overwhelmed by connection storm
  -> Cycle repeats
```

Root cause chain: Redis maxed out -> slow responses -> thread exhaustion -> health check failure -> total outage -> reconnection storm -> repeat.

**2. Circuit breaker design:**

```
States:
  CLOSED  -- Normal. Requests go to Redis. Track failure rate.
  OPEN    -- Redis is down. Requests bypass Redis immediately (no waiting).
  HALF-OPEN -- Testing. Send 1 request to Redis. If it succeeds, -> CLOSED.
              If it fails, -> OPEN.

Transitions:
  CLOSED -> OPEN:
    When failure rate > 50% over last 10 seconds
    OR when p99 latency > 100ms over last 10 seconds

  OPEN -> HALF-OPEN:
    After 30 seconds (configurable cooldown)

  HALF-OPEN -> CLOSED:
    If test request succeeds within 50ms

  HALF-OPEN -> OPEN:
    If test request fails or times out
```

```python
import time
from enum import Enum

class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class RedisCircuitBreaker:
    def __init__(self, failure_threshold=0.5, window=10, cooldown=30, timeout=0.05):
        self.state = State.CLOSED
        self.failures = 0
        self.successes = 0
        self.last_failure_time = 0
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.timeout = timeout

    async def call(self, redis_func, *args, fallback=None):
        if self.state == State.OPEN:
            if time.time() - self.last_failure_time > self.cooldown:
                self.state = State.HALF_OPEN
            else:
                return fallback() if fallback else None

        try:
            result = await asyncio.wait_for(redis_func(*args), timeout=self.timeout)
            self._record_success()
            return result
        except (asyncio.TimeoutError, ConnectionError):
            self._record_failure()
            return fallback() if fallback else None

    def _record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        rate = self.failures / max(self.failures + self.successes, 1)
        if rate > self.failure_threshold:
            self.state = State.OPEN

    def _record_success(self):
        self.successes += 1
        if self.state == State.HALF_OPEN:
            self.state = State.CLOSED
            self.failures = 0
            self.successes = 0
```

**3. Graceful degradation when circuit is OPEN:**

```python
async def get_user_session(user_id: str):
    # Circuit breaker wraps Redis call with fallback
    session = await breaker.call(
        redis.get, f"session:{user_id}",
        fallback=lambda: None  # Cache miss fallback
    )

    if session is None:
        # Fallback: validate JWT only (no session enrichment)
        # User gets basic functionality but not personalized data
        return decode_jwt_only(request.headers["Authorization"])

    return session
```

- **Cache reads (menus, restaurant data)**: Serve stale data from a local in-process cache (even 60-second-old data is fine). Or hit the database directly (slower but functional).
- **Cache writes (session updates)**: Queue writes and apply when Redis recovers. User session changes are eventually consistent.
- **Health check**: The `/health` endpoint must NOT depend on Redis. Check only: "can this server handle HTTP requests?" A health check that depends on Redis creates the exact cascade we saw.

**4. SSE reconnection storm prevention:**

```python
# Server-side: include a jittered retry field in the SSE stream
async def sse_stream(request):
    # On connection, send a retry interval with jitter
    jitter = random.randint(3000, 15000)  # 3-15 seconds
    yield f"retry: {jitter}\n\n"

    # On graceful shutdown, send a final event with staggered reconnect
    yield f"event: reconnect\ndata: {{\"delay\": {random.randint(1, 30)}}}\n\n"
```

```javascript
// Client-side: respect server's reconnect hint
eventSource.addEventListener("reconnect", (e) => {
    const delay = JSON.parse(e.data).delay;
    eventSource.close();
    setTimeout(() => {
        eventSource = new EventSource("/stream");
    }, delay * 1000);
});
```

Additionally: implement **connection rate limiting** on the server. Accept at most 1,000 new SSE connections per second. Excess connections get a `503 Retry-After: 5` response.

**5. Post-mortem action items:**

1. **[P0] Fix health check**: Remove Redis dependency from `/health`. Health check should verify the process is responsive, not that all dependencies are up.
2. **[P0] Add Redis circuit breaker**: Implement the circuit breaker pattern with fallback to JWT-only auth and database-direct reads.
3. **[P0] Set Redis connection timeout**: Currently no timeout -- requests block indefinitely. Set to 50ms. Better to fail fast than block a thread for 200ms.
4. **[P1] Increase Redis max connections**: 10K was too low for 10x traffic. Set to 50K or use connection pooling with a shared pool.
5. **[P1] SSE reconnection jitter**: Implement server-side `retry:` with randomized intervals (3-15s). Add connection rate limiting (1K new connections/sec).
6. **[P2] Separate Redis instances**: Cache and pub/sub on different Redis clusters. Cache failure shouldn't affect real-time features.
7. **[P2] Load test at 10x**: Add Black Friday load testing to the quarterly schedule. This failure was predictable.
8. **[P3] Add circuit breaker dashboard**: Expose circuit breaker state as a metric. Alert when any breaker opens.

</details>
