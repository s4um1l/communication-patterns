# Appendix A — gRPC / Protocol Buffers

## The Scene

FoodDash is humming along. Twelve chapters of communication patterns have taken you from simple request-response to a synthesis of real-time techniques. Everything runs over REST + JSON on HTTP/1.1, and for customer-facing endpoints, that's exactly right: browsers understand it natively, every developer on earth can debug it with `curl`, and the tooling ecosystem is vast.

But zoom in on what happens *inside* your backend when a customer places an order:

```
Browser                FoodDash Backend
  │                        │
  │  POST /orders (JSON)   │
  │───────────────────────>│
  │                        │──── order-service ──── kitchen-service (JSON over HTTP/1.1)
  │                        │──── order-service ──── billing-service (JSON over HTTP/1.1)
  │                        │──── order-service ──── driver-matching  (JSON over HTTP/1.1)
  │                        │──── order-service ──── notification-svc (JSON over HTTP/1.1)
  │  201 Created (JSON)    │
  │<───────────────────────│
```

Each of those internal calls carries ~800 bytes of HTTP headers for a ~150 byte JSON body. Each one serializes Python objects to JSON text, sends it, then deserializes on the other side. At 10,000 orders per minute, your inter-service communication is spending **more time encoding headers and parsing JSON than doing actual business logic**.

This is the problem gRPC was built to solve.

## Why gRPC Matters

You've built FoodDash using REST + JSON over HTTP/1.1. It works, but your inter-service communication (order to kitchen, order to billing, order to driver-matching) is spending more time serializing JSON and parsing HTTP headers than doing actual work. gRPC replaces both the serialization format (JSON to Protobuf) and the transport (HTTP/1.1 to HTTP/2) for dramatic efficiency gains.

gRPC is not a replacement for REST in all contexts. It's a surgical upgrade for the **internal, high-throughput, latency-sensitive** paths in your system. The customer's browser still talks REST. But service-to-service? That's where gRPC transforms performance.

**What gRPC gives you:**

| Dimension | REST + JSON + HTTP/1.1 | gRPC + Protobuf + HTTP/2 |
|-----------|----------------------|--------------------------|
| Serialization format | JSON (text, ~300 bytes/order) | Protobuf (binary, ~80 bytes/order) |
| Transport | HTTP/1.1 (one request per connection at a time) | HTTP/2 (multiplexed streams) |
| Schema | Optional (OpenAPI, added after) | Required (`.proto` files, code generated) |
| Type safety | Runtime validation | Compile-time guarantees |
| Streaming | Hacks (SSE, WebSocket) | Native (four patterns built in) |
| Browser support | Native | Requires grpc-web proxy |

---

## Protocol Buffers — The Wire Format

### Schema-First Development

With REST + JSON, you typically write code first and document the API later (OpenAPI/Swagger). With Protocol Buffers, you **write the schema first** in a `.proto` file, then **generate code** from it.

```protobuf
// fooddash.proto — the schema IS the contract
syntax = "proto3";
package fooddash;

message MenuItem {
  string id = 1;
  string name = 2;
  int32 price_cents = 3;
  string description = 4;
}
```

Run `protoc --python_out=. fooddash.proto` and you get Python classes with serialization, deserialization, type checking, and validation built in. No Pydantic needed. No manual parsing. The schema **is** the source of truth for every language in your system.

### Binary Encoding — How It Works

JSON encodes data as human-readable text:

```json
{
  "id": "ord_a1b2",
  "customer": {
    "id": "cust_01",
    "name": "Alice",
    "address": "742 Evergreen Terrace"
  },
  "restaurant_id": "rest_01",
  "items": [
    {
      "menu_item": {
        "id": "item_01",
        "name": "Classic Burger",
        "price_cents": 899
      },
      "quantity": 2
    }
  ],
  "status": "placed"
}
```

**Size: ~300 bytes.** Every field name is repeated as text. Braces, colons, quotes, commas — all structural overhead.

Protobuf encodes the same data as binary:

