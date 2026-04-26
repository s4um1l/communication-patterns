# Chapter 08 — Stateful vs Stateless

## The Scene

FoodDash is popular. One server can't handle the load. Your ops team provisions a second server behind a load balancer. The REST API (Ch01) works perfectly — requests hit either server, both return the same data. But WebSocket chat (Ch05) breaks immediately: Alice connects to Server 1, her driver connects to Server 2, messages don't reach each other. The SSE dashboard (Ch04) loses events. The pub/sub broker (Ch07) has split brain. Half your system works. Half is broken.

Welcome to the stateful vs stateless tension.

This chapter is different from the ones before it. It isn't introducing a new communication pattern. Instead, it looks *backward* at Chapters 01 through 07 and asks a single question: **which of these patterns require state, and what happens when we horizontally scale?**

The answer determines whether your system survives its first traffic spike or collapses under its own architecture.

---

## The Spectrum: Where Each Pattern Falls

Every communication pattern from the previous chapters sits somewhere on the stateful-stateless spectrum:

| Chapter | Pattern | State Model | Horizontally Scalable? |
|---------|---------|------------|----------------------|
| Ch01 | Request-Response | **Stateless** — all state is in the request or external DB | Trivially |
| Ch02 | Short Polling | **Stateless** — repeated independent requests | Trivially |
| Ch03 | Long Polling | **Stateful** — server holds open connections waiting for events | No, without session affinity |
| Ch04 | Server-Sent Events | **Stateful** — server maintains persistent connection per subscriber | No, without external state |
| Ch05 | WebSockets | **Stateful** — bidirectional connection state lives in server memory | No, without pub/sub bridge |
| Ch06 | Push Notifications | **Stateless** — state is in the push service (FCM/APNs), not your server | Trivially |
| Ch07 | Pub/Sub | **Stateful** — broker holds subscriptions and message queues in memory | No, without distributed broker |

The split is clean: patterns where the server *forgets you between interactions* scale horizontally. Patterns where the server *remembers you* do not — at least, not without significant engineering.

---

## Stateless Services — What Works at Scale

### Why Request-Response (Ch01) Just Works

When a customer calls `GET /orders/abc123`, the request carries *everything* the server needs:

```
GET /orders/abc123 HTTP/1.1
Host: api.fooddash.com
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...    <-- identity
Accept: application/json                           <-- format preference
```

The server does not need to know:
- Which server handled the customer's previous request
- Whether the customer has an active session
- What the customer did 30 seconds ago

It reads the JWT to identify the user, queries the shared database, and returns the response. Any server behind the load balancer can handle this. The magic is that **all state is in the request itself** (the JWT token, the request body, the URL path) or in a **shared external store** (the database).

### The Horizontal Scaling Recipe (Stateless)

```
                    ┌────────────────┐
                    │  Load Balancer │
                    │  (round-robin) │
                    └───┬────┬────┬──┘
                        │    │    │
                   ┌────┘    │    └────┐
                   ▼         ▼         ▼
             ┌──────────┐ ┌──────────┐ ┌──────────┐
             │ Server 1 │ │ Server 2 │ │ Server 3 │
             │ (no      │ │ (no      │ │ (no      │
             │  local   │ │  local   │ │  local   │
             │  state)  │ │  state)  │ │  state)  │
             └────┬─────┘ └────┬─────┘ └────┬─────┘
                  │            │            │
                  └────────┬───┘────────────┘
                           ▼
                    ┌──────────────┐
                    │ Shared Store │
                    │ (PostgreSQL) │
                    └──────────────┘
```

Adding a server is trivial:
1. Deploy the same code to a new machine
2. Register it with the load balancer
3. Done

No session migration. No connection draining. No coordination. The load balancer distributes requests round-robin and it doesn't matter which server handles which request. This is why stateless architectures are the default recommendation for web services.

### Short Polling (Ch02) — Also Stateless

Short polling is just repeated request-response. Each poll is an independent request. The server doesn't know (or care) that you polled 5 seconds ago and got `"status": "preparing"`. It looks up the order fresh every time. Wasteful? Yes. But horizontally scalable? Absolutely.

### Push Notifications (Ch06) — Outsourced State

