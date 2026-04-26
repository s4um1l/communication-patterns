# Exercises: Ch02 Short Polling & Ch03 Long Polling

---

## Exercise 1 -- Polling Math [Beginner]

**Question:** FoodDash has 10,000 active users, each polling `GET /orders/{id}` every 2 seconds to check order status. The average order takes 30 minutes and goes through 5 status changes.

1. How many requests/second does the server handle?
2. What percentage of those requests return *new* information (a status change)?
3. If each request+response is 500 bytes on the wire, how much bandwidth per hour is "wasted" (returns no new data)?

<details>
<summary>Solution</summary>

1. `10,000 users / 2 seconds = 5,000 requests/second`

2. Each order has 5 status changes over 30 minutes (1,800 seconds). That's 1 useful response per `1800/5 = 360 seconds`. The client polls every 2 seconds. So `2/360 = 0.56%` of requests return new data. **99.44% are wasted.**

3. Total requests/hour: `5,000/s * 3,600s = 18,000,000`. Wasted: `18,000,000 * 0.9944 = 17,899,200 requests`. Bandwidth: `17,899,200 * 500 bytes = 8.95 GB/hour` of wasted bandwidth.

This is exactly why FoodDash moved to long polling in Ch03.

</details>

---

## Exercise 2 -- Short Polling Backoff Strategy [Intermediate]

**Coding Challenge:** The naive 2-second polling interval wastes resources when orders are in "cooking" status (which lasts 15-20 minutes) but is too slow during "driver_arriving" (which lasts 2-3 minutes and users want real-time updates).

Design and implement an **adaptive polling** strategy:
- Poll every 10 seconds during `received` and `cooking` states
- Poll every 2 seconds during `ready_for_pickup` and `driver_arriving`
- Poll every 30 seconds during `delivered` (just to confirm finality)
- Implement exponential backoff if the server returns 429 (rate limited)
- Cap the backoff at 60 seconds

Write the client-side polling loop in Python using `asyncio` and `httpx`.

<details>
<summary>Hint</summary>

Structure your loop around a state machine. After each response, extract the order status and look up the next interval from a dict. For backoff, multiply the interval by 2 on each 429, and reset on a successful response.

</details>

<details>
<summary>Solution</summary>

```python
import asyncio
import httpx

INTERVALS = {
    "received": 10,
    "cooking": 10,
    "ready_for_pickup": 2,
    "driver_arriving": 2,
    "delivered": 30,
}
MAX_BACKOFF = 60

async def adaptive_poll(order_id: str):
    backoff_multiplier = 1
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(f"http://localhost:8000/orders/{order_id}")

                if resp.status_code == 429:
                    wait = min(2 * backoff_multiplier, MAX_BACKOFF)
                    backoff_multiplier *= 2
                    print(f"Rate limited. Backing off {wait}s")
                    await asyncio.sleep(wait)
                    continue

                backoff_multiplier = 1  # Reset on success
                data = resp.json()
                status = data["status"]
                print(f"Order {order_id}: {status}")

                if status == "delivered":
                    print("Order complete!")
                    break

                interval = INTERVALS.get(status, 5)
                await asyncio.sleep(interval)

            except httpx.ConnectError:
                wait = min(5 * backoff_multiplier, MAX_BACKOFF)
                backoff_multiplier *= 2
                print(f"Connection error. Retrying in {wait}s")
                await asyncio.sleep(wait)
```

Key design decisions:
- The interval dict makes it easy to tune without code changes (could be server-driven via a response header like `Retry-After`).
- Backoff resets on any success, not just on new data.
- A smarter version would have the *server* tell the client when to poll next via a custom header: `X-Poll-After: 10`.

</details>

---

## Exercise 3 -- Long Polling Timeout Cascade [Intermediate]

**Question:** FoodDash uses long polling with a 30-second server timeout. The architecture is:

```
Browser -> CDN/CloudFront -> ALB (idle timeout: 60s) -> Nginx (proxy_read_timeout: 30s) -> App Server
```