```
Field 1 (id):            tag=0x0A, len=8,  "ord_a1b2"
Field 2 (customer):      tag=0x12, len=36, {nested message}
  Field 1 (id):          tag=0x0A, len=7,  "cust_01"
  Field 2 (name):        tag=0x12, len=5,  "Alice"
  Field 3 (address):     tag=0x1A, len=20, "742 Evergreen Terrace"
Field 3 (restaurant_id): tag=0x1A, len=7,  "rest_01"
Field 4 (items):         tag=0x22, len=27, {nested repeated}
Field 5 (status):        tag=0x28, varint=1
```

**Size: ~80 bytes.** No field names on the wire — just field numbers (tags). No quotes, no braces. Integers are variable-length encoded (varint), so small numbers take fewer bytes.

### ASCII Art: Protobuf Binary Layout

```
Protobuf Binary Encoding of a FoodDash Order (~80 bytes)
═══════════════════════════════════════════════════════════

Byte:  0x0A  0x08  o  r  d  _  a  1  b  2   0x12  0x24 ...
       ├───┤ ├───┤ ├──────────────────────┤  ├───┤ ├───┤
       tag1  len8  field 1: id="ord_a1b2"    tag2  len36

Tag byte breakdown:
┌─────────────────────────────────────────────┐
│  0x0A = 0000 1010                           │
│         ├──────┤├┤                           │
│         field=1  wire_type=2 (length-delim)  │
└─────────────────────────────────────────────┘

Wire types:
  0 = Varint      (int32, int64, bool, enum)
  1 = 64-bit      (fixed64, double)
  2 = Length-delim (string, bytes, nested messages)
  5 = 32-bit      (fixed32, float)

JSON vs Protobuf — same order:
┌──────────────────────────────────────────────────────┐
│ JSON:     { " i d " : " o r d _ a 1 b 2 " , ...}    │
│ Bytes:    300 ████████████████████████████████████    │
│                                                      │
│ Protobuf: 0A 08 6F 72 64 5F 61 31 62 32 12 ...      │
│ Bytes:     80 ██████████                             │
│                                                      │
│ Savings:  4x smaller                                 │
└──────────────────────────────────────────────────────┘
```

### Schema Evolution — Adding Fields Without Breaking Clients

This is where Protobuf's design brilliance shows. Fields are identified by **numbers**, not names:

```protobuf
// Version 1 — original
message Order {
  string id = 1;
  string customer_id = 2;
  string status = 3;
}

// Version 2 — added delivery_eta, old clients still work
message Order {
  string id = 1;
  string customer_id = 2;
  string status = 3;
  int64 delivery_eta_unix = 4;  // NEW — old clients ignore field 4
}
```

An old client that only knows fields 1-3 will simply **skip** field 4 when deserializing. A new client talking to an old server will see field 4 as the default value (0 for integers, empty string for strings). No version negotiation. No breaking changes. This is forward and backward compatibility built into the wire format.

**Rules for safe evolution:**
- Never reuse a field number (even after deleting the field)
- Never change a field's type
- Adding new fields is always safe
- Removing fields is safe (but reserve the number)

### JSON vs Protobuf — The Trade-off

| Aspect | JSON | Protobuf |
|--------|------|----------|
| Human-readable | Yes — you can `curl` and read it | No — binary gibberish in terminal |
| Self-describing | Yes — field names in every message | No — need the `.proto` schema to decode |
| Size | Larger (text, redundant names) | 4-10x smaller |
| Parse speed | Slower (text parsing, type coercion) | 5-10x faster (binary, pre-compiled) |
| Schema required | No (but OpenAPI recommended) | Yes (mandatory `.proto` files) |
| Language support | Universal | Generated for 11+ languages |
| Debugging | Easy (`jq`, browser DevTools) | Hard (need `protoc --decode` or grpcurl) |
| Browser native | Yes | No (needs grpc-web) |

**The guideline**: Use JSON for the edges of your system (browser to server, public APIs, config files). Use Protobuf for the interior (service to service, high-throughput paths, storage of large datasets).

---

## gRPC — Four Communication Patterns in One