Push notifications are interesting because the *state* (device tokens, notification delivery queues) lives in an external service — Firebase Cloud Messaging or Apple Push Notification service. Your server is stateless: it fires off a push request and forgets about it. The statefulness is someone else's problem.

---

## Stateful Services — What Breaks at Scale

### The WebSocket Problem (Ch05)

This is the canonical example. In Chapter 05, we built a chat room where customers and drivers communicate in real time. Here's what the architecture looks like with one server:

```
     Alice (customer)  ──── WebSocket ────  Server 1  ──── WebSocket ────  Bob (driver)
```

Server 1 holds both connections in memory. When Alice sends a message, the server iterates over its in-memory connection list and forwards the message to Bob. Simple and fast.

Now add a second server behind a load balancer:

```
     Alice (customer)  ──── WebSocket ────  Server 1
                                                          (no connection between servers)
     Bob (driver)      ──── WebSocket ────  Server 2
```

Alice sends a message. Server 1 iterates over its in-memory connections. Bob isn't there — he's connected to Server 2. **The message is lost.** Server 1 has no idea Server 2 exists, let alone that Bob is connected to it.

This isn't a bug. It's a fundamental consequence of in-process state.

### SSE Streams (Ch04) — Same Problem

The order-tracking dashboard uses Server-Sent Events. A customer connects to Server 1 and opens an SSE stream. The kitchen updates the order status. If the status-update request happens to hit Server 2, Server 2 has no subscriber to notify. The customer stares at a stale dashboard.

### Long Polling (Ch03) — Subtler, Same Root Cause

Long polling parks an HTTP connection on the server, waiting for an event. If the event is triggered by a request to a *different* server, the parked connection never wakes up. The client eventually times out and reconnects — hopefully to the right server this time.

### Pub/Sub Broker (Ch07) — Split Brain

If your pub/sub broker runs in-process (as our Chapter 07 demo does), each server instance has its own independent broker with its own subscriber list. Publishing an event on Server 1 only reaches Server 1's subscribers. Server 2's subscribers are in the dark.

This is the **split brain** problem: two brokers that should be one, each with a partial view of the world.

---

## Solutions to the Stateful Problem

There are four fundamental approaches, each with distinct trade-offs:

### 1. Sticky Sessions (Session Affinity)

Route the same client to the same server every time.

```
     Alice ──── Load Balancer (hash(Alice) → Server 1) ──── Server 1
     Bob   ──── Load Balancer (hash(Bob)   → Server 2) ──── Server 2
```

**How it works**: The load balancer uses a cookie, IP hash, or client ID to consistently route a given client to the same backend server. AWS ALB calls this "sticky sessions." Nginx calls it `ip_hash`.

**Pros**:
- Simple to implement (load balancer config change)
- No code changes needed in the application
- Works for simple session-based state

**Cons**:
- **Uneven load**: If one server gets all the active users and another gets idle ones, you can't rebalance without breaking sessions
- **Server death = session death**: When Server 1 crashes, all of Alice's sessions, connections, and state vanish. There's no failover.
- **Doesn't solve cross-client communication**: Even with sticky sessions, Alice on Server 1 can't message Bob on Server 2. The WebSocket chat problem persists.
- **Limits auto-scaling**: You can't easily remove a server from the pool without migrating its sessions

### 2. External State Store

Move all connection state to an external store (Redis, database).

```
     Alice ──── Server 1 ────┐
                              ├──── Redis ────┐
     Bob   ──── Server 2 ────┘               │
                                              │
     Server 1 publishes to Redis              │
     Redis notifies Server 2                  │
     Server 2 forwards to Bob                 │
```

**For WebSockets specifically**: Use Redis pub/sub as a message bus between servers. When Alice sends a message on Server 1, Server 1 publishes it to a Redis channel. Server 2 subscribes to that channel and forwards the message to Bob.

**Pros**:
- Any server can handle any client
- Server death doesn't lose state (it's in Redis)
- Horizontal scaling works normally
- The standard production solution