1. The app server holds the request for 25 seconds, then responds with "no change." Trace the response through every layer. Does it work?
2. An engineer changes the app server timeout to 45 seconds. What breaks and why?
3. Design the correct timeout hierarchy. What should each layer's timeout be, and what's the general rule?

<details>
<summary>Solution</summary>

**1. 25-second hold -- works fine:**
```
App server holds 25s < Nginx timeout (30s)      OK
Nginx holds 25s   < ALB timeout (60s)           OK
ALB holds 25s     < CloudFront timeout (60s)     OK
Browser gets response after ~25s                  OK
```

**2. 45-second app server timeout -- breaks at Nginx:**
```
App server holds 45s > Nginx proxy_read_timeout (30s)  FAIL!
At t=30s, Nginx gives up and returns 504 Gateway Timeout to the ALB.
The app server doesn't know this. It's still holding the request.
At t=45s, the app server writes a response to a closed connection.
```
The browser sees a 504 after 30 seconds. The app server wastes resources holding a dead request for 15 more seconds.

**3. Correct timeout hierarchy -- each layer must be longer than the one below it:**
```
App server long-poll timeout:  25s
Nginx proxy_read_timeout:      35s  (app + 10s buffer)
ALB idle timeout:              65s  (nginx + 30s buffer)
CloudFront origin timeout:     75s  (ALB + 10s buffer)
Browser fetch timeout:         90s  (outermost, most generous)
```

**General rule:** `timeout[layer N] > timeout[layer N-1] + buffer`. The innermost layer (app) has the shortest timeout. Each outer layer adds buffer for network latency and processing. If any layer is shorter than the one inside it, that layer will kill the request before the inner layer finishes.

This is one of the most common production bugs with long polling. Always audit the full timeout chain.

</details>

---

## Exercise 4 -- Long Polling Memory Leak [Intermediate]

**Coding Challenge:** The Ch03 long polling server stores pending requests in a dict:

```python
pending: dict[str, asyncio.Future] = {}
```

Identify and fix these bugs:

1. A client connects, then closes their browser tab (TCP RST). The `Future` stays in `pending` forever. How do you detect disconnection and clean up?
2. Two browser tabs poll for the same `order_id`. The second overwrites the first in the dict. The first tab never gets a response. Fix this to support multiple waiters per order.
3. After fixing (2), if 1,000 tabs are watching the same order, a single status change creates 1,000 responses simultaneously. Could this cause a thundering herd? How would you mitigate it?

<details>
<summary>Hint</summary>

For (1), look at Starlette's `Request.is_disconnected()` or use `asyncio.shield` with a disconnect check. For (2), use `asyncio.Event` or a list of futures. For (3), think about jittered response times.

</details>

<details>
<summary>Solution</summary>

**1. Detecting client disconnect:**

```python
async def long_poll(request: Request, order_id: str):
    future = asyncio.Future()
    pending[order_id] = future

    try:
        # Race between: (a) future resolving, (b) client disconnecting
        disconnect = asyncio.create_task(wait_for_disconnect(request))
        done, _ = await asyncio.wait(
            [future, disconnect],
            timeout=25,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if disconnect in done:
            future.cancel()
            del pending[order_id]
            return  # Client gone, no response needed

        if future in done:
            return JSONResponse(future.result())

        # Timeout -- no update
        return JSONResponse({"status": "no_change"}, status_code=304)
    finally:
        pending.pop(order_id, None)

async def wait_for_disconnect(request: Request):
    while not await request.is_disconnected():
        await asyncio.sleep(1)
```

**2. Multiple waiters per order:**

```python
from collections import defaultdict

waiters: dict[str, list[asyncio.Future]] = defaultdict(list)

async def long_poll(request: Request, order_id: str):
    future = asyncio.Future()
    waiters[order_id].append(future)
    try:
        result = await asyncio.wait_for(future, timeout=25)
        return JSONResponse(result)
    except asyncio.TimeoutError:
        return JSONResponse({"status": "no_change"})
    finally:
        waiters[order_id].remove(future)

def notify_order_update(order_id: str, data: dict):
    for future in waiters.get(order_id, []):
        if not future.done():
            future.set_result(data)
```