gRPC doesn't just give you faster request-response. It gives you **four communication patterns** in a single framework, each of which maps to something you've already learned in this repo:

### Pattern 1: Unary RPC (Request-Response)

This is Ch01, but binary and multiplexed.

```protobuf
rpc PlaceOrder(PlaceOrderRequest) returns (PlaceOrderResponse);
```

```mermaid
sequenceDiagram
    participant C as Order Service
    participant S as Kitchen Service
    C->>S: PlaceOrder(order_data)
    Note over S: Validate, store, assign kitchen
    S-->>C: PlaceOrderResponse(order_id, status)
```

**What's different from REST**: The request and response are binary Protobuf, not JSON. The call travels over an HTTP/2 stream, sharing a single TCP connection with other concurrent RPCs. Headers are HPACK-compressed. The method name is a path (`/fooddash.OrderService/PlaceOrder`), not an HTTP verb + URL combination.

### Pattern 2: Server-Streaming RPC (Like SSE, but Typed)

This is Ch04 (Server-Sent Events), but with binary payloads and a typed schema.

```protobuf
rpc StreamOrderUpdates(OrderUpdateRequest) returns (stream OrderUpdate);
```

```mermaid
sequenceDiagram
    participant C as Customer App
    participant S as Order Service
    C->>S: StreamOrderUpdates(order_id)
    S-->>C: OrderUpdate(status: confirmed)
    S-->>C: OrderUpdate(status: preparing)
    S-->>C: OrderUpdate(status: ready)
    S-->>C: OrderUpdate(status: picked_up, driver: "Bob")
    S-->>C: OrderUpdate(status: en_route, eta: 12min)
    S-->>C: OrderUpdate(status: delivered)
    Note over S: Stream closes
```

**What's different from SSE**: SSE sends `text/event-stream` — untyped text that you parse manually. gRPC server-streaming sends typed Protobuf messages. The client gets deserialized objects, not strings. Errors propagate as gRPC status codes, not HTTP status codes embedded in text. And you get flow control — if the client can't keep up, gRPC applies backpressure automatically.

### Pattern 3: Client-Streaming RPC (No HTTP Equivalent!)

This pattern has **no direct equivalent** in the HTTP patterns we've covered. The client sends a stream of messages, and the server responds once when the stream closes.

```protobuf
rpc BatchPlaceOrders(stream PlaceOrderRequest) returns (BatchOrderResponse);
```

```mermaid
sequenceDiagram
    participant C as Catering Service
    participant S as Order Service
    C->>S: PlaceOrder(order_1)
    C->>S: PlaceOrder(order_2)
    C->>S: PlaceOrder(order_3)
    C->>S: PlaceOrder(order_4)
    Note over C: Stream closes
    S-->>C: BatchOrderResponse(4 orders placed)
```

**Use case**: A catering company placing 50 orders at once. Instead of 50 separate RPCs (50 round trips) or one massive request body, the client streams orders one at a time. The server processes them as they arrive and responds with a summary. This is more memory-efficient than sending all 50 in one message, and more network-efficient than 50 separate calls.

### Pattern 4: Bidirectional Streaming (Like WebSockets, but Structured)

This is Ch05 (WebSockets), but with schema enforcement and HTTP/2.

```protobuf
rpc LiveChat(stream ChatMessage) returns (stream ChatMessage);
```

```mermaid
sequenceDiagram
    participant C as Customer App
    participant S as Support Service
    C->>S: ChatMessage("Where is my order?")
    S-->>C: ChatMessage("Let me check...")
    S-->>C: ChatMessage("Your order is being prepared")
    C->>S: ChatMessage("How long will it take?")
    S-->>C: ChatMessage("About 15 minutes")
    C->>S: ChatMessage("Thanks!")
    Note over C,S: Either side can close
```

**What's different from WebSockets**: WebSockets send arbitrary bytes or text — you define your own message format, your own error handling, your own reconnection logic. gRPC bidirectional streaming gives you typed messages (Protobuf), built-in error codes, deadline propagation, and automatic reconnection policies. The trade-off: you need the gRPC framework on both ends, whereas WebSockets work natively in browsers.

---