**Cons**:
- **Latency increase**: Every message now has an extra network hop to Redis. Local state lookup is ~0.001ms; Redis round-trip is ~0.5-2ms.
- **New dependency**: Redis is now a critical part of your infrastructure. Redis goes down, chat goes down.
- **Redis becomes a SPOF**: You've moved the single-point-of-failure from your application servers to Redis. You need Redis Sentinel or Redis Cluster for HA.
- **Serialization overhead**: State must be serialized to store in Redis and deserialized on read. Complex connection state (like buffered messages) can be expensive to serialize.

### 3. State Replication

Broadcast state to all servers. Every server maintains a full copy.

```
     Server 1 ◄──── full mesh ────► Server 2
         ▲                              ▲
         │         broadcast            │
         └──────────────────────────────┘
                       ▲
                       │
                   Server 3
```

**Pros**:
- No external dependency
- Fast local lookups (state is always local)

**Cons**:
- **O(N^2) network traffic**: Every state change must be broadcast to every other server. With N servers and M state changes per second, you generate N*M messages per second.
- **Consistency hell**: What happens when two servers update the same state simultaneously? You need conflict resolution (CRDTs, vector clocks, or consensus protocols like Raft).
- **Doesn't scale beyond ~5-10 nodes**: The quadratic network cost makes this impractical for large clusters.
- **Memory multiplication**: Every server stores ALL state, not just its own clients' state.

### 4. Stateless Redesign

Rearchitect the service to be stateless by pushing all statefulness into external, purpose-built systems.

**Example — WebSocket Chat (Ch05)**:

Before (stateful):
```python
# In-process connection registry
connections: dict[str, WebSocket] = {}

async def broadcast(message: str):
    for ws in connections.values():
        await ws.send_text(message)
```

After (stateless WebSocket server + Redis pub/sub):
```python
# Each server subscribes to Redis
redis = aioredis.from_url("redis://localhost")
pubsub = redis.pubsub()
await pubsub.subscribe("chat:order_abc123")

# On message from client:
async def on_message(websocket, message):
    await redis.publish("chat:order_abc123", message)

# On message from Redis:
async def redis_listener():
    async for message in pubsub.listen():
        for ws in local_connections.values():
            await ws.send_text(message["data"])
```

Now each server only knows about its own local connections, but Redis ensures messages reach all servers.

**Pros**:
- True horizontal scaling
- Clean separation of concerns
- Battle-tested pattern (Socket.IO, Phoenix Channels, etc.)

**Cons**:
- Increased complexity
- Higher per-message latency
- Requires careful pub/sub channel management

---

## Systems Constraints Analysis

### CPU

**Stateless services** are CPU-efficient per request. The CPU does work (parse, validate, query, serialize) and is done. No background work, no heartbeats, no connection maintenance.

**Stateful services** may do *less* CPU work per message (no state lookup from external store), but they require CPU for state management overhead:
- Heartbeat/ping-pong for WebSocket keep-alive (~1 CPU microsecond per connection per ping interval)
- Connection timeout checking
- State cleanup for disconnected clients
- With 100K connections, these background tasks add up

### Memory — THE Key Trade-off

This is where the stateful vs stateless decision has its most tangible impact.

**Stateful (in-process state)**:
- Each WebSocket connection: ~30 KB (socket buffers, protocol state, application state)
- 100K connections on one server: **3 GB** just for connection state
- Plus message buffers, subscriber lists, pending events
- Limited by single-server RAM — you can't add memory without migrating connections

**Stateless (external state)**:
- Application server memory: minimal, just request buffers
- Redis/DB memory: holds all state, but can be scaled independently
- Redis can be a cluster with 100GB+ across multiple nodes
- Application servers are disposable — spin up and tear down freely

### Network I/O

**Stateful**: Avoids network hops to external stores. A WebSocket message goes client -> server -> client. The state lookup is a local dictionary access (~1 nanosecond).

**Stateless**: Adds a network hop. A WebSocket message goes client -> server -> Redis -> server -> client. That Redis round trip is ~0.5-2ms on a good day, ~5-10ms under load.

For latency-sensitive paths like chat messages, this matters. A human notices if chat feels sluggish at >100ms, and every millisecond counts in the hot path.

### Latency

**Stateful wins for hot-path latency**: State is local. No network hop, no serialization. A chat message delivered from local memory takes <0.1ms of server processing.