**3. Thundering herd mitigation:**

```python
import random

def notify_order_update(order_id: str, data: dict):
    futures = waiters.get(order_id, [])
    for i, future in enumerate(futures):
        if not future.done():
            # Stagger responses over 500ms to avoid a spike
            delay = random.uniform(0, 0.5) * (i / max(len(futures), 1))
            asyncio.get_event_loop().call_later(delay, future.set_result, data)
```

Alternatively, use `asyncio.Event` instead of individual futures -- one event per order, all waiters `await event.wait()`. But you still get the thundering herd on wake. The real fix at scale is to move to SSE (Ch04) or pub/sub (Ch07).

</details>

---

## Exercise 5 -- Short Polling vs Long Polling Decision Framework [Advanced]

**Design Problem:** You're consulting for three different companies. For each, decide whether short polling or long polling is the better fit. Justify with specific numbers.

**Company A -- Weather Dashboard:**
- 50,000 users viewing weather data
- Weather data updates every 10 minutes from a government API
- Users want "reasonably current" data (5-minute staleness is OK)
- Budget is tight, minimize server costs

**Company B -- Auction Platform:**
- 500 concurrent bidders per auction
- Bids arrive in bursts (calm periods, then 20 bids in 10 seconds near deadline)
- Bidders need to see new bids within 1 second
- Auction lasts 7 days

**Company C -- IoT Sensor Fleet:**
- 100,000 sensors reporting temperature every 5 seconds
- Central dashboard shows aggregate stats, updated every 30 seconds
- Sensors are on cellular connections (expensive per-byte, unreliable)
- Sensors push data TO the server (not the other way around)

<details>
<summary>Solution</summary>

**Company A -- Weather Dashboard: SHORT POLLING wins.**

Why: The data changes on a *known, fixed schedule* (every 10 minutes). Short polling every 5 minutes gives <5 minute staleness. With 50K users polling every 5 minutes: `50,000 / 300s = 167 req/s`. That's trivially small. Long polling would hold 50K connections open for 5 minutes each, wasting 50K server-side coroutines to wait for an update that happens on a predictable schedule. Short polling with aggressive caching (`Cache-Control: public, max-age=300`) means most requests are served from CDN, not even hitting the origin.

**Company B -- Auction Platform: LONG POLLING wins (or better: SSE/WebSocket).**

Why: Bids are *unpredictable* and *latency-sensitive*. Short polling at 1-second intervals with 500 users = 500 req/s sustained for 7 days = 302M requests. 99%+ are wasted during calm periods. Long polling: 500 held connections, responses fire instantly when a bid arrives. During bursts, the response-reconnect cycle adds ~100ms latency (acceptable vs 1s polling). The connection count (500) is tiny. Long polling is clearly better here, though SSE would avoid the reconnection overhead entirely.

**Company C -- IoT Sensor Fleet: SHORT POLLING (sensor-initiated) wins, but reframe the question.**

This is a trick scenario. The sensors are *pushing* data, not pulling. The "polling" is the *dashboard* polling an aggregation layer. Optimal design:
- Sensors: fire-and-forget `POST /readings` every 5 seconds (request-response, Ch01). No persistent connections. Cellular connections are expensive to hold open, and sensors don't need responses.
- Dashboard: short poll `GET /aggregates` every 30 seconds. Data updates on a known schedule (every 30s aggregation window). Same logic as Company A.
- Long polling makes no sense here because: (a) sensors shouldn't hold connections on cellular, (b) dashboard updates are periodic, not event-driven.

**Key principle:** Long polling wins when updates are *unpredictable* and *latency matters*. Short polling wins when updates are *periodic/predictable* or when *holding connections is expensive*.

</details>
