# Glossary

Quick reference for terms used across chapters. Organized alphabetically.

---

### At-least-once delivery
A guarantee that every message will be delivered to the consumer at least one time. The message may arrive more than once if the consumer fails to acknowledge it. Requires **idempotent** handlers to be safe. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### At-most-once delivery
A guarantee that a message will be delivered zero or one times. No retries — if delivery fails, the message is lost. Simplest to implement but unsuitable when losing messages is unacceptable. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### Backpressure
A mechanism for a consumer to signal a producer to slow down when it can't keep up. Without backpressure, fast producers overwhelm slow consumers, causing unbounded memory growth or dropped messages. TCP flow control is a form of backpressure. See [Ch04: SSE](chapters/ch04_server_sent_events/README.md), [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Binary framing
Encoding messages as binary frames with fixed-size headers (length, type, flags) instead of text delimiters. HTTP/2 and WebSockets use binary framing. More efficient to parse than text-based protocols. See [Ch05: WebSockets](chapters/ch05_websockets/README.md), [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### Broker
A middleman that routes messages between publishers and subscribers. The broker decouples producers from consumers — neither needs to know about the other. Examples: Redis, RabbitMQ, Kafka, or an in-process asyncio implementation. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### Cascading failure
When one service's failure causes its callers to fail, which causes *their* callers to fail, propagating through the entire system. The circuit breaker pattern exists to stop this cascade. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Circuit breaker
A resilience pattern with three states: **CLOSED** (normal), **OPEN** (failing fast — all requests rejected immediately), **HALF-OPEN** (testing if the downstream has recovered). Prevents cascading failures by cutting off communication to a failing service. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Competing consumers
A pattern where multiple workers consume from the same queue, and each message goes to exactly ONE worker. This distributes load. Contrast with **fan-out** where every subscriber gets every message. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Connection pooling
Reusing TCP connections across multiple requests instead of opening a new connection each time. Eliminates the TCP handshake (1 RTT) and TLS handshake (1-2 RTT) for subsequent requests. HTTP keep-alive enables this. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### Consumer group
A Kafka concept where multiple consumers share the work of reading from a topic's partitions. Each partition is assigned to exactly one consumer in the group. Provides competing consumers within a group and fan-out across groups. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Content negotiation
The process by which client and server agree on the format of the data (e.g., JSON, XML, Protobuf) using `Accept` and `Content-Type` headers. See [Ch01: Request-Response](chapters/ch01_request_response/README.md).

### Dead letter queue (DLQ)
A queue where messages that can't be processed are sent after exhausting retries. Prevents poison messages from blocking the main queue while preserving them for investigation. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Deadline propagation
Passing the remaining time budget through a chain of service calls so each service knows how much time it has left. gRPC has this built in. Prevents downstream services from doing work after the upstream caller has already timed out. See [Appendix A: gRPC](appendices/appendix_a_grpc/README.md), [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Demultiplexing
Receiving interleaved data from a single connection and routing it to the correct handler based on stream identifiers. The reverse of multiplexing. See [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### Detection latency
The time between an event occurring and the client learning about it. For short polling: `poll_interval / 2` on average. For long polling, SSE, and WebSockets: near-instant (server pushes immediately). See [Ch02: Short Polling](chapters/ch02_short_polling/README.md).

### Ephemeral port exhaustion
Running out of available source ports for outbound TCP connections. The OS has ~28,000-60,000 ephemeral ports, and each connection occupies one for the duration of the connection plus the TIME_WAIT period. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### EventSource
The browser API for consuming Server-Sent Events. Creates a persistent HTTP connection and automatically reconnects on failure. Fires `onmessage` for each event. See [Ch04: SSE](chapters/ch04_server_sent_events/README.md).

### Exactly-once delivery
The guarantee that every message is processed exactly one time. Extremely expensive to implement because it requires distributed transactions. In practice, **at-least-once + idempotent handlers** is preferred. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### Exponential backoff
A retry strategy where the wait time doubles with each attempt: 1s, 2s, 4s, 8s, 16s... Reduces load on a failing system faster than linear backoff. Usually combined with **jitter**. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Fan-out
Delivering one message to multiple recipients. In pub/sub, one published event reaches all subscribers. Contrast with **competing consumers** where each message reaches one recipient. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### Full-duplex
A communication mode where both parties can send data simultaneously without waiting for the other. WebSockets are full-duplex. HTTP request-response is half-duplex (client sends, then waits for server). See [Ch05: WebSockets](chapters/ch05_websockets/README.md).

### gRPC
A high-performance RPC framework using Protocol Buffers for serialization and HTTP/2 for transport. Supports four patterns: unary, server-streaming, client-streaming, and bidirectional streaming. See [Appendix A: gRPC](appendices/appendix_a_grpc/README.md).

### Half-duplex
A communication mode where both parties can send data, but only one at a time. HTTP/1.1 is half-duplex — the client sends a request, then the server responds. Compare with **full-duplex**.

### Head-of-line (HOL) blocking
When the first item in a queue blocks all items behind it. In HTTP/1.1: one slow response blocks all subsequent responses on that connection. In TCP: one lost packet blocks all data behind it, even data for different HTTP/2 streams. HTTP/3 (QUIC) solves this at the transport level. See [Ch00: Foundations](chapters/ch00_foundations/README.md), [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### Heartbeat
A periodic message sent to keep a connection alive and detect dead connections. WebSocket ping/pong frames, SSE comment lines (`:`), and TCP keepalive packets are all heartbeats. See [Ch04: SSE](chapters/ch04_server_sent_events/README.md), [Ch05: WebSockets](chapters/ch05_websockets/README.md).

### HPACK
HTTP/2's header compression algorithm. Maintains a shared table of previously sent headers and encodes new headers as differences. Dramatically reduces header overhead for repeated requests. See [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md), [Appendix A: gRPC](appendices/appendix_a_grpc/README.md).

### HTTP/2 multiplexing
Sending multiple request/response pairs concurrently over a single TCP connection using stream identifiers. Each stream is independent — a slow response on stream 3 doesn't block stream 7. See [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### Idempotency
An operation that produces the same result regardless of how many times it's executed. GET requests are naturally idempotent. POST requests are not — sending the same order twice creates two orders. Idempotency keys make non-idempotent operations safe to retry. See [Ch01: Request-Response](chapters/ch01_request_response/README.md), [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Jitter
Random variation added to retry timing to prevent multiple clients from retrying at the exact same moment (thundering herd). Full jitter: `random(0, base * 2^attempt)`. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Keep-alive
Reusing a TCP connection for multiple HTTP requests instead of closing it after each response. Controlled by `Connection: keep-alive` header (default in HTTP/1.1). See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### Last-Event-ID
An SSE header sent by the client when reconnecting after a dropped connection. The server uses it to replay missed events. Provides automatic resumption. See [Ch04: SSE](chapters/ch04_server_sent_events/README.md).

### Load shedding
Deliberately rejecting requests when a server is overloaded, returning 503 immediately instead of processing slowly. Protects the server from cascading failure. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Long polling
A pattern where the server holds a client's HTTP request open until new data is available or a timeout expires. Reduces wasted requests compared to short polling. See [Ch03: Long Polling](chapters/ch03_long_polling/README.md).

### Multiplexing
Interleaving multiple logical streams of data over a single physical connection. Each frame includes a stream identifier so the receiver can route it correctly. HTTP/2 and QUIC do this at the protocol level. See [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### N+1 problem
Making N additional requests to fetch related data after an initial request returns N items. REST APIs are prone to this. GraphQL solves it with nested field selection in a single query. The DataLoader pattern batches the N lookups. See [Appendix C: GraphQL](appendices/appendix_c_graphql_subscriptions/README.md).

### Nagle's algorithm
A TCP optimization that buffers small writes and sends them as one segment. Reduces the number of tiny packets but adds latency. Often disabled (`TCP_NODELAY`) for interactive applications. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### Offset
In Kafka, a consumer's position in a partition's log. The consumer tracks which offset it has processed and can rewind to any offset for replay. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Partition
A Kafka concept where a topic is split into ordered, append-only segments. Messages within a partition are strictly ordered. Partitions enable parallelism — different consumers read different partitions. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Poison message
A message that crashes the consumer every time it's processed. Without a dead letter queue, a poison message blocks the entire queue. See [Appendix B: Message Queues](appendices/appendix_b_message_queues/README.md).

### Protocol Buffers (Protobuf)
Google's binary serialization format. Schema-first (define `.proto` files, generate code), type-safe, and 3-10x smaller than JSON. Used by gRPC. See [Appendix A: gRPC](appendices/appendix_a_grpc/README.md).

### Pub/Sub (Publish-Subscribe)
A messaging pattern where publishers emit events to topics and subscribers receive events from topics they've registered interest in. Publishers and subscribers are decoupled — neither knows about the other. See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### Push notification
A message delivered to a user's device via a platform service (APNs, FCM, Web Push) even when the user's app is not running. The platform maintains a persistent connection to the device. See [Ch06: Push Notifications](chapters/ch06_push_notifications/README.md).

### QUIC
A transport protocol built on UDP that provides multiplexed streams without head-of-line blocking. The foundation of HTTP/3. Each stream is independently reliable — a lost packet on one stream doesn't block others. See [Ch09: Multiplexing](chapters/ch09_multiplexing/README.md).

### Retry budget
A limit on the total number or percentage of retries across all requests. Prevents retry amplification (where retries at each layer of a service chain multiply). Typically: retries should be <10% of total traffic. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Retry amplification
When retries at multiple layers of a service chain multiply exponentially. A 5-service chain with 3 retries each can generate 3^5 = 243 requests from a single failure. Retry budgets prevent this. See [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Server-Sent Events (SSE)
A protocol for streaming events from server to client over a persistent HTTP connection. Uses `Content-Type: text/event-stream` and a simple text format (`data: ...\n\n`). Built into browsers via the `EventSource` API. See [Ch04: SSE](chapters/ch04_server_sent_events/README.md).

### Service mesh
A dedicated infrastructure layer for handling service-to-service communication. Every service gets a sidecar proxy; together, the sidecars form a "mesh" that handles mTLS, load balancing, retries, and observability. Istio and Linkerd are examples. See [Ch10: Sidecar](chapters/ch10_sidecar/README.md).

### Session affinity (sticky sessions)
Routing all requests from the same client to the same server. Required for stateful services where the client's state is stored in server memory. Implemented via cookie-based routing, IP hashing, or consistent hashing. See [Ch08: Stateful vs Stateless](chapters/ch08_stateful_vs_stateless/README.md).

### Short polling
Repeatedly sending requests at a fixed interval to check for new data. Simple but wasteful — most responses contain no new information. See [Ch02: Short Polling](chapters/ch02_short_polling/README.md).

### Sidecar
A helper process that runs alongside a service (same host or pod), intercepting network traffic to handle cross-cutting concerns like authentication, logging, and rate limiting. The service focuses on business logic; the sidecar handles infrastructure. See [Ch10: Sidecar](chapters/ch10_sidecar/README.md).

### Stateful
A service that stores per-client state in its own memory. WebSocket servers, SSE servers, and session-based servers are stateful. Harder to scale horizontally because clients are bound to specific server instances. See [Ch08: Stateful vs Stateless](chapters/ch08_stateful_vs_stateless/README.md).

### Stateless
A service where every request contains all the information needed to process it. No per-client state is stored on the server between requests. JWT-based APIs are stateless. Easy to scale horizontally. See [Ch08: Stateful vs Stateless](chapters/ch08_stateful_vs_stateless/README.md).

### Subscription (GraphQL)
A GraphQL operation type that establishes a persistent connection (typically WebSocket) and receives real-time updates when the subscribed data changes. The client specifies exactly which fields to receive. See [Appendix C: GraphQL](appendices/appendix_c_graphql_subscriptions/README.md).

### TCP handshake (three-way)
The process of establishing a TCP connection: SYN → SYN-ACK → ACK. Takes 1 RTT (round-trip time). After this, data can flow. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### Thundering herd
When many clients simultaneously send requests (e.g., all reconnecting after a server restart, or all polling at the same instant). Creates a spike that can overwhelm the server. Mitigated by jitter. See [Ch02: Short Polling](chapters/ch02_short_polling/README.md), [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### TIME_WAIT
A TCP state where the connection remains in the kernel's connection table for 2×MSL (~60 seconds) after closing, to handle delayed packets. Can cause ephemeral port exhaustion under heavy connection churn. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### TLS handshake
The cryptographic negotiation that establishes an encrypted connection. TLS 1.2 takes 2 RTT; TLS 1.3 takes 1 RTT (0-RTT with session resumption). Adds latency but provides confidentiality and integrity. See [Ch00: Foundations](chapters/ch00_foundations/README.md).

### Token bucket
A rate limiting algorithm. A bucket holds tokens; each request consumes one token; tokens are added at a fixed rate. If the bucket is empty, requests are rejected or queued. See [Ch10: Sidecar](chapters/ch10_sidecar/README.md), [Appendix D: Resilience](appendices/appendix_d_resilience/README.md).

### Topic
A named channel in a pub/sub system. Publishers send messages to a topic; subscribers register interest in a topic. Topics can support wildcards (e.g., `order.*` matches `order.placed` and `order.cancelled`). See [Ch07: Pub/Sub](chapters/ch07_pub_sub/README.md).

### VAPID
Voluntary Application Server Identification — a protocol for identifying the application server sending push notifications. Uses public/private key pairs so the push service can verify the sender without OAuth. See [Ch06: Push Notifications](chapters/ch06_push_notifications/README.md).

### WebSocket
A protocol providing full-duplex communication over a single TCP connection. Starts as an HTTP request with an `Upgrade` header, then switches to the WebSocket frame protocol. See [Ch05: WebSockets](chapters/ch05_websockets/README.md).

### Wire format
The exact byte layout of data as it travels over the network. JSON's wire format is UTF-8 text. Protobuf's wire format is binary with field tags and varint encoding. The wire format determines serialization cost and payload size. See [Appendix A: gRPC](appendices/appendix_a_grpc/README.md).
