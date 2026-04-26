# Communication Patterns -- Decision Matrix

> Pin this to your desk. Share it in your architecture review.
> This is the one-page reference for choosing the right communication pattern.

---

## Full Constraint Heatmap

| Pattern | CPU | Memory | Network I/O | Latency | Complexity | Scalability |
|---|---|---|---|---|---|---|
| **Ch01 Request-Response** | LOW -- one thread per request, released immediately | LOW -- no state between requests | LOW -- one round trip per operation | MEDIUM -- blocked until response arrives | LOW -- simplest possible model | HIGH -- stateless, horizontal scaling trivial |
| **Ch02 Short Polling** | MEDIUM -- repeated request parsing and response generation | LOW -- no server-side state between polls | HIGH -- N polls per interval, most return empty | HIGH -- up to one full interval of staleness | LOW -- just a timer + HTTP calls | HIGH -- stateless, but wastes bandwidth at scale |
| **Ch03 Long Polling** | LOW -- thread/connection held idle, minimal processing | MEDIUM -- one held connection per waiting client | LOW -- only transmits when data exists | LOW -- near-instant when event fires | MEDIUM -- timeout handling, reconnection logic | MEDIUM -- held connections consume server resources |
| **Ch04 SSE** | LOW -- kernel handles idle connections efficiently | MEDIUM -- one persistent connection per client | LOW -- server pushes only when events exist | LOW -- events arrive within milliseconds | MEDIUM -- reconnection, event ID tracking | MEDIUM -- persistent connections limit max clients per server |
| **Ch05 WebSocket** | MEDIUM -- frame parsing, per-message callbacks | HIGH -- per-connection state, buffers in both directions | MEDIUM -- binary framing is efficient, but always-on | LOW -- sub-millisecond after connection established | HIGH -- state management, heartbeats, reconnection | LOW -- stateful connections are hard to load-balance |
| **Ch06 Push Notifications** | LOW -- fire and forget from server's perspective | LOW -- no server-side connection state | LOW -- one message per event via platform service | HIGH -- delivery depends on OS, battery, network | MEDIUM -- platform-specific APIs, token management | HIGH -- platform handles fan-out (APNs/FCM) |
| **Ch07 Pub/Sub** | MEDIUM -- broker routes every message to N subscribers | MEDIUM -- broker holds topics, subscriptions, unacked msgs | MEDIUM -- one publish fans out to N deliveries | LOW-MEDIUM -- broker adds one hop of latency | MEDIUM -- topic design, ordering, at-least-once semantics | HIGH -- brokers are designed for horizontal scaling |
| **Ch08 Stateful vs Stateless** | VARIES -- stateful may cache; stateless recomputes | HIGH (stateful) / LOW (stateless) | LOW -- both use standard request patterns | LOW (stateful, cached) / MEDIUM (stateless, re-fetch) | HIGH (stateful) / LOW (stateless) | LOW (stateful, sticky sessions) / HIGH (stateless) |
| **Ch09 Multiplexing** | MEDIUM -- stream management, priority, flow control | MEDIUM -- per-stream buffers and state | LOW -- one TCP connection carries N streams | LOW -- no head-of-line blocking across streams | HIGH -- stream IDs, flow control, prioritization | HIGH -- fewer connections = better connection reuse |
| **Ch10 Sidecar** | MEDIUM -- extra hop through proxy for every request | MEDIUM -- proxy process per service instance | LOW -- localhost communication adds negligible overhead | LOW -- adds ~1-2ms per hop (localhost) | MEDIUM -- deployment orchestration, config management | HIGH -- decouple cross-cutting concerns from app scaling |

---

## Pattern Comparison -- Communication Properties

