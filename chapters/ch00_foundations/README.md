# Chapter 00 -- Foundations: TCP and HTTP from First Principles

> **Before we build FoodDash, we need to understand what happens when two computers talk.**

Most developers treat HTTP as a magic incantation: you call `requests.post()` or `fetch()`,
and somehow JSON appears on the other side. We never ask what "a connection" actually *is*
--- what bytes move across the wire, what the kernel does with them, what the CPU and memory
costs are, or why certain failure modes exist.

This chapter strips away every abstraction. By the end, you will be able to draw the exact
sequence of bytes that flow when a FoodDash customer's browser requests the restaurant menu,
and you will understand the CPU, memory, and latency cost of every step.

---

## Table of Contents

1. [TCP from First Principles](#tcp-from-first-principles)
2. [HTTP on Top of TCP](#http-on-top-of-tcp)
3. [TLS: Encrypting the Channel](#tls-encrypting-the-channel)
4. [Systems Constraints Analysis](#systems-constraints-analysis)
5. [Production Depth](#production-depth)
6. [Trade-offs](#trade-offs)
7. [Running the Code](#running-the-code)
8. [Bridge to Chapter 01](#bridge-to-chapter-01)

---

## TCP from First Principles

TCP (Transmission Control Protocol) provides **reliable, ordered, byte-stream** delivery
over an unreliable network. Every HTTP request you have ever made rides on TCP. Let us
understand exactly what that means.

### What Is a "Connection"?

A TCP connection is not a physical wire. It is **state held in kernel memory on both ends**,
identified by a 4-tuple:

```
(source_ip, source_port, destination_ip, destination_port)
```

The kernel maintains this state in a data structure (the socket/connection table). Each
entry costs roughly **1-4 KB of kernel memory** --- the receive buffer, send buffer,
congestion window state, timer state, and the TCP control block itself.

### The 3-Way Handshake

Before any data flows, TCP establishes shared state via a 3-way handshake. This costs
**1.5 round-trip times (RTT)** before the first byte of application data can be sent.

```mermaid
sequenceDiagram
    participant C as Client (FoodDash App)
    participant S as Server (FoodDash API)

    Note over C: State: CLOSED
    Note over S: State: LISTEN

    C->>S: SYN (seq=x)
    Note over C: State: SYN_SENT
    Note over S: Kernel allocates half-open connection entry

    S->>C: SYN-ACK (seq=y, ack=x+1)
    Note over S: State: SYN_RECEIVED

    C->>S: ACK (ack=y+1)
    Note over C: State: ESTABLISHED
    Note over S: State: ESTABLISHED
    Note over S: Moved from SYN queue to accept queue

    C->>S: HTTP GET /menu (first data segment)
```

**What is actually happening in the kernel:**

1. **Client calls `connect()`**: The kernel crafts a SYN segment, picks an ephemeral port
   (typically 49152-65535), sets the initial sequence number (ISN) from a clock-based
   algorithm (to prevent sequence number prediction attacks), and places the socket in
   `SYN_SENT` state.

2. **Server receives SYN**: If the server's `listen()` backlog is not full, the kernel
   creates a half-open connection entry in the **SYN queue**. Under SYN flood attacks,
   this queue fills up --- which is why modern kernels use **SYN cookies** (a stateless
   way to validate the handshake without allocating memory until the ACK arrives).

3. **Client receives SYN-ACK, sends ACK**: The connection moves to `ESTABLISHED`. The
   server's kernel moves the connection from the SYN queue to the **accept queue**, where
   the application's `accept()` call can pick it up.

### TCP Segment Layout

Every TCP segment has this structure. Understanding it explains most TCP behavior:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          Source Port          |       Destination Port        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        Sequence Number                        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                    Acknowledgment Number                      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  Data |       |C|E|U|A|P|R|S|F|                               |
| Offset| Rsrvd |W|C|R|C|S|S|Y|I|            Window             |
|       |       |R|E|G|K|H|T|N|N|                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           Checksum            |         Urgent Pointer        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                    Options (variable length)                   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                             Data                              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

**Key fields explained:**

- **Sequence Number (32 bits)**: Byte offset of the first data byte in this segment within
  the stream. This is how TCP provides ordering --- the receiver can reassemble segments
  that arrive out of order.
- **Acknowledgment Number (32 bits)**: The next sequence number the sender expects to
  receive. This is cumulative --- ACKing byte 5000 means "I have received everything up to
  byte 4999."
- **Window (16 bits)**: How many bytes the receiver is willing to accept. This is **flow
  control** --- the receiver throttles the sender to avoid buffer overflow. With window
  scaling (an option), this can represent up to ~1 GB.
- **Flags**: SYN (synchronize), ACK (acknowledge), FIN (finish), RST (reset), PSH (push
  data to application immediately).

### Sequence Numbers and Reliability

TCP numbers every byte. If the client sends "GET /menu" (9 bytes) starting at sequence
number 1000, the segment header says `seq=1000`. The server ACKs with `ack=1009` ("I got
bytes 1000-1008, send me 1009 next").

If a segment is lost, the sender detects it via:

1. **Timeout**: No ACK arrives within the retransmission timeout (RTO), which is
   dynamically computed from measured RTT using Jacobson's algorithm.
2. **Triple duplicate ACK**: The receiver keeps ACKing the last contiguous byte it
   received. Three duplicate ACKs trigger **fast retransmit** without waiting for the
   timeout.

### Flow Control

The receiver advertises its **receive window** --- the amount of free space in its receive
buffer. If the application is slow to read data (say, FoodDash is doing a heavy database
query), the receive buffer fills up, the window shrinks to zero, and the sender pauses.

This is per-connection flow control. It prevents a fast sender from overwhelming a slow
receiver. The default receive buffer on Linux is typically 87380 bytes (tunable via
`net.ipv4.tcp_rmem`).

### Congestion Control

Flow control prevents overwhelming the *receiver*. Congestion control prevents
overwhelming the *network*.

TCP maintains a **congestion window (cwnd)** --- the number of bytes the sender can have
in flight (sent but not yet ACKed). The algorithm:

1. **Slow Start**: cwnd starts at ~10 segments (since Linux 3.0, IW10). It doubles every
   RTT (exponential growth) until a loss occurs or cwnd reaches the slow-start threshold.
2. **Congestion Avoidance**: After the threshold, cwnd grows by ~1 segment per RTT (linear
   growth).
3. **On Loss**: cwnd is cut (halved for CUBIC, the Linux default). This is why a single
   packet loss can dramatically reduce throughput.

**Why this matters for FoodDash**: A new TCP connection starts with cwnd of ~14 KB. If our
JSON menu response is 50 KB, the first flight can only carry 14 KB. We need several RTTs
to ramp up and deliver the full response. This is why **connection reuse** (HTTP
keep-alive) is so important --- a warm connection has a large cwnd.

---

## HTTP on Top of TCP

HTTP is **plain text** sent over a TCP connection. There is no magic. Let us see exactly
what bytes cross the wire.

### HTTP Request Format

When the FoodDash app requests the menu, here is the literal text sent over TCP:

```
GET /api/v1/restaurants/42/menu HTTP/1.1\r\n
Host: api.fooddash.example.com\r\n
Accept: application/json\r\n
Accept-Encoding: gzip, deflate\r\n
Connection: keep-alive\r\n
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...\r\n
\r\n
```

That is it. Literally ASCII text with `\r\n` line endings and a blank line to mark the end
of headers. No binary framing, no magic. The structure:

```
 +------------------------------------------------------+
 | Request Line: METHOD  SP  PATH  SP  VERSION  CRLF    |
 |   e.g., GET /api/v1/menu HTTP/1.1\r\n                |
 +------------------------------------------------------+
 | Header: Name: Value CRLF                             |
 | Header: Name: Value CRLF                             |
 |   ...                                                |
 +------------------------------------------------------+
 | CRLF  (blank line = end of headers)                  |
 +------------------------------------------------------+
 | Body (optional, for POST/PUT/PATCH)                  |
 |   Content-Length or Transfer-Encoding: chunked        |
 +------------------------------------------------------+
```

### HTTP Response Format

The server responds with similarly structured text:

```
HTTP/1.1 200 OK\r\n
Content-Type: application/json\r\n
Content-Length: 1847\r\n
X-Request-Id: fd-req-a1b2c3\r\n
\r\n
{"restaurant_id": 42, "menu": [{"item": "Margherita Pizza", ...}]}
```

### Status Codes (The Ones That Actually Matter)

| Code | Meaning | FoodDash Example |
|------|---------|------------------|
| 200  | OK | Menu loaded successfully |
| 201  | Created | New order placed |
| 301  | Moved Permanently | Old API endpoint redirected |
| 304  | Not Modified | Menu hasn't changed, use cached version |
| 400  | Bad Request | Invalid order (missing delivery address) |
| 401  | Unauthorized | Expired auth token |
| 404  | Not Found | Restaurant doesn't exist |
| 429  | Too Many Requests | Rate limit hit |
| 500  | Internal Server Error | Database crashed |
| 502  | Bad Gateway | Upstream service unreachable |
| 503  | Service Unavailable | Server overloaded |
| 504  | Gateway Timeout | Upstream service too slow |

### Content Negotiation

The client says what it wants via `Accept` headers. The server responds with what it chose:

```
Client:  Accept: application/json, text/html;q=0.9
Server:  Content-Type: application/json; charset=utf-8
```

The `q` parameter is a quality factor (0-1). The server picks the highest-q format it
supports. For FoodDash, this is almost always `application/json`, but the pattern matters
when building APIs that serve both browsers and mobile clients.

### HTTP Request/Response Lifecycle

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    Note over C,S: TCP Connection Already Established

    C->>S: GET /api/v1/menu HTTP/1.1<br/>Host: api.fooddash.example.com<br/>Accept: application/json

    Note over S: Parse request line<br/>Route to handler<br/>Query database<br/>Serialize JSON

    S->>C: HTTP/1.1 200 OK<br/>Content-Type: application/json<br/>Content-Length: 1847<br/><br/>{"menu": [...]}

    Note over C,S: Connection remains open (keep-alive)

    C->>S: POST /api/v1/orders HTTP/1.1<br/>Content-Type: application/json<br/><br/>{"items": [1, 3], "address": "..."}

    S->>C: HTTP/1.1 201 Created<br/>Location: /api/v1/orders/99
```

---

## TLS: Encrypting the Channel

In production, FoodDash uses HTTPS. TLS sits between TCP and HTTP, adding encryption and
authentication. The cost is real and worth understanding.

### TLS 1.3 Handshake

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    Note over C,S: TCP handshake already complete (1 RTT spent)

    C->>S: ClientHello<br/>+ supported ciphers<br/>+ key_share (ECDHE public key)<br/>+ supported_versions: TLS 1.3

    Note over S: Select cipher suite<br/>Generate key share<br/>Derive handshake keys

    S->>C: ServerHello + key_share<br/>EncryptedExtensions<br/>Certificate<br/>CertificateVerify<br/>Finished

    Note over C: Verify certificate chain<br/>Derive handshake keys<br/>Derive application keys

    C->>S: Finished

    Note over C,S: Application data can now flow<br/>(TLS 1.3 = 1 RTT for handshake)
```

**TLS 1.3 vs 1.2**: TLS 1.2 required 2 RTTs for the handshake; TLS 1.3 reduced this to
1 RTT by having the client send its key share in the first message. With **0-RTT resumption**
(PSK), a returning client can send application data in the very first flight --- but this
is replay-vulnerable and should only be used for idempotent requests (GET, not POST).

### CPU Cost of TLS

The TLS handshake is CPU-intensive:

- **ECDHE key exchange**: ~0.5ms per handshake on modern hardware (P-256 curve)
- **RSA-2048 signature verification**: ~0.1ms
- **Certificate chain validation**: Depends on chain length; typically 2-3 certs
- **Symmetric encryption (AES-GCM)**: Nearly free with AES-NI hardware instructions
  (~1 cycle/byte). A 50 KB JSON response adds ~50 microseconds.

The handshake is the expensive part. Once established, encryption overhead is negligible.
This is another reason connection reuse matters.

---

## Systems Constraints Analysis

Let us quantify the cost of a single HTTP request from the FoodDash mobile app to our API
server. Every resource the system spends is a resource it cannot spend on another request.

### CPU Costs

| Operation | CPU Time | Notes |
|-----------|----------|-------|
| SYN cookie computation | ~1 us | HMAC-based, only under SYN flood |
| TCP checksum (1 KB segment) | ~0.5 us | Hardware offloaded on modern NICs |
| TLS handshake (ECDHE + verify) | ~500 us | One-time per connection |
| AES-GCM encrypt (50 KB response) | ~50 us | AES-NI hardware acceleration |
| HTTP header parsing | ~10-50 us | Depends on header count |
| Syscall overhead (read/write) | ~1-5 us each | Context switch user/kernel |

### Memory Costs

| Resource | Memory | Notes |
|----------|--------|-------|
| TCP socket (kernel) | 1-4 KB | Connection control block |
| Receive buffer (default) | 87 KB | `net.ipv4.tcp_rmem` default |
| Send buffer (default) | 16-87 KB | `net.ipv4.tcp_wmem` default |
| TLS session state | ~1-2 KB | Cipher state, keys |
| Application buffer | Varies | Your framework's per-request allocation |
| **Total per connection** | **~100-180 KB** | Kernel + TLS + app overhead |

At 10,000 concurrent connections, that is **~1-1.8 GB** just for connection state. This is
why the C10K problem was hard, and why modern servers use epoll/kqueue to avoid one-thread-
per-connection memory overhead.

### Network I/O Costs

- **3-way handshake**: 1.5 RTT (SYN, SYN-ACK, ACK) before data flows
- **TLS handshake**: 1 RTT additional (TLS 1.3) or 2 RTT (TLS 1.2)
- **Per-request header overhead**: ~200-800 bytes of HTTP headers per request. For small
  API responses, headers can be larger than the body.
- **TCP slow start**: New connections start with cwnd ~14 KB. Large responses require
  multiple RTTs to ramp up throughput.

### Latency Breakdown: The Full Picture

When a FoodDash customer opens the app and requests the menu for the first time:

```
Time --|----|----|----|----|----|----|----|----|----|----|-->
       0   10   20   30   40   50   60   70   80   90  100ms

       |<-- DNS Lookup -->|
       |    (~20-50ms)    |
                          |<- TCP Handshake ->|
                          |   (1 RTT = 30ms)  |
                                              |<--- TLS Handshake --->|
                                              |   (1 RTT = 30ms)     |
                                                                     |<- HTTP ->|
                                                                     | Req+Resp |
                                                                     | (1 RTT)  |

       |<=================== Total: 3.5-4.5 RTT ===================>|
       |                     (~110-150ms typical)                     |

       DNS         TCP          TLS           HTTP
       [=========][==========][============][=========]
       ~20-50ms    ~30ms        ~30ms         ~30ms
```

**Each stage in detail:**

1. **DNS Lookup (variable, 0-50ms)**:
   - Cached in OS resolver: ~0ms
   - Cached in local DNS: ~1-5ms
   - Full recursive resolution: ~20-100ms
   - FoodDash mitigation: DNS prefetching, short TTLs with CDN

2. **TCP Handshake (1 RTT)**:
   - SYN to server, SYN-ACK back, ACK to server
   - Dominated by physical distance (speed of light)
   - NYC to SF: ~30ms RTT. NYC to London: ~70ms RTT.

3. **TLS Handshake (1 RTT for TLS 1.3)**:
   - ClientHello + key share, ServerHello + cert + finished
   - CPU-bound on the server side (key exchange + signing)
   - 0-RTT resumption eliminates this for returning clients

4. **HTTP Request/Response (1+ RTT)**:
   - Request flies to server, response comes back
   - If response > cwnd (~14 KB), multiple RTTs for TCP slow start
   - Server processing time adds to this

### What This Means for FoodDash

A customer in New York hitting our API server in Virginia (~15ms RTT):

- **First request, cold**: 15ms DNS + 15ms TCP + 15ms TLS + 15ms HTTP = **~60ms**
- **Subsequent request, same connection**: **~15ms** (just HTTP, connection reused)
- **4x improvement from connection reuse alone**

A customer in Mumbai hitting our Virginia server (~150ms RTT):

- **First request, cold**: ~150ms DNS + 150ms TCP + 150ms TLS + 150ms HTTP = **~600ms**
- **Subsequent request, same connection**: **~150ms**
- **This is why CDNs and regional deployments exist**

---

## Production Depth

These are the details that matter in production. Each of
these has caused production incidents at scale.

### Nagle's Algorithm and TCP_NODELAY

**Nagle's algorithm** (RFC 896) buffers small writes. If you have unACKed data in flight,
the kernel holds your small write until either (a) it accumulates a full MSS (~1460 bytes),
or (b) the outstanding ACK arrives.

**The problem**: Nagle interacts terribly with **delayed ACKs** (where the receiver waits
up to 40ms before ACKing, hoping to piggyback the ACK on a response). The combination:

1. Client sends small write (e.g., HTTP headers)
2. Nagle holds the next small write (HTTP body), waiting for ACK
3. Server's delayed ACK waits 40ms, hoping to piggyback
4. Result: **40ms artificial delay** on every request

**The fix**: Set `TCP_NODELAY` on the socket. Every HTTP server framework does this. If you
are writing raw socket code (like our examples), you must do it yourself:

```python
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
```

### TIME_WAIT and Ephemeral Port Exhaustion

When a TCP connection closes, the side that initiates the close enters **TIME_WAIT** state
for **2 * MSL** (Maximum Segment Lifetime, typically 60 seconds on Linux). This exists to:

1. Ensure delayed segments from the old connection do not corrupt a new connection on the
   same 4-tuple.
2. Ensure the final ACK of the FIN is delivered (if lost, the peer retransmits the FIN).

**The problem at scale**: If FoodDash's API server makes 1000 outbound HTTP requests per
second to microservices (each on a new connection), and each TIME_WAIT lasts 60 seconds,
we accumulate **60,000 sockets in TIME_WAIT**. Since ephemeral ports range from 49152 to
65535 (~16,000 ports), and each TIME_WAIT locks a port *for a specific destination*, we can
hit ephemeral port exhaustion connecting to a single backend service.

**Mitigations:**

- **Connection pooling**: Reuse connections instead of creating new ones (the most
  important fix)
- `net.ipv4.tcp_tw_reuse = 1`: Allow reusing TIME_WAIT sockets for new *outbound*
  connections (safe with timestamps)
- `SO_LINGER` with timeout 0: Send RST instead of FIN, skipping TIME_WAIT entirely (data
  loss risk --- use only when you are certain all data was received)
- Increase ephemeral port range: `net.ipv4.ip_local_port_range = 1024 65535`

### Keep-Alive vs New Connections

HTTP/1.1 defaults to `Connection: keep-alive` --- the TCP connection stays open after the
response, ready for the next request.

**Benefits of keep-alive:**

- Skip TCP handshake (1 RTT saved)
- Skip TLS handshake (1 RTT saved)
- TCP congestion window is warm (no slow start)
- No TIME_WAIT accumulation

**Costs of keep-alive:**

- Memory: idle connections consume ~100-180 KB each
- File descriptors: each open socket is an fd; default ulimit is often 1024
- Head-of-line blocking: in HTTP/1.1, only one request can be in-flight per connection
- Load balancing: long-lived connections may not distribute evenly

**FoodDash's approach**: Use connection pooling with a **max idle time** (say, 60 seconds)
and a **max connections per host** (say, 100). This balances reuse against resource waste.

### Head-of-Line Blocking in HTTP/1.1

HTTP/1.1 is strictly request-response on a single connection: request 1 must complete
before request 2 can start. If request 1 is slow (large response, slow backend), request 2
waits.

```
Connection 1:  [--Request A (200ms)--][--Request C (50ms)--]
Connection 2:  [--Request B (50ms)--]...(idle, waiting for work)

Request C is delayed 200ms even though it would only take 50ms,
because it is queued behind Request A on Connection 1.
```

**HTTP/1.1 workaround**: Browsers open 6 parallel TCP connections per hostname. This
partially mitigates HOL blocking but wastes resources (6x the memory, 6x the handshakes).

**HTTP/2 solution**: Multiplexing --- multiple streams share a single TCP connection,
interleaved at the frame level. But HTTP/2 has its own HOL blocking at the TCP layer
(a lost TCP segment blocks all streams). This is solved by HTTP/3 (QUIC over UDP).

We will explore HTTP/2 and HTTP/3 in later chapters.

### Connection Pooling

```mermaid
graph LR
    subgraph "FoodDash API Server"
        A[Request Handler 1]
        B[Request Handler 2]
        C[Request Handler 3]
    end

    subgraph "Connection Pool (max=5)"
        P1[Conn 1 - active]
        P2[Conn 2 - active]
        P3[Conn 3 - idle]
        P4[Conn 4 - idle]
        P5[Conn 5 - idle]
    end

    subgraph "Database / Upstream"
        DB[(PostgreSQL)]
    end

    A --> P1
    B --> P2
    P1 --> DB
    P2 --> DB
    P3 -.-> DB
    P4 -.-> DB
    P5 -.-> DB
```

A connection pool maintains a set of pre-established TCP connections. When a request
handler needs a connection, it **borrows** one from the pool. When done, it **returns** it.
No handshake, no slow start, no TIME_WAIT accumulation.

Pool sizing is critical: too small and requests queue; too large and you waste memory and
may overwhelm the backend. A good starting point is **2x the number of CPU cores** for
database connections, tuned based on observed wait times.

---

## Trade-offs

### Connection Reuse vs Fresh Connections

| Dimension | Reuse (keep-alive/pool) | Fresh (new per request) |
|-----------|------------------------|------------------------|
| Latency | Low (skip handshakes) | High (1.5-3.5 RTT overhead) |
| Memory | Higher (idle connections held) | Lower (freed after response) |
| Throughput | Higher (warm cwnd) | Lower (slow start each time) |
| Load balancing | Sticky (same backend) | Natural (DNS/LB per-request) |
| Failure blast radius | Connection death kills in-flight requests | Isolated failures |
| Complexity | Pool management, health checks | Simple, stateless |
| TIME_WAIT | Minimal | Can exhaust ephemeral ports |

**Verdict for FoodDash**: Always use connection pools for backend-to-backend communication.
For client-to-server, rely on HTTP keep-alive with reasonable timeouts.

### TCP vs UDP for Different Workloads

| Dimension | TCP | UDP |
|-----------|-----|-----|
| Delivery | Reliable, ordered | Best-effort, unordered |
| Connection | Stateful (handshake required) | Stateless (fire and forget) |
| Overhead | 20-60 byte header, handshake, ACKs | 8 byte header, no handshake |
| Latency | Higher (handshake + retransmits) | Lower (no handshake, no retransmit wait) |
| Use case | HTTP APIs, database queries, file transfers | DNS lookups, video streaming, real-time gaming, QUIC |
| FoodDash use | API calls, order processing | Real-time driver location updates (potentially), DNS |

**Key insight**: HTTP/3 uses QUIC, which is a reliable protocol built on *UDP*. It gets
TCP-like reliability with UDP-like flexibility (no HOL blocking, faster handshakes). We
will cover this in later chapters.

---

## Running the Code

This chapter includes hands-on code to make the concepts concrete.

### Prerequisites

Make sure you have [uv](https://docs.astral.sh/uv/) installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### TCP Echo Server

A bare TCP server using Python's `socket` module. Every syscall is annotated.

```bash
# Start the echo server (listens on port 9000)
uv run python -m chapters.ch00_foundations.tcp_echo_server

# In another terminal, connect and type messages:
telnet localhost 9000
# Or using netcat:
nc localhost 9000
```

### HTTP From Scratch

A minimal HTTP server built on raw sockets --- no framework, no `http.server`. This is
what Flask/FastAPI do under the hood.

```bash
# Start the HTTP server (listens on port 8000)
uv run python -m chapters.ch00_foundations.http_from_scratch

# In another terminal, make a request:
curl -v http://localhost:8000/
curl -v http://localhost:8000/api/v1/status
```

### Interactive Visualization

Open the HTML visualization in your browser to see animated TCP and HTTP flows:

```bash
open chapters/ch00_foundations/visual.html
# Or on Linux:
xdg-open chapters/ch00_foundations/visual.html
```

---

## Bridge to Chapter 01

We now understand what happens at the wire level:

- TCP provides a reliable byte stream, but it costs **1.5 RTT** to establish and
  **~100-180 KB of memory** per connection.
- HTTP is plain text riding on TCP, with a predictable request/response structure.
- TLS adds security at the cost of **1 additional RTT** and **CPU for key exchange**.
- Connection reuse (keep-alive, pooling) is the single most impactful optimization ---
  saving 2.5+ RTTs and avoiding slow start.

With this foundation, we are ready to build. In **Chapter 01: Synchronous Foundations**,
we will create FoodDash's first HTTP endpoint using FastAPI, understanding exactly what
the framework is abstracting over. When we call `@app.get("/menu")`, we will know that
FastAPI is:

1. Listening on a TCP socket via `socket() -> bind() -> listen() -> accept()`
2. Parsing the raw HTTP request text we just studied
3. Routing to our handler function
4. Serializing our return value to JSON
5. Crafting the HTTP response text with proper headers
6. Writing it back over the TCP connection

No more black boxes. Let us build.

**Next: [Chapter 01 -- Synchronous Foundations](../ch01_sync_foundations/)**