**Stateless wins for reliability**: Any server can serve any request. If a server dies, the load balancer routes to another one. Recovery is instant. There's no re-establishing connections or replaying missed messages.

**This is the fundamental tension**: optimize for speed (keep state local) or optimize for reliability (externalize state).

### Bottleneck Analysis

**Stateful servers create hot spots**: If a popular chat room has 50K participants and they all connected to Server 1 (because the room was created there), Server 1 is overwhelmed while Servers 2-10 sit idle. The load balancer can't help — those connections are pinned.

**Stateless servers distribute load evenly**: The load balancer sends requests round-robin. No server is special. But every request pays the external-store latency tax.

---

## Production Depth

### CAP Theorem Implications

When you distribute state across servers, you're in CAP theorem territory:

- **Consistency**: All servers see the same state at the same time
- **Availability**: Every request gets a response (no errors)
- **Partition tolerance**: The system works even if network between servers fails

For a chat system with Redis as the state store:
- **CP (Consistency + Partition tolerance)**: If Redis is unreachable, reject chat messages. Users see errors but never see inconsistent state.
- **AP (Availability + Partition tolerance)**: If Redis is unreachable, deliver messages to local connections only. Users see messages delivered to their server but not cross-server. Some messages may be lost or duplicated when the partition heals.

Most production chat systems choose AP with eventual consistency — it's better to deliver some messages than none.

### Session Affinity Strategies

Not all sticky sessions are created equal:

| Strategy | How It Works | Failure Mode |
|----------|-------------|-------------|
| **Cookie-based** | Load balancer sets a cookie mapping client to server | Cookie lost = session lost. Client must support cookies. |
| **IP hash** | `hash(client_ip) % num_servers` | NAT/proxy causes many clients to share an IP. Adding/removing servers rehashes ALL clients. |
| **Consistent hashing** | Hash ring minimizes redistribution when servers change | Only ~1/N of sessions are disrupted when a server is added/removed. More complex to implement. |
| **Header-based** | Route based on a custom header (e.g., `X-Session-Server`) | Requires client awareness. Client must know which server to request. |

Consistent hashing is the gold standard for stateful services. When Server 3 dies in a 5-server cluster, only ~20% of sessions are redistributed, not 100%.

### Graceful Shutdown — Draining Stateful Servers

You can't just kill a stateful server. It has 10K active WebSocket connections and 5K SSE streams. Those are real users who will see errors.

**The drain process**:
1. Tell the load balancer to stop sending NEW connections to this server
2. Send a "server shutting down" message to all connected clients
3. Wait for clients to reconnect to other servers (give them 30-60 seconds)
4. Force-close any remaining connections
5. Shut down the server

**For stateless servers**: Just kill it. The load balancer detects the health check failure and routes to other servers. No one notices.

This operational difference is why stateless architectures are preferred in cloud-native environments where servers are ephemeral (spot instances, auto-scaling groups, Kubernetes pods).

### Health Checks for Stateful Servers

A stateless server is "healthy" if it responds to `GET /health` with 200. Simple.