## HTTP/2 Under the Hood

gRPC **requires** HTTP/2. This isn't an arbitrary choice — every gRPC feature depends on HTTP/2 capabilities that don't exist in HTTP/1.1.

### Multiplexed Streams

In Ch09 (Multiplexing), we explored how HTTP/1.1 suffers from head-of-line blocking: one slow response blocks all subsequent responses on that connection. HTTP/2 solves this with **multiplexed streams**.

```
HTTP/1.1 — Sequential (one request at a time per connection):
┌──────────────────────────────────────────────────────┐
│ Connection 1: [Request A]──────[Response A]          │
│               [Request B]──────────────[Response B]  │
│               [Request C]──────[Response C]          │
│                                                      │
│ Connection 2: [Request D]──────[Response D]          │
│               [Request E]──────────[Response E]      │
│                                                      │
│ = 2 TCP connections, sequential within each          │
└──────────────────────────────────────────────────────┘

HTTP/2 — Multiplexed (all requests interleaved on one connection):
┌──────────────────────────────────────────────────────┐
│ Connection 1:                                        │
│   Stream 1: [Req A][Resp A chunk1][Resp A chunk2]    │
│   Stream 3: [Req B]  [Resp B chunk1][Resp B chunk2]  │
│   Stream 5: [Req C][Resp C]                          │
│   Stream 7: [Req D]    [Resp D chunk1][Resp D chunk2]│
│   Stream 9: [Req E]  [Resp E]                        │
│                                                      │
│ = 1 TCP connection, all requests concurrent          │
└──────────────────────────────────────────────────────┘
```

For gRPC, this means **hundreds of concurrent RPCs over a single TCP connection**. No connection pool needed. No head-of-line blocking at the HTTP layer (TCP-layer HOL blocking still exists, which is why HTTP/3 uses QUIC/UDP).

### Header Compression (HPACK)

HTTP/1.1 sends headers as uncompressed text on every request:

```
POST /orders HTTP/1.1
Host: api.fooddash.com
Content-Type: application/json
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
Accept: application/json
User-Agent: FoodDash-OrderService/2.1
X-Request-ID: 550e8400-e29b-41d4-a716-446655440000
```

That's ~400 bytes of headers, repeated identically on every request. Over 1000 requests, that's 400 KB of redundant header data.

HTTP/2 HPACK compression maintains a **header table** on both sides. After the first request, subsequent requests send only **the differences**:

```
First request:  ~400 bytes of headers (builds the table)
Second request: ~20 bytes  (references table entries, sends only changed values)
Third request:  ~15 bytes  (only X-Request-ID changed)
```

For gRPC's inter-service calls — where headers are nearly identical across requests — this reduces header overhead by **95-99%**.

### Binary Framing

HTTP/1.1 is a text protocol. The parser must scan byte-by-byte for `\r\n` delimiters, handle chunked encoding, and deal with ambiguous content-length scenarios. This parsing is a measurable CPU cost at high throughput.

HTTP/2 uses **binary frames**. Each frame has a fixed-size header (9 bytes) specifying the length, type, flags, and stream ID. The parser reads 9 bytes, knows exactly what's coming, and processes it. No scanning. No ambiguity.

```
HTTP/1.1 text frame:
  "POST /orders HTTP/1.1\r\nHost: api.fooddash.com\r\n..."
  Parser: scan for \r\n, split on :, trim whitespace...

HTTP/2 binary frame:
  [length: 3 bytes][type: 1 byte][flags: 1 byte][stream_id: 4 bytes][payload]
  Parser: read 9 bytes, done. Process payload of known length.
```

### Flow Control Per Stream

HTTP/2 provides **per-stream flow control**, which means a slow consumer on one RPC doesn't block other RPCs on the same connection. Each stream has its own flow control window. gRPC uses this to implement backpressure — if your order-service is overwhelmed by kitchen updates, it can slow down that specific stream without affecting billing-service communication on the same connection.

---

## Systems Constraints Analysis

### CPU

