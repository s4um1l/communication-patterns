# Exercises: Ch00 Foundations (TCP/HTTP) & Ch01 Request-Response

---

## Exercise 1 -- TCP Teardown Timing [Beginner]

**Question:** A FoodDash client places an order via HTTP/1.1 over TCP. After the server responds with `201 Created`, the client has no more requests to send.

1. Who initiates the TCP teardown -- the client or the server? Does HTTP/1.1 change the answer compared to HTTP/1.0?
2. What is `TIME_WAIT`, and why does the OS keep the socket in that state instead of freeing it immediately?
3. On a busy FoodDash server handling 1,000 orders/min over short-lived HTTP/1.0 connections, how many sockets could be stuck in `TIME_WAIT` if the default timeout is 60 seconds?

<details>
<summary>Solution</summary>

1. In HTTP/1.0, the **server** typically closes the connection after sending the response (no `Connection: keep-alive`). In HTTP/1.1, connections are persistent by default (`Connection: keep-alive`), so **neither** side tears down immediately. The client would send `Connection: close` in its last request, or the server may close after an idle timeout. Either side *can* initiate the FIN.

2. `TIME_WAIT` exists to handle delayed duplicate packets. If the socket were freed instantly and a new connection reused the same `(src_ip, src_port, dst_ip, dst_port)` tuple, a stale packet from the old connection could be misinterpreted. The standard wait is 2 * MSL (Maximum Segment Lifetime), typically 60 seconds on Linux.

3. 1,000 orders/min = ~1,000 connections/min. Each stays in `TIME_WAIT` for 60s. So at steady state, roughly **1,000 sockets** in `TIME_WAIT` at any moment. On a server with a limited ephemeral port range (e.g., 28,000 ports), this is fine. But at 100K orders/min you'd exhaust ports -- one reason to use persistent connections or `SO_REUSEADDR`.

</details>

---

## Exercise 2 -- HTTP Verb Semantics in FoodDash [Beginner]

**Question:** A junior engineer proposes this API for FoodDash:

```
POST /orders              -- Place a new order
POST /orders/123/cancel   -- Cancel order 123
GET  /orders/123/receipt  -- Download a PDF receipt
POST /orders/123/receipt  -- Re-send receipt to email
```

1. Is `POST /orders/123/cancel` idempotent? Should it be? What HTTP verb might be more appropriate and why?
2. The `GET /orders/123/receipt` endpoint generates a PDF on the fly (takes 2 seconds). A CDN is in front of the API. What happens? Is this a problem?
3. If the client's network drops *after* sending `POST /orders` but *before* receiving the response, the client retries. Now there are two orders. How would you redesign the endpoint to make it safe to retry?

<details>
<summary>Solution</summary>

1. Cancelling an order *should* be idempotent -- cancelling an already-cancelled order should succeed (or no-op). `PATCH /orders/123` with `{"status": "cancelled"}` or `PUT /orders/123/status` with `{"status": "cancelled"}` better communicates idempotency. `DELETE /orders/123` is another option but semantically different (delete vs cancel). The key insight: `POST` is defined as *not* idempotent, so clients and intermediaries won't retry it automatically.

2. The CDN may **cache** the GET response. If the receipt content can change (e.g., refund applied later), stale caches serve outdated PDFs. Use `Cache-Control: no-store` or `Cache-Control: private, max-age=0` if receipts are dynamic. If truly immutable, caching is a feature, not a bug.

3. Use an **idempotency key**. The client generates a UUID (e.g., `Idempotency-Key: 550e8400-...`) and includes it in the header. The server stores the key with the response. On retry with the same key, the server returns the stored response without creating a duplicate order. Stripe, PayPal, and most payment APIs use this pattern.

</details>

---

## Exercise 3 -- Measuring HTTP Overhead [Intermediate]

**Coding Challenge:** Write a Python script that makes a single `POST /orders` request to the Ch01 FoodDash server and captures:

1. The total bytes sent on the wire (request line + headers + body)
2. The total bytes received on the wire (status line + headers + body)
3. The ratio of "overhead bytes" (headers) to "payload bytes" (body)

For a typical JSON body like `{"items": ["burger"], "customer_id": 42}`, what percentage of the total transfer is HTTP overhead?

**Hint:** You can use `http.client` with `set_debuglevel(1)` to see raw bytes, or intercept at the socket level.

<details>
<summary>Solution</summary>

```python
import http.client
import json

body = json.dumps({"items": ["burger"], "customer_id": 42})

conn = http.client.HTTPConnection("localhost", 8000)
conn.set_debuglevel(1)  # Prints raw request/response
conn.request(
    "POST", "/orders",
    body=body,
    headers={"Content-Type": "application/json", "Content-Length": str(len(body))}
)
resp = conn.getresponse()
resp_body = resp.read()

# Approximate calculation:
request_headers = (
    f"POST /orders HTTP/1.1\r\n"
    f"Host: localhost:8000\r\n"
    f"Content-Type: application/json\r\n"
    f"Content-Length: {len(body)}\r\n"
    f"\r\n"
)
req_overhead = len(request_headers.encode())
req_payload = len(body.encode())

# Response headers are typically ~200 bytes for a simple JSON response
# The resp_body is maybe 40-60 bytes
# Overhead ratio for small payloads is often 70-80%!

print(f"Request overhead: {req_overhead} bytes")
print(f"Request payload:  {req_payload} bytes")
print(f"Response body:    {len(resp_body)} bytes")
print(f"Overhead ratio:   {req_overhead / (req_overhead + req_payload):.0%}")
```

For a 42-byte JSON payload, request headers are ~120-150 bytes. Response headers add another ~200 bytes. The response body might be ~60 bytes. **Total overhead is often 70-80% for small payloads.** This is why short polling (Ch02) is so expensive -- most of the bandwidth is headers, not data.