A stateful server needs more nuanced health:
- **Liveness**: "I'm running" (respond to health check)
- **Readiness**: "I can accept new connections" (maybe not, if I'm already at 95% memory)
- **Load reporting**: "I have 10K connections, Server 2 has 2K" (so the load balancer can make informed routing decisions)

A server with 10K connections might be "live" but shouldn't accept new connections. Kubernetes supports separate liveness and readiness probes for exactly this reason.

### The Hybrid Approach

The production answer is usually not pure stateless or pure stateful, but a **hybrid**:

```
┌─────────────────────────────────────────────────────┐
│                   Load Balancer                      │
│          (routes by request type)                    │
└──────────┬──────────────────────┬────────────────────┘
           │                      │
    ┌──────▼──────┐       ┌──────▼──────┐
    │  API Pool   │       │  WS Pool    │
    │ (stateless) │       │ (stateful)  │
    │ 20 servers  │       │ 5 servers   │
    │ round-robin │       │ consistent  │
    │             │       │   hashing   │
    └──────┬──────┘       └──────┬──────┘
           │                      │
           └──────────┬───────────┘
                      │
               ┌──────▼──────┐
               │   Redis +   │
               │  PostgreSQL │
               └─────────────┘
```

- **API requests** (Ch01, Ch02): Stateless pool, round-robin, auto-scale freely
- **WebSocket connections** (Ch05): Stateful pool, consistent hashing, drain before scaling down
- **SSE streams** (Ch04): Stateful pool, with Redis pub/sub for cross-server event delivery

This gives you the best of both: stateless scaling for the bulk of your traffic and carefully managed stateful servers for real-time features.

### JWT vs Server-Side Sessions — The Purest Form

The most fundamental expression of stateful vs stateless is how you handle authentication:

**Server-side sessions (stateful)**:
```
POST /login → Server creates session, stores in memory:
  sessions["sess_abc123"] = {user_id: "user_42", created: ...}
  Set-Cookie: session_id=sess_abc123

GET /orders → Server reads cookie, looks up session in memory:
  session = sessions["sess_abc123"]  # MUST hit the same server
```

**JWT (stateless)**:
```
POST /login → Server creates JWT, signs it, sends to client:
  JWT = sign({user_id: "user_42", exp: ...}, SECRET_KEY)

GET /orders → Server reads JWT from header, verifies signature:
  payload = verify(JWT, SECRET_KEY)  # ANY server can do this
```

With JWTs, the server is completely stateless. The client carries its own identity proof. Any server with the signing key can verify it. No session store, no sticky sessions, no state.

The trade-off: you can't revoke a JWT before it expires without maintaining a blacklist — which is... server-side state. There's no escaping the tension entirely.

---

## Trade-offs at a Glance

| Dimension | Stateful | Stateless |
|-----------|----------|-----------|
| **Horizontal scaling** | Requires session affinity, connection draining, careful orchestration | Add servers, update load balancer, done |
| **Hot-path latency** | Excellent — state is local memory (~0.001ms lookup) | Good — but adds external store RTT (~0.5-2ms) |
| **Fault tolerance** | Server death = lost connections, lost state | Server death = next request goes elsewhere |
| **Memory per server** | High — holds all connection/session state | Low — holds only request buffers |
| **Operational complexity** | High — drain, migrate, health check nuances | Low — servers are disposable |
| **Implementation complexity** | Low — simple in-process data structures | Medium — need external store, serialization |
| **External dependencies** | None (state is local) | Redis, database, etc. |
| **Consistency** | Strong (single source of truth per server) | Depends on external store consistency model |
| **Cost efficiency** | Fewer network hops per operation | More predictable scaling, smaller instance sizes |
| **Best suited for** | Real-time connections, low-latency paths | CRUD APIs, batch processing, microservices |

---

## Running the Code

### Stateless API Demo

Start a stateless API server with JWT auth — any server can handle any request:

```bash
uv run uvicorn chapters.ch08_stateful_vs_stateless.stateless_api:app --port 8008
```

### Stateful Session Demo

Start a stateful session server — sessions only work on the server that created them:

```bash
uv run uvicorn chapters.ch08_stateful_vs_stateless.stateful_session:app --port 8018
```

### Scaling Demo (the main event)

Runs both servers and demonstrates what works and what breaks when you horizontally scale:

```bash
uv run python -m chapters.ch08_stateful_vs_stateless.scaling_demo
```

### Interactive Visualization

Open `chapters/ch08_stateful_vs_stateless/visual.html` in your browser. Toggle between stateless and stateful modes, add/remove/kill servers, and watch the difference in real time.

---

## Bridge to Chapter 09

Now that we understand the state management challenge, there's another dimension to consider. Look at what a single FoodDash customer is doing right now:

1. **One WebSocket connection** for chat with their driver (Ch05)
2. **One SSE connection** for live order-status updates (Ch04)
3. **Multiple HTTP requests** for API calls (Ch01)

That's three concurrent connections per customer. At 100K customers, that's 300K connections. Each has its own TCP handshake, TLS negotiation, and header overhead. Each consumes a file descriptor on the server.

What if we could collapse all three into a **single connection** that carries multiple independent streams? The customer's chat messages, order updates, and API responses all flow through one pipe, multiplexed by stream ID.

That's multiplexing, and it's the subject of [Chapter 09 — Multiplexing](../ch09_multiplexing/).