**Protobuf encode/decode is 5-10x faster than JSON.** JSON requires:
- Text parsing (scan for quotes, commas, braces)
- String-to-number conversion for every integer
- Unicode handling
- Dynamic type coercion

Protobuf requires:
- Read tag byte, switch on field number
- Copy bytes (strings) or decode varint (integers)
- No text parsing, no type coercion

**The cost**: Code generation. You must run `protoc` as a build step, generate code for each language, and keep generated code in sync with `.proto` files. This is a CI/CD pipeline concern. Also, Protobuf messages are harder to inspect in debuggers — you see binary data, not readable JSON.

**gRPC framing**: HTTP/2 binary framing adds a small per-frame overhead (9 bytes per frame), but eliminates the CPU cost of text-based HTTP/1.1 parsing. Net result: lower CPU per request.

### Memory

**Smaller payloads = less buffer memory.** A JSON order is ~300 bytes; a Protobuf order is ~80 bytes. At 10,000 concurrent requests, that's 3 MB vs 800 KB in flight. Not transformative for a single service, but across a mesh of 20 microservices each handling 10K concurrent requests, the difference is real.

**Generated code has fixed schemas = predictable memory layout.** Protobuf-generated classes have fixed fields with known types. The runtime can allocate exactly the right amount of memory. JSON parsing, by contrast, builds dynamic dictionaries — hash tables with pointer overhead, string interning decisions, and unpredictable allocation patterns.

**HTTP/2 multiplexing reduces connection memory.** Instead of a pool of 6-20 TCP connections (each with ~16 KB send/receive buffers), you maintain one connection. That's 5-19 fewer TLS contexts, 5-19 fewer socket buffers.

### Network I/O

This is where gRPC shines brightest.

**4-10x smaller payloads.** Protobuf binary encoding eliminates field names, structural characters, and text encoding overhead. For FoodDash's inter-service calls — small, schema-stable messages sent millions of times per day — this is a direct bandwidth reduction.

**Multiplexed streams over one connection.** No connection pool management. No head-of-line blocking at the application layer. One TCP handshake, one TLS negotiation, then unlimited concurrent RPCs.

**Header compression eliminates redundant headers.** Inter-service calls have nearly identical headers. HPACK compresses them to near-zero after the first exchange. For a system making 100,000 inter-service calls per minute, this saves ~40 MB/minute of header traffic alone.

**For inter-service communication at high throughput, this is transformative.** A FoodDash backend doing 10K orders/minute, each triggering 4 inter-service calls, sends 40K internal HTTP requests/minute. Switching from REST+JSON to gRPC+Protobuf could reduce internal network traffic by 60-80%.

### Latency

**Lower serialization time + smaller payloads + connection reuse = measurably lower latency.**

```
REST + JSON + HTTP/1.1 (inter-service call):
  JSON serialize:      ~0.05ms
  HTTP/1.1 headers:    ~800 bytes
  JSON payload:        ~300 bytes
  Network transit:     ~2ms (same datacenter)
  HTTP/1.1 parse:      ~0.02ms
  JSON deserialize:    ~0.08ms
  Total:               ~2.15ms

gRPC + Protobuf + HTTP/2 (same call):
  Protobuf serialize:  ~0.008ms
  HTTP/2 headers:      ~20 bytes (after HPACK, reused connection)
  Protobuf payload:    ~80 bytes
  Network transit:     ~1.5ms (smaller payload)
  HTTP/2 frame parse:  ~0.002ms
  Protobuf deserialize:~0.01ms
  Total:               ~1.52ms
```

**Typical improvement: 2-5x for small messages, more for large payloads.** The latency savings compound when a single customer request triggers a chain of internal calls. If `PlaceOrder` calls 4 services sequentially, saving 0.6ms per call saves 2.4ms total — noticeable to the customer.

### Where the Bottleneck Is

gRPC is harder to debug. You can't `curl` a gRPC endpoint and read the response. You need `grpcurl` or `grpc_cli`. Browser DevTools don't show gRPC traffic natively — you need specialized tools or a grpc-web proxy.

gRPC requires code generation in your build pipeline. Every `.proto` change requires regenerating client and server code, rebuilding, and redeploying. This is a real friction cost in fast-moving teams.