| Pattern | Direction | Connection Model | Initiator | Protocol | Browser Support | Reconnection | Ordering Guarantee |
|---|---|---|---|---|---|---|---|
| **Request-Response** | Client -> Server -> Client | Short-lived | Client only | HTTP/1.1, HTTP/2 | Universal | N/A (new connection per request) | Per-request (single exchange) |
| **Short Polling** | Client -> Server -> Client (repeated) | Short-lived (repeated) | Client only | HTTP/1.1, HTTP/2 | Universal | Built-in (next poll) | Per-poll (may miss between intervals) |
| **Long Polling** | Client -> Server ... Server -> Client | Medium-lived (held open) | Client initiates, server decides when to respond | HTTP/1.1, HTTP/2 | Universal | Client reconnects after each response | Per-response (sequential) |
| **SSE** | Server -> Client | Long-lived (persistent) | Client opens, server pushes | HTTP/1.1 with text/event-stream | All modern browsers (EventSource API) | Automatic with Last-Event-ID | Guaranteed (single TCP stream) |
| **WebSocket** | Bidirectional | Long-lived (persistent) | Client initiates upgrade, then either side sends | ws:// or wss:// | All modern browsers (WebSocket API) | Manual (application must reconnect) | Guaranteed per-connection (TCP) |
| **Push Notifications** | Server -> Device | None (fire-and-forget via platform) | Server via platform service | APNs (Apple), FCM (Google), WNS (Microsoft) | Limited (Web Push API, partial support) | Platform handles retry | Best-effort (no ordering guarantee) |
| **Pub/Sub** | Publishers -> Broker -> Subscribers | Varies (persistent to broker, or per-message) | Publishers fire, subscribers listen | AMQP, MQTT, Kafka protocol, Redis Streams | Via backend services (not direct browser) | Broker redelivers unacked messages | Per-partition (Kafka), per-queue (AMQP) |
| **Stateful/Stateless** | N/A (architecture concern) | N/A | N/A | Any | N/A | Stateful: complex; Stateless: trivial | Depends on underlying pattern |
| **Multiplexing** | Bidirectional (multiple concurrent streams) | Long-lived (single connection, many streams) | Client opens connection, either side opens streams | HTTP/2, HTTP/3, QUIC | HTTP/2 is default in all modern browsers | Per-stream reset without killing connection | Per-stream guaranteed; across streams: independent |
| **Sidecar** | N/A (infrastructure concern) | N/A (sits between client and service) | N/A | Any (transparent proxy) | N/A (backend pattern) | Sidecar handles retry/circuit-breaking | Preserves underlying protocol ordering |

---

## Best and Worst Use Cases

| Pattern | Best Use Case | Worst Use Case |
|---|---|---|
| **Request-Response** | CRUD APIs, form submissions, any "ask and wait" operation | Live dashboards needing real-time updates |
| **Short Polling** | Simple status checks where staleness is acceptable (build status, batch job) | High-frequency updates (you'll DDoS yourself) |
| **Long Polling** | Chat applications with moderate traffic, notification feeds | Very high concurrency (connections pile up on server) |
| **SSE** | Live feeds, stock tickers, order tracking, any server-push-only scenario | Bidirectional communication (client needs to send frequent data too) |
| **WebSocket** | Chat, collaborative editing, multiplayer games, live trading | Simple notification feeds (overkill, SSE is simpler) |
| **Push Notifications** | Alerting offline/backgrounded users: delivery updates, breaking news | Real-time in-app updates (too slow, unreliable for interactive use) |
| **Pub/Sub** | Event-driven microservices, fan-out to multiple consumers | Simple two-service communication (overhead not justified) |
| **Stateful vs Stateless** | Stateful: WebSocket servers, game servers. Stateless: REST APIs, Functions | Stateful for commodity HTTP APIs (scaling nightmare) |
| **Multiplexing** | APIs with many concurrent requests, gRPC streaming, HTTP/2 asset loading | Single infrequent request (setup overhead not amortized) |
| **Sidecar** | Service mesh, consistent auth/logging/rate-limiting across microservices | Monoliths (just use middleware) or < 3 services (overhead not justified) |

---

## Quick-Decision Cheat Sheet

```
Need real-time server -> client updates?
  No  --> Request-Response
  Yes --> Does client also need to send data in real-time?
            No  --> SSE
            Yes --> WebSocket

Client might be offline?
  Yes --> Push Notifications

Multiple services react to one event?
  Yes --> Pub/Sub

Multiple concurrent streams over one connection?
  Yes --> Multiplexing (HTTP/2)

Same cross-cutting logic in every service?
  Yes --> Sidecar

Everything else?
  --> Request-Response (start simple, evolve when you hit the wall)
```

---

## When to Combine Patterns

Most production systems use 3-5 patterns simultaneously:

| System | Patterns Used | Why |
|---|---|---|
| Slack | WebSocket + HTTP + Push | Real-time chat + API operations + offline alerts |
| Uber | HTTP + WebSocket + Push + Pub/Sub | Booking + live tracking + arrival alerts + internal event bus |
| Netflix | HTTP + SSE + Multiplexing + Sidecar | Browsing + playback events + HTTP/2 asset loading + service mesh |
| Stripe | HTTP + Pub/Sub + WebSocket | API calls + webhook fan-out + dashboard live updates |

---

*Generated from the Communication Patterns educational repository -- Chapter 11: Synthesis.*