</details>

---

## Exercise 4 -- Connection Pooling Design [Intermediate]

**Design Problem:** FoodDash's backend makes 3 downstream HTTP calls for every incoming order:

- `POST payment-service:8001/charge`
- `POST kitchen-service:8002/ticket`
- `POST notification-service:8003/email`

Currently each call opens a new TCP connection, does the TLS handshake, sends the request, reads the response, and closes the connection.

1. Draw a timeline showing the latency cost of 3 sequential calls, each with a fresh TCP+TLS connection.
2. Redesign with connection pooling. How many connections should the pool hold per downstream service? What's the formula?
3. What happens to idle connections? How do you detect a half-closed connection where the server sent FIN but your pool still thinks it's alive?

**Hint:** Think about Little's Law: `L = lambda * W` (connections_in_use = request_rate * avg_response_time).

<details>
<summary>Solution</summary>

**1. Timeline (sequential, no pooling):**
```
t=0ms    TCP handshake to payment (1.5 RTT)        ~3ms
t=3ms    TLS handshake (2 RTT for TLS 1.2)         ~6ms
t=9ms    POST /charge + response                    ~50ms
t=59ms   TCP close (FIN handshake)                  ~2ms
t=61ms   TCP handshake to kitchen                   ~3ms
t=64ms   TLS handshake                              ~6ms
t=70ms   POST /ticket + response                    ~30ms
...
Total: ~180ms just for 3 calls (connection setup is ~40% of time)
```

**2. Connection pooling:**
Using Little's Law: if FoodDash handles 100 orders/sec, each downstream call takes ~50ms:
- `L = 100 req/s * 0.05s = 5 connections` needed per service at steady state
- Add headroom for bursts: `pool_size = ceil(L * 2) = 10` per service
- With pooling, the TCP+TLS handshake cost is amortized. The 3 calls (if parallelized) take only `max(50, 30, 20) = 50ms` instead of 180ms.

**3. Idle connection management:**
- Set a **max idle time** (e.g., 90 seconds). Evict connections idle longer than this.
- Before reusing a pooled connection, do a **liveness check**: attempt a non-blocking read. If you get EOF (FIN received), discard and open a new one. Python's `urllib3` does exactly this.
- Alternatively, use HTTP/2 with PING frames as a heartbeat.
- Watch out for the **server's** idle timeout being shorter than yours. If the server closes at 60s but your pool thinks it's good for 90s, you'll get "Connection reset" errors. Always set your pool's idle timeout *below* the server's.

</details>

---

## Exercise 5 -- The Nagle-Delayed ACK Interaction [Principal]

**Question:** A FoodDash engineer notices that small JSON responses (< 100 bytes) from the order service sometimes take an extra 40ms to arrive. The network RTT is only 1ms. After investigation, they find the issue is the interaction between **Nagle's algorithm** and **TCP delayed ACK**.

1. Explain what Nagle's algorithm does and why it exists.
2. Explain what delayed ACK does and why it exists.
3. How do these two algorithms interact to create a 40ms artificial delay in the following scenario: the server sends a 60-byte HTTP response in one `write()` call, then sends a 20-byte trailer in a second `write()` call?
4. Name three different ways to fix this. Which would you choose for FoodDash and why?

**Hint:** Nagle says "don't send small packets if there's unacknowledged data in flight." Delayed ACK says "don't ACK immediately -- wait up to 40ms to piggyback the ACK on data going the other direction."

<details>
<summary>Solution</summary>

**1. Nagle's algorithm** (RFC 896): If there is unacknowledged data in flight and the data to send is smaller than MSS (~1460 bytes), buffer it and wait until either (a) the outstanding ACK arrives, or (b) enough data accumulates to fill a segment. Purpose: prevent "silly window syndrome" where tiny packets waste bandwidth.

**2. Delayed ACK** (RFC 1122): Instead of ACKing every segment immediately, wait up to 40ms (200ms on some OSes) hoping to piggyback the ACK on a data segment going the other direction. Purpose: reduce the number of pure-ACK packets on the network.

**3. The deadly interaction:**
```
Server sends 60-byte response (segment 1) -> Client receives it
  Client's delayed ACK timer starts (wait up to 40ms)
Server wants to send 20-byte trailer (segment 2)
  Nagle says: "There's unACKed data in flight (segment 1) and
  this data (20 bytes) is < MSS. HOLD IT."
  -> Server waits for ACK of segment 1
Client is waiting 40ms before ACKing segment 1
  -> DEADLOCK for up to 40ms
Client's delayed ACK timer fires, sends ACK
Server receives ACK, Nagle releases segment 2
  -> 20-byte trailer finally sent
```
Result: 40ms artificial latency for a 20-byte write.

**4. Three fixes:**

- **`TCP_NODELAY`**: Disable Nagle's algorithm entirely (`setsockopt(TCP_NODELAY, 1)`). Every `write()` sends immediately. Slight bandwidth increase from small packets but latency-optimal.
- **Coalesce writes**: Instead of two `write()` calls, combine the response + trailer into a single `write()`. Nagle never triggers because there's no second small write.
- **`TCP_CORK` / `TCP_NOPUSH`** (Linux/BSD): Cork the socket, do both writes, then uncork. The OS sends everything in one segment.

**Best for FoodDash:** `TCP_NODELAY` is the standard choice for request-response services. Most web frameworks (uvicorn, nginx) enable it by default. The bandwidth cost of small packets is negligible compared to the 40ms latency penalty. Coalescing writes is fragile (framework-dependent). `TCP_CORK` is Linux-specific and requires explicit cork/uncork around every response.

</details>