gRPC has a steeper learning curve. REST is intuitive — URLs are resources, HTTP verbs are actions. gRPC is RPC-style — remote procedure calls that happen to go over the network. This can lead to chatty APIs if you're not careful about service boundary design.

**The complexity cost is real.** For a team of 3 building a startup, REST + JSON is the right choice. For a team of 30 operating 20 microservices at high throughput, gRPC for internal communication is likely worth the investment.

---

## Production Depth

### gRPC-Web: Bridging Browsers to gRPC Services

Browsers don't support HTTP/2 trailers, which gRPC uses to send status codes after the response body. This means **browsers cannot speak native gRPC**. The solution is gRPC-Web, a compatibility layer:

```
Browser (gRPC-Web)                Envoy Proxy              gRPC Service
     │                               │                          │
     │ POST /fooddash.OrderService/  │                          │
     │     PlaceOrder                │                          │
     │ Content-Type: application/    │                          │
     │     grpc-web+proto            │                          │
     │──────────────────────────────>│                          │
     │                               │  Native gRPC (HTTP/2)   │
     │                               │─────────────────────────>│
     │                               │<─────────────────────────│
     │  HTTP/1.1 response with       │                          │
     │  trailers in body             │                          │
     │<──────────────────────────────│                          │
```

The proxy (typically Envoy) translates between gRPC-Web (HTTP/1.1 compatible) and native gRPC (HTTP/2). This adds latency and operational complexity, which is why most teams use REST for browser-to-server and gRPC for server-to-server.

### Load Balancing: Connection-Level vs Stream-Level

This is one of the most common gRPC production pitfalls.

HTTP/1.1 load balancing is simple: each request is a separate TCP connection (or a reused connection with a new request), and the load balancer distributes requests across backends.

gRPC uses **one long-lived HTTP/2 connection** with multiplexed streams. A naive L4 (TCP) load balancer sees one connection and sends **all traffic to one backend**. The other backends sit idle.

```
WRONG — L4 load balancing with gRPC:
┌──────────────────────────────────────┐
│ Client ──(1 TCP conn)──> LB ──────> Backend A (100% load)
│                                      Backend B (0% load)
│                                      Backend C (0% load)
└──────────────────────────────────────┘

RIGHT — L7 load balancing with gRPC:
┌──────────────────────────────────────┐
│ Client ──(1 TCP conn)──> LB ──┬──> Backend A (stream 1, 4)
│                               ├──> Backend B (stream 2, 5)
│                               └──> Backend C (stream 3, 6)
└──────────────────────────────────────┘
```

**You need an L7 (application-layer) load balancer** that understands HTTP/2 streams and distributes individual RPCs across backends. Envoy, Linkerd, and Istio all do this. AWS ALB supports gRPC as of 2020. If you're using a classic ELB or a basic Nginx config, all your gRPC traffic hits one pod.

Alternatively, use **client-side load balancing**: the gRPC client maintains connections to multiple backends and distributes RPCs itself. Libraries like `grpc-go` and `grpclib` (Python) support this via name resolution and load balancing policies.

### Deadlines and Cancellation

gRPC has **built-in deadline propagation** across service chains. This is something you'd have to build manually with REST.

```
Customer App          Order Service         Kitchen Service       Inventory Service
     │                     │                      │                      │
     │ PlaceOrder          │                      │                      │
     │ deadline: 5s        │                      │                      │
     │────────────────────>│                      │                      │
     │                     │ ValidateOrder         │                      │
     │                     │ deadline: 4.2s        │                      │
     │                     │ (5s - 0.8s elapsed)   │                      │
     │                     │─────────────────────>│                      │
     │                     │                      │ CheckStock            │
     │                     │                      │ deadline: 3.5s        │
     │                     │                      │ (4.2s - 0.7s elapsed) │
     │                     │                      │─────────────────────>│
     │                     │                      │                      │
```

The deadline **decreases automatically** as it propagates through the call chain. If the inventory service takes too long, the kitchen service's call is cancelled, which cancels the order service's call, which returns a `DEADLINE_EXCEEDED` error to the customer. No orphaned requests consuming resources in downstream services.

With REST, you'd need to manually propagate timeout headers, subtract elapsed time, and implement cancellation logic at each hop. Most teams don't do this, leading to cascade failures where a slow downstream service causes all upstream services to accumulate blocked requests.

### Interceptors: Middleware for gRPC

gRPC interceptors are the equivalent of HTTP middleware. They wrap every RPC call with cross-cutting concerns:

```python
# Conceptual — what a gRPC interceptor looks like
class LoggingInterceptor:
    def intercept(self, request, context, next_handler):
        start = time.time()
        log.info(f"RPC {context.method} started")

        response = next_handler(request, context)

        duration = time.time() - start
        log.info(f"RPC {context.method} completed in {duration:.3f}s")
        return response
```

Common interceptors in production:
- **Authentication**: Validate JWT tokens from metadata (gRPC's equivalent of HTTP headers)
- **Logging**: Record every RPC call, duration, and status code
- **Metrics**: Export latency histograms and error rates to Prometheus
- **Retry**: Automatically retry failed RPCs with exponential backoff
- **Rate limiting**: Throttle clients exceeding their quota

### Health Checking Protocol

gRPC defines a **standard health check service** (`grpc.health.v1.Health`). Unlike REST, where every team invents their own `/health` or `/healthz` endpoint with their own response format, gRPC's health check is a formal specification:

```protobuf
service Health {
  rpc Check(HealthCheckRequest) returns (HealthCheckResponse);
  rpc Watch(HealthCheckRequest) returns (stream HealthCheckResponse);
}

message HealthCheckResponse {
  enum ServingStatus {
    UNKNOWN = 0;
    SERVING = 1;
    NOT_SERVING = 2;
    SERVICE_UNKNOWN = 3;
  }
  ServingStatus status = 1;
}
```

Kubernetes, Envoy, and other infrastructure tools understand this protocol natively. The `Watch` RPC is particularly powerful — instead of polling a health endpoint, the load balancer **subscribes** to health status changes via server-streaming.

### When NOT to Use gRPC

gRPC is not universally superior. Avoid it when:

- **Public-facing APIs**: Your customers expect REST. They want to test with `curl`, read JSON responses, and use any HTTP client library. gRPC's binary format and code-generation requirement are barriers.
- **Simple CRUD applications**: If your service is a basic database frontend with 5 endpoints and low throughput, gRPC's complexity overhead (`.proto` files, code generation, build pipeline changes) isn't worth the performance gain.
- **Browser clients (without proxy infrastructure)**: If your architecture can't accommodate a grpc-web proxy, REST is the practical choice.
- **Teams unfamiliar with RPC patterns**: gRPC's RPC-style API design requires different thinking than REST's resource-oriented design. If your team thinks in URLs and HTTP verbs, forcing gRPC will lead to poorly designed APIs.
- **Debugging-heavy development phases**: Early in a project, when you're iterating rapidly and debugging constantly, JSON's readability is a genuine productivity advantage.

---

## gRPC vs REST vs GraphQL

| Dimension | REST | gRPC | GraphQL |
|-----------|------|------|---------|
| **Transport** | HTTP/1.1 or HTTP/2 | HTTP/2 (required) | HTTP/1.1 or HTTP/2 |
| **Data format** | JSON (typically) | Protobuf (binary) | JSON |
| **Schema** | Optional (OpenAPI) | Required (.proto) | Required (SDL) |
| **Code generation** | Optional | Required | Optional (but common) |
| **Streaming** | SSE, WebSocket (bolt-on) | Native (4 patterns) | Subscriptions (WebSocket) |
| **Browser support** | Native | Requires proxy | Native |
| **Payload size** | Larger (text) | 4-10x smaller (binary) | Varies (client-specified) |
| **Coupling** | Loose | Tight (shared .proto) | Medium (shared schema) |
| **Discoverability** | URLs are self-documenting | Need service reflection or docs | Introspection built-in |
| **Best for** | Public APIs, web frontends | Internal high-throughput services | Flexible client queries |
| **Learning curve** | Low | Medium-High | Medium |
| **Tooling maturity** | Excellent | Good | Good |
| **Error handling** | HTTP status codes | gRPC status codes (richer) | Errors in response body |

**The modern pattern**: REST for the edge (browser to API gateway), gRPC for the interior (service to service), GraphQL for mobile/frontend where clients need flexible queries.

---

## FoodDash Application — Restructuring with gRPC

Here's how you'd restructure FoodDash's inter-service communication using gRPC, while keeping REST for external-facing APIs:

```
                     ┌─────────────────────────────────────────┐
                     │              FoodDash Backend            │
    REST + JSON      │                                         │
    (external)       │  ┌──────────────┐   gRPC + Protobuf     │
 ┌──────────────────>│  │ API Gateway  │   (internal)          │
 │  Browser/Mobile   │  │  (REST→gRPC) │                       │
 │                   │  └──────┬───────┘                       │
 │                   │         │                               │
 │                   │    ┌────┴────┐                          │
 │                   │    │         │                          │
 │                   │  ┌─▼──┐  ┌──▼───┐                      │
 │                   │  │Order│  │ Menu │                      │
 │                   │  │ Svc │  │ Svc  │                      │
 │                   │  └─┬───┘  └──────┘                      │
 │                   │    │                                    │
 │                   │  ┌─┴──────────┬──────────┐              │
 │                   │  │            │          │              │
 │                   │  ▼            ▼          ▼              │
 │                   │ ┌───────┐ ┌───────┐ ┌────────┐         │
 │                   │ │Kitchen│ │Billing│ │ Driver │         │
 │                   │ │ Svc   │ │ Svc   │ │ Match  │         │
 │                   │ └───────┘ └───────┘ └────────┘         │
 │                   │                                         │
 │                   │  All internal arrows = gRPC             │
 │                   └─────────────────────────────────────────┘
```

**Specific gRPC patterns for each internal call:**

| Internal Call | gRPC Pattern | Why |
|---------------|-------------|-----|
| Order to Kitchen: "New order" | Unary RPC | Fire-and-confirm, like REST but faster |
| Order to Billing: "Charge customer" | Unary RPC | Must be synchronous (wait for payment confirmation) |
| Order to Driver Match: "Find a driver" | Server-streaming | Match service streams candidate drivers as it finds them |
| Kitchen to Order: "Status updates" | Server-streaming | Kitchen streams status changes as they happen |
| Catering to Order: "50 orders" | Client-streaming | Stream orders one at a time, get batch confirmation |
| Customer to Support: "Live chat" | Bidirectional streaming | Both sides send messages in real time |

**The API Gateway** (Envoy, gRPC-Gateway, or custom) translates between the REST world (browsers, mobile apps, third-party integrations) and the gRPC world (internal services). This is the seam between human-readable and machine-efficient communication.

---

## Running the Code

### Run the demo

```bash
# From the repo root
uv run python -m appendices.appendix_a_grpc.grpc_demo
```

This runs a pure-Python simulation (no `grpcio` dependency) that demonstrates:
1. Protobuf-style binary encoding using Python's `struct` module
2. JSON vs binary size comparison for a FoodDash Order
3. All four RPC pattern simulations with `asyncio`
4. Serialization speed benchmarks: JSON vs binary

### Open the visual

Open `appendices/appendix_a_grpc/visual.html` in your browser. No server needed — it's a self-contained visualization of gRPC concepts.

---

## Bridge to Other Appendices

gRPC handles the **transport and serialization** layer. But what about:

- **Message queuing and async processing?** When `PlaceOrder` shouldn't block on notifying the kitchen? That's [Appendix B — Message Queues](../appendix_b_message_queues/).
- **Flexible client queries?** When your mobile app needs different data than your web app? That's [Appendix C — GraphQL Subscriptions](../appendix_c_graphql_subscriptions/).
- **What happens when a gRPC call fails?** Retries, circuit breakers, bulkheads? That's [Appendix D — Resilience Patterns](../appendix_d_resilience/).
